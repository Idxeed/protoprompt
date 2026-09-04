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
from typing import Any, Iterable, Iterator, Sequence
import uuid

from protoprompt.ledger._backend import _LedgerCommandBackend
from protoprompt.ledger.storage_conformance import (
    LedgerStorageCapabilities,
    sqlite_v7_storage_capabilities,
)
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
    SCOPE_PAYLOAD_PURGE_SCHEMA_VERSION,
    ScopePayloadPurgeReceipt,
    ScopePayloadReadback,
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
_CHECKPOINT_SCHEMA_VERSION = 6
_SCOPE_PAYLOAD_PURGE_STORAGE_VERSION = 7
_RECALL_CHECKPOINT_MANIFEST_SCHEMA_VERSION = 1
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
# Keep each ``IN`` batch safely below SQLite's conservative host-parameter
# limit after scope parameters are added.  The PostgreSQL adapter inherits the
# same read path, so this is intentionally a portable bounded batch rather
# than a SQLite-specific temporary-table trick.
_ACTIVE_READ_BATCH_SIZE = 400
# Cache only immutable, content-free admission-validation markers for one
# bounded active-read window.  A long-lived process can visit many scopes
# without a write, so this must not become an unbounded per-record registry.
_ACTIVE_ADMISSION_CACHE_MAX_ENTRIES = 10_000
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
    CREATE TABLE IF NOT EXISTS memory_scope_payload_purge_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        operation_key TEXT NOT NULL,
        command_fingerprint TEXT NOT NULL,
        records_forgotten INTEGER NOT NULL CHECK (records_forgotten >= 0),
        payload_rows_deleted INTEGER NOT NULL CHECK (payload_rows_deleted >= 0),
        source_refs_deleted INTEGER NOT NULL CHECK (source_refs_deleted >= 0),
        relations_deleted INTEGER NOT NULL CHECK (relations_deleted >= 0),
        completed_at TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        PRIMARY KEY (scope_id, scope_json, operation_key),
        CHECK (payload_rows_deleted = records_forgotten)
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
    """
    CREATE TABLE IF NOT EXISTS memory_recall_checkpoints (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'invalidated')),
        continuation_ref TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        counter_id TEXT NOT NULL,
        token_budget INTEGER NOT NULL CHECK (token_budget >= 0),
        byte_budget INTEGER NOT NULL CHECK (byte_budget >= 0),
        used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0),
        used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
        selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
        created_at TEXT NOT NULL,
        invalidated_at TEXT,
        invalidation_reason TEXT,
        integrity_tag TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, checkpoint_id),
        CHECK (used_tokens <= token_budget),
        CHECK (used_bytes <= byte_budget),
        CHECK (
            (state = 'active' AND invalidated_at IS NULL AND invalidation_reason IS NULL)
            OR
            (state = 'invalidated' AND invalidated_at IS NOT NULL
             AND invalidation_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_recall_checkpoint_selections (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        record_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        content_hash TEXT NOT NULL,
        kind TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, checkpoint_id, ordinal),
        UNIQUE (scope_id, scope_json, checkpoint_id, record_id),
        FOREIGN KEY (scope_id, scope_json, checkpoint_id)
            REFERENCES memory_recall_checkpoints(scope_id, scope_json, checkpoint_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_recall_checkpoint_selection_record
    ON memory_recall_checkpoint_selections (scope_id, scope_json, record_id)
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

_V6_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_recall_checkpoints (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'invalidated')),
        continuation_ref TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_fingerprint TEXT NOT NULL,
        counter_id TEXT NOT NULL,
        token_budget INTEGER NOT NULL CHECK (token_budget >= 0),
        byte_budget INTEGER NOT NULL CHECK (byte_budget >= 0),
        used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0),
        used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
        selected_count INTEGER NOT NULL CHECK (selected_count >= 0),
        created_at TEXT NOT NULL,
        invalidated_at TEXT,
        invalidation_reason TEXT,
        integrity_tag TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY (scope_id, scope_json, checkpoint_id),
        CHECK (used_tokens <= token_budget),
        CHECK (used_bytes <= byte_budget),
        CHECK (
            (state = 'active' AND invalidated_at IS NULL AND invalidation_reason IS NULL)
            OR
            (state = 'invalidated' AND invalidated_at IS NOT NULL
             AND invalidation_reason IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_recall_checkpoint_selections (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        record_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        content_hash TEXT NOT NULL,
        kind TEXT NOT NULL,
        PRIMARY KEY (scope_id, scope_json, checkpoint_id, ordinal),
        UNIQUE (scope_id, scope_json, checkpoint_id, record_id),
        FOREIGN KEY (scope_id, scope_json, checkpoint_id)
            REFERENCES memory_recall_checkpoints(scope_id, scope_json, checkpoint_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_recall_checkpoint_selection_record
    ON memory_recall_checkpoint_selections (scope_id, scope_json, record_id)
    """,
)

_V7_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_scope_payload_purge_receipts (
        scope_id TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        operation_key TEXT NOT NULL,
        command_fingerprint TEXT NOT NULL,
        records_forgotten INTEGER NOT NULL CHECK (records_forgotten >= 0),
        payload_rows_deleted INTEGER NOT NULL CHECK (payload_rows_deleted >= 0),
        source_refs_deleted INTEGER NOT NULL CHECK (source_refs_deleted >= 0),
        relations_deleted INTEGER NOT NULL CHECK (relations_deleted >= 0),
        completed_at TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        PRIMARY KEY (scope_id, scope_json, operation_key),
        CHECK (payload_rows_deleted = records_forgotten)
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

_V6_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_recall_checkpoints": frozenset({
        "scope_id", "scope_json", "checkpoint_id", "state", "continuation_ref",
        "policy_id", "policy_fingerprint", "counter_id", "token_budget",
        "byte_budget", "used_tokens", "used_bytes", "selected_count", "created_at",
        "invalidated_at", "invalidation_reason", "integrity_tag", "schema_version",
    }),
    "memory_recall_checkpoint_selections": frozenset({
        "scope_id", "scope_json", "checkpoint_id", "ordinal", "record_id",
        "revision", "content_hash", "kind",
    }),
}

