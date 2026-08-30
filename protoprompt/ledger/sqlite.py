"""SQLite implementation of the experimental scope-pinned memory ledger.

It is intentionally separate from :class:`protoprompt.store.sqlite.SqliteStore`.
The vector store remains a legacy recall projection; this ledger is an opt-in
authoritative lifecycle store and never silently dual-writes to that projection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import re
import sqlite3
import threading
from typing import Any, Iterable, Iterator
import uuid

from protoprompt.ledger.types import (
    LEDGER_SCHEMA_VERSION,
    ErasureReceipt,
    LedgerConflictError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryAdmissionAction,
    MemoryAdmissionAudit,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryRelation,
    MemoryRelationType,
    MemoryState,
    MemoryTrust,
    canonical_json,
    coerce_datetime,
    command_hash,
    content_hash,
    format_timestamp,
    parse_timestamp,
    scope_dict,
    utc_now,
    validate_content,
    validate_identifier,
    validate_reference,
    validate_references,
)
from protoprompt.scope import MemoryScope


_COMPONENT = "memory_ledger"
_REDACTED_CONTENT_HASH = "0" * 64
_LEGACY_CREATION_EVENT_PAYLOAD_HASH = command_hash({
    "format": "memory-ledger-v4-redacted-creation-command",
})
_ERASED_RELATION_EVENT_PAYLOAD_HASH = command_hash({
    "format": "memory-ledger-v4-erased-relation-command",
})
_EVENT_UPDATE_TRIGGER_NAME = "protoprompt_memory_ledger_events_reject_update_v1"
_LEGACY_EVENT_UPDATE_TRIGGER_NAME = "memory_events_reject_update"
_EVENT_GUARD_SCHEMA_VERSION = 4
_ADMISSION_GUARD_SCHEMA_VERSION = 5
_ADMISSION_METADATA_UPDATE_TRIGGER_NAME = (
    "protoprompt_memory_ledger_admission_metadata_reject_update_v1"
)
_ADMISSION_METADATA_DELETE_TRIGGER_NAME = (
    "protoprompt_memory_ledger_admission_metadata_reject_delete_v1"
)
_ADMISSION_METADATA_INSERT_TRIGGER_NAME = (
    "protoprompt_memory_ledger_admission_metadata_reject_replace_v1"
)
_REVIEW_AUDIT_UPDATE_TRIGGER_NAME = (
    "protoprompt_memory_ledger_review_audits_reject_update_v1"
)
_REVIEW_AUDIT_DELETE_TRIGGER_NAME = (
    "protoprompt_memory_ledger_review_audits_reject_delete_v1"
)
_REVIEW_AUDIT_INSERT_TRIGGER_NAME = (
    "protoprompt_memory_ledger_review_audits_reject_replace_v1"
)
_ADMISSION_ACTION_EVENTS = {
    MemoryAdmissionAction.ALLOW: MemoryEventType.CONFIRMED,
    MemoryAdmissionAction.QUARANTINE: MemoryEventType.QUARANTINED,
    MemoryAdmissionAction.REJECT: MemoryEventType.FORGOTTEN,
}
_EVENT_UPDATE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_EVENT_UPDATE_TRIGGER_NAME}
BEFORE UPDATE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events are append-only');
END
"""
_ADMISSION_METADATA_UPDATE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_ADMISSION_METADATA_UPDATE_TRIGGER_NAME}
BEFORE UPDATE ON memory_record_admission_metadata
BEGIN
    SELECT RAISE(ABORT, 'memory admission metadata are immutable');
END
"""
_ADMISSION_METADATA_DELETE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_ADMISSION_METADATA_DELETE_TRIGGER_NAME}
BEFORE DELETE ON memory_record_admission_metadata
BEGIN
    SELECT RAISE(ABORT, 'memory admission metadata are append-only');
END
"""
_ADMISSION_METADATA_INSERT_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_ADMISSION_METADATA_INSERT_TRIGGER_NAME}
BEFORE INSERT ON memory_record_admission_metadata
WHEN EXISTS (
    SELECT 1 FROM memory_record_admission_metadata
    WHERE scope_id = NEW.scope_id AND scope_json = NEW.scope_json
    AND record_id = NEW.record_id
)
BEGIN
    SELECT RAISE(ABORT, 'memory admission metadata are append-only');
END
"""
_REVIEW_AUDIT_UPDATE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_REVIEW_AUDIT_UPDATE_TRIGGER_NAME}
BEFORE UPDATE ON memory_review_audits
BEGIN
    SELECT RAISE(ABORT, 'memory review audits are immutable');
END
"""
_REVIEW_AUDIT_DELETE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_REVIEW_AUDIT_DELETE_TRIGGER_NAME}
BEFORE DELETE ON memory_review_audits
BEGIN
    SELECT RAISE(ABORT, 'memory review audits are append-only');
