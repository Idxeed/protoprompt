"""PostgreSQL v6 implementation of the experimental Memory Ledger.

The public Ledger command surface is synchronous because ``MemoryWriter``,
admission, recall, and the composed-request boundary are synchronous at their
storage edge.  Existing asynchronous PostgreSQL vector/profile adapters are
intentionally separate.  This module imports psycopg only when a PostgreSQL
Ledger is constructed, so the dependency-free core remains importable.

The internal engine reuses the already-tested Ledger command implementation,
but substitutes a deliberately small PostgreSQL DB-API adapter and PostgreSQL
schema/transaction boundary.  It is not a generic SQL abstraction: SQLite
keeps its native DDL, trigger, migration, and backup contracts.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import re
import threading
from typing import Any, Iterator

from protoprompt.ledger._backend import _LedgerCommandBackend
from protoprompt.ledger.sqlite import (
    _ALL_LEDGER_TABLE_COLUMNS,
    _COMPONENT,
    _LEDGER_INDEX_SIGNATURES,
    _SCHEMA_STATEMENTS,
    SqliteMemoryLedger,
)
from protoprompt.ledger.types import (
    LedgerConflictError,
    LedgerNotReadyError,
    LedgerStateError,
    format_timestamp,
    utc_now,
)


_PG_EVENT_TRIGGER = "protoprompt_memory_ledger_events_immutable_v1"
_PG_METADATA_TRIGGER = "protoprompt_memory_ledger_admission_metadata_immutable_v1"
_PG_AUDIT_TRIGGER = "protoprompt_memory_ledger_review_audits_immutable_v1"
_PG_EVENT_FUNCTION = "protoprompt_memory_ledger_event_guard_v1"
_PG_SIDECAR_FUNCTION = "protoprompt_memory_ledger_sidecar_guard_v1"
_PG_HARD_ERASE_SETTING = "protoprompt.ledger_hard_erase"
_PG_MAX_IDENTIFIER_BYTES = 63
_PG_FORBIDDEN_SCHEMA_NAMES = frozenset({"information_schema", "pg_catalog", "public"})
_PG_EXPECTED_TRIGGERS = {
    _PG_EVENT_TRIGGER: ("memory_events", _PG_EVENT_FUNCTION),
    _PG_METADATA_TRIGGER: (
        "memory_record_admission_metadata",
        _PG_SIDECAR_FUNCTION,
    ),
    _PG_AUDIT_TRIGGER: ("memory_review_audits", _PG_SIDECAR_FUNCTION),
}
_PG_EXPECTED_INDEXES = {
    "idx_memory_events_scope_record": (
        "memory_events",
        "btree (scope_id, scope_json, record_id, sequence)",
    ),
    "idx_memory_records_recall": (
        "memory_records",
        "btree (scope_id, scope_json, state, valid_from, valid_until, updated_at)",
    ),
    "idx_memory_sources_lookup": (
        "memory_sources",
        "btree (scope_id, scope_json, source_ref, record_id)",
    ),
    "idx_memory_relations_target": (
        "memory_relations",
        "btree (scope_id, scope_json, to_record_id)",
    ),
    "idx_memory_review_audits_record": (
        "memory_review_audits",
        "btree (scope_id, scope_json, record_id, event_id)",
    ),
    "idx_memory_recall_checkpoint_selection_record": (
        "memory_recall_checkpoint_selections",
        "btree (scope_id, scope_json, record_id)",
    ),
}
_PG_EXPECTED_CONSTRAINTS = {
    "ledger_schema": {
        ("p", "primary key (component)"),
        ("c", "check (version > 0)"),
    },
    "memory_events": {
        ("p", "primary key (sequence)"),
        ("u", "unique (scope_id, scope_json, event_id)"),
        ("c", "check (revision > 0)"),
    },
    "memory_records": {
        ("p", "primary key (scope_id, scope_json, record_id)"),
        ("c", "check (confidence >= 0 and confidence <= 1)"),
        ("c", "check (revision > 0)"),
        ("c", "check (last_event_sequence > 0)"),
    },
    "memory_payloads": {("p", "primary key (scope_id, scope_json, record_id)")},
    "memory_sources": {
        ("p", "primary key (scope_id, scope_json, source_ref, record_id)")
    },
    "memory_source_revocation_tombstones": {
        ("p", "primary key (scope_id, scope_json, source_key)")
    },
    "memory_erasure_receipts": {
        ("p", "primary key (scope_id, scope_json, event_id)")
    },
    "memory_erasure_tombstones": {
        ("p", "primary key (scope_id, scope_json, record_key)")
    },
    "memory_erased_event_tombstones": {
        ("p", "primary key (scope_id, scope_json, event_key)")
    },
    "memory_hard_erase_receipts": {
        ("p", "primary key (scope_id, scope_json, event_id)")
    },
    "memory_relations": {
        ("p", "primary key (scope_id, scope_json, from_record_id, to_record_id, relation)")
    },
    "memory_record_admission_metadata": {
        ("p", "primary key (scope_id, scope_json, record_id)"),
        (
            "f",
            "foreign key (scope_id, scope_json, record_id) references memory_records(scope_id, scope_json, record_id) on delete cascade",
        ),
    },
    "memory_review_audits": {
        ("p", "primary key (scope_id, scope_json, event_id)"),
        ("c", "check (candidate_revision > 0)"),
        ("c", "check (action = any (array['allow', 'quarantine', 'reject']))"),
        (
            "f",
            "foreign key (scope_id, scope_json, record_id) references memory_records(scope_id, scope_json, record_id) on delete cascade",
        ),
        (
            "f",
            "foreign key (scope_id, scope_json, event_id) references memory_events(scope_id, scope_json, event_id) on delete cascade",
        ),
    },
    "memory_recall_checkpoints": {
        ("p", "primary key (scope_id, scope_json, checkpoint_id)"),
        ("c", "check (state = any (array['active', 'invalidated']))"),
        ("c", "check (token_budget >= 0)"),
        ("c", "check (byte_budget >= 0)"),
        ("c", "check (used_tokens >= 0)"),
        ("c", "check (used_bytes >= 0)"),
        ("c", "check (selected_count >= 0)"),
        ("c", "check (used_tokens <= token_budget)"),
        ("c", "check (used_bytes <= byte_budget)"),
        (
            "c",
            "check (state = 'active' and invalidated_at is null and invalidation_reason is null or state = 'invalidated' and invalidated_at is not null and invalidation_reason is not null)",
        ),
    },
    "memory_recall_checkpoint_selections": {
        ("p", "primary key (scope_id, scope_json, checkpoint_id, ordinal)"),
        ("u", "unique (scope_id, scope_json, checkpoint_id, record_id)"),
        ("c", "check (ordinal >= 0)"),
        ("c", "check (revision > 0)"),
        (
            "f",
            "foreign key (scope_id, scope_json, checkpoint_id) references memory_recall_checkpoints(scope_id, scope_json, checkpoint_id) on delete cascade",
        ),
    },
}
_PG_NULLABLE_COLUMNS = {
    "memory_events": frozenset({"related_record_id", "reason_code", "content_hash"}),
    "memory_records": frozenset(
        {"valid_from", "valid_until", "superseded_by", "retracted_at", "expired_at"}
    ),
    "memory_recall_checkpoints": frozenset({"invalidated_at", "invalidation_reason"}),
}
_PG_INTEGER_COLUMNS = frozenset(
    {
        "version",
        "revision",
        "last_event_sequence",
        "schema_version",
        "payload_deleted",
        "source_refs_deleted",
        "relations_deleted",
        "events_deleted",
        "candidate_revision",
        "ordinal",
        "token_budget",
        "byte_budget",
        "used_tokens",
        "used_bytes",
        "selected_count",
    }
)
_PG_EVENT_FUNCTION_BODY = """
BEGIN
    IF pg_catalog.current_setting('protoprompt.ledger_hard_erase', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'memory_events are append-only';
END;
"""
_PG_SIDECAR_FUNCTION_BODY = """
BEGIN
    IF pg_catalog.current_setting('protoprompt.ledger_hard_erase', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'memory_record_admission_metadata' THEN
        RAISE EXCEPTION 'memory admission metadata are immutable';
    END IF;
    RAISE EXCEPTION 'memory review audits are immutable';
END;
"""
_PG_EXPECTED_FUNCTION_BODIES = {
    _PG_EVENT_FUNCTION: _PG_EVENT_FUNCTION_BODY,
    _PG_SIDECAR_FUNCTION: _PG_SIDECAR_FUNCTION_BODY,
}
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_EVENT_INSERT = re.compile(r"\A\s*INSERT\s+INTO\s+memory_events\b", re.IGNORECASE)
_SCHEMA_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _dependencies() -> tuple[Any, Any, Any]:
    """Load the optional synchronous psycopg dependency lazily."""

    try:
        import psycopg
        from psycopg import errors
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised without extra
        raise ImportError(
            "PostgreSQL Ledger requires psycopg 3. "
            "Install with: pip install 'protoprompt[postgres]'"
        ) from exc
    return psycopg, errors, dict_row


def _schema_identifier(value: str) -> str:
    normalized = value.casefold() if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or not _SCHEMA_IDENTIFIER.fullmatch(value)
        or len(value.encode("ascii")) > _PG_MAX_IDENTIFIER_BYTES
        or normalized in _PG_FORBIDDEN_SCHEMA_NAMES
        or normalized.startswith("pg_")
    ):
        raise ValueError(
            "schema must be a dedicated PostgreSQL identifier of at most 63 ASCII bytes"
        )
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalise_catalog_sql(value: str) -> str:
    """Normalize stable PostgreSQL catalog text for strict structural checks."""

    without_casts = re.sub(
        r"::(?:double precision|text|integer|bigint|regclass)(?:\[\])?",
        "",
        value.casefold(),
    )
    return re.sub(r"\s+", " ", without_casts).strip()


def _index_signature(definition: str) -> str:
    _, marker, suffix = definition.partition(" USING ")
    return _normalise_catalog_sql(suffix) if marker else ""


def _postgres_schema_statements() -> tuple[str, ...]:
    """Translate the v6 table shape, not SQLite's schema-validation model."""

    statements: list[str] = []
    for statement in _SCHEMA_STATEMENTS:
        statements.append(
            re.sub(
                r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                "BIGSERIAL PRIMARY KEY",
                statement,
                flags=re.IGNORECASE,
            )
        )
    return tuple(statements)


class _PostgresRow(dict[str, Any]):
    """Mapping row compatible with SQLite's one positional ``COUNT(*)`` read."""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class _PostgresCursor:
    """Small cursor adapter used only by inherited Ledger command code."""

    def __init__(self, cursor: Any, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @staticmethod
    def _row(value: Any) -> Any:
        return _PostgresRow(value) if isinstance(value, dict) else value

    def fetchone(self) -> Any:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _PostgresConnection:
    """Translate only the SQLite syntax used by inherited command methods."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def _statement(source: str) -> str:
        translated = source.replace("?", "%s")
        if _INSERT_OR_IGNORE.search(translated):
            translated = _INSERT_OR_IGNORE.sub("INSERT INTO", translated, count=1)
            translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return translated

    def execute(self, source: str, parameters: Any = None) -> _PostgresCursor:
        statement = self._statement(source)
        event_insert = bool(_EVENT_INSERT.match(statement))
        if event_insert:
            if re.search(r"\bRETURNING\b", statement, re.IGNORECASE):
                raise RuntimeError("memory event insert adapter owns its RETURNING sequence clause")
            statement = statement.rstrip().rstrip(";") + " RETURNING sequence"
        if parameters is None:
            cursor = self._connection.execute(statement, prepare=False)
        else:
            cursor = self._connection.execute(statement, parameters, prepare=False)
        lastrowid: int | None = None
        if event_insert:
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("memory event insert did not return a sequence")
            lastrowid = int(row["sequence"] if isinstance(row, dict) else row[0])
        return _PostgresCursor(cursor, lastrowid=lastrowid)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class _PostgresLedgerEngine(SqliteMemoryLedger):
    """Private PostgreSQL execution engine for the existing v6 command logic."""

    MIGRATION_VERSION = 6

    def __init__(self, conninfo: str, *, schema: str) -> None:
        if not isinstance(conninfo, str) or not conninfo.strip():
            raise ValueError("conninfo must be a non-empty PostgreSQL connection string")
        psycopg, errors, dict_row = _dependencies()
        self._schema = _schema_identifier(schema)
        self._quoted_schema = _quote_identifier(self._schema)
        self._raw = psycopg.connect(
            conninfo,
            autocommit=True,
            row_factory=dict_row,
        )
        self._conn = _PostgresConnection(self._raw)
        self._retryable_errors = (
            errors.LockNotAvailable,
            errors.SerializationFailure,
            errors.DeadlockDetected,
        )
        self._lock = threading.RLock()
        self._closed = False
        # Do not inherit a startup/session value for the internal escape hatch.
        # Individual hard-erases set it transaction-locally through the only
        # controlled path below.
        self._raw.execute(
            "SELECT pg_catalog.set_config('protoprompt.ledger_hard_erase', 'off', false)",
            prepare=False,
        )
        self._set_search_path_locked()

    @property
    def schema(self) -> str:
        """Return the PostgreSQL schema reserved for this Ledger instance."""

        return self._schema

    def _set_search_path_locked(self) -> None:
        self._raw.execute(
            f"SET search_path TO pg_catalog, {self._quoted_schema}",
            prepare=False,
        )

    def _set_fresh_ddl_search_path_locked(self) -> None:
        """Use the Ledger schema first only for validated fresh v6 DDL."""

        self._conn.execute(
            f"SET LOCAL search_path TO {self._quoted_schema}, pg_catalog"
        )

    def _restore_runtime_search_path_local_locked(self) -> None:
        """Restore pg_catalog-first resolution before guards or host commands."""

        self._conn.execute(
            f"SET LOCAL search_path TO pg_catalog, {self._quoted_schema}"
        )

    def _relation_name(self, name: str) -> str:
        return f"{self._quoted_schema}.{_quote_identifier(name)}"

    def _regclass(self, name: str) -> str:
        return f"{self._quoted_schema}.{_quote_identifier(name)}"

    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open_locked()
            return self._schema_version_locked()

    def dry_run_setup(self) -> dict[str, Any]:
        """Describe the explicit fresh v6 setup without mutating PostgreSQL."""

        with self._lock:
            self._ensure_open_locked()
            current = self._schema_version_locked()
            self._assert_schema_compatible_locked(
                current,
                validate_admission_data=True,
                validate_checkpoint_data=True,
            )
            if current not in {0, self.MIGRATION_VERSION}:
                raise LedgerStateError(
                    "PostgreSQL Ledger supports an explicit fresh v6 setup only; "
                    f"found schema v{current}"
                )
            return {
                "component": _COMPONENT,
                "from_version": current,
                "to_version": self.MIGRATION_VERSION,
                "changes_required": current != self.MIGRATION_VERSION,
                "actions": (
                    ["create isolated PostgreSQL memory-ledger v6 tables and guards"]
                    if current == 0
                    else []
                ),
            }

    def setup(self) -> None:
        """Create or validate the isolated PostgreSQL Ledger v6 schema."""

        with self._lock:
            self._ensure_open_locked()
            self._raw.execute(
                f"CREATE SCHEMA IF NOT EXISTS {self._quoted_schema}",
                prepare=False,
            )
            self._set_search_path_locked()
            with self._write_transaction_locked():
                current = self._schema_version_locked()
                self._assert_schema_compatible_locked(
                    current,
                    allow_event_guard_repair=True,
                    allow_admission_guard_repair=True,
                    validate_admission_data=True,
                    validate_checkpoint_data=True,
                )
                if current not in {0, self.MIGRATION_VERSION}:
                    raise LedgerStateError(
                        "PostgreSQL Ledger supports an explicit fresh v6 setup only; "
                        f"found schema v{current}"
                    )
                if current == 0:
                    # _assert_schema_compatible_locked has already proved the
                    # dedicated schema has no objects that could shadow this
                    # unqualified inherited SQLite-v6 DDL.
                    self._set_fresh_ddl_search_path_locked()
                    for statement in _postgres_schema_statements():
                        self._conn.execute(statement)
                    self._restore_runtime_search_path_local_locked()
                    self._install_guards_locked(repair=False)
                    self._conn.execute(
                        "INSERT INTO ledger_schema (component, version, applied_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(component) DO UPDATE SET "
                        "version = excluded.version, applied_at = excluded.applied_at",
                        (
                            _COMPONENT,
                            self.MIGRATION_VERSION,
                            format_timestamp(utc_now()),
                        ),
                    )
                else:
                    self._install_guards_locked(repair=True)
                self._assert_schema_compatible_locked(
                    self.MIGRATION_VERSION,
                    validate_admission_data=True,
                    validate_checkpoint_data=True,
                )

    def backup(self, destination: str) -> None:
        """Refuse a misleading file-copy abstraction for PostgreSQL backups."""

        raise NotImplementedError(
            "PostgreSQL Ledger backup is an operator responsibility; use pg_dump "
            "or the database platform's backup policy"
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "_PostgresLedgerEngine":
        return self

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def _write_transaction_locked(self) -> Iterator[None]:
        """Serialize all Ledger writes across PostgreSQL connections.

        SQLite's ``BEGIN IMMEDIATE`` gives the final lifecycle validation a
        linearization point.  PostgreSQL's ordinary MVCC reads do not, so this
        conservative transaction-scoped advisory lock intentionally covers
        lifecycle commands, checkpoint changes, and final active snapshots.
        It favours exact semantics over premature per-scope lock optimisation.
        """

        try:
            self._conn.execute("BEGIN")
            self._conn.execute("SET LOCAL lock_timeout = '5s'")
            self._conn.execute(
                "SELECT pg_catalog.set_config(?, 'off', true) "
                "AS hard_erase_disabled",
                (_PG_HARD_ERASE_SETTING,),
            )
            self._conn.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(?, 0)) "
                "AS ledger_write_lock",
                (f"protoprompt:ledger:v6:{self._schema}",),
            )
        except BaseException as exc:
            self._conn.rollback()
            self._raise_retryable_transaction_error(exc)
            raise
        try:
            yield
        except BaseException as exc:
            self._conn.rollback()
            self._raise_retryable_transaction_error(exc)
            raise
        else:
            try:
                self._conn.commit()
            except BaseException as exc:
                self._conn.rollback()
                self._raise_retryable_transaction_error(exc)
                raise

    @contextmanager
    def _read_transaction_locked(self) -> Iterator[None]:
        self._conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _raise_retryable_transaction_error(self, exc: BaseException) -> None:
        if isinstance(exc, self._retryable_errors):
            raise LedgerConflictError(
                "PostgreSQL Ledger write boundary was contended; retry the host command"
            ) from exc

    def _schema_version_locked(self) -> int:
        row = self._conn.execute(
            "SELECT pg_catalog.to_regclass(?) AS relation",
            (self._regclass("ledger_schema"),),
        ).fetchone()
        if row is None or row["relation"] is None:
            return 0

        relation_rows = self._conn.execute(
            "SELECT relation.oid AS relation_oid, relation.relkind AS relation_kind, "
            "relation.relispartition AS is_partition, "
            "relation.relpersistence AS persistence, "
            "relation.relrowsecurity AS row_security_enabled, "
            "relation.relforcerowsecurity AS row_security_forced, "
            "access_method.amname AS access_method "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_am AS access_method "
            "ON access_method.oid = relation.relam "
            "WHERE namespace.nspname = ? AND relation.relname = 'ledger_schema'",
            (self._schema,),
        ).fetchall()
        if len(relation_rows) != 1:
            raise LedgerStateError(
                "existing PostgreSQL ledger_schema relation is not compatible with "
                "the ProtoPrompt memory ledger"
            )
        relation = relation_rows[0]
        if (
            str(relation["relation_kind"]) != "r"
            or bool(relation["is_partition"])
            or str(relation["persistence"]) != "p"
            or str(relation["access_method"]) != "heap"
            or bool(relation["row_security_enabled"])
            or bool(relation["row_security_forced"])
        ):
            raise LedgerStateError(
                "existing PostgreSQL ledger_schema relation is not compatible with "
                "the ProtoPrompt memory ledger"
            )
        relation_oid = int(relation["relation_oid"])
        policy_row = self._conn.execute(
            "SELECT 1 AS policy_exists FROM pg_catalog.pg_policy "
            "WHERE polrelid = ? LIMIT 1",
            (relation_oid,),
        ).fetchone()
        inheritance_row = self._conn.execute(
            "SELECT 1 AS inheritance_exists FROM pg_catalog.pg_inherits "
            "WHERE inhrelid = ? OR inhparent = ? LIMIT 1",
            (relation_oid, relation_oid),
        ).fetchone()
        rewrite_row = self._conn.execute(
            "SELECT 1 AS rewrite_exists FROM pg_catalog.pg_rewrite "
            "WHERE ev_class = ? AND rulename <> '_RETURN' LIMIT 1",
            (relation_oid,),
        ).fetchone()
        if policy_row is not None or inheritance_row is not None or rewrite_row is not None:
            raise LedgerStateError(
                "existing PostgreSQL ledger_schema relation is not compatible with "
                "the ProtoPrompt memory ledger"
            )
        identity_rows = self._conn.execute(
            "SELECT attribute_data.attname AS column_name, "
            "attribute_data.atttypid = 'pg_catalog.text'::pg_catalog.regtype "
            "AS is_text, "
            "attribute_data.atttypid = 'pg_catalog.int4'::pg_catalog.regtype "
            "AS is_integer, attribute_data.attgenerated AS generated, "
            "attribute_data.attidentity AS identity "
            "FROM pg_catalog.pg_attribute AS attribute_data "
            "WHERE attribute_data.attrelid = ? AND attribute_data.attname "
            "IN ('component', 'version') AND attribute_data.attnum > 0 "
            "AND NOT attribute_data.attisdropped",
            (relation_oid,),
        ).fetchall()
        identity_by_name = {
            str(identity_row["column_name"]): identity_row
            for identity_row in identity_rows
        }
        component = identity_by_name.get("component")
        version = identity_by_name.get("version")
        if (
            component is None
            or version is None
            or not bool(component["is_text"])
            or not bool(version["is_integer"])
            or str(component["generated"]) != ""
            or str(version["generated"]) != ""
            or str(component["identity"]) != ""
            or str(version["identity"]) != ""
        ):
            raise LedgerStateError(
                "existing PostgreSQL ledger_schema relation is not compatible with "
                "the ProtoPrompt memory ledger"
            )
        try:
            version_row = self._conn.execute(
                f"SELECT version FROM {self._relation_name('ledger_schema')} "
                "WHERE component = ?",
                (_COMPONENT,),
            ).fetchone()
        except Exception as exc:
            raise LedgerStateError(
                "existing PostgreSQL ledger_schema table is not compatible with "
                "the ProtoPrompt memory ledger"
            ) from exc
        return int(version_row["version"]) if version_row is not None else 0

    def _assert_schema_compatible_locked(
        self,
        current: int,
        *,
        allow_event_guard_repair: bool = False,
        allow_admission_guard_repair: bool = False,
        validate_admission_data: bool = False,
        validate_checkpoint_data: bool = False,
    ) -> None:
        """Fail closed on a partial or unrelated PostgreSQL Ledger layout."""

        table_rows = self._conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = ?",
            (self._schema,),
        ).fetchall()
        tables = {str(row["table_name"]): str(row["table_type"]) for row in table_rows}
        relation_rows = self._conn.execute(
            "SELECT relation.oid AS relation_oid, relation.relname AS relation_name, "
            "relation.relkind AS relation_kind, "
            "relation.relispartition AS is_partition, "
            "relation.relpersistence AS persistence, "
            "access_method.amname AS access_method, "
            "relation.relrowsecurity AS row_security_enabled, "
            "relation.relforcerowsecurity AS row_security_forced "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_am AS access_method "
            "ON access_method.oid = relation.relam "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        type_rows = self._conn.execute(
            "SELECT type_data.typname AS type_name "
            "FROM pg_catalog.pg_type AS type_data "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = type_data.typnamespace "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        operator_rows = self._conn.execute(
            "SELECT operator_data.oprname AS operator_name "
            "FROM pg_catalog.pg_operator AS operator_data "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = operator_data.oprnamespace "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        index_rows = self._conn.execute(
            "SELECT index_relation.relname AS index_name, table_relation.relname AS table_name, "
            "pg_catalog.pg_get_indexdef(index_relation.oid) AS definition, "
            "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint AS constraint_object "
            "WHERE constraint_object.conindid = index_relation.oid) AS is_explicit, "
            "index_data.indisunique AS is_unique, index_data.indisvalid AS is_valid, "
            "index_data.indisready AS is_ready "
            "FROM pg_catalog.pg_index AS index_data "
            "JOIN pg_catalog.pg_class AS index_relation "
            "ON index_relation.oid = index_data.indexrelid "
            "JOIN pg_catalog.pg_class AS table_relation "
            "ON table_relation.oid = index_data.indrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = table_relation.relnamespace "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        index_names = {str(row["index_name"]) for row in index_rows}
        constraint_rows = self._conn.execute(
            "SELECT table_relation.relname AS table_name, constraint_data.contype AS constraint_type, "
            "pg_catalog.pg_get_constraintdef(constraint_data.oid, true) AS definition "
            "FROM pg_catalog.pg_constraint AS constraint_data "
            "JOIN pg_catalog.pg_class AS table_relation "
            "ON table_relation.oid = constraint_data.conrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = table_relation.relnamespace "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        function_rows = self._conn.execute(
            "SELECT procedure.proname AS function_name, "
            "pg_catalog.pg_get_function_identity_arguments(procedure.oid) AS arguments, "
            "pg_catalog.pg_get_function_result(procedure.oid) AS result, "
            "language.lanname AS language, procedure.prosrc AS source, "
            "procedure.prokind AS function_kind, "
            "procedure.provolatile AS volatility, "
            "procedure.proisstrict AS is_strict, "
            "procedure.prosecdef AS security_definer, "
            "procedure.proconfig AS configuration "
            "FROM pg_catalog.pg_proc AS procedure "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = procedure.pronamespace "
            "JOIN pg_catalog.pg_language AS language ON language.oid = procedure.prolang "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        trigger_rows = self._conn.execute(
            "SELECT t.tgname AS trigger_name, c.relname AS table_name, "
            "p.proname AS function_name, t.tgenabled AS enabled, "
            "pg_catalog.pg_get_triggerdef(t.oid, true) AS definition "
            "FROM pg_catalog.pg_trigger AS t "
            "JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid "
            "WHERE n.nspname = ? AND NOT t.tgisinternal",
            (self._schema,),
        ).fetchall()
        attribute_rows = self._conn.execute(
            "SELECT table_relation.relname AS table_name, "
            "attribute_data.attname AS column_name, "
            "attribute_data.atttypid = 'pg_catalog.text'::pg_catalog.regtype "
            "AS is_text, collation_data.collisdeterministic "
            "AS collation_is_deterministic "
            "FROM pg_catalog.pg_attribute AS attribute_data "
            "JOIN pg_catalog.pg_class AS table_relation "
            "ON table_relation.oid = attribute_data.attrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = table_relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_collation AS collation_data "
            "ON collation_data.oid = attribute_data.attcollation "
            "WHERE namespace.nspname = ? AND attribute_data.attnum > 0 "
            "AND NOT attribute_data.attisdropped",
            (self._schema,),
        ).fetchall()
        event_sequence_rows = self._conn.execute(
            "SELECT sequence_relation.oid AS sequence_oid, "
            "sequence_relation.relname AS sequence_name, "
            "sequence_relation.relkind AS relation_kind, "
            "sequence_relation.relpersistence AS persistence, "
            "sequence_namespace.nspname AS sequence_schema, "
            "default_dependency.deptype AS default_dependency, "
            "ownership.deptype AS ownership_dependency, "
            "pg_catalog.pg_get_expr(default_data.adbin, default_data.adrelid, false) "
            "AS default_expression, "
            "pg_catalog.format("
            "'nextval(' || pg_catalog.chr(37) || 'L::regclass)', "
            "sequence_relation.oid::pg_catalog.regclass::text"
            ") AS expected_default_expression, "
            "pg_catalog.format_type(sequence_data.seqtypid, NULL) AS sequence_type, "
            "sequence_data.seqstart AS start_value, "
            "sequence_data.seqincrement AS increment_value, "
            "sequence_data.seqmin AS minimum_value, "
            "sequence_data.seqmax AS maximum_value, "
            "sequence_data.seqcache AS cache_value, "
            "sequence_data.seqcycle AS cycles "
            "FROM pg_catalog.pg_class AS table_relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = table_relation.relnamespace "
            "JOIN pg_catalog.pg_attribute AS attribute_data "
            "ON attribute_data.attrelid = table_relation.oid "
            "AND attribute_data.attname = 'sequence' "
            "AND NOT attribute_data.attisdropped "
            "JOIN pg_catalog.pg_attrdef AS default_data "
            "ON default_data.adrelid = table_relation.oid "
            "AND default_data.adnum = attribute_data.attnum "
            "JOIN pg_catalog.pg_depend AS default_dependency "
            "ON default_dependency.classid = 'pg_catalog.pg_attrdef'::pg_catalog.regclass "
            "AND default_dependency.objid = default_data.oid "
            "AND default_dependency.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass "
            "AND default_dependency.refobjsubid = 0 "
            "AND default_dependency.deptype = 'n' "
            "JOIN pg_catalog.pg_class AS sequence_relation "
            "ON sequence_relation.oid = default_dependency.refobjid "
            "AND sequence_relation.relkind = 'S' "
            "JOIN pg_catalog.pg_namespace AS sequence_namespace "
            "ON sequence_namespace.oid = sequence_relation.relnamespace "
            "JOIN pg_catalog.pg_depend AS ownership "
            "ON ownership.classid = 'pg_catalog.pg_class'::pg_catalog.regclass "
            "AND ownership.objid = sequence_relation.oid "
            "AND ownership.objsubid = 0 "
            "AND ownership.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass "
            "AND ownership.refobjid = table_relation.oid "
            "AND ownership.refobjsubid = attribute_data.attnum "
            "AND ownership.deptype = 'a' "
            "JOIN pg_catalog.pg_sequence AS sequence_data "
            "ON sequence_data.seqrelid = sequence_relation.oid "
            "WHERE namespace.nspname = ? "
            "AND table_relation.relname = 'memory_events'",
            (self._schema,),
        ).fetchall()
        inheritance_rows = self._conn.execute(
            "SELECT child_relation.relname AS child_table_name, "
            "child_namespace.nspname AS child_schema_name, "
            "parent_relation.relname AS parent_table_name, "
            "parent_namespace.nspname AS parent_schema_name "
            "FROM pg_catalog.pg_inherits AS inheritance "
            "JOIN pg_catalog.pg_class AS child_relation "
            "ON child_relation.oid = inheritance.inhrelid "
            "JOIN pg_catalog.pg_namespace AS child_namespace "
            "ON child_namespace.oid = child_relation.relnamespace "
            "JOIN pg_catalog.pg_class AS parent_relation "
            "ON parent_relation.oid = inheritance.inhparent "
            "JOIN pg_catalog.pg_namespace AS parent_namespace "
            "ON parent_namespace.oid = parent_relation.relnamespace "
            "WHERE child_namespace.nspname = ? OR parent_namespace.nspname = ?",
            (self._schema, self._schema),
        ).fetchall()
        policy_rows = self._conn.execute(
            "SELECT relation.relname AS table_name, policy.polname AS policy_name "
            "FROM pg_catalog.pg_policy AS policy "
            "JOIN pg_catalog.pg_class AS relation ON relation.oid = policy.polrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = ?",
            (self._schema,),
        ).fetchall()
        rewrite_rule_rows = self._conn.execute(
            "SELECT relation.relname AS table_name, rewrite.rulename AS rule_name, "
            "rewrite.ev_type AS event_type "
            "FROM pg_catalog.pg_rewrite AS rewrite "
            "JOIN pg_catalog.pg_class AS relation ON relation.oid = rewrite.ev_class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = ? "
            "AND rewrite.ev_type IN ('2', '3', '4')",
            (self._schema,),
        ).fetchall()
        trigger_names = {str(row["trigger_name"]) for row in trigger_rows}
        reserved_tables = set(_ALL_LEDGER_TABLE_COLUMNS)
        reserved_indexes = set(_PG_EXPECTED_INDEXES)
        reserved_triggers = set(_PG_EXPECTED_TRIGGERS)
        reserved_functions = set(_PG_EXPECTED_FUNCTION_BODIES)
        function_names = {str(row["function_name"]) for row in function_rows}

        if current == 0:
            collisions = sorted(
                reserved_tables.intersection(tables)
                | reserved_indexes.intersection(index_names)
                | reserved_triggers.intersection(trigger_names)
                | reserved_functions.intersection(function_names)
            )
            if collisions:
                raise LedgerStateError(
                    "refusing PostgreSQL Ledger setup because reserved schema names "
                    "already exist: "
                    + ", ".join(collisions)
                )
            fresh_objects = sorted(
                [
                    f"relation {str(row['relation_name'])!r}"
                    for row in relation_rows
                ]
                + [
                    f"function {str(row['function_name'])!r}"
                    for row in function_rows
                ]
                + [f"type {str(row['type_name'])!r}" for row in type_rows]
                + [
                    f"operator {str(row['operator_name'])!r}"
                    for row in operator_rows
                ]
            )
            if fresh_objects:
                raise LedgerStateError(
                    "refusing PostgreSQL Ledger setup because the dedicated schema "
                    "must be empty before fresh v6 setup; found "
                    + ", ".join(fresh_objects[:5])
                    + (" ..." if len(fresh_objects) > 5 else "")
                )
            return
        if current != self.MIGRATION_VERSION:
            raise LedgerStateError(
                f"PostgreSQL Ledger schema v{current} is unsupported by v{self.MIGRATION_VERSION} code"
            )

        unexpected_functions = sorted(
            f"{str(row['function_name'])}({str(row['arguments'])})"
            for row in function_rows
            if str(row["function_name"]) not in reserved_functions
        )
        if unexpected_functions:
            raise LedgerStateError(
                "PostgreSQL Ledger schema has an unexpected user-defined function: "
                + ", ".join(unexpected_functions)
            )

        column_rows = self._conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default, "
            "is_identity, is_generated FROM information_schema.columns "
            "WHERE table_schema = ?",
            (self._schema,),
        ).fetchall()
        columns: dict[str, dict[str, Any]] = {}
        for row in column_rows:
            columns.setdefault(str(row["table_name"]), {})[str(row["column_name"])] = row
        for table_name, expected_columns in _ALL_LEDGER_TABLE_COLUMNS.items():
            if tables.get(table_name) != "BASE TABLE":
                raise LedgerStateError(
                    f"PostgreSQL Ledger schema is missing required table {table_name!r}"
                )
            table_columns = columns.get(table_name, {})
            if set(table_columns) != set(expected_columns):
                raise LedgerStateError(
                    f"PostgreSQL Ledger table {table_name!r} does not match v6 columns"
                )
            for column_name, row in table_columns.items():
                expected_type = (
                    "bigint"
                    if table_name == "memory_events" and column_name == "sequence"
                    else "real"
                    if table_name == "memory_records" and column_name == "confidence"
                    else "integer"
                    if column_name in _PG_INTEGER_COLUMNS
                    else "text"
                )
                expected_nullable = column_name in _PG_NULLABLE_COLUMNS.get(table_name, ())
                if (
                    str(row["data_type"]) != expected_type
                    or (str(row["is_nullable"]) == "YES") != expected_nullable
                    or str(row["is_identity"]) != "NO"
                    or str(row["is_generated"]) != "NEVER"
                ):
                    raise LedgerStateError(
                        f"PostgreSQL Ledger column {table_name}.{column_name} does not match v6"
                    )
                default = row["column_default"]
                if table_name == "memory_events" and column_name == "sequence":
                    if default is None:
                        raise LedgerStateError(
                            "PostgreSQL Ledger event sequence does not have a BIGSERIAL default"
                        )
                elif default is not None:
                    raise LedgerStateError(
                        f"PostgreSQL Ledger column {table_name}.{column_name} has an unexpected default"
                    )

        ledger_relations_by_name = {
            str(row["relation_name"]): row for row in relation_rows
        }
        for table_name in sorted(reserved_tables):
            relation = ledger_relations_by_name.get(table_name)
            if relation is None:
                raise LedgerStateError(
                    f"PostgreSQL Ledger table {table_name!r} is missing its relation catalog entry"
                )
            if (
                str(relation["relation_kind"]) != "r"
                or bool(relation["is_partition"])
                or str(relation["persistence"]) != "p"
                or str(relation["access_method"]) != "heap"
            ):
                raise LedgerStateError(
                    f"PostgreSQL Ledger table {table_name!r} must be an ordinary "
                    "persistent non-partition relation"
                )
            if bool(relation["row_security_enabled"]) or bool(
                relation["row_security_forced"]
            ):
                raise LedgerStateError(
                    f"PostgreSQL Ledger table {table_name!r} has unsupported row-level security"
                )

        attribute_by_column = {
            (str(row["table_name"]), str(row["column_name"])): row
            for row in attribute_rows
        }
        for table_name, expected_columns in _ALL_LEDGER_TABLE_COLUMNS.items():
            for column_name in expected_columns:
                if (
                    table_name == "memory_events" and column_name == "sequence"
                ):
                    expected_type = "bigint"
                elif table_name == "memory_records" and column_name == "confidence":
                    expected_type = "real"
                elif column_name in _PG_INTEGER_COLUMNS:
                    expected_type = "integer"
                else:
                    expected_type = "text"
                if expected_type != "text":
                    continue
                attribute = attribute_by_column.get((table_name, column_name))
                if (
                    attribute is None
                    or not bool(attribute["is_text"])
                    or not bool(attribute["collation_is_deterministic"])
                ):
                    raise LedgerStateError(
                        f"PostgreSQL Ledger text column {table_name}.{column_name} "
                        "does not use a deterministic collation"
                    )

        if len(event_sequence_rows) != 1:
            raise LedgerStateError(
                "PostgreSQL Ledger event sequence does not have exactly one "
                "auto-owned BIGSERIAL sequence"
            )
        event_sequence = event_sequence_rows[0]
        if (
            str(event_sequence["sequence_name"]) != "memory_events_sequence_seq"
            or str(event_sequence["relation_kind"]) != "S"
            or str(event_sequence["persistence"]) != "p"
            or str(event_sequence["sequence_schema"]) != self._schema
            or str(event_sequence["default_dependency"]) != "n"
            or str(event_sequence["ownership_dependency"]) != "a"
            or str(event_sequence["default_expression"])
            != str(event_sequence["expected_default_expression"])
            or str(event_sequence["sequence_type"]) != "bigint"
            or int(event_sequence["start_value"]) != 1
            or int(event_sequence["increment_value"]) != 1
            or int(event_sequence["minimum_value"]) != 1
            or int(event_sequence["maximum_value"]) != 9223372036854775807
            or int(event_sequence["cache_value"]) != 1
            or bool(event_sequence["cycles"])
        ):
            raise LedgerStateError(
                "PostgreSQL Ledger event sequence does not match the v6 BIGSERIAL contract"
            )

        ledger_inheritance_links: list[tuple[str, str, str, str]] = []
        for row in inheritance_rows:
            child_schema = str(row["child_schema_name"])
            child_table = str(row["child_table_name"])
            parent_schema = str(row["parent_schema_name"])
            parent_table = str(row["parent_table_name"])
            if (
                child_schema == self._schema and child_table in reserved_tables
            ) or (
                parent_schema == self._schema and parent_table in reserved_tables
            ):
                ledger_inheritance_links.append(
                    (child_schema, child_table, parent_schema, parent_table)
                )
        if ledger_inheritance_links:
            child_schema, child_table, parent_schema, parent_table = sorted(
                ledger_inheritance_links
            )[0]
            raise LedgerStateError(
                "PostgreSQL Ledger table participates in unsupported inheritance: "
                f"{child_schema}.{child_table} -> {parent_schema}.{parent_table}"
            )

        ledger_policies = sorted(
            (
                str(row["table_name"]),
                str(row["policy_name"]),
            )
            for row in policy_rows
            if str(row["table_name"]) in reserved_tables
        )
        if ledger_policies:
            table_name, policy_name = ledger_policies[0]
            raise LedgerStateError(
                f"PostgreSQL Ledger table {table_name!r} has unsupported row-level "
                f"security policy {policy_name!r}"
            )

        # A DML rewrite rule can add, replace, or suppress a Ledger command
        # before its constraints and guards see it.  Ledger relations are
        # ordinary base tables, so *every* UPDATE/INSERT/DELETE rule is
        # rejected — including a user rule deliberately named ``_RETURN``.
        ledger_rewrite_rules = sorted(
            (
                str(row["table_name"]),
                str(row["rule_name"]),
            )
            for row in rewrite_rule_rows
            if str(row["table_name"]) in reserved_tables
        )
        if ledger_rewrite_rules:
            table_name, rule_name = ledger_rewrite_rules[0]
            raise LedgerStateError(
                f"PostgreSQL Ledger table {table_name!r} has unsupported DML "
                f"rewrite rule {rule_name!r}"
            )

        constraints: dict[str, Counter[tuple[str, str]]] = {}
        for row in constraint_rows:
            table_name = str(row["table_name"])
            constraints.setdefault(table_name, Counter())[
                (str(row["constraint_type"]), _normalise_catalog_sql(str(row["definition"])))
            ] += 1
        for table_name, expected_constraints in _PG_EXPECTED_CONSTRAINTS.items():
            if constraints.get(table_name, Counter()) != Counter(expected_constraints):
                raise LedgerStateError(
                    f"PostgreSQL Ledger constraints for {table_name!r} do not match v6"
                )

        explicit_indexes = {
            str(row["index_name"]): row
            for row in index_rows
            if bool(row["is_explicit"])
            and str(row["table_name"]) in reserved_tables
        }
        if set(explicit_indexes) != set(_PG_EXPECTED_INDEXES):
            raise LedgerStateError("PostgreSQL Ledger explicit index set does not match v6")
        for index_name, (expected_table, expected_signature) in _PG_EXPECTED_INDEXES.items():
            row = explicit_indexes[index_name]
            if (
                str(row["table_name"]) != expected_table
                or _index_signature(str(row["definition"])) != expected_signature
                or bool(row["is_unique"])
                or not bool(row["is_valid"])
                or not bool(row["is_ready"])
            ):
                raise LedgerStateError(
                    f"PostgreSQL Ledger index {index_name!r} does not match v6"
                )

        functions_by_name: dict[str, list[Any]] = {}
        for row in function_rows:
            functions_by_name.setdefault(str(row["function_name"]), []).append(row)
        for function_name, expected_body in _PG_EXPECTED_FUNCTION_BODIES.items():
            rows = functions_by_name.get(function_name, [])
            valid = (
                len(rows) == 1
                and str(rows[0]["arguments"]) == ""
                and str(rows[0]["result"]).casefold() == "trigger"
                and str(rows[0]["language"]).casefold() == "plpgsql"
                and str(rows[0]["function_kind"]) == "f"
                and str(rows[0]["volatility"]) == "v"
                and not bool(rows[0]["is_strict"])
                and not bool(rows[0]["security_definer"])
                and rows[0]["configuration"] is None
                and _normalise_catalog_sql(str(rows[0]["source"]))
                == _normalise_catalog_sql(expected_body)
            )
            allow_repair = (
                allow_event_guard_repair
                if function_name == _PG_EVENT_FUNCTION
                else allow_admission_guard_repair
            )
            repairable = (
                not rows
                or (
                    len(rows) == 1
                    and str(rows[0]["arguments"]) == ""
                    and str(rows[0]["result"]).casefold() == "trigger"
                    and str(rows[0]["language"]).casefold() == "plpgsql"
                )
            )
            if not valid and not (allow_repair and repairable):
                raise LedgerStateError(
                    f"PostgreSQL Ledger guard function {function_name!r} is invalid"
                )

        for row in trigger_rows:
            table_name = str(row["table_name"])
            trigger_name = str(row["trigger_name"])
            if table_name in reserved_tables and trigger_name not in reserved_triggers:
                raise LedgerStateError(
                    f"unexpected PostgreSQL trigger {trigger_name!r} targets Ledger table"
                )
        by_name: dict[str, list[Any]] = {}
        for row in trigger_rows:
            by_name.setdefault(str(row["trigger_name"]), []).append(row)
        for trigger_name, (expected_table, expected_function) in _PG_EXPECTED_TRIGGERS.items():
            rows = by_name.get(trigger_name, [])
            expected_definition = (
                f"CREATE TRIGGER {trigger_name} BEFORE DELETE OR UPDATE ON {expected_table} "
                f"FOR EACH ROW EXECUTE FUNCTION {expected_function}()"
            )
            valid = (
                len(rows) == 1
                and str(rows[0]["table_name"]) == expected_table
                and str(rows[0]["function_name"]) == expected_function
                and str(rows[0]["enabled"]) == "O"
                and _normalise_catalog_sql(str(rows[0]["definition"]))
                == _normalise_catalog_sql(expected_definition)
            )
            allow_repair = (
                allow_event_guard_repair
                if trigger_name == _PG_EVENT_TRIGGER
                else allow_admission_guard_repair
            )
            repairable = not rows or (
                len(rows) == 1 and str(rows[0]["table_name"]) == expected_table
            )
            if not valid and not (allow_repair and repairable):
                raise LedgerStateError(
                    f"PostgreSQL Ledger immutability trigger {trigger_name!r} is invalid"
                )
        if validate_admission_data:
            self._validate_admission_sidecars_locked()
        if validate_checkpoint_data:
            self._validate_recall_checkpoint_sidecars_locked()

    def _install_guards_locked(self, *, repair: bool) -> None:
        event_function = self._relation_name(_PG_EVENT_FUNCTION)
        sidecar_function = self._relation_name(_PG_SIDECAR_FUNCTION)
        events = self._relation_name("memory_events")
        metadata = self._relation_name("memory_record_admission_metadata")
        audits = self._relation_name("memory_review_audits")
        create_function = "CREATE OR REPLACE FUNCTION" if repair else "CREATE FUNCTION"
        for function, body in (
            (event_function, _PG_EVENT_FUNCTION_BODY),
            (sidecar_function, _PG_SIDECAR_FUNCTION_BODY),
        ):
            self._raw.execute(
                f"{create_function} {function}() RETURNS trigger LANGUAGE plpgsql "
                "VOLATILE CALLED ON NULL INPUT SECURITY INVOKER "
                f"AS $protoprompt_ledger_guard${body}$protoprompt_ledger_guard$",
                prepare=False,
            )
            self._raw.execute(
                f"ALTER FUNCTION {function}() SECURITY INVOKER",
                prepare=False,
            )
            self._raw.execute(
                f"ALTER FUNCTION {function}() VOLATILE",
                prepare=False,
            )
            self._raw.execute(
                f"ALTER FUNCTION {function}() CALLED ON NULL INPUT",
                prepare=False,
            )
            self._raw.execute(
                f"ALTER FUNCTION {function}() RESET ALL",
                prepare=False,
            )
        trigger_specs = (
            (_PG_EVENT_TRIGGER, events, event_function),
            (_PG_METADATA_TRIGGER, metadata, sidecar_function),
            (_PG_AUDIT_TRIGGER, audits, sidecar_function),
        )
        for trigger_name, table, function in trigger_specs:
            self._raw.execute(
                f"DROP TRIGGER IF EXISTS {_quote_identifier(trigger_name)} ON {table}",
                prepare=False,
            )
            self._raw.execute(
                f"CREATE TRIGGER {_quote_identifier(trigger_name)} "
                f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                f"EXECUTE FUNCTION {function}()",
                prepare=False,
            )

    def _ensure_event_immutability_locked(self) -> None:
        """Keep inherited hard-erase redaction on the controlled path only."""

        self._assert_schema_compatible_locked(self.MIGRATION_VERSION)

    def _ensure_admission_immutability_locked(self) -> None:
        self._assert_schema_compatible_locked(self.MIGRATION_VERSION)

    def _allow_hard_erase_locked(self) -> None:
        self._conn.execute(
            "SELECT pg_catalog.set_config(?, 'on', true) AS hard_erase_enabled",
            (_PG_HARD_ERASE_SETTING,),
        )

    def _drop_owned_event_update_triggers_locked(self) -> None:
        """Inherited redaction requests the transaction-local controlled path."""

        self._allow_hard_erase_locked()

    @contextmanager
    def _suspend_admission_immutability_locked(self) -> Iterator[None]:
        """Allow only this hard-erase transaction to delete sidecar rows."""

        self._allow_hard_erase_locked()
        yield


class PostgresMemoryLedger(_LedgerCommandBackend):
    """Explicit-setup PostgreSQL backend for the experimental v6 Ledger.

    ``conninfo`` is a normal psycopg connection string. ``schema`` is owned by
    this Ledger instance; no DDL runs at import time or when the module is
    imported. Call :meth:`setup` explicitly before constructing a
    :class:`~protoprompt.ledger.MemoryWriter`.

    PostgreSQL uses an isolated schema and a transaction-scoped advisory lock
    for write/final-validation linearization. It does not offer SQLite's
    file-copy ``backup`` method; use the database platform's backup policy.
    """

    MIGRATION_VERSION = _PostgresLedgerEngine.MIGRATION_VERSION

    def __init__(self, conninfo: str, *, schema: str = "protoprompt_ledger") -> None:
        self._engine = _PostgresLedgerEngine(conninfo, schema=schema)

    @property
    def schema(self) -> str:
        return self._engine.schema

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    def __enter__(self) -> "PostgresMemoryLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        self._engine.close()