_V7_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "memory_scope_payload_purge_receipts": frozenset({
        "scope_id", "scope_json", "operation_key", "command_fingerprint",
        "records_forgotten", "payload_rows_deleted", "source_refs_deleted",
        "relations_deleted", "completed_at", "schema_version",
    }),
}

_ALL_LEDGER_TABLE_COLUMNS = (
    _V1_TABLE_COLUMNS
    | _V2_TABLE_COLUMNS
    | _V3_TABLE_COLUMNS
    | _V4_TABLE_COLUMNS
    | _V5_TABLE_COLUMNS
    | _V6_TABLE_COLUMNS
    | _V7_TABLE_COLUMNS
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
_INDEX_INTRODUCED_VERSION = {
    "idx_memory_review_audits_record": 5,
    "idx_memory_recall_checkpoint_selection_record": 6,
}
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
        if current >= _INDEX_INTRODUCED_VERSION.get(name, 1)
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


class SqliteMemoryLedger(_LedgerCommandBackend):
    """An explicit-setup, SQLite-backed operational memory ledger.

    ``MemoryWriter`` is the preferred host-facing facade because it pins a
    non-empty :class:`~protoprompt.scope.MemoryScope`.  The lower-level store
    still requires that scope for every operation so no query can accidentally
    broaden into a neighbouring tenant or conversation.

    Construction performs no DDL.  Call :meth:`setup` from an explicit
    migration/setup job before serving traffic.
    """

    MIGRATION_VERSION = _SCOPE_PAYLOAD_PURGE_STORAGE_VERSION

    @staticmethod
    def storage_capabilities() -> LedgerStorageCapabilities:
        """Return the fixed v1 SQLite receipt without opening a database.

        This is a sealed description of the built-in storage contract, not a
        capability probe or extension mechanism.  It is available directly on
        the class so callers need no database path, instance, or setup work to
        inspect the public operational boundary.
        """

        return sqlite_v7_storage_capabilities()

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._closed = False
        # Admission sidecars are immutable after review.  The cache stores
        # only successful validation markers (never records or payloads) and
        # is invalidated whenever SQLite reports a local or external change.
        self._active_admission_cache: set[tuple[str, str, str, str]] = set()
        self._active_admission_cache_epoch: tuple[int, int] | None = None

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
                validate_checkpoint_data=True,
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
                    validate_checkpoint_data=True,
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
                    validate_checkpoint_data=True,
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

    def payload_readback(self, scope: MemoryScope) -> ScopePayloadReadback:
        """Return a content-free payload count for one exact host scope.

        This is deliberately a narrow host API rather than an export: the
        result contains only a domain-separated scope digest and the number of
        remaining canonical payload rows.  Corrupt local payload/source
        sidecars fail closed instead of allowing an adapter to mistake an
        incomplete check for a successful deletion boundary.
        """

        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            with self._read_transaction_locked():
                self._require_ready_locked()
                return self._scope_payload_readback_locked(scope, scope_id, scope_json)

    def purge_payloads(
        self,
        scope: MemoryScope,
        operation_id: str,
        *,
        reason_code: str = "scope_payload_purged",
        actor: str = "host",
        occurred_at: datetime | str | None = None,
    ) -> ScopePayloadPurgeReceipt:
        """Atomically remove every current canonical payload in one scope.

        The stored aggregate receipt is keyed by a hash of the host-minted
        operation ID and exact scope.  A successful retry therefore returns
        the original content-free aggregate result after restart and cannot
        accidentally erase records created after that first commit.  This is
        a logical canonical-payload purge: retained lifecycle/audit metadata
        deliberately remains non-plaintext, and the caller must separately
        fence later ingress and external projections before claiming a wider
        deletion guarantee.
        """

        operation = validate_identifier(operation_id, field="operation_id")
        reason = validate_identifier(reason_code, field="reason_code")
        host_actor = _actor(actor)
        current_time = self._timestamp(occurred_at)
        scope_id, scope_json = _scope_storage(scope)
        operation_key = self._scope_payload_purge_operation_key(
            scope_id,
            scope_json,
            operation,
        )
        command_fingerprint = self._scope_payload_purge_command_fingerprint(
            operation_key=operation_key,
            reason_code=reason,
            actor=host_actor,
        )
        scope_fingerprint = self._scope_payload_scope_fingerprint(scope)

        with self._lock:
            with self._ready_write_transaction_locked():
                existing = self._scope_payload_purge_receipt_locked(
                    scope_fingerprint=scope_fingerprint,
                    operation_id=operation,
                    scope_id=scope_id,
                    scope_json=scope_json,
                    operation_key=operation_key,
                    command_fingerprint=command_fingerprint,
                )
                if existing is not None:
                    return existing

                # Prove every affected row can be materialized before the
                # first lifecycle mutation.  This turns orphaned/tampered
                # payload or source sidecars into a transaction rollback,
                # never a misleading partial receipt.
                self._assert_scope_payload_integrity_locked(scope_id, scope_json)
                rows = self._conn.execute(
                    "SELECT p.record_id, r.revision FROM memory_payloads AS p "
                    "JOIN memory_records AS r ON r.scope_id = p.scope_id "
                    "AND r.scope_json = p.scope_json AND r.record_id = p.record_id "
                    "WHERE p.scope_id = ? AND p.scope_json = ? "
                    "ORDER BY p.record_id",
                    (scope_id, scope_json),
                ).fetchall()
                records: list[MemoryRecord] = []
                for row in rows:
                    record_id = str(row["record_id"])
                    record = self._load_record_locked(scope, record_id)
                    if record is None or record.revision != int(row["revision"]):
                        raise LedgerStateError(
                            "scope payload record changed during purge preflight"
                        )
                    if not record.content_available:
                        raise LedgerStateError(
                            "payload row could not be materialized during scope purge"
                        )
                    records.append(record)

                receipts: list[ErasureReceipt] = []
                for record in records:
                    receipts.append(
                        self._forget_locked(
                            scope,
                            record_id=record.record_id,
                            expected_revision=record.revision,
                            reason_code=reason,
                            actor=host_actor,
                            event_id=self._scope_payload_purge_event_id(
                                operation_key,
                                record.record_id,
                            ),
                            occurred_at=current_time,
                            payload_hash=self._scope_payload_purge_record_fingerprint(
                                operation_key=operation_key,
                                record_id=record.record_id,
                                expected_revision=record.revision,
                                reason_code=reason,
                                actor=host_actor,
                            ),
                        )
                    )

                payload_rows_deleted = sum(
                    1 for receipt in receipts if receipt.payload_deleted
                )
                if payload_rows_deleted != len(records):
                    raise LedgerStateError(
                        "scope payload purge did not delete every selected payload row"
                    )
                readback = self._scope_payload_readback_locked(scope, scope_id, scope_json)
                if not readback.is_empty:
                    raise LedgerStateError(
                        "scope payload purge final readback is not empty"
                    )
                receipt = ScopePayloadPurgeReceipt(
                    operation_id=operation,
                    scope_fingerprint=scope_fingerprint,
                    records_forgotten=len(receipts),
                    payload_rows_deleted=payload_rows_deleted,
                    source_refs_deleted=sum(
                        item.source_refs_deleted for item in receipts
                    ),
                    relations_deleted=sum(item.relations_deleted for item in receipts),
                    readback=readback,
                )
                self._store_scope_payload_purge_receipt_locked(
                    scope_id=scope_id,
                    scope_json=scope_json,
                    operation_key=operation_key,
                    command_fingerprint=command_fingerprint,
                    receipt=receipt,
                    completed_at=current_time,
                )
                return receipt

    @staticmethod
    def _scope_payload_scope_fingerprint(scope: MemoryScope) -> str:
        """Return a receipt-safe digest for the exact host scope."""

        return command_hash({
            "operation": "scope_payload_purge_scope_fingerprint_v1",
            "scope": scope_dict(scope),
        })

    @staticmethod
    def _scope_payload_purge_operation_key(
        scope_id: str,
        scope_json: str,
        operation_id: str,
    ) -> str:
        """Hash an opaque host operation ID before durable storage."""

        return command_hash({
            "operation": "scope_payload_purge_operation_key_v1",
            "scope_id": scope_id,
            "scope_json": scope_json,
            "operation_id": operation_id,
        })

    @staticmethod
    def _scope_payload_purge_command_fingerprint(
        *,
        operation_key: str,
        reason_code: str,
        actor: str,
    ) -> str:
        """Bind a durable retry key to its immutable host command metadata."""

        return command_hash({
            "operation": "scope_payload_purge_v1",
            "operation_key": operation_key,
            "reason_code": reason_code,
            "actor": actor,
            "schema_version": SCOPE_PAYLOAD_PURGE_SCHEMA_VERSION,
        })

    @staticmethod
    def _scope_payload_purge_event_id(operation_key: str, record_id: str) -> str:
        """Derive one opaque per-record lifecycle event ID for a purge."""

        return "scope-purge-" + command_hash({
            "operation": "scope_payload_purge_event_v1",
            "operation_key": operation_key,
            "record_id": record_id,
        })

    @staticmethod
    def _scope_payload_purge_record_fingerprint(
        *,
        operation_key: str,
        record_id: str,
        expected_revision: int,
        reason_code: str,
        actor: str,
    ) -> str:
        """Fingerprint a per-record purge transition without payload data."""

        return command_hash({
            "operation": "scope_payload_purge_record_v1",
            "operation_key": operation_key,
            "record_id": record_id,
            "expected_revision": expected_revision,
            "reason_code": reason_code,
            "actor": actor,
        })

    def _scope_payload_readback_locked(
        self,
        scope: MemoryScope,
        scope_id: str,
        scope_json: str,
    ) -> ScopePayloadReadback:
        """Count canonical payload rows after validating local sidecars."""

        self._assert_scope_payload_integrity_locked(scope_id, scope_json)
        row = self._conn.execute(
            "SELECT COUNT(*) AS payload_record_count FROM memory_payloads "
            "WHERE scope_id = ? AND scope_json = ?",
            (scope_id, scope_json),
        ).fetchone()
        return ScopePayloadReadback(
            scope_fingerprint=self._scope_payload_scope_fingerprint(scope),
            payload_record_count=int(row["payload_record_count"]),
        )

    def _assert_scope_payload_integrity_locked(
        self,
        scope_id: str,
        scope_json: str,
    ) -> None:
        """Reject malformed target payload/source sidecars before a receipt."""

        orphan_payload = self._conn.execute(
            "SELECT 1 FROM memory_payloads AS p LEFT JOIN memory_records AS r "
            "ON r.scope_id = p.scope_id AND r.scope_json = p.scope_json "
            "AND r.record_id = p.record_id WHERE p.scope_id = ? "
            "AND p.scope_json = ? AND r.record_id IS NULL LIMIT 1",
            (scope_id, scope_json),
        ).fetchone()
        if orphan_payload is not None:
            raise LedgerStateError("scope payload row is orphaned")
        orphan_source = self._conn.execute(
            "SELECT 1 FROM memory_sources AS s LEFT JOIN memory_payloads AS p "
            "ON p.scope_id = s.scope_id AND p.scope_json = s.scope_json "
            "AND p.record_id = s.record_id WHERE s.scope_id = ? "
            "AND s.scope_json = ? AND p.record_id IS NULL LIMIT 1",
            (scope_id, scope_json),
        ).fetchone()
        if orphan_source is not None:
            raise LedgerStateError("scope payload source row is orphaned")
        missing_origin = self._conn.execute(
            "SELECT 1 FROM memory_payloads AS p LEFT JOIN "
            "memory_record_admission_metadata AS m ON m.scope_id = p.scope_id "
            "AND m.scope_json = p.scope_json AND m.record_id = p.record_id "
            "WHERE p.scope_id = ? AND p.scope_json = ? AND m.record_id IS NULL LIMIT 1",
            (scope_id, scope_json),
        ).fetchone()
        if missing_origin is not None:
            raise LedgerStateError(
                "payload-bearing memory record is missing admission origin metadata"
            )

    def _scope_payload_purge_receipt_locked(
        self,
        *,
        scope_fingerprint: str,
        operation_id: str,
        scope_id: str,
        scope_json: str,
        operation_key: str,
        command_fingerprint: str,
    ) -> ScopePayloadPurgeReceipt | None:
        """Load one immutable aggregate retry receipt or reject command drift."""

        row = self._conn.execute(
            "SELECT command_fingerprint, records_forgotten, payload_rows_deleted, "
            "source_refs_deleted, relations_deleted, completed_at, schema_version "
            "FROM memory_scope_payload_purge_receipts WHERE scope_id = ? "
            "AND scope_json = ? AND operation_key = ?",
            (scope_id, scope_json, operation_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["command_fingerprint"]) != command_fingerprint:
            raise LedgerConflictError(
                "operation_id was already used for a different scope payload purge command"
            )
        try:
            parse_timestamp(str(row["completed_at"]), field="scope purge completed_at")
            if int(row["schema_version"]) != SCOPE_PAYLOAD_PURGE_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            readback = ScopePayloadReadback(
                scope_fingerprint=scope_fingerprint,
                payload_record_count=0,
            )
            return ScopePayloadPurgeReceipt(
                operation_id=operation_id,
                scope_fingerprint=scope_fingerprint,
                records_forgotten=int(row["records_forgotten"]),
                payload_rows_deleted=int(row["payload_rows_deleted"]),
                source_refs_deleted=int(row["source_refs_deleted"]),
                relations_deleted=int(row["relations_deleted"]),
                readback=readback,
            )
        except (TypeError, ValueError) as exc:
            raise LedgerStateError("scope payload purge receipt is malformed") from exc

    def _store_scope_payload_purge_receipt_locked(
        self,
        *,
        scope_id: str,
        scope_json: str,
        operation_key: str,
        command_fingerprint: str,
        receipt: ScopePayloadPurgeReceipt,
        completed_at: datetime,
    ) -> None:
        """Persist the final aggregate receipt as the last purge mutation."""

        self._conn.execute(
            "INSERT INTO memory_scope_payload_purge_receipts "
            "(scope_id, scope_json, operation_key, command_fingerprint, "
            "records_forgotten, payload_rows_deleted, source_refs_deleted, "
            "relations_deleted, completed_at, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                operation_key,
                command_fingerprint,
                receipt.records_forgotten,
                receipt.payload_rows_deleted,
                receipt.source_refs_deleted,
                receipt.relations_deleted,
                format_timestamp(completed_at),
                receipt.schema_version,
            ),
        )

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
                self._invalidate_recall_checkpoints_for_record_locked(
                    scope_id,
                    scope_json,
                    identity,
                    reason_code="selected_record_erased",
                    occurred_at=utc_now(),
                )
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

    def _load_active_markers(
        self,
        scope: MemoryScope,
        *,
        now: datetime | str | None,
        limit: int,
        record_ids: Iterable[str],
    ) -> list[MemoryRecord]:
        """Re-read named active records for a private recall resolve boundary.

        Unlike :meth:`list_active`, this private helper intentionally does not
        enumerate unrelated active memory.  Callers must already hold sealed
        record markers and it applies the same exact-scope lifecycle and
        admission-audit checks to every returned record.
        """

        if isinstance(record_ids, (str, bytes)):
            raise TypeError("record_ids must be an iterable of record identifiers")
        self._validate_active_limit(limit)
        identities = tuple(
            validate_identifier(record_id, field="record_id")
            for record_id in record_ids
        )
        instant = self._timestamp(now)
        _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            # An empty sealed selection cannot expose stale memory, so it has
            # no active-window membership to prove.  Preserve the ready/scope
            # boundary above while avoiding an otherwise unrelated top-N ID
            # scan for a legitimate empty recall context.
            if not identities:
                return []
            with self._read_transaction_locked():
                # A plan is tied not just to selected records but to the
                # bounded active snapshot from which it was chosen.  Check
                # current top-N membership without deserializing unrelated
                # payloads, so a newer active record cannot silently push a
                # sealed selection outside the policy window.
                visible_ids = self._active_window_record_ids_locked(
                    scope,
                    instant=instant,
                    limit=limit,
                )
                if not set(identities).issubset(visible_ids):
                    return []
                return self._active_records_for_ids_locked(
                    scope,
                    instant=instant,
                    record_ids=identities,
                )

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
                visible_ids = self._active_window_record_ids_locked(
                    scope,
                    instant=instant,
                    limit=limit,
                )
                if not {
                    selection[0] for selection in normalized_selections
                }.issubset(visible_ids):
                    return False
                records = self._active_records_for_ids_locked(
                    scope,
                    instant=instant,
                    record_ids=tuple(selection[0] for selection in normalized_selections),
                )
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

    def _create_recall_checkpoint(
        self,
        scope: MemoryScope,
        *,
        checkpoint_id: str,
        continuation_ref: str,
        policy_id: str,
        policy_fingerprint: str,
        counter_id: str,
        token_budget: int,
        byte_budget: int,
        used_tokens: int,
        used_bytes: int,
        selections: Iterable[tuple[str, int, str, MemoryKind]],
        active_read_limit: int,
        created_at: datetime,
        integrity_tag: str,
    ) -> dict[str, object]:
        """Persist a sealed strict-recall manifest after final lifecycle validation.

        This private capability accepts only opaque host identifiers and
        selection markers.  It deliberately receives no task text, plaintext
        memory, provider messages, or generic host state.  The caller creates
        its HMAC seal before entering this transaction; SQLite never receives
        the corresponding host key.
        """

        identity = validate_identifier(checkpoint_id, field="checkpoint_id")
        continuation = validate_identifier(continuation_ref, field="continuation_ref")
        policy_identity = validate_identifier(policy_id, field="policy_id")
        counter_identity = validate_identifier(counter_id, field="counter_id")
        normalized_selections = self._normalize_active_snapshot_markers(selections)
        if len({marker[0] for marker in normalized_selections}) != len(normalized_selections):
            raise ValueError("checkpoint selections must not contain duplicate record IDs")
        self._validate_active_limit(active_read_limit)
        for field_name, value in (
            ("token_budget", token_budget),
            ("byte_budget", byte_budget),
            ("used_tokens", used_tokens),
            ("used_bytes", used_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if used_tokens > token_budget or used_bytes > byte_budget:
            raise ValueError("checkpoint receipt exceeds its configured budget")
        for field_name, digest in (
            ("policy_fingerprint", policy_fingerprint),
            ("integrity_tag", integrity_tag),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a lowercase 64-character digest")
        instant = coerce_datetime(created_at, field="created_at")
        assert instant is not None
        scope_id, scope_json = _scope_storage(scope)
        with self._lock:
            with self._ready_write_transaction_locked():
                existing = self._conn.execute(
                    "SELECT 1 FROM memory_recall_checkpoints "
                    "WHERE scope_id = ? AND scope_json = ? AND checkpoint_id = ?",
                    (scope_id, scope_json, identity),
                ).fetchone()
                if existing is not None:
                    raise LedgerConflictError("checkpoint_id was already used in this scope")
                current_by_id = {
                    record.record_id: record
                    for record in self._list_active_locked(
                        scope,
                        instant=instant,
                        limit=active_read_limit,
                    )
                }
                if not all(
                    (
                        (record := current_by_id.get(record_id)) is not None
                        and record.revision == revision
                        and record.content_hash == expected_hash
                        and record.kind is kind
                    )
                    for record_id, revision, expected_hash, kind in normalized_selections
                ):
                    raise LedgerStateError(
                        "selected ledger memory changed before checkpoint creation; replan"
                    )
                self._conn.execute(
                    "INSERT INTO memory_recall_checkpoints "
                    "(scope_id, scope_json, checkpoint_id, state, continuation_ref, policy_id, "
                    "policy_fingerprint, counter_id, token_budget, byte_budget, used_tokens, "
                    "used_bytes, selected_count, created_at, invalidated_at, invalidation_reason, "
                    "integrity_tag, schema_version) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (
                        scope_id,
                        scope_json,
                        identity,
                        continuation,
                        policy_identity,
                        policy_fingerprint,
                        counter_identity,
                        token_budget,
                        byte_budget,
                        used_tokens,
                        used_bytes,
                        len(normalized_selections),
                        format_timestamp(instant),
                        integrity_tag,
                        _RECALL_CHECKPOINT_MANIFEST_SCHEMA_VERSION,
                    ),
                )
                for ordinal, (record_id, revision, expected_hash, kind) in enumerate(
                    normalized_selections
                ):
                    self._conn.execute(
                        "INSERT INTO memory_recall_checkpoint_selections "
                        "(scope_id, scope_json, checkpoint_id, ordinal, record_id, revision, "
                        "content_hash, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            scope_id,
                            scope_json,
                            identity,
                            ordinal,
                            record_id,
                            revision,
                            expected_hash,
                            kind.value,
                        ),
                    )
                return self._recall_checkpoint_manifest_locked(scope, identity)

    def _load_recall_checkpoint(
        self,
        scope: MemoryScope,
        checkpoint_id: str,
    ) -> dict[str, object]:
        """Load one checkpoint manifest without resolving any memory payload."""

        identity = validate_identifier(checkpoint_id, field="checkpoint_id")
        _scope_storage(scope)
        with self._lock:
            self._require_ready_locked()
            with self._read_transaction_locked():
                return self._recall_checkpoint_manifest_locked(scope, identity)

    def _invalidate_recall_checkpoint(
        self,
        scope: MemoryScope,
        checkpoint_id: str,
        *,
        reason_code: str,
        occurred_at: datetime,
    ) -> bool:
        """Invalidate one manifest and remove its derived selection metadata."""

        identity = validate_identifier(checkpoint_id, field="checkpoint_id")
        reason = validate_identifier(reason_code, field="reason_code")
        instant = coerce_datetime(occurred_at, field="occurred_at")
        assert instant is not None
        with self._lock:
            with self._ready_write_transaction_locked():
                return self._invalidate_recall_checkpoints_locked(
                    scope,
                    checkpoint_ids=(identity,),
                    reason_code=reason,
                    occurred_at=instant,
                ) > 0

    def _invalidate_recall_checkpoints_for_record_locked(
        self,
        scope_id: str,
        scope_json: str,
        record_id: str,
        *,
        reason_code: str,
        occurred_at: datetime,
    ) -> int:
        """Fail-close every active manifest that selected one changed record."""

        rows = self._conn.execute(
            "SELECT DISTINCT s.checkpoint_id "
            "FROM memory_recall_checkpoint_selections AS s "
            "JOIN memory_recall_checkpoints AS c ON c.scope_id = s.scope_id "
            "AND c.scope_json = s.scope_json AND c.checkpoint_id = s.checkpoint_id "
            "WHERE s.scope_id = ? AND s.scope_json = ? AND s.record_id = ? "
            "AND c.state = 'active' ORDER BY s.checkpoint_id",
            (scope_id, scope_json, record_id),
        ).fetchall()
        return self._invalidate_recall_checkpoints_locked(
            None,
            checkpoint_ids=tuple(str(row["checkpoint_id"]) for row in rows),
            reason_code=reason_code,
            occurred_at=occurred_at,
            scope_id=scope_id,
            scope_json=scope_json,
        )

    def _invalidate_recall_checkpoints_locked(
        self,
        scope: MemoryScope | None,
        *,
        checkpoint_ids: Iterable[str],
        reason_code: str,
        occurred_at: datetime,
        scope_id: str | None = None,
        scope_json: str | None = None,
    ) -> int:
        """Apply one-way checkpoint invalidation inside an open write transaction."""

        if scope is not None:
            scope_id, scope_json = _scope_storage(scope)
        assert scope_id is not None and scope_json is not None
        identities = tuple(
            validate_identifier(checkpoint_id, field="checkpoint_id")
            for checkpoint_id in checkpoint_ids
        )
        if not identities:
            return 0
        changed = 0
        for identity in identities:
            cursor = self._conn.execute(
                "UPDATE memory_recall_checkpoints SET state = 'invalidated', "
                "invalidated_at = ?, invalidation_reason = ? "
                "WHERE scope_id = ? AND scope_json = ? AND checkpoint_id = ? "
                "AND state = 'active'",
                (
                    format_timestamp(occurred_at),
                    reason_code,
                    scope_id,
                    scope_json,
                    identity,
                ),
            )
            if cursor.rowcount == 1:
                self._conn.execute(
                    "DELETE FROM memory_recall_checkpoint_selections "
                    "WHERE scope_id = ? AND scope_json = ? AND checkpoint_id = ?",
                    (scope_id, scope_json, identity),
                )
                changed += 1
        return changed

    def _recall_checkpoint_manifest_locked(
        self,
        scope: MemoryScope,
        checkpoint_id: str,
    ) -> dict[str, object]:
        """Decode a sealed manifest while a caller owns a stable DB snapshot."""

        identity = validate_identifier(checkpoint_id, field="checkpoint_id")
        scope_id, scope_json = _scope_storage(scope)
        row = self._conn.execute(
            "SELECT checkpoint_id, state, continuation_ref, policy_id, policy_fingerprint, "
            "counter_id, token_budget, byte_budget, used_tokens, used_bytes, selected_count, "
            "created_at, invalidated_at, invalidation_reason, integrity_tag, schema_version "
            "FROM memory_recall_checkpoints WHERE scope_id = ? AND scope_json = ? "
            "AND checkpoint_id = ?",
            (scope_id, scope_json, identity),
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        selection_rows = self._conn.execute(
            "SELECT ordinal, record_id, revision, content_hash, kind "
            "FROM memory_recall_checkpoint_selections "
            "WHERE scope_id = ? AND scope_json = ? AND checkpoint_id = ? ORDER BY ordinal",
            (scope_id, scope_json, identity),
        ).fetchall()
        try:
            state = str(row["state"])
            if state not in {"active", "invalidated"}:
                raise ValueError("checkpoint state is invalid")
            if int(row["schema_version"]) != _RECALL_CHECKPOINT_MANIFEST_SCHEMA_VERSION:
                raise ValueError("checkpoint schema version is invalid")
            for field_name in ("checkpoint_id", "continuation_ref", "policy_id", "counter_id"):
                validate_identifier(str(row[field_name]), field=field_name)
            for field_name in ("policy_fingerprint", "integrity_tag"):
                digest = str(row[field_name])
                if len(digest) != 64 or any(
                    character not in "0123456789abcdef" for character in digest
                ):
                    raise ValueError(f"{field_name} is invalid")
            for field_name in (
                "token_budget",
                "byte_budget",
                "used_tokens",
                "used_bytes",
                "selected_count",
            ):
                value = int(row[field_name])
                if value < 0:
                    raise ValueError(f"{field_name} is invalid")
            if int(row["used_tokens"]) > int(row["token_budget"]) or int(
                row["used_bytes"]
            ) > int(row["byte_budget"]):
                raise ValueError("checkpoint budget receipt is invalid")
            created_at = parse_timestamp(str(row["created_at"]), field="created_at")
            if state == "active":
                if row["invalidated_at"] is not None or row["invalidation_reason"] is not None:
                    raise ValueError("active checkpoint has invalid invalidation metadata")
                if len(selection_rows) != int(row["selected_count"]):
                    raise ValueError("active checkpoint selection count is invalid")
            else:
                if row["invalidated_at"] is None or row["invalidation_reason"] is None:
                    raise ValueError("invalidated checkpoint is missing invalidation metadata")
                parse_timestamp(str(row["invalidated_at"]), field="invalidated_at")
                validate_identifier(str(row["invalidation_reason"]), field="invalidation_reason")
                if selection_rows:
                    raise ValueError("invalidated checkpoint retains selection metadata")
            selections: list[dict[str, object]] = []
            for ordinal, selection in enumerate(selection_rows):
                if int(selection["ordinal"]) != ordinal:
                    raise ValueError("checkpoint selection ordering is invalid")
                record_id = validate_identifier(str(selection["record_id"]), field="record_id")
                revision = int(selection["revision"])
                if revision < 1:
                    raise ValueError("checkpoint selection revision is invalid")
                content_digest = str(selection["content_hash"])
                if len(content_digest) != 64 or any(
                    character not in "0123456789abcdef" for character in content_digest
                ):
                    raise ValueError("checkpoint selection content hash is invalid")
                kind = MemoryKind(str(selection["kind"]))
                selections.append({
                    "record_id": record_id,
                    "revision": revision,
                    "content_hash": content_digest,
                    "kind": kind.value,
                })
        except (TypeError, ValueError) as exc:
            raise LedgerStateError("sealed recall checkpoint manifest is invalid") from exc
        return {
            "checkpoint_id": str(row["checkpoint_id"]),
            "state": state,
            "continuation_ref": str(row["continuation_ref"]),
            "policy_id": str(row["policy_id"]),
            "policy_fingerprint": str(row["policy_fingerprint"]),
            "counter_id": str(row["counter_id"]),
            "token_budget": int(row["token_budget"]),
            "byte_budget": int(row["byte_budget"]),
            "used_tokens": int(row["used_tokens"]),
            "used_bytes": int(row["used_bytes"]),
            "selected_count": int(row["selected_count"]),
            "created_at": created_at,
            "integrity_tag": str(row["integrity_tag"]),
            "selections": tuple(selections),
        }

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
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer from 1 to 10000")

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
            "SELECT r.*, p.content, p.source_refs_json, p.evidence_refs_json, m.origin "
            "FROM memory_records AS r "
            "JOIN memory_payloads AS p ON "
            "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
            "AND p.record_id = r.record_id "
            "LEFT JOIN memory_record_admission_metadata AS m ON "
            "m.scope_id = r.scope_id AND m.scope_json = r.scope_json "
            "AND m.record_id = r.record_id "
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
        return self._active_records_from_rows_locked(scope, rows, instant=instant)

    def _active_window_record_ids_locked(
        self,
        scope: MemoryScope,
        *,
        instant: datetime,
        limit: int,
    ) -> set[str]:
        """Return the current bounded active-window IDs without payload decode.

        This preserves the selection-window boundary used by the prior full
        snapshot validation while keeping resolve's reread limited to its
        sealed markers.  It deliberately shares the exact active/payload
        predicate and order used by :meth:`_list_active_locked`.
        """

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
        return {str(row["record_id"]) for row in rows}

    def _active_records_for_ids_locked(
        self,
        scope: MemoryScope,
        *,
        instant: datetime,
        record_ids: tuple[str, ...],
    ) -> list[MemoryRecord]:
        """Load only named active records for final selection validation.

        A resolve operation needs to re-read and validate its sealed selected
        markers, not deserialize every unrelated record in the planning window.
        Each named row remains subject to the same lifecycle, payload, origin,
        relation, and immutable-admission-audit checks as ``list_active``.
        """

        if not record_ids:
            return []
        scope_id, scope_json = _scope_storage(scope)
        rows: list[sqlite3.Row] = []
        for batch in self._record_id_batches(record_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows.extend(
                self._conn.execute(
                    "SELECT r.*, p.content, p.source_refs_json, p.evidence_refs_json, m.origin "
                    "FROM memory_records AS r "
                    "JOIN memory_payloads AS p ON "
                    "p.scope_id = r.scope_id AND p.scope_json = r.scope_json "
                    "AND p.record_id = r.record_id "
                    "LEFT JOIN memory_record_admission_metadata AS m ON "
                    "m.scope_id = r.scope_id AND m.scope_json = r.scope_json "
                    "AND m.record_id = r.record_id "
                    "WHERE r.scope_id = ? AND r.scope_json = ? AND r.state = ? "
                    "AND r.trust = ? "
                    "AND (r.valid_from IS NULL OR r.valid_from <= ?) "
                    "AND (r.valid_until IS NULL OR r.valid_until > ?) "
                    "AND r.record_id IN ("
                    + placeholders
                    + ") ORDER BY r.updated_at DESC, r.record_id ASC",
                    (
                        scope_id,
                        scope_json,
                        MemoryState.ACTIVE.value,
                        MemoryTrust.HOST_CONFIRMED.value,
                        format_timestamp(instant),
                        format_timestamp(instant),
                        *batch,
                    ),
                ).fetchall()
            )
        return self._active_records_from_rows_locked(scope, rows, instant=instant)

    def _active_records_from_rows_locked(
        self,
        scope: MemoryScope,
        rows: Sequence[sqlite3.Row],
        *,
        instant: datetime,
    ) -> list[MemoryRecord]:
        """Decode an already-filtered active-row snapshot with strict sidecars."""

        cache_enabled = self._refresh_active_admission_cache_locked()
        scope_id, scope_json = _scope_storage(scope)
        relation_rows = self._relation_rows_for_records_locked(
            scope,
            tuple(str(row["record_id"]) for row in rows),
        )
        # Fetch all audit sidecars in bounded batches before constructing the
        # public snapshot.  The subsequent per-record check still parses every
        # audit and validates it against its lifecycle event, exactly as the
        # former one-query-per-record path did.
        concrete_markers = tuple(
            (str(row["record_id"]), str(row["origin"]))
            for row in rows
            if row["origin"] not in {
                MemoryOrigin.UNKNOWN.value,
                MemoryOrigin.LEGACY_UNKNOWN.value,
            }
        )
        concrete_record_ids = tuple(record_id for record_id, _ in concrete_markers)
        cache_miss_ids = tuple(
            record_id
            for record_id, origin in concrete_markers
            if not cache_enabled
            or (
                scope_id,
                scope_json,
                record_id,
                origin,
            )
            not in self._active_admission_cache
        )
        # Resolve cache capacity *before* selecting the raw audit rows.  A
        # mid-loop eviction could otherwise turn a key that was a hit during
        # this snapshot into a miss after its sidecar rows were omitted.
        if (
            cache_enabled
            and len(self._active_admission_cache) + len(cache_miss_ids)
            > _ACTIVE_ADMISSION_CACHE_MAX_ENTRIES
        ):
            self._active_admission_cache.clear()
            cache_miss_ids = concrete_record_ids
            if len(concrete_record_ids) > _ACTIVE_ADMISSION_CACHE_MAX_ENTRIES:
                # One snapshot larger than the bounded cache is still fully
                # validated, but is intentionally not retained at all.
                cache_enabled = False
        audit_rows = self._admission_audit_rows_for_records_locked(
            scope,
            cache_miss_ids,
        )
        records: list[MemoryRecord] = []
        for row in rows:
            record_id = str(row["record_id"])
            record = self._memory_record_from_row_locked(
                scope,
                row,
                relation_rows.get(record_id, ()),
            )
            if not record.is_recallable(now=instant):
                continue
            if record.origin not in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
                cache_key = (scope_id, scope_json, record.record_id, record.origin.value)
                if not cache_enabled or cache_key not in self._active_admission_cache:
                    self._assert_active_admission_verified_rows_locked(
                        scope,
                        record,
                        audit_rows.get(record.record_id, ()),
                    )
                    if cache_enabled:
                        self._active_admission_cache.add(cache_key)
            records.append(record)
        return records

    def _refresh_active_admission_cache_locked(self) -> bool:
        """Invalidate SQLite-only immutable-audit markers on any DB change.

        ``PRAGMA data_version`` observes committed writes from other SQLite
        connections. ``total_changes`` additionally catches controlled or
        test-only direct writes on this connection. PostgreSQL intentionally
        keeps the uncached strict-audit path inherited from this engine: its
        adapter does not expose either SQLite invalidation signal.
        """

        if not isinstance(self._conn, sqlite3.Connection):
            return False
        row = self._conn.execute("PRAGMA data_version").fetchone()
        epoch = (int(row[0]), self._conn.total_changes)
        if self._active_admission_cache_epoch != epoch:
            self._active_admission_cache.clear()
            self._active_admission_cache_epoch = epoch
        return True

    @staticmethod
    def _record_id_batches(record_ids: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
        """Yield bounded opaque record-ID batches for portable SQL ``IN`` reads."""

        for start in range(0, len(record_ids), _ACTIVE_READ_BATCH_SIZE):
            yield record_ids[start : start + _ACTIVE_READ_BATCH_SIZE]

    def _relation_rows_for_records_locked(
        self,
        scope: MemoryScope,
        record_ids: tuple[str, ...],
    ) -> dict[str, list[sqlite3.Row]]:
        """Load relations for an active snapshot without a per-record query."""

        if not record_ids:
            return {}
        scope_id, scope_json = _scope_storage(scope)
        result: dict[str, list[sqlite3.Row]] = {}
        for batch in self._record_id_batches(record_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT from_record_id, relation, to_record_id FROM memory_relations "
                "WHERE scope_id = ? AND scope_json = ? AND from_record_id IN ("
                + placeholders
                + ") ORDER BY from_record_id, relation, to_record_id",
                (scope_id, scope_json, *batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["from_record_id"]), []).append(row)
        return result

    def _admission_audit_rows_for_records_locked(
        self,
        scope: MemoryScope,
        record_ids: tuple[str, ...],
    ) -> dict[str, list[sqlite3.Row]]:
        """Load audit/event sidecars for a snapshot in bounded batches.

        Rows remain raw here.  They are decoded at the original per-record
        admission boundary below so corrupt sidecars still fail closed rather
        than being reduced to an SQL ``EXISTS`` shortcut.
        """

        if not record_ids:
            return {}
        scope_id, scope_json = _scope_storage(scope)
        result: dict[str, list[sqlite3.Row]] = {}
        for batch in self._record_id_batches(record_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
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
                "AND a.scope_json = ? AND a.record_id IN ("
                + placeholders
                + ") ORDER BY a.record_id, e.sequence",
                (scope_id, scope_json, *batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["record_id"]), []).append(row)
        return result

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
        self._assert_active_admission_verified_rows_locked(
            scope,
            record,
            self._admission_audit_rows_for_record_locked(scope, record.record_id),
        )

    def _assert_active_admission_verified_rows_locked(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        rows: Sequence[sqlite3.Row],
    ) -> None:
        """Decode an active record's complete admission audit set and fail closed."""

        if record.origin in {MemoryOrigin.UNKNOWN, MemoryOrigin.LEGACY_UNKNOWN}:
            return
        audits = [self._admission_audit_from_row(scope, row) for row in rows]
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
        self._invalidate_recall_checkpoints_for_record_locked(
            scope_id,
            scope_json,
            record.record_id,
            reason_code="selected_record_changed",
            occurred_at=occurred_at,
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
        return self._memory_record_from_row_locked(scope, row, relation_rows)

    def _memory_record_from_row_locked(
        self,
        scope: MemoryScope,
        row: sqlite3.Row,
        relation_rows: Sequence[sqlite3.Row],
    ) -> MemoryRecord:
        """Decode one joined record row shared by point and batched active reads."""

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
        if current <= 5:
            statements += _V6_MIGRATION_STATEMENTS
        if current <= 6:
            statements += _V7_MIGRATION_STATEMENTS
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
                "add v6 sealed recall checkpoint manifests",
                "add v7 durable exact-scope payload-purge receipts",
            ]
        if current == 2:
            return [
                "add v3 erased-command replay barriers",
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
                "add v5 admission provenance and review audit tables",
                "add v6 sealed recall checkpoint manifests",
                "add v7 durable exact-scope payload-purge receipts",
            ]
        if current == 3:
            return [
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
                "add v5 admission provenance and review audit tables",
                "add v6 sealed recall checkpoint manifests",
                "add v7 durable exact-scope payload-purge receipts",
            ]
        if current == 4:
            return [
                "add v5 admission provenance and review audit tables",
                "add v6 sealed recall checkpoint manifests",
                "add v7 durable exact-scope payload-purge receipts",
            ]
        if current == 5:
            return [
                "add v6 sealed recall checkpoint manifests",
                "add v7 durable exact-scope payload-purge receipts",
            ]
        if current == 6:
            return ["add v7 durable exact-scope payload-purge receipts"]
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
        validate_checkpoint_data: bool = False,
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
                    | set(_V6_TABLE_COLUMNS)
                    | set(_V7_TABLE_COLUMNS)
                    | {
                        name
                        for name, introduced in _INDEX_INTRODUCED_VERSION.items()
                        if introduced >= 5
                    }
                    | set(_ADMISSION_IMMUTABILITY_TRIGGERS)
                )
            )
            if unexpected_future:
                raise LedgerStateError(
                    f"ledger schema v{current} unexpectedly contains future Ledger objects: "
                    + ", ".join(unexpected_future)
                )
        elif current < 6:
            unexpected_v6 = sorted(
                set(objects).intersection(
                    set(_V6_TABLE_COLUMNS)
                    | set(_V7_TABLE_COLUMNS)
                    | {
                        name
                        for name, introduced in _INDEX_INTRODUCED_VERSION.items()
                        if introduced == 6
                    }
                )
            )
            if unexpected_v6:
                raise LedgerStateError(
                    "ledger schema v5 unexpectedly contains future Ledger objects: "
                    + ", ".join(unexpected_v6)
                )
        elif current < 7:
            unexpected_v7 = sorted(set(objects).intersection(set(_V7_TABLE_COLUMNS)))
            if unexpected_v7:
                raise LedgerStateError(
                    "ledger schema v6 unexpectedly contains v7 scope-purge objects: "
                    + ", ".join(unexpected_v7)
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
        if current >= 6:
            required.update(_V6_TABLE_COLUMNS)
        if current >= 7:
            required.update(_V7_TABLE_COLUMNS)
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
        if current >= 6 and validate_checkpoint_data:
            self._validate_recall_checkpoint_sidecars_locked()

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

    def _validate_recall_checkpoint_sidecars_locked(self) -> None:
        """Fail closed on malformed or orphaned v6 checkpoint sidecars.

        The checkpoint HMAC is intentionally verified by the host-owned
        planner, which alone possesses its secret.  Storage validation still
        proves that rows form a well-shaped, scope-bound manifest and that an
        invalidated manifest no longer retains derived record markers.
        """

        orphan = self._conn.execute(
            "SELECT 1 FROM memory_recall_checkpoint_selections AS s LEFT JOIN "
            "memory_recall_checkpoints AS c ON c.scope_id = s.scope_id "
            "AND c.scope_json = s.scope_json AND c.checkpoint_id = s.checkpoint_id "
            "WHERE c.checkpoint_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan is not None:
            raise LedgerStateError("sealed recall checkpoint selection is orphaned")
        rows = self._conn.execute(
            "SELECT scope_id, scope_json, checkpoint_id FROM memory_recall_checkpoints "
            "ORDER BY scope_id, scope_json, checkpoint_id"
        ).fetchall()
        for row in rows:
            try:
                scope_data = json.loads(str(row["scope_json"]))
                if not isinstance(scope_data, dict):
                    raise TypeError("scope JSON must be an object")
                scope = MemoryScope(**scope_data)
                scope_id, scope_json = _scope_storage(scope)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerStateError("sealed recall checkpoint has an invalid scope") from exc
            if scope_id != str(row["scope_id"]) or scope_json != str(row["scope_json"]):
                raise LedgerStateError(
                    "sealed recall checkpoint scope does not match its storage key"
                )
            try:
                self._recall_checkpoint_manifest_locked(scope, str(row["checkpoint_id"]))
            except (KeyError, LedgerStateError) as exc:
                raise LedgerStateError("sealed recall checkpoint manifest is invalid") from exc

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