END
"""
_REVIEW_AUDIT_INSERT_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_REVIEW_AUDIT_INSERT_TRIGGER_NAME}
BEFORE INSERT ON memory_review_audits
WHEN EXISTS (
    SELECT 1 FROM memory_review_audits
    WHERE scope_id = NEW.scope_id AND scope_json = NEW.scope_json
    AND event_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'memory review audits are append-only');
END
"""

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ledger_schema (
        component TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK (version > 0),
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        occurred_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        related_record_id TEXT,
        reason_code TEXT,
        content_hash TEXT,
        payload_hash TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        UNIQUE (scope_id, scope_json, event_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_events_scope_record
    ON memory_events (scope_id, scope_json, record_id, sequence)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_records (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        trust TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        valid_from TEXT,
        valid_until TEXT,
        retention_policy TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        last_event_sequence INTEGER NOT NULL CHECK (last_event_sequence > 0),
        superseded_by TEXT,
        retracted_at TEXT,
        expired_at TEXT,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_records_recall
    ON memory_records (scope_id, scope_json, state, valid_from, valid_until, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_payloads (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_id TEXT NOT NULL,
        content TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_sources (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        record_id TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, source_ref, record_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_sources_lookup
    ON memory_sources (scope_id, scope_json, source_ref, record_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_source_revocation_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        source_key TEXT NOT NULL,
        revoked_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, source_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_erasure_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        payload_deleted INTEGER NOT NULL,
        source_refs_deleted INTEGER NOT NULL,
        relations_deleted INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_erasure_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_key TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_erased_event_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_key TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_hard_erase_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        payload_deleted INTEGER NOT NULL,
        source_refs_deleted INTEGER NOT NULL,
        relations_deleted INTEGER NOT NULL,
        events_deleted INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_relations (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        from_record_id TEXT NOT NULL,
        to_record_id TEXT NOT NULL,
        relation TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, from_record_id, to_record_id, relation)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations (scope_id, scope_json, to_record_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_record_admission_metadata (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_id),
        FOREIGN KEY (scope_id, scope_json, record_id)
            REFERENCES memory_records(scope_id, scope_json, record_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_review_audits (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        candidate_revision INTEGER NOT NULL CHECK (candidate_revision > 0),
        origin TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('allow', 'quarantine', 'reject')),
        reason_code TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id),
        FOREIGN KEY (scope_id, scope_json, record_id)
            REFERENCES memory_records(scope_id, scope_json, record_id) ON DELETE CASCADE,
        FOREIGN KEY (scope_id, scope_json, event_id)
            REFERENCES memory_events(scope_id, scope_json, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_review_audits_record
    ON memory_review_audits (scope_id, scope_json, record_id, event_id)
    """,
)

_V2_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_erasure_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        payload_deleted INTEGER NOT NULL,
        source_refs_deleted INTEGER NOT NULL,
        relations_deleted INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_erasure_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_key TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_key)
    )
    """,
)

_V3_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_erased_event_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_key TEXT NOT NULL,
        erased_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_hard_erase_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_key TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        payload_deleted INTEGER NOT NULL,
        source_refs_deleted INTEGER NOT NULL,
        relations_deleted INTEGER NOT NULL,
        events_deleted INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id)
    )
    """,
)

_V4_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_source_revocation_tombstones (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        source_key TEXT NOT NULL,
        revoked_at TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, source_key)
    )
    """,
)

_V5_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_record_admission_metadata (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        record_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, record_id),
        FOREIGN KEY (scope_id, scope_json, record_id)
            REFERENCES memory_records(scope_id, scope_json, record_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_review_audits (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        event_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        candidate_revision INTEGER NOT NULL CHECK (candidate_revision > 0),
        origin TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('allow', 'quarantine', 'reject')),
        reason_code TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, event_id),
        FOREIGN KEY (scope_id, scope_json, record_id)
            REFERENCES memory_records(scope_id, scope_json, record_id) ON DELETE CASCADE,
        FOREIGN KEY (scope_id, scope_json, event_id)
            REFERENCES memory_events(scope_id, scope_json, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_review_audits_record
    ON memory_review_audits (scope_id, scope_json, record_id, event_id)
    """,
)

_V1_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "ledger_schema": frozenset({"component", "version", "applied_at"}),
    "memory_events": frozenset({
        "sequence", "scope_id", "scope_json", "record_id", "event_id", "event_type",
        "revision", "occurred_at", "actor", "related_record_id", "reason_code",
        "content_hash", "payload_hash", "schema_version",
    }),
    "memory_records": frozenset({
        "scope_id", "scope_json", "record_id", "kind", "state", "trust", "content_hash",
        "confidence", "valid_from", "valid_until", "retention_policy", "created_at",
        "updated_at", "revision", "last_event_sequence", "superseded_by", "retracted_at",
        "expired_at", "schema_version",
    }),
    "memory_payloads": frozenset({
        "scope_id", "scope_json", "record_id", "content", "source_refs_json",
        "evidence_refs_json",
    }),
    "memory_sources": frozenset({"scope_id", "scope_json", "source_ref", "record_id"}),
    "memory_relations": frozenset({
        "scope_id", "scope_json", "from_record_id", "to_record_id", "relation", "created_at",
    }),
}

_V2_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_erasure_receipts": frozenset({
        "scope_id", "scope_json", "event_id", "record_id", "payload_hash", "state",
        "payload_deleted", "source_refs_deleted", "relations_deleted",
    }),
    "memory_erasure_tombstones": frozenset({
        "scope_id", "scope_json", "record_key", "erased_at",
    }),
}

_V3_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_erased_event_tombstones": frozenset({
        "scope_id", "scope_json", "event_key", "erased_at",
    }),
    "memory_hard_erase_receipts": frozenset({
        "scope_id", "scope_json", "event_id", "record_key", "payload_hash",
        "payload_deleted", "source_refs_deleted", "relations_deleted", "events_deleted",
    }),
}

_V4_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_source_revocation_tombstones": frozenset({
        "scope_id", "scope_json", "source_key", "revoked_at",
    }),
}

_V5_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_record_admission_metadata": frozenset({
        "scope_id", "scope_json", "record_id", "origin",
    }),
    "memory_review_audits": frozenset({
        "scope_id", "scope_json", "event_id", "record_id", "candidate_revision",
        "origin", "policy_id", "policy_version", "policy_fingerprint", "action",
        "reason_code",
    }),
}

_ALL_LEDGER_TABLE_COLUMNS = (
    _V1_TABLE_COLUMNS
    | _V2_TABLE_COLUMNS
    | _V3_TABLE_COLUMNS
    | _V4_TABLE_COLUMNS
    | _V5_TABLE_COLUMNS
)


def _normalized_table_sql(statement: str) -> str:
    """Return a stable, fail-closed signature for a ledger-owned table DDL."""
    without_create_guard = re.sub(
        r"\bIF\s+NOT\s+EXISTS\s+", "", statement, flags=re.IGNORECASE
    )
    return re.sub(r"\s+", " ", without_create_guard).strip().casefold()


def _ledger_table_signatures() -> dict[str, str]:
    """Build canonical DDL signatures from the schemas this module owns.

    A matching column set is not enough for an operational ledger: a foreign
    ``CHECK`` constraint, altered primary key, or restrictive default can make
    a migration appear successful and fail only when a lifecycle command is
    issued. The ledger has no extension-point contract for these tables, so
    accepted layouts must match the known DDL exactly (apart from whitespace
    and SQLite's omitted ``IF NOT EXISTS`` creation guard).
    """
    signatures: dict[str, str] = {}
    expression = re.compile(
        r"\A\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
        flags=re.IGNORECASE,
    )
    for statement in _SCHEMA_STATEMENTS:
        match = expression.match(statement)
        if match is not None:
            signatures[match.group("name")] = _normalized_table_sql(statement)
    if set(signatures) != set(_ALL_LEDGER_TABLE_COLUMNS):
        raise RuntimeError("memory ledger table signature definitions are incomplete")
    return signatures


_LEDGER_TABLE_SIGNATURES = _ledger_table_signatures()


def _ledger_index_signatures() -> dict[str, str]:
    """Build canonical signatures for explicit indexes owned by the ledger."""
    signatures: dict[str, str] = {}
    expression = re.compile(
        r"\A\s*CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
        flags=re.IGNORECASE,
    )
    for statement in _SCHEMA_STATEMENTS:
        match = expression.match(statement)
        if match is not None:
            signatures[match.group("name")] = _normalized_table_sql(statement)
    return signatures


_LEDGER_INDEX_SIGNATURES = _ledger_index_signatures()
_V5_INDEX_NAMES = frozenset({"idx_memory_review_audits_record"})
_EVENT_UPDATE_TRIGGER_SIGNATURE = _normalized_table_sql(_EVENT_UPDATE_TRIGGER_SQL)
_ADMISSION_IMMUTABILITY_TRIGGERS = {
    _ADMISSION_METADATA_UPDATE_TRIGGER_NAME: (
        "memory_record_admission_metadata",
        _normalized_table_sql(_ADMISSION_METADATA_UPDATE_TRIGGER_SQL),
    ),
    _ADMISSION_METADATA_DELETE_TRIGGER_NAME: (
        "memory_record_admission_metadata",
        _normalized_table_sql(_ADMISSION_METADATA_DELETE_TRIGGER_SQL),
    ),
    _ADMISSION_METADATA_INSERT_TRIGGER_NAME: (
        "memory_record_admission_metadata",
        _normalized_table_sql(_ADMISSION_METADATA_INSERT_TRIGGER_SQL),
    ),
    _REVIEW_AUDIT_UPDATE_TRIGGER_NAME: (
        "memory_review_audits",
        _normalized_table_sql(_REVIEW_AUDIT_UPDATE_TRIGGER_SQL),
    ),
    _REVIEW_AUDIT_DELETE_TRIGGER_NAME: (
        "memory_review_audits",
        _normalized_table_sql(_REVIEW_AUDIT_DELETE_TRIGGER_SQL),
    ),
    _REVIEW_AUDIT_INSERT_TRIGGER_NAME: (
        "memory_review_audits",
        _normalized_table_sql(_REVIEW_AUDIT_INSERT_TRIGGER_SQL),
    ),
}
_LEDGER_RESERVED_OBJECT_NAMES = frozenset(
    set(_ALL_LEDGER_TABLE_COLUMNS)
    | set(_LEDGER_INDEX_SIGNATURES)
    | {_EVENT_UPDATE_TRIGGER_NAME}
    | set(_ADMISSION_IMMUTABILITY_TRIGGERS)
)


def _required_index_signatures(current: int) -> dict[str, str]:
    """Return only indexes introduced by the installed schema version."""

    return {
        name: signature
        for name, signature in _LEDGER_INDEX_SIGNATURES.items()
        if current >= 5 or name not in _V5_INDEX_NAMES
    }


def _scope_storage(scope: MemoryScope) -> tuple[str, str]:
    """Return an index key plus an exact canonical scope comparison value."""
    return scope.correlation_id(), canonical_json(scope_dict(scope))


def _event_id(value: str | None) -> str:
    return validate_identifier(value, field="event_id") if value else uuid.uuid4().hex


def _actor(value: str) -> str:
    return validate_identifier(value, field="actor")


def _expected_revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision must be a positive integer")
    return value


def _retention_policy(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("retention_policy must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("retention_policy must not be empty")
    if len(normalized) > 128 or any(
        character.isspace() or ord(character) < 32 for character in normalized
    ):
        raise ValueError("retention_policy must be a short opaque policy identifier")
    return normalized


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError("confidence must be a number")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return normalized


class SqliteMemoryLedger:
    """An explicit-setup, SQLite-backed operational memory ledger.

    ``MemoryWriter`` is the preferred host-facing facade because it pins a
    non-empty :class:`~protoprompt.scope.MemoryScope`.  The lower-level store
    still requires that scope for every operation so no query can accidentally
    broaden into a neighbouring tenant or conversation.

    Construction performs no DDL.  Call :meth:`setup` from an explicit
    migration/setup job before serving traffic.
    """

    MIGRATION_VERSION = 5

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._closed = False

    def schema_version(self) -> int:
        """Return the installed ledger schema version without mutating it."""
        with self._lock:
            self._ensure_open_locked()
            return self._schema_version_locked()

    def dry_run_setup(self) -> dict[str, Any]:
        """Describe the explicit setup/migration action without writing data."""
        with self._lock:
            self._ensure_open_locked()
            current = self._schema_version_locked()
            self._assert_schema_compatible_locked(
                current,
                validate_admission_data=True,
            )
            if current > self.MIGRATION_VERSION:
                raise LedgerStateError(
                    f"ledger schema v{current} is newer than supported v{self.MIGRATION_VERSION}"
                )
            return {
                "component": _COMPONENT,
                "from_version": current,
                "to_version": self.MIGRATION_VERSION,
                "changes_required": current != self.MIGRATION_VERSION,
                "actions": self._migration_actions(current),
            }

    def setup(self) -> None:
        """Apply the explicit, idempotent ledger schema migration."""
        with self._lock:
            self._ensure_open_locked()
            with self._write_transaction_locked():
                current = self._schema_version_locked()
                self._assert_schema_compatible_locked(
                    current,
                    allow_event_guard_repair=True,
                    allow_admission_guard_repair=True,
                    validate_admission_data=True,
                )
                if current > self.MIGRATION_VERSION:
                    raise LedgerStateError(
                        f"ledger schema v{current} is newer than supported v{self.MIGRATION_VERSION}"
                    )
                if current == self.MIGRATION_VERSION:
                    self._ensure_event_immutability_locked()
                    self._ensure_admission_immutability_locked()
                    return
                statements = self._migration_statements(current)
                for statement in statements:
                    self._conn.execute(statement)
                if current < 4:
                    self._scrub_legacy_creation_event_fingerprints_locked()
                if current < 5:
                    self._backfill_legacy_admission_metadata_locked()
                self._assert_schema_compatible_locked(
                    self.MIGRATION_VERSION,
                    allow_event_guard_repair=True,
                    allow_admission_guard_repair=True,
                    validate_admission_data=True,
                )
                self._conn.execute(
                    "INSERT INTO ledger_schema (component, version, applied_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(component) DO UPDATE SET "
                    "version = excluded.version, applied_at = excluded.applied_at",
                    (_COMPONENT, self.MIGRATION_VERSION, format_timestamp(utc_now())),
                )
                self._ensure_event_immutability_locked()
                self._ensure_admission_immutability_locked()

    def observe(
        self,
        scope: MemoryScope,
        *,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...] | list[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Persist untrusted input as a candidate; it is not recallable yet."""
        return self._create_candidate(
            scope,
            event_type=MemoryEventType.OBSERVED,
            origin=MemoryOrigin.UNKNOWN,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def assert_candidate(
        self,
        scope: MemoryScope,
        *,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...] | list[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Persist a host assertion as a candidate pending explicit confirmation."""
        return self._create_candidate(
            scope,
            event_type=MemoryEventType.ASSERTED,
            origin=MemoryOrigin.UNKNOWN,
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def _observe_with_origin(
        self,
        scope: MemoryScope,
        *,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...] | list[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Create a candidate with a trusted, immutable ingress category.

        This is intentionally an internal bridge for ``MemoryReviewGate``.
        Model-facing integrations should receive the gate, not the Ledger or
        this raw mutator.
        """

        return self._create_candidate(
            scope,
            event_type=MemoryEventType.OBSERVED,
            origin=MemoryOrigin(origin),
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def _assert_candidate_with_origin(
        self,
        scope: MemoryScope,
        *,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...] | list[str] = (),
        confidence: float = 0.5,
        record_id: str | None = None,
        retention_policy: str = "default",
        valid_from: datetime | str | None = None,
        valid_until: datetime | str | None = None,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Internal host-admission bridge for an asserted candidate."""

        return self._create_candidate(
            scope,
            event_type=MemoryEventType.ASSERTED,
            origin=MemoryOrigin(origin),
            kind=kind,
            content=content,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
            confidence=confidence,
            record_id=record_id,
            retention_policy=retention_policy,
            valid_from=valid_from,
            valid_until=valid_until,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def confirm(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Move one candidate into the only state eligible for default recall."""
        return self._transition(
            scope,
            record_id,
            expected_revision=expected_revision,
            event_type=MemoryEventType.CONFIRMED,
            target_state=MemoryState.ACTIVE,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
            raw_confirmation=True,
        )

    def quarantine(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Exclude a candidate or active record pending host review."""
        return self._transition(
            scope,
            record_id,
            expected_revision=expected_revision,
            event_type=MemoryEventType.QUARANTINED,
            target_state=MemoryState.QUARANTINED,
            reason_code=reason_code,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def expire(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Move an active/candidate record into a terminal expired state."""
        return self._transition(
            scope,
            record_id,
            expected_revision=expected_revision,
            event_type=MemoryEventType.EXPIRED,
            target_state=MemoryState.EXPIRED,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def retract(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Exclude a record from recall while retaining its local payload."""
        return self._transition(
            scope,
            record_id,
            expected_revision=expected_revision,
            event_type=MemoryEventType.RETRACTED,
            target_state=MemoryState.RETRACTED,
            reason_code=reason_code,
            actor=actor,
            event_id=event_id,
            occurred_at=occurred_at,
        )

    def supersede(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        replacement_record_id: str,
        expected_revision: int,
        expected_replacement_revision: int,
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> MemoryRecord:
        """Explicitly replace one active record with another active record.

        The caller must provide both revisions: this prevents a stale decision
        from silently replacing a record that changed during review.
        """
        source_id = validate_identifier(record_id, field="record_id")
        replacement_id = validate_identifier(
            replacement_record_id, field="replacement_record_id"
        )
        if source_id == replacement_id:
            raise LedgerStateError("a record cannot supersede itself")
        expected_revision = _expected_revision(expected_revision)
        expected_replacement_revision = _expected_revision(expected_replacement_revision)
        current_time = self._timestamp(occurred_at)
        event_identity = _event_id(event_id)
        actor = _actor(actor)
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = command_hash({
            "event_type": MemoryEventType.SUPERSEDED.value,
            "record_id": source_id,
            "replacement_record_id": replacement_id,
            "expected_revision": expected_revision,
            "expected_replacement_revision": expected_replacement_revision,
            "actor": actor,
        })

        with self._lock:
            with self._ready_write_transaction_locked():
                duplicate = self._idempotent_event_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                    source_id,
                    payload_hash,
                )
                if duplicate:
                    record = self._load_record_locked(scope, source_id)
                    if record is None:
                        raise LedgerConflictError("event_id belongs to an erased record")
                    return record
                self._ensure_no_erasure_receipt_event_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )

                source = self._load_record_locked(scope, source_id)
                replacement = self._load_record_locked(scope, replacement_id)
                if source is None or replacement is None:
                    raise KeyError("both records must exist in the pinned scope")
                self._check_revision(source, expected_revision)
                self._check_revision(replacement, expected_replacement_revision)
                if source.state is not MemoryState.ACTIVE:
                    raise LedgerStateError("only an active record can be superseded")
                if not replacement.is_recallable(now=current_time):
                    raise LedgerStateError(
                        "replacement must be an active, currently recallable record"
                    )
                updated = self._transition_locked(
                    scope,
                    source,
                    event_id=event_identity,
                    payload_hash=payload_hash,
                    event_type=MemoryEventType.SUPERSEDED,
                    target_state=MemoryState.SUPERSEDED,
                    actor=actor,
                    occurred_at=current_time,
                    related_record_id=replacement_id,
                    reason_code=None,
                )
                self._conn.execute(
                    "INSERT INTO memory_relations "
                    "(scope_id, scope_json, from_record_id, to_record_id, relation, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        scope_id,
                        scope_json,
                        replacement_id,
                        source_id,
                        MemoryRelationType.SUPERSEDES.value,
                        format_timestamp(current_time),
                    ),
                )
                return updated

    def expire_due(
        self,
        scope: MemoryScope,
        *,
        now: datetime | str | None = None,
        actor: str = "host",
    ) -> list[MemoryRecord]:
        """Expire due candidates/active records exactly once at the boundary."""
        instant = self._timestamp(now)
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            rows = self._conn.execute(
                "SELECT record_id, revision FROM memory_records "
                "WHERE scope_id = ? AND scope_json = ? "
                "AND state IN (?, ?) AND valid_until IS NOT NULL AND valid_until <= ? "
                "ORDER BY record_id",
                (
                    scope_id,
                    scope_json,
                    MemoryState.CANDIDATE.value,
                    MemoryState.ACTIVE.value,
                    format_timestamp(instant),
                ),
            ).fetchall()
        expired: list[MemoryRecord] = []
        for row in rows:
            try:
                expired.append(
                    self.expire(
                        scope,
                        str(row["record_id"]),
                        expected_revision=int(row["revision"]),
                        actor=actor,
                        occurred_at=instant,
                    )
                )
            except (KeyError, LedgerConflictError, LedgerStateError):
                # A separate process won the race; it is safe to re-read next run.
                continue
        return expired

    def forget(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        reason_code: str = "user_requested",
        actor: str = "host",
        event_id: str | None = None,
        occurred_at: datetime | str | None = None,
    ) -> ErasureReceipt:
        """Retract a record and erase its local plaintext/source payload.

        Events intentionally contain no plaintext, source IDs or evidence IDs,
        so the retained operational receipt cannot recreate the forgotten text.
        External vector/FTS projections are not yet connected to this isolated
        experimental slice; an adapter must acknowledge their erasure before it
        may expose this operation as a cross-store deletion guarantee.
        """
        identity = validate_identifier(record_id, field="record_id")
        expected_revision = _expected_revision(expected_revision)
        reason_code = validate_identifier(reason_code, field="reason_code")
        current_time = self._timestamp(occurred_at)
        event_identity = _event_id(event_id)
        actor = _actor(actor)
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = self._forget_command_hash(
            record_id=identity,
            expected_revision=expected_revision,
            reason_code=reason_code,
            actor=actor,
        )
        with self._lock:
            with self._ready_write_transaction_locked():
                return self._forget_locked(
                    scope,
                    record_id=identity,
                    expected_revision=expected_revision,
                    reason_code=reason_code,
                    actor=actor,
                    event_id=event_identity,
                    occurred_at=current_time,
                    payload_hash=payload_hash,
                )

    def forget_by_source(
        self,
        scope: MemoryScope,
        source_ref: str,
        *,
        reason_code: str = "source_revoked",
        actor: str = "host",
        occurred_at: datetime | str | None = None,
    ) -> list[ErasureReceipt]:
        """Atomically forget and permanently revoke one opaque scoped source."""
        source = validate_reference(source_ref, field="source_ref")
        reason = validate_identifier(reason_code, field="reason_code")
        host_actor = _actor(actor)
        current_time = self._timestamp(occurred_at)
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            with self._ready_write_transaction_locked():
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory_source_revocation_tombstones "
                    "(scope_id, scope_json, source_key, revoked_at) VALUES (?, ?, ?, ?)",
                    (
                        scope_id,
                        scope_json,
                        self._source_tombstone_key(scope_id, scope_json, source),
                        format_timestamp(current_time),
                    ),
                )
                rows = self._conn.execute(
                    "SELECT record_id FROM memory_sources "
                    "WHERE scope_id = ? AND scope_json = ? AND source_ref = ? "
                    "ORDER BY record_id",
                    (scope_id, scope_json, source),
                ).fetchall()
                receipts: list[ErasureReceipt] = []
                for row in rows:
                    identity = str(row["record_id"])
                    record = self._load_record_locked(scope, identity)
                    if record is None:
                        continue
                    receipts.append(
                        self._forget_locked(
                            scope,
                            record_id=identity,
                            expected_revision=record.revision,
                            reason_code=reason,
                            actor=host_actor,
                            event_id=_event_id(None),
                            occurred_at=current_time,
                            payload_hash=self._forget_command_hash(
                                record_id=identity,
                                expected_revision=record.revision,
                                reason_code=reason,
                                actor=host_actor,
                            ),
                        )
                    )
                return receipts

    @staticmethod
    def _forget_command_hash(
        *,
        record_id: str,
        expected_revision: int,
        reason_code: str,
        actor: str,
    ) -> str:
        """Fingerprint a forget command without its payload or provenance."""
        return command_hash({
            "event_type": MemoryEventType.FORGOTTEN.value,
            "record_id": record_id,
            "expected_revision": expected_revision,
            "reason_code": reason_code,
            "actor": actor,
            "operation": "forget",
        })

    def _forget_locked(
        self,
        scope: MemoryScope,
        *,
        record_id: str,
        expected_revision: int,
        reason_code: str,
        actor: str,
        event_id: str,
        occurred_at: datetime,
        payload_hash: str,
    ) -> ErasureReceipt:
        """Apply one forget command inside an already-open write transaction."""
        scope_id, scope_json = _scope_storage(scope)
        existing_receipt = self._erasure_receipt_locked(
            scope_id,
            scope_json,
            event_id,
            record_id,
            payload_hash,
        )
        if existing_receipt is not None:
            return existing_receipt
        self._ensure_no_erasure_receipt_event_id_locked(
            scope_id,
            scope_json,
            event_id,
        )
        record = self._load_record_locked(scope, record_id)
        if record is None:
            raise KeyError(record_id)
        duplicate = self._idempotent_event_locked(
            scope_id,
            scope_json,
            event_id,
            record_id,
            payload_hash,
        )
        if not duplicate:
            self._check_revision(record, expected_revision)
            record = self._transition_locked(
                scope,
                record,
                event_id=event_id,
                payload_hash=payload_hash,
                event_type=MemoryEventType.FORGOTTEN,
                target_state=MemoryState.RETRACTED,
                actor=actor,
                occurred_at=occurred_at,
                related_record_id=None,
                reason_code=reason_code,
            )
        else:
            record = self._load_record_locked(scope, record_id)
            if record is None:
                raise LedgerConflictError("event_id belongs to an erased record")

        payload_deleted = self._conn.execute(
            "DELETE FROM memory_payloads WHERE scope_id = ? AND scope_json = ? "
            "AND record_id = ?",
            (scope_id, scope_json, record_id),
        ).rowcount == 1
        self._conn.execute(
            "UPDATE memory_records SET content_hash = ? "
            "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
            (_REDACTED_CONTENT_HASH, scope_id, scope_json, record_id),
        )
        source_refs_deleted = self._conn.execute(
            "DELETE FROM memory_sources WHERE scope_id = ? AND scope_json = ? "
            "AND record_id = ?",
            (scope_id, scope_json, record_id),
        ).rowcount
        relations_deleted = self._conn.execute(
            "DELETE FROM memory_relations WHERE scope_id = ? AND scope_json = ? "
            "AND (from_record_id = ? OR to_record_id = ?)",
            (scope_id, scope_json, record_id, record_id),
        ).rowcount
        relations_deleted += self._conn.execute(
            "UPDATE memory_records SET superseded_by = NULL "
            "WHERE scope_id = ? AND scope_json = ? "
            "AND superseded_by = ?",
            (scope_id, scope_json, record_id),
        ).rowcount
        self._conn.execute(
            "UPDATE memory_records SET superseded_by = NULL "
            "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
            (scope_id, scope_json, record_id),
        )
        receipt = ErasureReceipt(
            record_id=record_id,
            state=MemoryState.RETRACTED,
            payload_deleted=payload_deleted,
            source_refs_deleted=int(source_refs_deleted),
            relations_deleted=int(relations_deleted),
        )
        self._store_erasure_receipt_locked(
            scope_id,
            scope_json,
            event_id,
            payload_hash,
            receipt,
        )
        return receipt

    def erase(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        event_id: str | None = None,
    ) -> ErasureReceipt:
        """Hard-delete one local record, its relations and its event receipts.

        This explicit irreversible escape hatch is the sole exception to the
        operational append-only history. A non-plaintext scoped tombstone stays
        behind to prevent an in-flight retry from resurrecting the erased
        record id; related IDs in events owned by other records are redacted.
        SQLite cannot promise physical erasure from backups or storage media;
        hosts needing that property must add encryption/key destruction and a
        backup-retention policy.
        """
        identity = validate_identifier(record_id, field="record_id")
        expected_revision = _expected_revision(expected_revision)
        event_identity = _event_id(event_id)
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = command_hash({
            "operation": "hard_erase",
            "record_id": identity,
            "expected_revision": expected_revision,
        })
        with self._lock:
            with self._ready_write_transaction_locked():
                existing_receipt = self._hard_erase_receipt_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                    identity,
                    payload_hash,
                )
                if existing_receipt is not None:
                    return existing_receipt
                if self._event_for_id_locked(scope_id, scope_json, event_identity) is not None:
                    raise LedgerConflictError("event_id was already used for a lifecycle command")
                self._ensure_no_erasure_receipt_event_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                record = self._load_record_locked(scope, identity)
                if record is None:
                    raise KeyError(identity)
                self._check_revision(record, expected_revision)
                prior_event_ids = [
                    str(row["event_id"])
                    for row in self._conn.execute(
                        "SELECT event_id FROM memory_events "
                        "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
                        (scope_id, scope_json, identity),
                    ).fetchall()
                ]
                prior_event_ids.extend(
                    str(row["event_id"])
                    for row in self._conn.execute(
                        "SELECT event_id FROM memory_erasure_receipts "
                        "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
                        (scope_id, scope_json, identity),
                    ).fetchall()
                )
                self._redact_erased_relation_references_locked(
                    scope_id,
                    scope_json,
                    identity,
                )
                erased_at = format_timestamp(utc_now())
                payload_deleted = self._conn.execute(
                    "DELETE FROM memory_payloads WHERE scope_id = ? AND scope_json = ? "
                    "AND record_id = ?",
                    (scope_id, scope_json, identity),
                ).rowcount == 1
                source_refs_deleted = self._conn.execute(
                    "DELETE FROM memory_sources WHERE scope_id = ? AND scope_json = ? "
                    "AND record_id = ?",
                    (scope_id, scope_json, identity),
                ).rowcount
                self._conn.execute(
                    "DELETE FROM memory_erasure_receipts "
                    "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
                    (scope_id, scope_json, identity),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory_erasure_tombstones "
                    "(scope_id, scope_json, record_key, erased_at) VALUES (?, ?, ?, ?)",
                    (
                        scope_id,
                        scope_json,
                        self._tombstone_key(scope_id, scope_json, identity),
                        erased_at,
                    ),
                )
                for prior_event_id in sorted(set(prior_event_ids)):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO memory_erased_event_tombstones "
                        "(scope_id, scope_json, event_key, erased_at) VALUES (?, ?, ?, ?)",
                        (
                            scope_id,
                            scope_json,
                            self._event_tombstone_key(
                                scope_id,
                                scope_json,
                                prior_event_id,
                            ),
                            erased_at,
                        ),
                    )
                relations_deleted = self._conn.execute(
                    "DELETE FROM memory_relations WHERE scope_id = ? AND scope_json = ? "
                    "AND (from_record_id = ? OR to_record_id = ?)",
                    (scope_id, scope_json, identity, identity),
                ).rowcount
                relations_deleted += self._conn.execute(
                    "UPDATE memory_records SET superseded_by = NULL "
                    "WHERE scope_id = ? AND scope_json = ? "
                    "AND superseded_by = ?",
                    (scope_id, scope_json, identity),
                ).rowcount
                # Sidecar provenance and audit rows are write-once.  Their
                # only deletion path is the controlled, all-or-nothing hard
                # erase below; dropping the guards happens while this same
                # SQLite write transaction already excludes other writers and
                # they are immediately reinstalled before commit.
                with self._suspend_admission_immutability_locked():
                    events_deleted = self._conn.execute(
                        "DELETE FROM memory_events WHERE scope_id = ? AND scope_json = ? "
                        "AND record_id = ?",
                        (scope_id, scope_json, identity),
                    ).rowcount
                    self._conn.execute(
                        "DELETE FROM memory_records WHERE scope_id = ? AND scope_json = ? "
                        "AND record_id = ?",
                        (scope_id, scope_json, identity),
                    )
                receipt = ErasureReceipt(
                    record_id=identity,
                    state=None,
                    payload_deleted=payload_deleted,
                    source_refs_deleted=int(source_refs_deleted),
                    relations_deleted=int(relations_deleted),
                    events_deleted=int(events_deleted),
                )
                self._store_hard_erase_receipt_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                    payload_hash,
                    receipt,
                )
                return receipt

    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        """Read one record in the supplied exact host scope."""
        identity = validate_identifier(record_id, field="record_id")
        _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            with self._read_transaction_locked():
                return self._load_record_locked(scope, identity)

    def list_active(
        self,
        scope: MemoryScope,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Return only currently valid, content-present, active memories."""
        self._validate_active_limit(limit)
        instant = self._timestamp(now)
        _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            with self._read_transaction_locked():
                return self._list_active_locked(scope, instant=instant, limit=limit)

    def _validate_active_snapshot(
        self,
        scope: MemoryScope,
        *,
        now: datetime | str | None,
        limit: int,
        selections: Iterable[tuple[str, int, str, MemoryKind]],
    ) -> bool:
        """Check private recall markers under a short ledger write lock.

        The caller must do plaintext rendering and arbitrary token accounting
        before this method. ``BEGIN IMMEDIATE`` is only a final, short
        lifecycle linearization point: it re-reads the active snapshot and
        verifies that each selected record still has the planned revision,
        content hash and kind.  No caller-supplied callback executes inside
        the transaction. This method is intentionally private; public readers
        should use :meth:`list_active`.
        """

        self._validate_active_limit(limit)
        normalized_selections = self._normalize_active_snapshot_markers(selections)
        instant = self._timestamp(now)
        _scope_storage(scope)
        with self._lock:
            with self._ready_write_transaction_locked():
                records = self._list_active_locked(scope, instant=instant, limit=limit)
                current_by_id = {record.record_id: record for record in records}
                return all(
                    (
                        (record := current_by_id.get(record_id)) is not None
                        and record.revision == revision
                        and record.content_hash == expected_hash
                        and record.kind is kind
                    )
                    for record_id, revision, expected_hash, kind in normalized_selections
                )

    @staticmethod
    def _normalize_active_snapshot_markers(
        selections: Iterable[tuple[str, int, str, MemoryKind]],
    ) -> tuple[tuple[str, int, str, MemoryKind], ...]:
        """Copy and validate private recall markers before opening a transaction."""

        if isinstance(selections, (str, bytes)):
            raise TypeError("selections must be an iterable of recall markers")
        normalized: list[tuple[str, int, str, MemoryKind]] = []
        for selection in selections:
            if not isinstance(selection, tuple) or len(selection) != 4:
                raise TypeError("each recall marker must be a four-item tuple")
            record_id, revision, expected_hash, kind = selection
            identity = validate_identifier(record_id, field="record_id")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError("revision must be a positive integer")
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise ValueError("content_hash must be a 64-character operational marker")
            normalized.append((identity, revision, expected_hash, MemoryKind(kind)))
        return tuple(normalized)

    @staticmethod
    def _validate_active_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")

    def _list_active_locked(
        self,
        scope: MemoryScope,
        *,
        instant: datetime,
        limit: int,
    ) -> list[MemoryRecord]:
        """Load one active-memory snapshot while the caller owns a transaction."""

        scope_id, scope_json = _scope_storage(scope)
        rows = self._conn.execute(
            "SELECT r.record_id FROM memory_records AS r "
            "JOIN memory_payloads AS p ON "
            "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
            "AND p.record_id = r.record_id "
            "WHERE r.scope_id = ? AND r.scope_json = ? AND r.state = ? "
            "AND r.trust = ? "
            "AND (r.valid_from IS NULL OR r.valid_from <= ?) "
            "AND (r.valid_until IS NULL OR r.valid_until > ?) "
            "ORDER BY r.updated_at DESC, r.record_id ASC LIMIT ?",
            (
                scope_id,
                scope_json,
                MemoryState.ACTIVE.value,
                MemoryTrust.HOST_CONFIRMED.value,
                format_timestamp(instant),
                format_timestamp(instant),
                limit,
            ),
        ).fetchall()
        records: list[MemoryRecord] = []
        for row in rows:
            record = self._load_record_locked(scope, str(row["record_id"]))
            if record is None or not record.is_recallable(now=instant):
                continue
            self._assert_active_admission_verified_locked(scope, record)
            records.append(record)
        return records

    def events(self, scope: MemoryScope, record_id: str) -> list[MemoryEvent]:
        """Return content-free operational history in sequence order."""
        identity = validate_identifier(record_id, field="record_id")
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            rows = self._conn.execute(
                "SELECT sequence, event_id, record_id, event_type, occurred_at, "
                "revision, actor, related_record_id, reason_code, schema_version "
                "FROM memory_events WHERE scope_id = ? AND scope_json = ? "
                "AND record_id = ? ORDER BY sequence",
                (scope_id, scope_json, identity),
            ).fetchall()
        return [
            MemoryEvent(
                sequence=local_sequence,
                event_id=str(row["event_id"]),
                record_id=str(row["record_id"]),
                scope=scope,
                event_type=MemoryEventType(str(row["event_type"])),
                occurred_at=parse_timestamp(str(row["occurred_at"]), field="occurred_at"),
                revision=int(row["revision"]),
                actor=str(row["actor"]),
                related_record_id=(
                    str(row["related_record_id"])
                    if row["related_record_id"] is not None
                    else None
                ),
                reason_code=(
                    str(row["reason_code"]) if row["reason_code"] is not None else None
                ),
                schema_version=int(row["schema_version"]),
            )
            for local_sequence, row in enumerate(rows, start=1)
        ]

    def admission_audits(
        self,
        scope: MemoryScope,
        record_id: str,
    ) -> list[MemoryAdmissionAudit]:
        """Return content-free policy receipts for one exact-scope record."""

        identity = validate_identifier(record_id, field="record_id")
        with self._lock:
            self._require_ready_locked()
            rows = self._admission_audit_rows_for_record_locked(scope, identity)
        return [self._admission_audit_from_row(scope, row) for row in rows]

    def _admission_audit_rows_for_record_locked(
        self,
        scope: MemoryScope,
        record_id: str,
    ) -> list[sqlite3.Row]:
        """Return joined audit rows; callers must parse each row fail-closed."""

        scope_id, scope_json = _scope_storage(scope)
        return self._conn.execute(
            "SELECT a.event_id, a.record_id, a.candidate_revision, a.origin, "
            "a.policy_id, a.policy_version, a.policy_fingerprint, a.action, "
            "a.reason_code, e.record_id AS event_record_id, e.event_type, "
            "e.revision AS event_revision, e.occurred_at, e.actor, "
            "e.reason_code AS event_reason_code, e.payload_hash AS event_payload_hash, "
            "m.origin AS record_origin FROM memory_review_audits AS a "
            "JOIN memory_events AS e ON e.scope_id = a.scope_id "
            "AND e.scope_json = a.scope_json AND e.event_id = a.event_id "
            "LEFT JOIN memory_record_admission_metadata AS m ON "
            "m.scope_id = a.scope_id AND m.scope_json = a.scope_json "
            "AND m.record_id = a.record_id WHERE a.scope_id = ? "
            "AND a.scope_json = ? AND a.record_id = ? ORDER BY e.sequence",
            (scope_id, scope_json, record_id),
        ).fetchall()

    def _assert_active_admission_verified_locked(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
    ) -> None:
        """Require a fully bound allow receipt before recalling v5 provenance.

        This targeted check runs for records about to cross into the active
        recall lane.  It is stronger than an ``EXISTS`` query: every candidate
        audit is parsed against its event payload, record, revision, reason,
        and immutable origin, so a valid-looking audit attached to another
        lifecycle event cannot make a corrupt record recallable.
        """

        if record.origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            return
        audits = [
            self._admission_audit_from_row(scope, row)
            for row in self._admission_audit_rows_for_record_locked(scope, record.record_id)
        ]
        if not any(
            audit.action is MemoryAdmissionAction.ALLOW
            and audit.origin is record.origin
            for audit in audits
        ):
            raise LedgerStateError(
                "concrete-origin active memory record is missing a valid allow admission audit"
            )

    def export(
        self,
        scope: MemoryScope,
        *,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Create an explicit local export; plaintext is opt-in."""
        _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            with self._read_transaction_locked():
                record_rows = self._conn.execute(
                    "SELECT record_id FROM memory_records WHERE scope_id = ? AND scope_json = ? "
                    "ORDER BY record_id",
                    _scope_storage(scope),
                ).fetchall()
                records = [
                    record
                    for row in record_rows
                    if (record := self._load_record_locked(scope, str(row["record_id"])))
                    is not None
                ]
                events: list[MemoryEvent] = []
                audits: list[MemoryAdmissionAudit] = []
                for record in records:
                    events.extend(self.events(scope, record.record_id))
                    audits.extend(self.admission_audits(scope, record.record_id))
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_schema_version": self.MIGRATION_VERSION,
            "scope": scope_dict(scope),
            "records": [record.to_dict(include_content=include_content) for record in records],
            "events": [event.to_dict() for event in events],
            "admission_audits": [audit.to_dict() for audit in audits],
        }

    def count(self, scope: MemoryScope) -> int:
        """Count materialized records in the exact scope, regardless of state."""
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_records WHERE scope_id = ? AND scope_json = ?",
                (scope_id, scope_json),
            ).fetchone()
        return int(row[0])

    def backup(self, destination: str) -> None:
        """Copy the current SQLite database to an operator-chosen destination."""
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("destination must be a non-empty path")
        with self._lock:
            self._ensure_open_locked()
            target = sqlite3.connect(destination)
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        """Close the owned SQLite connection."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "SqliteMemoryLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass

    def _create_candidate(
        self,
        scope: MemoryScope,
        *,
        event_type: MemoryEventType,
        origin: MemoryOrigin,
        kind: MemoryKind | str,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...] | list[str],
        confidence: float,
        record_id: str | None,
        retention_policy: str,
        valid_from: datetime | str | None,
        valid_until: datetime | str | None,
        actor: str,
        event_id: str | None,
        occurred_at: datetime | str | None,
    ) -> MemoryRecord:
        if event_type not in {MemoryEventType.OBSERVED, MemoryEventType.ASSERTED}:
            raise ValueError("candidate creation requires observed or asserted event type")
        requested_identity = (
            validate_identifier(record_id, field="record_id") if record_id else None
        )
        semantic_kind = MemoryKind(kind)
        origin = MemoryOrigin(origin)
        plaintext = validate_content(content)
        source = validate_reference(source_ref, field="source_ref")
        evidence = validate_references(evidence_refs, field="evidence_refs")
        confidence = _confidence(confidence)
        retention = _retention_policy(retention_policy)
        current_time = self._timestamp(occurred_at)
        starts_at = coerce_datetime(valid_from, field="valid_from")
        ends_at = coerce_datetime(valid_until, field="valid_until")
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            raise ValueError("valid_until must not be before valid_from")
        event_identity = _event_id(event_id)
        actor = _actor(actor)
        digest = content_hash(scope, plaintext)
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = self._candidate_command_hash(
            event_type=event_type,
            requested_record_id=requested_identity,
            kind=semantic_kind,
            confidence=confidence,
            retention_policy=retention,
            valid_from=starts_at,
            valid_until=ends_at,
            actor=actor,
            origin=origin,
        )

        with self._lock:
            with self._ready_write_transaction_locked():
                if self._is_source_revoked_locked(scope_id, scope_json, source):
                    raise LedgerStateError(
                        "source_ref was revoked in this scope and cannot be ingested"
                    )
                existing_event = self._event_for_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                if existing_event is not None:
                    existing_record_id = str(existing_event["record_id"])
                    if (
                        str(existing_event["event_type"]) != event_type.value
                        or str(existing_event["actor"]) != actor
                    ):
                        raise LedgerConflictError(
                            "event_id was already used for a different command"
                        )
                    if (
                        requested_identity is not None
                        and existing_record_id != requested_identity
                    ):
                        raise LedgerConflictError(
                            "event_id was already used for a different record_id"
                        )
                    record = self._load_record_locked(scope, existing_record_id)
                    if record is None:
                        raise LedgerConflictError("event_id belongs to an erased record")
                    if not self._candidate_matches_record(
                        record,
                        kind=semantic_kind,
                        content=plaintext,
                        source_ref=source,
                        evidence_refs=evidence,
                        confidence=confidence,
                        retention_policy=retention,
                        valid_from=starts_at,
                        valid_until=ends_at,
                        origin=origin,
                    ):
                        raise LedgerConflictError(
                            "event_id was already used for a different command"
                        )
                    return record
                self._ensure_no_erasure_receipt_event_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                identity = requested_identity or uuid.uuid4().hex
                if self._is_tombstoned_locked(scope_id, scope_json, identity):
                    raise LedgerStateError(
                        "record_id was erased in this scope and cannot be reused"
                    )
                if self._load_record_locked(scope, identity) is not None:
                    raise LedgerConflictError("record_id already exists in this scope")
                self._append_event_locked(
                    scope_id=scope_id,
                    scope_json=scope_json,
                    record_id=identity,
                    event_id=event_identity,
                    event_type=event_type,
                    revision=1,
                    occurred_at=current_time,
                    actor=actor,
                    payload_hash=payload_hash,
                )
                self._conn.execute(
                    "INSERT INTO memory_records "
                    "(scope_id, scope_json, record_id, kind, state, trust, content_hash, "
                    "confidence, valid_from, valid_until, retention_policy, created_at, updated_at, "
                    "revision, last_event_sequence, superseded_by, retracted_at, expired_at, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                    (
                        scope_id,
                        scope_json,
                        identity,
                        semantic_kind.value,
                        MemoryState.CANDIDATE.value,
                        MemoryTrust.UNTRUSTED.value,
                        digest,
                        confidence,
                        format_timestamp(starts_at) if starts_at else None,
                        format_timestamp(ends_at) if ends_at else None,
                        retention,
                        format_timestamp(current_time),
                        format_timestamp(current_time),
                        1,
                        1,
                        LEDGER_SCHEMA_VERSION,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO memory_payloads "
                    "(scope_id, scope_json, record_id, content, source_refs_json, evidence_refs_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        scope_id,
                        scope_json,
                        identity,
                        plaintext,
                        json.dumps([source], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(list(evidence), ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                self._conn.execute(
                    "INSERT INTO memory_record_admission_metadata "
                    "(scope_id, scope_json, record_id, origin) VALUES (?, ?, ?, ?)",
                    (scope_id, scope_json, identity, origin.value),
                )
                self._conn.execute(
                    "INSERT INTO memory_sources (scope_id, scope_json, source_ref, record_id) "
                    "VALUES (?, ?, ?, ?)",
                    (scope_id, scope_json, source, identity),
                )
                record = self._load_record_locked(scope, identity)
                assert record is not None
                return record

    @staticmethod
    def _candidate_command_hash(
        *,
        event_type: MemoryEventType,
        requested_record_id: str | None,
        kind: MemoryKind,
        confidence: float,
        retention_policy: str,
        valid_from: datetime | None,
        valid_until: datetime | None,
        actor: str,
        origin: MemoryOrigin,
    ) -> str:
        """Fingerprint non-sensitive candidate command metadata only.

        The event row must survive a future ``forget()`` without becoming an
        offline verifier for guessed plaintext, source IDs, or evidence IDs.
        Exact candidate-retry equality is checked against the live payload
        while it exists instead of persisting those fields in this fingerprint.
        """
        payload: dict[str, object] = {
            "event_type": event_type.value,
            "record_id": requested_record_id,
            "kind": kind.value,
            "confidence": confidence,
            "retention_policy": retention_policy,
            "valid_from": format_timestamp(valid_from) if valid_from else None,
            "valid_until": format_timestamp(valid_until) if valid_until else None,
            "actor": actor,
            "format": "memory-ledger-v4-metadata-only-candidate-command",
        }
        if origin is not MemoryOrigin.UNKNOWN:
            payload["origin"] = origin.value
            payload["format"] = "memory-ledger-v5-admission-origin-candidate-command"
        return command_hash(payload)

    @staticmethod
    def _candidate_matches_record(
        record: MemoryRecord,
        *,
        kind: MemoryKind,
        content: str,
        source_ref: str,
        evidence_refs: tuple[str, ...],
        confidence: float,
        retention_policy: str,
        valid_from: datetime | None,
        valid_until: datetime | None,
        origin: MemoryOrigin,
    ) -> bool:
        """Compare a candidate retry while its private payload is still live."""
        return (
            record.kind is kind
            and record.content == content
            and record.source_refs == (source_ref,)
            and record.evidence_refs == evidence_refs
            and record.confidence == confidence
            and record.retention_policy == retention_policy
            and record.valid_from == valid_from
            and record.valid_until == valid_until
            and (
                record.origin is origin
                or (
                    origin is MemoryOrigin.UNKNOWN
                    and record.origin is MemoryOrigin.LEGACY_UNKNOWN
                )
            )
        )

    @staticmethod
    def _admission_command_hash(
        *,
        record_id: str,
        expected_revision: int,
        origin: MemoryOrigin,
        policy_id: str,
        policy_version: str,
        policy_fingerprint: str,
        action: MemoryAdmissionAction,
        reason_code: str,
        actor: str,
    ) -> str:
        """Fingerprint one gated decision without preserving private payloads."""

        return command_hash({
            "format": "memory-ledger-v5-admission-review",
            "event_type": _ADMISSION_ACTION_EVENTS[action].value,
            "record_id": record_id,
            "expected_revision": expected_revision,
            "origin": origin.value,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_fingerprint": policy_fingerprint,
            "action": action.value,
            "reason_code": reason_code,
            "actor": actor,
        })

    def _apply_admission_review(
        self,
        scope: MemoryScope,
        *,
        record_id: str,
        expected_revision: int,
        expected_content_hash: str,
        origin: MemoryOrigin,
        policy_id: str,
        policy_version: str,
        policy_fingerprint: str,
        action: MemoryAdmissionAction,
        reason_code: str,
        actor: str,
        event_id: str,
        occurred_at: datetime | str,
    ) -> MemoryRecord | ErasureReceipt:
        """Atomically resolve one gate-authenticated admission review.

        The gate owns review capability verification.  This internal Ledger
        entry point supplies the durable half of that contract: it re-reads a
        candidate and its sidecar origin only after owning the SQLite write
        lock, validates expiry against its own UTC clock at that boundary,
        then writes the lifecycle event and content-free companion audit in
        one transaction. The host writer timestamp is captured before the
        lock and is never called from this path; it intentionally does not
        call a model, policy, or arbitrary host callback.
        """

        identity = validate_identifier(record_id, field="record_id")
        expected_revision = _expected_revision(expected_revision)
        if not isinstance(expected_content_hash, str) or len(expected_content_hash) != 64:
            raise ValueError("expected_content_hash must be a 64-character operational marker")
        origin = MemoryOrigin(origin)
        policy_id = validate_identifier(policy_id, field="policy_id")
        policy_version = validate_identifier(policy_version, field="policy_version")
        if not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64:
            raise ValueError("policy_fingerprint must be a 64-character digest")
        action = MemoryAdmissionAction(action)
        reason_code = validate_identifier(reason_code, field="reason_code")
        actor = _actor(actor)
        event_identity = validate_identifier(event_id, field="event_id")
        instant = coerce_datetime(occurred_at, field="occurred_at")
        assert instant is not None
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = self._admission_command_hash(
            record_id=identity,
            expected_revision=expected_revision,
            origin=origin,
            policy_id=policy_id,
            policy_version=policy_version,
            policy_fingerprint=policy_fingerprint,
            action=action,
            reason_code=reason_code,
            actor=actor,
        )

        with self._lock:
            with self._ready_write_transaction_locked():
                audit = self._admission_audit_for_event_locked(
                    scope,
                    event_identity,
                )
                if audit is not None:
                    self._assert_matching_admission_audit(
                        audit,
                        record_id=identity,
                        expected_revision=expected_revision,
                        origin=origin,
                        policy_id=policy_id,
                        policy_version=policy_version,
                        policy_fingerprint=policy_fingerprint,
                        action=action,
                        reason_code=reason_code,
                        actor=actor,
                    )
                    event = self._event_for_id_locked(scope_id, scope_json, event_identity)
                    if event is None or str(event["payload_hash"]) != payload_hash:
                        raise LedgerStateError(
                            "admission audit is not paired with its expected lifecycle event"
                        )
                    if action is MemoryAdmissionAction.REJECT:
                        receipt = self._erasure_receipt_locked(
                            scope_id,
                            scope_json,
                            event_identity,
                            identity,
                            payload_hash,
                        )
                        if receipt is None:
                            raise LedgerStateError(
                                "reject admission audit is missing its erasure receipt"
                            )
                        return receipt
                    record = self._load_record_locked(scope, identity)
                    if record is None:
                        raise LedgerStateError(
                            "admission audit refers to an erased memory record"
                        )
                    return record

                existing_event = self._event_for_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                if existing_event is not None:
                    if str(existing_event["payload_hash"]) == payload_hash:
                        raise LedgerStateError(
                            "admission lifecycle event is missing its companion audit"
                        )
                    raise LedgerConflictError("event_id was already used for a different command")
                self._ensure_no_erasure_receipt_event_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                record = self._load_record_locked(scope, identity)
                if record is None:
                    raise KeyError(identity)
                if (
                    record.state is not MemoryState.CANDIDATE
                    or record.trust is not MemoryTrust.UNTRUSTED
                    or record.revision != expected_revision
                    or record.content is None
                    or record.content_hash != expected_content_hash
                    or record.origin is not origin
                ):
                    raise LedgerConflictError(
                        "candidate changed before its admission review could be applied"
                    )
                if any(
                    self._is_source_revoked_locked(scope_id, scope_json, source_ref)
                    for source_ref in record.source_refs
                ):
                    raise LedgerStateError(
                        "candidate source was revoked before its admission review could be applied"
                    )
                # The host clock was sampled by ``MemoryReviewGate`` before
                # this transaction begins. Never invoke arbitrary host code
                # while ``BEGIN IMMEDIATE`` is held; this immutable timestamp
                # keeps deterministic writer clocks coherent with candidate
                # creation and ordinary lifecycle transitions. Admission
                # expiry additionally uses the Ledger's own UTC time after
                # this write boundary is owned, so a queued allow cannot
                # become active after a real-time validity deadline.
                admission_instant = utc_now()
                if (
                    action is MemoryAdmissionAction.ALLOW
                    and record.valid_until is not None
                    and (
                        instant >= record.valid_until
                        or admission_instant >= record.valid_until
                    )
                ):
                    raise LedgerStateError(
                        "cannot confirm a record whose validity already ended"
                    )
                if action is MemoryAdmissionAction.REJECT:
                    outcome: MemoryRecord | ErasureReceipt = self._forget_locked(
                        scope,
                        record_id=identity,
                        expected_revision=expected_revision,
                        reason_code=reason_code,
                        actor=actor,
                        event_id=event_identity,
                        occurred_at=instant,
                        payload_hash=payload_hash,
                    )
                else:
                    event_type = _ADMISSION_ACTION_EVENTS[action]
                    target_state = (
                        MemoryState.ACTIVE
                        if action is MemoryAdmissionAction.ALLOW
                        else MemoryState.QUARANTINED
                    )
                    outcome = self._transition_locked(
                        scope,
                        record,
                        event_id=event_identity,
                        payload_hash=payload_hash,
                        event_type=event_type,
                        target_state=target_state,
                        actor=actor,
                        occurred_at=instant,
                        related_record_id=None,
                        reason_code=reason_code,
                    )
                self._insert_admission_audit_locked(
                    scope,
                    event_id=event_identity,
                    record_id=identity,
                    candidate_revision=expected_revision,
                    origin=origin,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    policy_fingerprint=policy_fingerprint,
                    action=action,
                    reason_code=reason_code,
                )
                return outcome

    @staticmethod
    def _assert_matching_admission_audit(
        audit: MemoryAdmissionAudit,
        *,
        record_id: str,
        expected_revision: int,
        origin: MemoryOrigin,
        policy_id: str,
        policy_version: str,
        policy_fingerprint: str,
        action: MemoryAdmissionAction,
        reason_code: str,
        actor: str,
    ) -> None:
        if (
            audit.record_id != record_id
            or audit.candidate_revision != expected_revision
            or audit.origin is not origin
            or audit.policy_id != policy_id
            or audit.policy_version != policy_version
            or audit.policy_fingerprint != policy_fingerprint
            or audit.action is not action
            or audit.reason_code != reason_code
            or audit.reviewer_actor != actor
        ):
            raise LedgerConflictError("event_id was already used for a different admission review")

    def _insert_admission_audit_locked(
        self,
        scope: MemoryScope,
        *,
        event_id: str,
        record_id: str,
        candidate_revision: int,
        origin: MemoryOrigin,
        policy_id: str,
        policy_version: str,
        policy_fingerprint: str,
        action: MemoryAdmissionAction,
        reason_code: str,
    ) -> None:
        """Persist a review companion after its lifecycle event was appended."""

        scope_id, scope_json = _scope_storage(scope)
        self._conn.execute(
            "INSERT INTO memory_review_audits "
            "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
            "policy_id, policy_version, policy_fingerprint, action, reason_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                event_id,
                record_id,
                candidate_revision,
                origin.value,
                policy_id,
                policy_version,
                policy_fingerprint,
                action.value,
                reason_code,
            ),
        )

    def _admission_audit_for_event_locked(
        self,
        scope: MemoryScope,
        event_id: str,
    ) -> MemoryAdmissionAudit | None:
        """Load and validate one event's immutable admission companion."""

        scope_id, scope_json = _scope_storage(scope)
        row = self._conn.execute(
            "SELECT a.event_id, a.record_id, a.candidate_revision, a.origin, "
            "a.policy_id, a.policy_version, a.policy_fingerprint, a.action, "
            "a.reason_code, e.record_id AS event_record_id, e.event_type, "
            "e.revision AS event_revision, e.occurred_at, e.actor, "
            "e.reason_code AS event_reason_code, e.payload_hash AS event_payload_hash, "
            "m.origin AS record_origin "
            "FROM memory_review_audits AS a JOIN memory_events AS e ON "
            "e.scope_id = a.scope_id AND e.scope_json = a.scope_json "
            "AND e.event_id = a.event_id "
            "LEFT JOIN memory_record_admission_metadata AS m ON "
            "m.scope_id = a.scope_id AND m.scope_json = a.scope_json "
            "AND m.record_id = a.record_id "
            "WHERE a.scope_id = ? AND a.scope_json = ? AND a.event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()
        return self._admission_audit_from_row(scope, row) if row is not None else None

    def _admission_audit_from_row(
        self,
        scope: MemoryScope,
        row: sqlite3.Row,
    ) -> MemoryAdmissionAudit:
        """Parse and cross-check one sidecar audit joined to its event."""

        try:
            audit = MemoryAdmissionAudit(
                event_id=str(row["event_id"]),
                record_id=str(row["record_id"]),
                scope=scope,
                candidate_revision=int(row["candidate_revision"]),
                origin=MemoryOrigin(str(row["origin"])),
                policy_id=str(row["policy_id"]),
                policy_version=str(row["policy_version"]),
                policy_fingerprint=str(row["policy_fingerprint"]),
                action=MemoryAdmissionAction(str(row["action"])),
                reason_code=str(row["reason_code"]),
                occurred_at=parse_timestamp(str(row["occurred_at"]), field="occurred_at"),
                reviewer_actor=str(row["actor"]),
            )
        except (TypeError, ValueError) as exc:
            raise LedgerStateError("memory admission audit row is invalid") from exc
        expected_event = _ADMISSION_ACTION_EVENTS[audit.action]
        try:
            record_origin = MemoryOrigin(str(row["record_origin"]))
        except (TypeError, ValueError) as exc:
            raise LedgerStateError(
                "memory admission audit record origin is invalid or missing"
            ) from exc
        expected_payload_hash = self._admission_command_hash(
            record_id=audit.record_id,
            expected_revision=audit.candidate_revision,
            origin=audit.origin,
            policy_id=audit.policy_id,
            policy_version=audit.policy_version,
            policy_fingerprint=audit.policy_fingerprint,
            action=audit.action,
            reason_code=audit.reason_code,
            actor=audit.reviewer_actor,
        )
        if (
            audit.origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}
            or record_origin is not audit.origin
            or str(row["event_record_id"]) != audit.record_id
            or str(row["event_type"]) != expected_event.value
            or int(row["event_revision"]) != audit.candidate_revision + 1
            or row["event_reason_code"] is None
            or str(row["event_reason_code"]) != audit.reason_code
            or str(row["event_payload_hash"]) != expected_payload_hash
        ):
            raise LedgerStateError(
                "memory admission audit does not match its lifecycle event"
            )
        return audit

    def _transition(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        expected_revision: int,
        event_type: MemoryEventType,
        target_state: MemoryState,
        actor: str,
        event_id: str | None,
        occurred_at: datetime | str | None,
        related_record_id: str | None = None,
        reason_code: str | None = None,
        raw_confirmation: bool = False,
    ) -> MemoryRecord:
        identity = validate_identifier(record_id, field="record_id")
        expected_revision = _expected_revision(expected_revision)
        related = (
            validate_identifier(related_record_id, field="related_record_id")
            if related_record_id is not None
            else None
        )
        reason = (
            validate_identifier(reason_code, field="reason_code")
            if reason_code is not None
            else None
        )
        current_time = self._timestamp(occurred_at)
        event_identity = _event_id(event_id)
        actor = _actor(actor)
        scope_id, scope_json = _scope_storage(scope)
        payload_hash = command_hash({
            "event_type": event_type.value,
            "target_state": target_state.value,
            "record_id": identity,
            "expected_revision": expected_revision,
            "related_record_id": related,
            "reason_code": reason,
            "actor": actor,
        })
        with self._lock:
            with self._ready_write_transaction_locked():
                duplicate = self._idempotent_event_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                    identity,
                    payload_hash,
                )
                if duplicate:
                    record = self._load_record_locked(scope, identity)
                    if record is None:
                        raise LedgerConflictError("event_id belongs to an erased record")
                    if raw_confirmation:
                        self._assert_raw_confirmation_eligible(record)
                    return record
                self._ensure_no_erasure_receipt_event_id_locked(
                    scope_id,
                    scope_json,
                    event_identity,
                )
                record = self._load_record_locked(scope, identity)
                if record is None:
                    raise KeyError(identity)
                self._check_revision(record, expected_revision)
                if raw_confirmation:
                    self._assert_raw_confirmation_eligible(record)
                return self._transition_locked(
                    scope,
                    record,
                    event_id=event_identity,
                    payload_hash=payload_hash,
                    event_type=event_type,
                    target_state=target_state,
                    actor=actor,
                    occurred_at=current_time,
                    related_record_id=related,
                    reason_code=reason,
                )

    @staticmethod
    def _assert_raw_confirmation_eligible(record: MemoryRecord) -> None:
        """Keep provenance-backed v5 candidates behind the review gate.

        The legacy writer API remains a compatibility escape hatch only for
        candidates with no concrete v5 ingress origin. Otherwise it could
        promote a gate-created document, tool, model, or host assertion
        without recording the paired admission audit.
        """

        if record.origin not in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            raise LedgerStateError(
                "concrete-origin candidate must be confirmed through MemoryReviewGate"
            )

    def _transition_locked(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        *,
        event_id: str,
        payload_hash: str,
        event_type: MemoryEventType,
        target_state: MemoryState,
        actor: str,
        occurred_at: datetime,
        related_record_id: str | None,
        reason_code: str | None,
    ) -> MemoryRecord:
        allowed_states = {
            MemoryEventType.CONFIRMED: {MemoryState.CANDIDATE},
            MemoryEventType.SUPERSEDED: {MemoryState.ACTIVE},
            MemoryEventType.RETRACTED: {
                MemoryState.CANDIDATE,
                MemoryState.ACTIVE,
                MemoryState.SUPERSEDED,
                MemoryState.EXPIRED,
                MemoryState.QUARANTINED,
            },
            MemoryEventType.FORGOTTEN: {
                MemoryState.CANDIDATE,
                MemoryState.ACTIVE,
                MemoryState.SUPERSEDED,
                MemoryState.RETRACTED,
                MemoryState.EXPIRED,
                MemoryState.QUARANTINED,
            },
            MemoryEventType.EXPIRED: {MemoryState.CANDIDATE, MemoryState.ACTIVE},
            MemoryEventType.QUARANTINED: {MemoryState.CANDIDATE, MemoryState.ACTIVE},
        }
        if event_type not in allowed_states or record.state not in allowed_states[event_type]:
            raise LedgerStateError(
                f"cannot apply {event_type.value} to {record.state.value} record"
            )
        if event_type is MemoryEventType.CONFIRMED and (
            record.valid_until is not None and occurred_at >= record.valid_until
        ):
            raise LedgerStateError("cannot confirm a record whose validity already ended")
        scope_id, scope_json = _scope_storage(scope)
        next_revision = record.revision + 1
        self._append_event_locked(
            scope_id=scope_id,
            scope_json=scope_json,
            record_id=record.record_id,
            event_id=event_id,
            event_type=event_type,
            revision=next_revision,
            occurred_at=occurred_at,
            actor=actor,
            related_record_id=related_record_id,
            reason_code=reason_code,
            payload_hash=payload_hash,
        )
        trust = (
            MemoryTrust.HOST_CONFIRMED.value
            if event_type is MemoryEventType.CONFIRMED
            else record.trust.value
        )
        retracted_at = (
            format_timestamp(occurred_at)
            if event_type is MemoryEventType.RETRACTED
            or (
                event_type is MemoryEventType.FORGOTTEN
                and record.retracted_at is None
            )
            else format_timestamp(record.retracted_at) if record.retracted_at else None
        )
        expired_at = (
            format_timestamp(occurred_at)
            if event_type is MemoryEventType.EXPIRED
            else format_timestamp(record.expired_at) if record.expired_at else None
        )
        superseded_by = (
            related_record_id
            if event_type is MemoryEventType.SUPERSEDED
            else record.superseded_by
        )
        self._conn.execute(
            "UPDATE memory_records SET state = ?, trust = ?, updated_at = ?, revision = ?, "
            "last_event_sequence = ?, superseded_by = ?, retracted_at = ?, expired_at = ? "
            "WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
            (
                target_state.value,
                trust,
                format_timestamp(occurred_at),
                next_revision,
                next_revision,
                superseded_by,
                retracted_at,
                expired_at,
                scope_id,
                scope_json,
                record.record_id,
            ),
        )
        updated = self._load_record_locked(scope, record.record_id)
        assert updated is not None
        return updated

    def _append_event_locked(
        self,
        *,
        scope_id: str,
        scope_json: str,
        record_id: str,
        event_id: str,
        event_type: MemoryEventType,
        revision: int,
        occurred_at: datetime,
        actor: str,
        payload_hash: str,
        related_record_id: str | None = None,
        reason_code: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO memory_events "
            "(scope_id, scope_json, record_id, event_id, event_type, revision, occurred_at, "
            "actor, related_record_id, reason_code, content_hash, payload_hash, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                record_id,
                event_id,
                event_type.value,
                revision,
                format_timestamp(occurred_at),
                actor,
                related_record_id,
                reason_code,
                None,
                payload_hash,
                LEDGER_SCHEMA_VERSION,
            ),
        )
        return int(cursor.lastrowid)

    def _idempotent_event_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
        record_id: str,
        payload_hash: str,
    ) -> bool:
        row = self._event_for_id_locked(scope_id, scope_json, event_id)
        if row is None:
            return False
        if str(row["record_id"]) != record_id or str(row["payload_hash"]) != payload_hash:
            raise LedgerConflictError("event_id was already used for a different command")
        return True

    def _event_for_id_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT record_id, event_type, actor, payload_hash FROM memory_events "
            "WHERE scope_id = ? AND scope_json = ? AND event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()

    def _erasure_receipt_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
        record_id: str,
        payload_hash: str,
    ) -> ErasureReceipt | None:
        row = self._conn.execute(
            "SELECT record_id, payload_hash, state, payload_deleted, "
            "source_refs_deleted, relations_deleted FROM memory_erasure_receipts "
            "WHERE scope_id = ? AND scope_json = ? AND event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["record_id"]) != record_id or str(row["payload_hash"]) != payload_hash:
            raise LedgerConflictError("event_id was already used for a different command")
        return ErasureReceipt(
            record_id=record_id,
            state=MemoryState(str(row["state"])),
            payload_deleted=bool(row["payload_deleted"]),
            source_refs_deleted=int(row["source_refs_deleted"]),
            relations_deleted=int(row["relations_deleted"]),
        )

    def _store_erasure_receipt_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
        payload_hash: str,
        receipt: ErasureReceipt,
    ) -> None:
        self._conn.execute(
            "INSERT INTO memory_erasure_receipts "
            "(scope_id, scope_json, event_id, record_id, payload_hash, state, "
            "payload_deleted, source_refs_deleted, relations_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                event_id,
                receipt.record_id,
                payload_hash,
                receipt.state.value if receipt.state is not None else None,
                int(receipt.payload_deleted),
                receipt.source_refs_deleted,
                receipt.relations_deleted,
            ),
        )

    def _hard_erase_receipt_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
        record_id: str,
        payload_hash: str,
    ) -> ErasureReceipt | None:
        row = self._conn.execute(
            "SELECT record_key, payload_hash, payload_deleted, source_refs_deleted, "
            "relations_deleted, events_deleted FROM memory_hard_erase_receipts "
            "WHERE scope_id = ? AND scope_json = ? AND event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row["record_key"])
            != self._tombstone_key(scope_id, scope_json, record_id)
            or str(row["payload_hash"]) != payload_hash
        ):
            raise LedgerConflictError("event_id was already used for a different command")
        return ErasureReceipt(
            record_id=record_id,
            state=None,
            payload_deleted=bool(row["payload_deleted"]),
            source_refs_deleted=int(row["source_refs_deleted"]),
            relations_deleted=int(row["relations_deleted"]),
            events_deleted=int(row["events_deleted"]),
        )

    def _store_hard_erase_receipt_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
        payload_hash: str,
        receipt: ErasureReceipt,
    ) -> None:
        self._conn.execute(
            "INSERT INTO memory_hard_erase_receipts "
            "(scope_id, scope_json, event_id, record_key, payload_hash, payload_deleted, "
            "source_refs_deleted, relations_deleted, events_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                event_id,
                self._tombstone_key(scope_id, scope_json, receipt.record_id),
                payload_hash,
                int(receipt.payload_deleted),
                receipt.source_refs_deleted,
                receipt.relations_deleted,
                receipt.events_deleted,
            ),
        )

    @staticmethod
    def _tombstone_key(scope_id: str, scope_json: str, record_id: str) -> str:
        """Return a non-plaintext replay barrier for a hard-erased record."""
        return command_hash({
            "scope_id": scope_id,
            "scope_json": scope_json,
            "record_id": record_id,
            "purpose": "hard_erase_replay_barrier",
        })

    def _is_tombstoned_locked(
        self,
        scope_id: str,
        scope_json: str,
        record_id: str,
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memory_erasure_tombstones "
            "WHERE scope_id = ? AND scope_json = ? AND record_key = ?",
            (
                scope_id,
                scope_json,
                self._tombstone_key(scope_id, scope_json, record_id),
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def _event_tombstone_key(scope_id: str, scope_json: str, event_id: str) -> str:
        """Return a non-plaintext replay barrier for an erased command ID."""
        return command_hash({
            "scope_id": scope_id,
            "scope_json": scope_json,
            "event_id": event_id,
            "purpose": "hard_erase_event_replay_barrier",
        })

    @staticmethod
    def _source_tombstone_key(scope_id: str, scope_json: str, source_ref: str) -> str:
        """Return a scoped non-plaintext barrier for a revoked source."""
        return command_hash({
            "scope_id": scope_id,
            "scope_json": scope_json,
            "source_ref": source_ref,
            "purpose": "source_revocation_replay_barrier",
        })

    def _is_source_revoked_locked(
        self,
        scope_id: str,
        scope_json: str,
        source_ref: str,
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memory_source_revocation_tombstones "
            "WHERE scope_id = ? AND scope_json = ? AND source_key = ?",
            (
                scope_id,
                scope_json,
                self._source_tombstone_key(scope_id, scope_json, source_ref),
            ),
        ).fetchone()
        return row is not None

    def _ensure_no_erasure_receipt_event_id_locked(
        self,
        scope_id: str,
        scope_json: str,
        event_id: str,
    ) -> None:
        """Keep idempotency keys collision-free across all command receipts."""
        row = self._conn.execute(
            "SELECT 1 FROM memory_erasure_receipts "
            "WHERE scope_id = ? AND scope_json = ? AND event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()
        if row is not None:
            raise LedgerConflictError("event_id was already used for an erasure command")
        row = self._conn.execute(
            "SELECT 1 FROM memory_hard_erase_receipts "
            "WHERE scope_id = ? AND scope_json = ? AND event_id = ?",
            (scope_id, scope_json, event_id),
        ).fetchone()
        if row is not None:
            raise LedgerConflictError("event_id was already used for a hard erase command")
        row = self._conn.execute(
            "SELECT 1 FROM memory_erased_event_tombstones "
            "WHERE scope_id = ? AND scope_json = ? AND event_key = ?",
            (
                scope_id,
                scope_json,
                self._event_tombstone_key(scope_id, scope_json, event_id),
            ),
        ).fetchone()
        if row is not None:
            raise LedgerStateError("event_id belongs to a hard-erased record")

    def _redact_erased_relation_references_locked(
        self,
        scope_id: str,
        scope_json: str,
        record_id: str,
    ) -> None:
        """Remove references to a hard-erased record from other event rows.

        A hard erase is the narrow, explicit exception to event immutability:
        a supersession event owned by another record must not keep the erased
        ID (or a deterministic command fingerprint containing it) alive.
        """
        row = self._conn.execute(
            "SELECT 1 FROM memory_events WHERE scope_id = ? AND scope_json = ? "
            "AND record_id != ? AND related_record_id = ? LIMIT 1",
            (scope_id, scope_json, record_id, record_id),
        ).fetchone()
        if row is None:
            return
        self._drop_owned_event_update_triggers_locked()
        self._conn.execute(
            "UPDATE memory_events SET related_record_id = NULL, payload_hash = ? "
            "WHERE scope_id = ? AND scope_json = ? AND record_id != ? "
            "AND related_record_id = ?",
            (
                _ERASED_RELATION_EVENT_PAYLOAD_HASH,
                scope_id,
                scope_json,
                record_id,
                record_id,
            ),
        )
        self._ensure_event_immutability_locked()

    def _load_record_locked(
        self,
        scope: MemoryScope,
        record_id: str,
    ) -> MemoryRecord | None:
        scope_id, scope_json = _scope_storage(scope)
        row = self._conn.execute(
            "SELECT r.*, p.content, p.source_refs_json, p.evidence_refs_json, m.origin "
            "FROM memory_records AS r LEFT JOIN memory_payloads AS p ON "
            "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
            "AND p.record_id = r.record_id "
            "LEFT JOIN memory_record_admission_metadata AS m ON "
            "m.scope_id = r.scope_id AND m.scope_json = r.scope_json "
            "AND m.record_id = r.record_id "
            "WHERE r.scope_id = ? AND r.scope_json = ? AND r.record_id = ?",
            (scope_id, scope_json, record_id),
        ).fetchone()
        if row is None:
            return None
        relation_rows = self._conn.execute(
            "SELECT relation, to_record_id FROM memory_relations "
            "WHERE scope_id = ? AND scope_json = ? AND from_record_id = ? "
            "ORDER BY relation, to_record_id",
            (scope_id, scope_json, record_id),
        ).fetchall()
        source_refs = (
            tuple(json.loads(str(row["source_refs_json"])))
            if row["source_refs_json"] is not None
            else ()
        )
        evidence_refs = (
            tuple(json.loads(str(row["evidence_refs_json"])))
            if row["evidence_refs_json"] is not None
            else ()
        )
        if row["origin"] is None:
            if row["content"] is not None:
                raise LedgerStateError(
                    "payload-bearing memory record is missing admission origin metadata"
                )
            origin = MemoryOrigin.UNKNOWN
        else:
            origin = MemoryOrigin(str(row["origin"]))
        return MemoryRecord(
            record_id=str(row["record_id"]),
            scope=scope,
            kind=MemoryKind(str(row["kind"])),
            state=MemoryState(str(row["state"])),
            trust=MemoryTrust(str(row["trust"])),
            origin=origin,
            content=str(row["content"]) if row["content"] is not None else None,
            content_hash=str(row["content_hash"]),
            source_refs=source_refs,
            evidence_refs=evidence_refs,
            confidence=float(row["confidence"]),
            created_at=parse_timestamp(str(row["created_at"]), field="created_at"),
            updated_at=parse_timestamp(str(row["updated_at"]), field="updated_at"),
            valid_from=(
                parse_timestamp(str(row["valid_from"]), field="valid_from")
                if row["valid_from"] is not None
                else None
            ),
            valid_until=(
                parse_timestamp(str(row["valid_until"]), field="valid_until")
                if row["valid_until"] is not None
                else None
            ),
            retention_policy=str(row["retention_policy"]),
            revision=int(row["revision"]),
            last_event_sequence=int(row["last_event_sequence"]),
            relations=tuple(
                MemoryRelation(
                    relation=MemoryRelationType(str(relation_row["relation"])),
                    record_id=str(relation_row["to_record_id"]),
                )
                for relation_row in relation_rows
            ),
            superseded_by=(
                str(row["superseded_by"])
                if row["superseded_by"] is not None
                else None
            ),
            retracted_at=(
                parse_timestamp(str(row["retracted_at"]), field="retracted_at")
                if row["retracted_at"] is not None
                else None
            ),
            expired_at=(
                parse_timestamp(str(row["expired_at"]), field="expired_at")
                if row["expired_at"] is not None
                else None
            ),
            schema_version=int(row["schema_version"]),
        )

    @staticmethod
    def _check_revision(record: MemoryRecord, expected_revision: int) -> None:
        if record.revision != expected_revision:
            raise LedgerConflictError(
                f"record {record.record_id!r} has revision {record.revision}, "
                f"not expected {expected_revision}"
            )

    @staticmethod
    def _migration_statements(current: int) -> tuple[str, ...]:
        if current == 0:
            return _SCHEMA_STATEMENTS
        statements: tuple[str, ...] = ()
        if current <= 1:
            statements += _V2_MIGRATION_STATEMENTS
        if current <= 2:
            statements += _V3_MIGRATION_STATEMENTS
        if current <= 3:
            statements += _V4_MIGRATION_STATEMENTS
        if current <= 4:
            statements += _V5_MIGRATION_STATEMENTS
        return statements

    @staticmethod
    def _migration_actions(current: int) -> list[str]:
        if current == 0:
            return ["create isolated memory-ledger tables"]
        if current == 1:
            return [
                "add v2 erasure receipts and replay tombstones",
                "add v3 erased-command replay barriers",
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
                "add v5 admission provenance and review audit tables",
            ]
        if current == 2:
            return [
                "add v3 erased-command replay barriers",
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
                "add v5 admission provenance and review audit tables",
            ]
        if current == 3:
            return [
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
                "add v5 admission provenance and review audit tables",
            ]
        if current == 4:
            return ["add v5 admission provenance and review audit tables"]
        return []

    def _backfill_legacy_admission_metadata_locked(self) -> None:
        """Mark live pre-v5 payloads as unknown without inventing review history.

        A v4 Ledger did not persist an ingress category.  The migration may
        therefore only label payload-bearing rows as ``legacy_unknown``; it
        must not infer provenance from an actor, an event type, or payload
        text.  Records already forgotten before the migration deliberately
        receive no new provenance projection.
        """

        self._conn.execute(
            "INSERT INTO memory_record_admission_metadata "
            "(scope_id, scope_json, record_id, origin) "
            "SELECT r.scope_id, r.scope_json, r.record_id, ? "
            "FROM memory_records AS r "
            "JOIN memory_payloads AS p ON p.scope_id = r.scope_id "
            "AND p.scope_json = r.scope_json AND p.record_id = r.record_id "
            "LEFT JOIN memory_record_admission_metadata AS m "
            "ON m.scope_id = r.scope_id AND m.scope_json = r.scope_json "
            "AND m.record_id = r.record_id "
            "WHERE m.record_id IS NULL",
            (MemoryOrigin.LEGACY_UNKNOWN.value,),
        )

    def _scrub_legacy_creation_event_fingerprints_locked(self) -> None:
        """Redact pre-v4 event fingerprints during an explicit migration.

        Versions 1--3 stored a deterministic content-derived fingerprint in
        creation-event rows.  The lifecycle event table is normally immutable,
        but this one controlled setup migration removes that legacy value before
        reinstalling the namespaced append-only trigger.
        """
        self._drop_owned_event_update_triggers_locked()
        self._conn.execute("UPDATE memory_events SET content_hash = NULL")
        self._conn.execute(
            "UPDATE memory_events SET payload_hash = ? "
            "WHERE event_type IN (?, ?)",
            (
                _LEGACY_CREATION_EVENT_PAYLOAD_HASH,
                MemoryEventType.OBSERVED.value,
                MemoryEventType.ASSERTED.value,
            ),
        )
        self._conn.execute(
            "UPDATE memory_records SET content_hash = ? "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM memory_payloads AS p "
            "WHERE p.scope_id = memory_records.scope_id "
            "AND p.scope_json = memory_records.scope_json "
            "AND p.record_id = memory_records.record_id"
            ")",
            (_REDACTED_CONTENT_HASH,),
        )
        self._conn.execute("UPDATE memory_records SET last_event_sequence = revision")

    def _drop_owned_event_update_triggers_locked(self) -> None:
        """Temporarily remove only known ProtoPrompt event-update guards."""
        for trigger_name in (
            _EVENT_UPDATE_TRIGGER_NAME,
            _LEGACY_EVENT_UPDATE_TRIGGER_NAME,
        ):
            row = self._conn.execute(
                "SELECT tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = ? COLLATE NOCASE",
                (trigger_name,),
            ).fetchone()
            if row is None:
                continue
            target = str(row["tbl_name"]).casefold()
            statement = str(row["sql"]).lower()
            if (
                trigger_name == _EVENT_UPDATE_TRIGGER_NAME
                and target == "memory_events"
            ):
                self._conn.execute(f"DROP TRIGGER {trigger_name}")
            elif (
                target == "memory_events"
                and "memory_events are append-only" in statement
            ):
                self._conn.execute(f"DROP TRIGGER {trigger_name}")
            elif trigger_name == _EVENT_UPDATE_TRIGGER_NAME:
                raise LedgerStateError(
                    "memory event immutability trigger name is owned by another table"
                )

    @staticmethod
    def _timestamp(value: datetime | str | None) -> datetime:
        timestamp = coerce_datetime(value, field="occurred_at") if value is not None else utc_now()
        assert timestamp is not None
        return timestamp

    @contextmanager
    def _write_transaction_locked(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @contextmanager
    def _ready_write_transaction_locked(self) -> Iterator[None]:
        """Lock schema writers before verifying a ledger write boundary.

        SQLite DDL and triggers are writes. Checking compatibility before
        ``BEGIN IMMEDIATE`` would leave a window for another connection to add
        a payload-copying trigger or restrictive index between validation and a
        lifecycle mutation. Every operational write therefore validates the
        complete ledger object boundary only after it owns the write lock.
        """
        with self._write_transaction_locked():
            self._require_ready_locked()
            yield

    @contextmanager
    def _read_transaction_locked(self) -> Iterator[None]:
        """Hold one SQLite snapshot across a multi-query reader operation."""
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _schema_version_locked(self) -> int:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ledger_schema' COLLATE NOCASE"
        ).fetchone()
        if exists is None:
            return 0
        try:
            row = self._conn.execute(
                "SELECT version FROM ledger_schema WHERE component = ?", (_COMPONENT,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise LedgerStateError(
                "existing ledger_schema table is not compatible with the ProtoPrompt memory ledger"
            ) from exc
        return int(row["version"]) if row is not None else 0

    def _assert_schema_compatible_locked(
        self,
        current: int,
        *,
        allow_event_guard_repair: bool = False,
        allow_admission_guard_repair: bool = False,
        validate_admission_data: bool = False,
    ) -> None:
        """Fail closed instead of adopting an unrelated generic SQLite table."""
        objects = {
            str(row["name"]).casefold(): (
                str(row["type"]),
                str(row["tbl_name"]).casefold(),
                row["sql"],
            )
            for row in self._conn.execute(
                "SELECT name, type, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'view', 'index', 'trigger')"
            ).fetchall()
        }
        table_names = {
            name for name, (object_type, _, _) in objects.items() if object_type == "table"
        }
        if current == 0:
            collisions = sorted(set(objects).intersection(_LEDGER_RESERVED_OBJECT_NAMES))
            if collisions:
                raise LedgerStateError(
                    "refusing memory-ledger setup because reserved schema names already exist: "
                    + ", ".join(collisions)
                )
            return
        if current < 5:
            unexpected_future = sorted(
                set(objects).intersection(
                    set(_V5_TABLE_COLUMNS)
                    | _V5_INDEX_NAMES
                    | set(_ADMISSION_IMMUTABILITY_TRIGGERS)
                )
            )
            if unexpected_future:
                raise LedgerStateError(
                    f"ledger schema v{current} unexpectedly contains v5 admission objects: "
                    + ", ".join(unexpected_future)
                )
        for table_name in _ALL_LEDGER_TABLE_COLUMNS:
            existing = objects.get(table_name)
            if existing is not None and existing[0] != "table":
                raise LedgerStateError(
                    f"reserved ledger table name {table_name!r} is owned by a {existing[0]!r}"
                )
        required = dict(_V1_TABLE_COLUMNS)
        if current >= 2:
            required.update(_V2_TABLE_COLUMNS)
        if current >= 3:
            required.update(_V3_TABLE_COLUMNS)
        if current >= 4:
            required.update(_V4_TABLE_COLUMNS)
        if current >= 5:
            required.update(_V5_TABLE_COLUMNS)
        for table_name, expected_columns in _ALL_LEDGER_TABLE_COLUMNS.items():
            if table_name not in table_names:
                continue
            actual_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if not expected_columns.issubset(actual_columns):
                raise LedgerStateError(
                    f"existing table {table_name!r} is not compatible with the memory ledger"
                )
            table_sql = objects[table_name][2]
            if (
                table_sql is None
                or _normalized_table_sql(str(table_sql))
                != _LEDGER_TABLE_SIGNATURES[table_name]
            ):
                raise LedgerStateError(
                    f"existing table {table_name!r} does not match the memory ledger definition"
                )
        for table_name in required:
            if table_name not in table_names:
                raise LedgerStateError(
                    f"memory ledger schema v{current} is missing required table {table_name!r}"
                )
        for index_name, expected_signature in _required_index_signatures(current).items():
            index = objects.get(index_name)
            if (
                index is None
                or index[0] != "index"
                or index[2] is None
                or _normalized_table_sql(str(index[2])) != expected_signature
            ):
                raise LedgerStateError(
                    f"existing index {index_name!r} does not match the memory ledger definition"
                )
        for object_name, (object_type, target, definition) in objects.items():
            if (
                object_type == "index"
                and definition is not None
                and target in _ALL_LEDGER_TABLE_COLUMNS
                and object_name not in _LEDGER_INDEX_SIGNATURES
            ):
                raise LedgerStateError(
                    f"unexpected explicit index {object_name!r} targets memory ledger table {target!r}"
                )
        event_guard = objects.get(_EVENT_UPDATE_TRIGGER_NAME)
        if event_guard is not None and event_guard[0] != "trigger":
            raise LedgerStateError(
                "memory event immutability trigger name is owned by another schema object"
            )
        admission_guards = {
            name: objects.get(name) for name in _ADMISSION_IMMUTABILITY_TRIGGERS
        }
        for name, guard in admission_guards.items():
            if guard is not None and guard[0] != "trigger":
                raise LedgerStateError(
                    f"admission immutability trigger name {name!r} is owned by another schema object"
                )
        for object_name, (object_type, target, definition) in objects.items():
            if object_type != "trigger":
                continue
            if object_name == _EVENT_UPDATE_TRIGGER_NAME:
                if target != "memory_events":
                    raise LedgerStateError(
                        "memory event immutability trigger name is owned by another table"
                    )
                if (
                    current >= _EVENT_GUARD_SCHEMA_VERSION
                    and not allow_event_guard_repair
                    and (
                        definition is None
                        or _normalized_table_sql(str(definition))
                        != _EVENT_UPDATE_TRIGGER_SIGNATURE
                    )
                ):
                    raise LedgerStateError(
                        "memory event immutability trigger does not match the ledger definition"
                    )
                continue
            if object_name in _ADMISSION_IMMUTABILITY_TRIGGERS:
                expected_target, expected_signature = _ADMISSION_IMMUTABILITY_TRIGGERS[object_name]
                if target != expected_target:
                    raise LedgerStateError(
                        f"admission immutability trigger {object_name!r} targets another table"
                    )
                if (
                    current >= _ADMISSION_GUARD_SCHEMA_VERSION
                    and not allow_admission_guard_repair
                    and (
                        definition is None
                        or _normalized_table_sql(str(definition)) != expected_signature
                    )
                ):
                    raise LedgerStateError(
                        f"admission immutability trigger {object_name!r} does not match the ledger definition"
                    )
                continue
            if target not in _ALL_LEDGER_TABLE_COLUMNS:
                continue
            if (
                current < _EVENT_GUARD_SCHEMA_VERSION
                and object_name == _LEGACY_EVENT_UPDATE_TRIGGER_NAME
                and target == "memory_events"
                and definition is not None
                and "memory_events are append-only" in str(definition).lower()
            ):
                continue
            raise LedgerStateError(
                f"unexpected trigger {object_name!r} targets memory ledger table {target!r}"
            )
        if (
            current >= _EVENT_GUARD_SCHEMA_VERSION
            and event_guard is None
            and not allow_event_guard_repair
        ):
            raise LedgerStateError("memory event immutability trigger is missing")
        if current >= _ADMISSION_GUARD_SCHEMA_VERSION and not allow_admission_guard_repair:
            missing_admission_guards = [
                name for name, guard in admission_guards.items() if guard is None
            ]
            if missing_admission_guards:
                raise LedgerStateError(
                    "admission immutability trigger is missing: "
                    + ", ".join(sorted(missing_admission_guards))
                )
        if current >= 5 and validate_admission_data:
            self._validate_admission_sidecars_locked()

    def _validate_admission_sidecars_locked(self) -> None:
        """Fail closed on corrupted or orphaned v5 admission sidecars.

        Foreign keys protect normal writes, but SQLite databases may be
        created or edited with foreign-key enforcement disabled.  Setup and
        explicit setup and dry-run validation therefore independently prove
        that every live payload has a closed origin and every audit has the
        matching record, event, action, revision, and opaque metadata it
        claims. Ordinary operation paths validate only rows they touch to
        keep reads and writes bounded.
        """

        missing_origin = self._conn.execute(
            "SELECT 1 FROM memory_records AS r JOIN memory_payloads AS p ON "
            "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
            "AND p.record_id = r.record_id LEFT JOIN "
            "memory_record_admission_metadata AS m ON m.scope_id = r.scope_id "
            "AND m.scope_json = r.scope_json AND m.record_id = r.record_id "
            "WHERE m.record_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_origin is not None:
            raise LedgerStateError(
                "payload-bearing memory record is missing admission origin metadata"
            )
        metadata_rows = self._conn.execute(
            "SELECT m.scope_id, m.scope_json, m.record_id, m.origin, "
            "r.record_id AS matched_record_id FROM memory_record_admission_metadata AS m "
            "LEFT JOIN memory_records AS r ON r.scope_id = m.scope_id "
            "AND r.scope_json = m.scope_json AND r.record_id = m.record_id"
        ).fetchall()
        for row in metadata_rows:
            if row["matched_record_id"] is None:
                raise LedgerStateError("memory admission origin metadata is orphaned")
            try:
                MemoryOrigin(str(row["origin"]))
            except ValueError as exc:
                raise LedgerStateError("memory admission origin metadata is invalid") from exc
        audit_rows = self._conn.execute(
            "SELECT a.scope_id, a.scope_json, a.event_id, a.record_id, "
            "a.candidate_revision, a.origin, a.policy_id, a.policy_version, "
            "a.policy_fingerprint, a.action, a.reason_code, "
            "r.record_id AS matched_record_id, e.record_id AS event_record_id, "
            "e.event_type, e.revision AS event_revision, e.occurred_at, e.actor, "
            "e.reason_code AS event_reason_code, e.payload_hash AS event_payload_hash, "
            "m.origin AS record_origin FROM memory_review_audits AS a "
            "LEFT JOIN memory_records AS r ON r.scope_id = a.scope_id "
            "AND r.scope_json = a.scope_json AND r.record_id = a.record_id "
            "LEFT JOIN memory_events AS e ON e.scope_id = a.scope_id "
            "AND e.scope_json = a.scope_json AND e.event_id = a.event_id"
            " LEFT JOIN memory_record_admission_metadata AS m ON "
            "m.scope_id = a.scope_id AND m.scope_json = a.scope_json "
            "AND m.record_id = a.record_id"
        ).fetchall()
        for row in audit_rows:
            if row["matched_record_id"] is None or row["event_record_id"] is None:
                raise LedgerStateError("memory admission audit is orphaned")
            try:
                scope_data = json.loads(str(row["scope_json"]))
                if not isinstance(scope_data, dict):
                    raise TypeError("scope JSON must be an object")
                audit_scope = MemoryScope(**scope_data)
                scope_id, scope_json = _scope_storage(audit_scope)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerStateError("memory admission audit has an invalid scope") from exc
            if scope_id != str(row["scope_id"]) or scope_json != str(row["scope_json"]):
                raise LedgerStateError("memory admission audit scope does not match its storage key")
            self._admission_audit_from_row(audit_scope, row)
        unreviewed_active = self._conn.execute(
            "SELECT 1 FROM memory_records AS r JOIN "
            "memory_record_admission_metadata AS m ON m.scope_id = r.scope_id "
            "AND m.scope_json = r.scope_json AND m.record_id = r.record_id "
            "WHERE r.state = ? AND r.trust = ? AND m.origin NOT IN (?, ?) "
            "AND NOT EXISTS (SELECT 1 FROM memory_review_audits AS a JOIN "
            "memory_events AS e ON e.scope_id = a.scope_id "
            "AND e.scope_json = a.scope_json AND e.event_id = a.event_id "
            "WHERE a.scope_id = r.scope_id AND a.scope_json = r.scope_json "
            "AND a.record_id = r.record_id AND a.origin = m.origin "
            "AND a.action = ? AND e.event_type = ?) LIMIT 1",
            (
                MemoryState.ACTIVE.value,
                MemoryTrust.HOST_CONFIRMED.value,
                MemoryOrigin.UNKNOWN.value,
                MemoryOrigin.LEGACY_UNKNOWN.value,
                MemoryAdmissionAction.ALLOW.value,
                MemoryEventType.CONFIRMED.value,
            ),
        ).fetchone()
        if unreviewed_active is not None:
            raise LedgerStateError(
                "concrete-origin active memory record is missing an allow admission audit"
            )

    def _ensure_event_immutability_locked(self) -> None:
        """Install and behaviorally verify the uniquely named update guard.

        SQLite trigger names are database-global. A generic trigger name plus
        ``IF NOT EXISTS`` can silently skip the guard when an unrelated legacy
        trigger owns that name. Setup therefore owns a namespaced trigger and
        proves that it aborts an update inside a rolled-back savepoint.
        """
        row = self._conn.execute(
            "SELECT tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = ? COLLATE NOCASE",
            (_EVENT_UPDATE_TRIGGER_NAME,),
        ).fetchone()
        if row is not None:
            if str(row["tbl_name"]).casefold() != "memory_events":
                raise LedgerStateError(
                    "memory event immutability trigger name is owned by another table"
                )
            self._conn.execute(f"DROP TRIGGER {_EVENT_UPDATE_TRIGGER_NAME}")
        self._conn.execute(_EVENT_UPDATE_TRIGGER_SQL)

        probe_id = f"__ledger-trigger-probe-{uuid.uuid4().hex}"
        self._conn.execute("SAVEPOINT memory_event_trigger_probe")
        try:
            self._conn.execute(
                "INSERT INTO memory_events "
                "(scope_id, scope_json, record_id, event_id, event_type, revision, occurred_at, "
                "actor, related_record_id, reason_code, content_hash, payload_hash, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    "__ledger-trigger-probe__",
                    "{}",
                    probe_id,
                    probe_id,
                    MemoryEventType.OBSERVED.value,
                    1,
                    format_timestamp(utc_now()),
                    "host",
                    "probe",
                    LEDGER_SCHEMA_VERSION,
                ),
            )
            try:
                self._conn.execute(
                    "UPDATE memory_events SET actor = 'mutated' WHERE event_id = ?",
                    (probe_id,),
                )
            except sqlite3.DatabaseError as exc:
                if "memory_events are append-only" not in str(exc):
                    raise LedgerStateError(
                        "memory event immutability trigger rejected the probe unexpectedly"
                    ) from exc
            else:
                raise LedgerStateError("memory event immutability trigger is missing or invalid")
        finally:
            self._conn.execute("ROLLBACK TO memory_event_trigger_probe")
            self._conn.execute("RELEASE memory_event_trigger_probe")

    def _ensure_admission_immutability_locked(self) -> None:
        """Install and behaviorally verify immutable v5 sidecar guards.

        Origin and review rows are companion provenance records, not mutable
        annotations.  Deletes remain permitted for hard erasure and foreign
        key cascade; normal lifecycle code never updates either table.
        """

        trigger_sql = {
            _ADMISSION_METADATA_UPDATE_TRIGGER_NAME: _ADMISSION_METADATA_UPDATE_TRIGGER_SQL,
            _ADMISSION_METADATA_DELETE_TRIGGER_NAME: _ADMISSION_METADATA_DELETE_TRIGGER_SQL,
            _ADMISSION_METADATA_INSERT_TRIGGER_NAME: _ADMISSION_METADATA_INSERT_TRIGGER_SQL,
            _REVIEW_AUDIT_UPDATE_TRIGGER_NAME: _REVIEW_AUDIT_UPDATE_TRIGGER_SQL,
            _REVIEW_AUDIT_DELETE_TRIGGER_NAME: _REVIEW_AUDIT_DELETE_TRIGGER_SQL,
            _REVIEW_AUDIT_INSERT_TRIGGER_NAME: _REVIEW_AUDIT_INSERT_TRIGGER_SQL,
        }
        for trigger_name, (target, _) in _ADMISSION_IMMUTABILITY_TRIGGERS.items():
            row = self._conn.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type = 'trigger' "
                "AND name = ? COLLATE NOCASE",
                (trigger_name,),
            ).fetchone()
            if row is not None:
                if str(row["tbl_name"]).casefold() != target:
                    raise LedgerStateError(
                        f"admission immutability trigger {trigger_name!r} is owned by another table"
                    )
                self._conn.execute(f"DROP TRIGGER {trigger_name}")
            self._conn.execute(trigger_sql[trigger_name])

        probe_id = f"__ledger-admission-trigger-probe-{uuid.uuid4().hex}"
        probe_scope_id = "__ledger-admission-trigger-probe__"
        probe_scope_json = "{}"
        timestamp = format_timestamp(utc_now())
        self._conn.execute("SAVEPOINT memory_admission_trigger_probe")
        try:
            self._conn.execute(
                "INSERT INTO memory_records "
                "(scope_id, scope_json, record_id, kind, state, trust, content_hash, "
                "confidence, valid_from, valid_until, retention_policy, created_at, updated_at, "
                "revision, last_event_sequence, superseded_by, retracted_at, expired_at, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                (
                    probe_scope_id,
                    probe_scope_json,
                    probe_id,
                    MemoryKind.FACT.value,
                    MemoryState.CANDIDATE.value,
                    MemoryTrust.UNTRUSTED.value,
                    _REDACTED_CONTENT_HASH,
                    0.0,
                    "probe",
                    timestamp,
                    timestamp,
                    1,
                    1,
                    LEDGER_SCHEMA_VERSION,
                ),
            )
            self._conn.execute(
                "INSERT INTO memory_events "
                "(scope_id, scope_json, record_id, event_id, event_type, revision, occurred_at, "
                "actor, related_record_id, reason_code, content_hash, payload_hash, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
                (
                    probe_scope_id,
                    probe_scope_json,
                    probe_id,
                    probe_id,
                    MemoryEventType.CONFIRMED.value,
                    2,
                    timestamp,
                    "host",
                    "probe",
                    "probe",
                    LEDGER_SCHEMA_VERSION,
                ),
            )
            self._conn.execute(
                "INSERT INTO memory_record_admission_metadata "
                "(scope_id, scope_json, record_id, origin) VALUES (?, ?, ?, ?)",
                (probe_scope_id, probe_scope_json, probe_id, MemoryOrigin.UNKNOWN.value),
            )
            self._conn.execute(
                "INSERT INTO memory_review_audits "
                "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
                "policy_id, policy_version, policy_fingerprint, action, reason_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    probe_scope_id,
                    probe_scope_json,
                    probe_id,
                    probe_id,
                    1,
                    MemoryOrigin.UNKNOWN.value,
                    "probe",
                    "1",
                    "0" * 64,
                    MemoryAdmissionAction.ALLOW.value,
                    "probe",
                ),
            )
            probes = (
                (
                    "UPDATE memory_record_admission_metadata SET origin = ? "
                    "WHERE record_id = ?",
                    (MemoryOrigin.USER_INPUT.value, probe_id),
                    "memory admission metadata are immutable",
                ),
                (
                    "UPDATE memory_review_audits SET policy_id = ? WHERE event_id = ?",
                    ("mutated", probe_id),
                    "memory review audits are immutable",
                ),
                (
                    "DELETE FROM memory_record_admission_metadata WHERE record_id = ?",
                    (probe_id,),
                    "memory admission metadata are append-only",
                ),
                (
                    "DELETE FROM memory_review_audits WHERE event_id = ?",
                    (probe_id,),
                    "memory review audits are append-only",
                ),
                (
                    "INSERT OR REPLACE INTO memory_record_admission_metadata "
                    "(scope_id, scope_json, record_id, origin) VALUES (?, ?, ?, ?)",
                    (
                        probe_scope_id,
                        probe_scope_json,
                        probe_id,
                        MemoryOrigin.USER_INPUT.value,
                    ),
                    "memory admission metadata are append-only",
                ),
                (
                    "INSERT OR REPLACE INTO memory_review_audits "
                    "(scope_id, scope_json, event_id, record_id, candidate_revision, origin, "
                    "policy_id, policy_version, policy_fingerprint, action, reason_code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        probe_scope_id,
                        probe_scope_json,
                        probe_id,
                        probe_id,
                        1,
                        MemoryOrigin.UNKNOWN.value,
                        "probe",
                        "1",
                        "0" * 64,
                        MemoryAdmissionAction.ALLOW.value,
                        "probe",
                    ),
                    "memory review audits are append-only",
                ),
            )
            for statement, parameters, expected_error in probes:
                try:
                    self._conn.execute(statement, parameters)
                except sqlite3.DatabaseError as exc:
                    if expected_error not in str(exc):
                        raise LedgerStateError(
                            "admission immutability trigger rejected the probe unexpectedly"
                        ) from exc
                else:
                    raise LedgerStateError(
                        "admission immutability trigger is missing or invalid"
                    )
        finally:
            self._conn.execute("ROLLBACK TO memory_admission_trigger_probe")
            self._conn.execute("RELEASE memory_admission_trigger_probe")

    def _drop_owned_admission_immutability_triggers_locked(self) -> None:
        """Remove exact sidecar guards inside the sole hard-erase path."""

        for trigger_name, (target, expected_signature) in (
            _ADMISSION_IMMUTABILITY_TRIGGERS.items()
        ):
            row = self._conn.execute(
                "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = ? COLLATE NOCASE",
                (trigger_name,),
            ).fetchone()
            if (
                row is None
                or str(row["tbl_name"]).casefold() != target
                or row["sql"] is None
                or _normalized_table_sql(str(row["sql"])) != expected_signature
            ):
                raise LedgerStateError(
                    "admission immutability trigger cannot be safely suspended"
                )
            self._conn.execute(f"DROP TRIGGER {trigger_name}")

    @contextmanager
    def _suspend_admission_immutability_locked(self) -> Iterator[None]:
        """Permit sidecar FK cascades only for one hard-erase transaction."""

        self._drop_owned_admission_immutability_triggers_locked()
        try:
            yield
        finally:
            # Reinstall before the outer write transaction can commit.  If a
            # statement above failed, rollback still restores its prior guard
            # definitions; this explicit reinstall also proves the committed
            # successful path remains protected.
            self._ensure_admission_immutability_locked()

    def _require_ready_locked(self) -> None:
        self._ensure_open_locked()
        current = self._schema_version_locked()
        if current == 0:
            raise LedgerNotReadyError(
                "memory ledger is not set up; call ledger.setup() from an explicit setup job"
            )
        if current != self.MIGRATION_VERSION:
            raise LedgerStateError(
                f"ledger schema v{current} is unsupported by v{self.MIGRATION_VERSION} code"
            )
        self._assert_schema_compatible_locked(current)

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("memory ledger is closed")
