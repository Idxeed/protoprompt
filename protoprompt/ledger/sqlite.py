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
from typing import Any, Iterator
import uuid

from protoprompt.ledger.types import (
    LEDGER_SCHEMA_VERSION,
    ErasureReceipt,
    LedgerConflictError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
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
_EVENT_UPDATE_TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {_EVENT_UPDATE_TRIGGER_NAME}
BEFORE UPDATE ON memory_events
BEGIN
    SELECT RAISE(ABORT, 'memory_events are append-only');
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

_ALL_LEDGER_TABLE_COLUMNS = (
    _V1_TABLE_COLUMNS | _V2_TABLE_COLUMNS | _V3_TABLE_COLUMNS | _V4_TABLE_COLUMNS
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
_EVENT_UPDATE_TRIGGER_SIGNATURE = _normalized_table_sql(_EVENT_UPDATE_TRIGGER_SQL)
_LEDGER_RESERVED_OBJECT_NAMES = frozenset(
    set(_ALL_LEDGER_TABLE_COLUMNS)
    | set(_LEDGER_INDEX_SIGNATURES)
    | {_EVENT_UPDATE_TRIGGER_NAME}
)


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

    MIGRATION_VERSION = 4

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
            self._assert_schema_compatible_locked(current)
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
                    current, allow_event_guard_repair=True
                )
                if current > self.MIGRATION_VERSION:
                    raise LedgerStateError(
                        f"ledger schema v{current} is newer than supported v{self.MIGRATION_VERSION}"
                    )
                if current == self.MIGRATION_VERSION:
                    self._ensure_event_immutability_locked()
                    return
                statements = self._migration_statements(current)
                for statement in statements:
                    self._conn.execute(statement)
                if current < 4:
                    self._scrub_legacy_creation_event_fingerprints_locked()
                self._assert_schema_compatible_locked(
                    self.MIGRATION_VERSION, allow_event_guard_repair=True
                )
                self._conn.execute(
                    "INSERT INTO ledger_schema (component, version, applied_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(component) DO UPDATE SET "
                    "version = excluded.version, applied_at = excluded.applied_at",
                    (_COMPONENT, self.MIGRATION_VERSION, format_timestamp(utc_now())),
                )
                self._ensure_event_immutability_locked()

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
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        instant = self._timestamp(now)
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            with self._read_transaction_locked():
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
                records = [
                    record
                    for row in rows
                    if (record := self._load_record_locked(scope, str(row["record_id"])))
                    is not None
                ]
        return [record for record in records if record.is_recallable(now=instant)]

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
                for record in records:
                    events.extend(self.events(scope, record.record_id))
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_schema_version": self.MIGRATION_VERSION,
            "scope": scope_dict(scope),
            "records": [record.to_dict(include_content=include_content) for record in records],
            "events": [event.to_dict() for event in events],
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
    ) -> str:
        """Fingerprint non-sensitive candidate command metadata only.

        The event row must survive a future ``forget()`` without becoming an
        offline verifier for guessed plaintext, source IDs, or evidence IDs.
        Exact candidate-retry equality is checked against the live payload
        while it exists instead of persisting those fields in this fingerprint.
        """
        return command_hash({
            "event_type": event_type.value,
            "record_id": requested_record_id,
            "kind": kind.value,
            "confidence": confidence,
            "retention_policy": retention_policy,
            "valid_from": format_timestamp(valid_from) if valid_from else None,
            "valid_until": format_timestamp(valid_until) if valid_until else None,
            "actor": actor,
            "format": "memory-ledger-v4-metadata-only-candidate-command",
        })

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
        )

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
            "SELECT r.*, p.content, p.source_refs_json, p.evidence_refs_json "
            "FROM memory_records AS r LEFT JOIN memory_payloads AS p ON "
            "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
            "AND p.record_id = r.record_id "
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
        return MemoryRecord(
            record_id=str(row["record_id"]),
            scope=scope,
            kind=MemoryKind(str(row["kind"])),
            state=MemoryState(str(row["state"])),
            trust=MemoryTrust(str(row["trust"])),
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
            ]
        if current == 2:
            return [
                "add v3 erased-command replay barriers",
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
            ]
        if current == 3:
            return ["add v4 source-revocation barriers and scrub legacy event fingerprints"]
        return []

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
        except Exception:
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
        except Exception:
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
        for index_name, expected_signature in _LEDGER_INDEX_SIGNATURES.items():
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
        for object_name, (object_type, target, definition) in objects.items():
            if object_type != "trigger":
                continue
            if object_name == _EVENT_UPDATE_TRIGGER_NAME:
                if target != "memory_events":
                    raise LedgerStateError(
                        "memory event immutability trigger name is owned by another table"
                    )
                if (
                    current >= self.MIGRATION_VERSION
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
            if target not in _ALL_LEDGER_TABLE_COLUMNS:
                continue
            if (
                current < self.MIGRATION_VERSION
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
            current >= self.MIGRATION_VERSION
            and event_guard is None
            and not allow_event_guard_repair
        ):
            raise LedgerStateError("memory event immutability trigger is missing")

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
