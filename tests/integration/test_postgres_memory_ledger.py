"""Live PostgreSQL conformance and tamper-boundary tests for Ledger v6.

The PostgreSQL Ledger deliberately has the same synchronous, scope-pinned
host contract as SQLite, but owns an isolated schema and must independently
prove its PostgreSQL constraints, guards, and write linearization.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
import os
import threading
from typing import Any
import uuid

import pytest

from protoprompt.ledger import (
    LedgerConflictError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
)
from protoprompt.ledger.recall import (
    LedgerCheckpointError,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter
from ledger_conformance.core import (
    assert_admission_boundary_and_strict_recall,
    assert_candidate_confirmation_and_content_free_events,
    assert_checkpoint_reopen_resume_and_selected_record_invalidation,
    assert_exact_scope_isolation_and_scoped_forget,
    assert_idempotent_retries_and_conflicting_event_reuse,
    assert_lifecycle_forget_source_and_hard_erase,
    assert_restart_and_setup_persistence,
)


try:
    from protoprompt.ledger.postgres import PostgresMemoryLedger
except ModuleNotFoundError as exc:
    # Keep the future backend's import failure visible in the integration
    # report, but do not hide unrelated missing optional dependencies.
    if exc.name != "protoprompt.ledger.postgres":
        raise
    PostgresMemoryLedger = None  # type: ignore[assignment]


pytestmark: list[Any] = [pytest.mark.integration]
if PostgresMemoryLedger is None:
    pytestmark.append(pytest.mark.xfail(
        strict=True,
        reason="PostgresMemoryLedger has not been implemented yet",
    ))


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("PROTOPROMPT_POSTGRES_DSN")
    if not value:
        pytest.skip("set PROTOPROMPT_POSTGRES_DSN to run PostgreSQL Ledger tests")
    return value


@pytest.fixture
def schema(dsn: str) -> str:
    value = "pp_ledger_test_" + uuid.uuid4().hex
    try:
        yield value
    finally:
        _drop_schema(dsn, value)


def _backend(dsn: str, schema: str) -> Any:
    if PostgresMemoryLedger is None:
        pytest.fail("PostgresMemoryLedger is not implemented yet")
    return PostgresMemoryLedger(dsn, schema=schema)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _raw_connection(dsn: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _drop_schema(dsn: str, schema: str) -> None:
    with _raw_connection(dsn) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {_quoted_identifier(schema)} CASCADE")


def _writer(ledger: Any, scope: MemoryScope) -> MemoryWriter:
    return MemoryWriter(ledger, scope=scope, actor="postgres-ledger-host")


def _document_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="postgres-document-policy-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def _admitted_document(
    writer: MemoryWriter,
    *,
    record_id: str,
    content: str,
):
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=f"pdf:{record_id}",
        evidence_refs=(f"pdf:{record_id}:page:1",),
        confidence=0.9,
    ).submit(content)
    return gate.confirm(gate.review(candidate.record_id), event_id=f"admission:{record_id}")


def _planner(writer: MemoryWriter) -> LedgerRecallPlanner:
    return LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=RegexTokenCounter(),
        checkpoint_secret=b"postgres-ledger-v013-integration-secret",
    )


def _setup(ledger: Any) -> None:
    result = ledger.setup()
    assert not inspect.isawaitable(result), (
        "PostgresMemoryLedger.setup() must be synchronous to preserve the "
        "existing MemoryWriter contract"
    )


def _close(ledger: Any) -> None:
    result = ledger.close()
    assert not inspect.isawaitable(result), (
        "PostgresMemoryLedger.close() must be synchronous to preserve the "
        "existing MemoryWriter contract"
    )


def test_postgres_memory_ledger_v6_conformance(dsn: str, schema: str) -> None:
    """Run the backend-neutral public Ledger conformance suite once."""

    def factory() -> Any:
        return _backend(dsn, schema)

    assert_candidate_confirmation_and_content_free_events(factory)
    assert_admission_boundary_and_strict_recall(factory)
    assert_exact_scope_isolation_and_scoped_forget(factory)
    assert_idempotent_retries_and_conflicting_event_reuse(factory)
    assert_lifecycle_forget_source_and_hard_erase(factory)
    assert_restart_and_setup_persistence(factory)
    assert_checkpoint_reopen_resume_and_selected_record_invalidation(factory)


def test_postgres_memory_ledger_two_connection_contention_and_restart(
    dsn: str,
    schema: str,
) -> None:
    """One revision wins; a reopened client observes the durable outcome."""

    first = _backend(dsn, schema)
    second = _backend(dsn, schema)
    record_id = "pg-ledger-contention-" + uuid.uuid4().hex
    scope = MemoryScope(tenant="acme", user="alice", thread="contention")
    first_writer = MemoryWriter(first, scope=scope, actor="first-host")
    second_writer = MemoryWriter(second, scope=scope, actor="second-host")

    try:
        _setup(first)
        candidate = first_writer.propose(
            kind=MemoryKind.FACT,
            content="PostgreSQL Ledger contention restart sentinel.",
            source_ref="host:postgres-contention",
            confidence=0.9,
            record_id=record_id,
            event_id="pg-ledger-contention-observed",
        )
        barrier = threading.Barrier(2)

        def confirm(writer: MemoryWriter, event_id: str) -> object:
            barrier.wait(timeout=5)
            try:
                return writer.confirm(
                    record_id,
                    expected_revision=candidate.revision,
                    event_id=event_id,
                )
            except BaseException as exc:  # Assert exact contention outcome below.
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                lambda args: confirm(*args),
                (
                    (first_writer, "pg-ledger-contention-confirm-first"),
                    (second_writer, "pg-ledger-contention-confirm-second"),
                ),
            ))

        successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], LedgerConflictError)
    finally:
        _close(second)
        _close(first)

    reopened = _backend(dsn, schema)
    try:
        restored = MemoryWriter(reopened, scope=scope, actor="restart-host").get(record_id)
        assert restored is not None
        assert restored.content == "PostgreSQL Ledger contention restart sentinel."
        assert restored.revision == candidate.revision + 1
    finally:
        _close(reopened)


def test_postgres_setup_is_explicit_and_dedicated_to_its_schema(
    dsn: str,
    schema: str,
) -> None:
    """No implicit DDL, no shared/system schema, and idempotent fresh v6 setup."""

    ledger = _backend(dsn, schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="setup")
    writer = _writer(ledger, scope)
    try:
        with pytest.raises(LedgerNotReadyError):
            writer.propose(
                kind=MemoryKind.FACT,
                content="No write before an explicit PostgreSQL setup.",
                source_ref="host:pre-setup",
                event_id="pre-setup-observed",
            )
        assert ledger.dry_run_setup()["changes_required"] is True
        with _raw_connection(dsn) as connection:
            row = connection.execute(
                "SELECT to_regnamespace(%s) AS schema_name",
                (schema,),
            ).fetchone()
        assert row["schema_name"] is None

        ledger.setup()
        ledger.setup()
        assert ledger.schema_version() == 6
        assert ledger.dry_run_setup()["changes_required"] is False
    finally:
        _close(ledger)

    for invalid_schema in ("public", "pg_catalog", "information_schema", "x" * 64):
        with pytest.raises(ValueError, match="dedicated PostgreSQL identifier"):
            _backend(dsn, invalid_schema)


def test_postgres_fresh_setup_preserves_a_reserved_function_collision(dsn: str) -> None:
    """Fresh setup must never overwrite a same-named unrelated guard function."""

    collision_schema = "pp_ledger_collision_" + uuid.uuid4().hex
    quoted_schema = _quoted_identifier(collision_schema)
    try:
        with _raw_connection(dsn) as connection:
            connection.execute(f"CREATE SCHEMA {quoted_schema}")
            connection.execute(
                f"CREATE FUNCTION {quoted_schema}.protoprompt_memory_ledger_event_guard_v1() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
            )
        ledger = _backend(dsn, collision_schema)
        try:
            with pytest.raises(LedgerStateError, match="reserved schema names"):
                ledger.dry_run_setup()
            with pytest.raises(LedgerStateError, match="reserved schema names"):
                ledger.setup()
        finally:
            _close(ledger)
        with _raw_connection(dsn) as connection:
            row = connection.execute(
                "SELECT procedure.prosrc FROM pg_proc AS procedure "
                "JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace "
                "WHERE namespace.nspname = %s "
                "AND procedure.proname = %s",
                (collision_schema, "protoprompt_memory_ledger_event_guard_v1"),
            ).fetchone()
        assert row is not None
        assert "RETURN NEW" in str(row["prosrc"])
    finally:
        _drop_schema(dsn, collision_schema)


def test_postgres_function_shadowing_cannot_run_or_weaken_a_guard(
    dsn: str,
    schema: str,
) -> None:
    """A same-signature builtin shadow is refused before DDL and after setup."""

    quoted_schema = _quoted_identifier(schema)
    shadow_function = f"{quoted_schema}.current_setting(text, boolean)"
    with _raw_connection(dsn) as connection:
        connection.execute(f"CREATE SCHEMA {quoted_schema}")
        connection.execute(
            f"CREATE FUNCTION {shadow_function} RETURNS text LANGUAGE sql IMMUTABLE "
            "AS $$ SELECT 'on'::text $$"
        )

    ledger = _backend(dsn, schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="function-shadow")
    writer = _writer(ledger, scope)
    try:
        with pytest.raises(LedgerStateError, match="dedicated schema must be empty"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="dedicated schema must be empty"):
            ledger.setup()
        with _raw_connection(dsn) as connection:
            row = connection.execute(
                "SELECT pg_catalog.to_regclass(%s) AS relation",
                (f'{schema}.memory_events',),
            ).fetchone()
            connection.execute(f"DROP FUNCTION {shadow_function}")
        assert row["relation"] is None

        ledger.setup()
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="The guard must use pg_catalog.current_setting.",
            source_ref="host:function-shadow",
            event_id="function-shadow-observed",
        )
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"CREATE FUNCTION {shadow_function} RETURNS text LANGUAGE sql IMMUTABLE "
                "AS $$ SELECT 'on'::text $$"
            )
            connection.execute(f"SET search_path TO {quoted_schema}, pg_catalog")
            with pytest.raises(Exception, match="append-only"):
                connection.execute(
                    "UPDATE memory_events SET actor = %s",
                    ("attacker",),
                )
        with pytest.raises(LedgerStateError, match="unexpected user-defined function"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="unexpected user-defined function"):
            ledger.setup()
        with pytest.raises(LedgerStateError, match="unexpected user-defined function"):
            writer.get(candidate.record_id)
    finally:
        _close(ledger)


def test_postgres_guard_security_attributes_are_validated_and_repaired(
    dsn: str,
    schema: str,
) -> None:
    """A guard cannot retain SECURITY DEFINER or a function-local search path."""

    ledger = _backend(dsn, schema)
    quoted_schema = _quoted_identifier(schema)
    function = f"{quoted_schema}.protoprompt_memory_ledger_event_guard_v1()"
    scope = MemoryScope(tenant="acme", user="alice", thread="guard-attributes")
    writer = _writer(ledger, scope)
    try:
        ledger.setup()
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="The repaired guard remains a volatile invoker function.",
            source_ref="host:guard-attributes",
            event_id="guard-attributes-observed",
        )
        with _raw_connection(dsn) as connection:
            connection.execute(f"ALTER FUNCTION {function} SECURITY DEFINER")
            connection.execute(f"ALTER FUNCTION {function} SET search_path TO pg_catalog")
        with pytest.raises(LedgerStateError, match="guard function"):
            ledger.dry_run_setup()

        ledger.setup()
        with _raw_connection(dsn) as connection:
            row = connection.execute(
                "SELECT procedure.prokind AS function_kind, "
                "procedure.provolatile AS volatility, "
                "procedure.proisstrict AS is_strict, "
                "procedure.prosecdef AS security_definer, "
                "procedure.proconfig AS configuration, procedure.prosrc AS source "
                "FROM pg_catalog.pg_proc AS procedure "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = procedure.pronamespace "
                "WHERE namespace.nspname = %s AND procedure.proname = %s",
                (schema, "protoprompt_memory_ledger_event_guard_v1"),
            ).fetchone()
            with pytest.raises(Exception, match="append-only"):
                connection.execute(
                    f"UPDATE {quoted_schema}.memory_events SET actor = %s",
                    ("attacker",),
                )
        assert row is not None
        assert row["function_kind"] == "f"
        assert row["volatility"] == "v"
        assert row["is_strict"] is False
        assert row["security_definer"] is False
        assert row["configuration"] is None
        assert "pg_catalog.current_setting" in str(row["source"])
        assert writer.get(candidate.record_id) is not None
    finally:
        _close(ledger)


def test_postgres_refuses_pre_v6_ledger_schemas(dsn: str, schema: str) -> None:
    """This backend starts at fresh v6; it must not guess a SQLite migration."""

    quoted_schema = _quoted_identifier(schema)
    with _raw_connection(dsn) as connection:
        connection.execute(f"CREATE SCHEMA {quoted_schema}")
        connection.execute(
            f"CREATE TABLE {quoted_schema}.ledger_schema "
            "(component text PRIMARY KEY, version integer NOT NULL, applied_at text NOT NULL)"
        )
        connection.execute(
            f"INSERT INTO {quoted_schema}.ledger_schema (component, version, applied_at) "
            "VALUES (%s, %s, %s)",
            ("memory_ledger", 5, "2037-01-01T00:00:00+00:00"),
        )
    ledger = _backend(dsn, schema)
    try:
        assert ledger.schema_version() == 5
        with pytest.raises(LedgerStateError, match="unsupported"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="unsupported"):
            ledger.setup()
    finally:
        _close(ledger)


def test_postgres_catalog_validator_rejects_guard_and_index_tampering(
    dsn: str,
    schema: str,
) -> None:
    """Same-name no-op guards or indexes cannot turn a ready Ledger permissive."""

    ledger = _backend(dsn, schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="tamper")
    writer = _writer(ledger, scope)
    quoted_schema = _quoted_identifier(schema)
    try:
        ledger.setup()
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="A real event row proves the restored append-only trigger runs.",
            source_ref="host:guard-probe",
            event_id="guard-probe-observed",
        )
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"CREATE OR REPLACE FUNCTION {quoted_schema}."
                "protoprompt_memory_ledger_event_guard_v1() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$"
            )
        with pytest.raises(LedgerStateError, match="guard function"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="guard function"):
            writer.get(candidate.record_id)

        # A validated v6 schema can repair only its own no-op guard, matching
        # SQLite's explicit setup-repair boundary.
        ledger.setup()
        with _raw_connection(dsn) as connection:
            with pytest.raises(Exception, match="append-only"):
                connection.execute(
                    f"UPDATE {quoted_schema}.memory_events SET actor = %s",
                    ("attacker",),
                )
            connection.execute(
                f"DROP INDEX {quoted_schema}.idx_memory_events_scope_record"
            )
            connection.execute(
                f"CREATE INDEX idx_memory_events_scope_record "
                f"ON {quoted_schema}.memory_events (record_id)"
            )
        with pytest.raises(LedgerStateError, match="index"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="index"):
            ledger.setup()
        with pytest.raises(LedgerStateError, match="index"):
            writer.confirm(
                candidate.record_id,
                expected_revision=candidate.revision,
                event_id="guard-probe-confirmed",
            )
    finally:
        _close(ledger)


def test_postgres_catalog_validator_rejects_rls_policies_and_dml_rewrite_rules(
    dsn: str,
    schema: str,
) -> None:
    """RLS and user rewrite rules cannot silently change Ledger DML semantics."""

    ledger = _backend(dsn, schema)
    quoted_schema = _quoted_identifier(schema)
    table = f"{quoted_schema}.memory_records"
    try:
        ledger.setup()

        with _raw_connection(dsn) as connection:
            connection.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        with pytest.raises(LedgerStateError, match="row-level security"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="row-level security"):
            ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

        with _raw_connection(dsn) as connection:
            connection.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        with pytest.raises(LedgerStateError, match="row-level security"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="row-level security"):
            ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

        with _raw_connection(dsn) as connection:
            connection.execute(
                f"CREATE POLICY protoprompt_ledger_deny_all ON {table} USING (false)"
            )
        with pytest.raises(LedgerStateError, match="row-level security policy"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="row-level security policy"):
            ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"DROP POLICY protoprompt_ledger_deny_all ON {table}"
            )

        with _raw_connection(dsn) as connection:
            connection.execute(
                f"CREATE RULE protoprompt_ledger_block_update AS ON UPDATE TO {table} "
                "DO INSTEAD NOTHING"
            )
        with pytest.raises(LedgerStateError, match="rewrite rule"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="rewrite rule"):
            ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"DROP RULE protoprompt_ledger_block_update ON {table}"
            )

        assert ledger.dry_run_setup()["changes_required"] is False
    finally:
        _close(ledger)


def test_postgres_catalog_validator_rejects_ledger_inheritance(
    dsn: str,
    schema: str,
) -> None:
    """A normal-looking inherited child still changes Ledger table semantics."""

    ledger = _backend(dsn, schema)
    quoted_schema = _quoted_identifier(schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="inheritance")
    writer = _writer(ledger, scope)
    child = f"{quoted_schema}.memory_records_child"
    parent = f"{quoted_schema}.memory_records"
    try:
        ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(f"CREATE TABLE {child} () INHERITS ({parent})")
        with pytest.raises(LedgerStateError, match="unsupported inheritance"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="unsupported inheritance"):
            ledger.setup()
        with pytest.raises(LedgerStateError, match="unsupported inheritance"):
            writer.propose(
                kind=MemoryKind.FACT,
                content="Inherited Ledger relations must fail closed.",
                source_ref="host:inheritance",
                event_id="inheritance-observed",
            )
        with _raw_connection(dsn) as connection:
            connection.execute(f"DROP TABLE {child}")
        assert ledger.dry_run_setup()["changes_required"] is False
    finally:
        _close(ledger)


def test_postgres_catalog_validator_rejects_wrong_event_sequence_default(
    dsn: str,
    schema: str,
) -> None:
    """The event default must retain its exact auto-owned BIGSERIAL sequence."""

    ledger = _backend(dsn, schema)
    quoted_schema = _quoted_identifier(schema)
    table = f"{quoted_schema}.memory_events"
    wrong_sequence = "protoprompt_ledger_wrong_event_sequence"
    try:
        ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(f"CREATE SEQUENCE {quoted_schema}.{wrong_sequence}")
            connection.execute(
                f"ALTER TABLE {table} ALTER COLUMN sequence SET DEFAULT "
                f"pg_catalog.nextval('{schema}.{wrong_sequence}'::pg_catalog.regclass)"
            )
        with pytest.raises(LedgerStateError, match="event sequence"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="event sequence"):
            ledger.setup()
    finally:
        _close(ledger)


def test_postgres_catalog_validator_rejects_non_deterministic_text_collation(
    dsn: str,
    schema: str,
) -> None:
    """Run only where PostgreSQL exposes an installed non-deterministic collation."""

    with _raw_connection(dsn) as connection:
        collation = connection.execute(
            "SELECT namespace.nspname AS schema_name, "
            "collation_data.collname AS collation_name "
            "FROM pg_catalog.pg_collation AS collation_data "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = collation_data.collnamespace "
            "WHERE NOT collation_data.collisdeterministic "
            "ORDER BY namespace.nspname, collation_data.collname LIMIT 1"
        ).fetchone()
    if collation is None:
        pytest.skip("PostgreSQL has no installed non-deterministic collation")

    ledger = _backend(dsn, schema)
    quoted_schema = _quoted_identifier(schema)
    qualified_collation = (
        f"{_quoted_identifier(str(collation['schema_name']))}."
        f"{_quoted_identifier(str(collation['collation_name']))}"
    )
    try:
        ledger.setup()
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"ALTER TABLE {quoted_schema}.memory_events ALTER COLUMN actor "
                f"TYPE text COLLATE {qualified_collation}"
            )
        with pytest.raises(LedgerStateError, match="deterministic collation"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="deterministic collation"):
            ledger.setup()
    finally:
        _close(ledger)


def test_postgres_write_boundary_forces_hard_erase_off(
    dsn: str,
    schema: str,
) -> None:
    """A pre-existing session ON value cannot make ordinary DML permissive."""

    ledger = _backend(dsn, schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="hard-erase-guc")
    writer = _writer(ledger, scope)
    try:
        ledger.setup()
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="Write transactions must locally disable the hard-erase escape hatch.",
            source_ref="host:hard-erase-guc",
            event_id="hard-erase-guc-observed",
        )
        engine = ledger._engine
        initial = engine._raw.execute(
            "SELECT pg_catalog.current_setting(%s, true) AS hard_erase_setting",
            ("protoprompt.ledger_hard_erase",),
        ).fetchone()
        assert initial["hard_erase_setting"] == "off"
        engine._raw.execute(
            "SELECT pg_catalog.set_config(%s, 'on', false)",
            ("protoprompt.ledger_hard_erase",),
        )
        with engine._lock:
            with pytest.raises(Exception, match="append-only"):
                with engine._write_transaction_locked():
                    engine._conn.execute(
                        "UPDATE memory_events SET actor = ?",
                        ("attacker",),
                    )
        confirmed = writer.confirm(
            candidate.record_id,
            expected_revision=candidate.revision,
            event_id="hard-erase-guc-confirmed",
        )
        assert confirmed.revision == candidate.revision + 1
    finally:
        _close(ledger)


def test_postgres_hard_erase_uses_a_controlled_path_and_restores_all_guards(
    dsn: str,
    schema: str,
) -> None:
    """Direct DML is blocked; hard erase alone can cross the guard boundary."""

    ledger = _backend(dsn, schema)
    scope = MemoryScope(tenant="acme", user="alice", thread="hard-erase")
    writer = _writer(ledger, scope)
    quoted_schema = _quoted_identifier(schema)
    try:
        ledger.setup()
        active = _admitted_document(
            writer,
            record_id="erase-guarded-document",
            content="A hard erase must not weaken durable append-only guards.",
        )
        with _raw_connection(dsn) as connection:
            for statement, expected_error in (
                (
                    f"UPDATE {quoted_schema}.memory_events SET actor = %s",
                    "append-only",
                ),
                (
                    f"DELETE FROM {quoted_schema}.memory_record_admission_metadata "
                    "WHERE record_id = %s",
                    "admission metadata are immutable",
                ),
                (
                    f"DELETE FROM {quoted_schema}.memory_review_audits WHERE record_id = %s",
                    "review audits are immutable",
                ),
            ):
                with pytest.raises(Exception, match=expected_error):
                    connection.execute(statement, (active.record_id,) if "%s" in statement else ("attacker",))

        receipt = writer.erase(
            active.record_id,
            expected_revision=active.revision,
            event_id="erase-guarded-document",
        )
        assert receipt.events_deleted == 2
        assert writer.get(active.record_id) is None
        _close(ledger)
        ledger = _backend(dsn, schema)
        ledger.setup()
        reopened_writer = _writer(ledger, scope)
        restored_guard_record = _admitted_document(
            reopened_writer,
            record_id="erase-guarded-document-after-reopen",
            content="A reopened Ledger must restore the same immutable boundary.",
        )
        with _raw_connection(dsn) as connection:
            with pytest.raises(Exception, match="append-only"):
                connection.execute(
                    f"DELETE FROM {quoted_schema}.memory_events WHERE record_id = %s",
                    (restored_guard_record.record_id,),
                )
            with pytest.raises(Exception, match="admission metadata are immutable"):
                connection.execute(
                    f"DELETE FROM {quoted_schema}.memory_record_admission_metadata "
                    "WHERE record_id = %s",
                    (restored_guard_record.record_id,),
                )
            with pytest.raises(Exception, match="review audits are immutable"):
                connection.execute(
                    f"DELETE FROM {quoted_schema}.memory_review_audits "
                    "WHERE record_id = %s",
                    (restored_guard_record.record_id,),
                )
    finally:
        _close(ledger)


def test_postgres_checkpoint_hmac_restart_and_lifecycle_invalidation(
    dsn: str,
    schema: str,
) -> None:
    """A sealed checkpoint remains durable, scope-bound, and invalidated on forget."""

    scope = MemoryScope(tenant="acme", user="alice", thread="checkpoint")
    quoted_schema = _quoted_identifier(schema)
    first = _backend(dsn, schema)
    try:
        first.setup()
        first_writer = _writer(first, scope)
        active = _admitted_document(
            first_writer,
            record_id="checkpoint-document",
            content="The selected document is protected by a sealed PostgreSQL manifest.",
        )
        planner = _planner(first_writer)
        plan = planner.plan(
            task="How does PostgreSQL Ledger checkpoint safely resume?",
            token_budget=300,
            byte_budget=10_000,
        )
        checkpoint = planner.checkpoint(
            plan,
            checkpoint_id="postgres-checkpoint",
            continuation_ref="postgres-continuation",
        )
        assert checkpoint.continuation_ref == "postgres-continuation"
    finally:
        _close(first)

    reopened = _backend(dsn, schema)
    try:
        reopened.setup()
        writer = _writer(reopened, scope)
        planner = _planner(writer)
        resumed = planner.resume_checkpoint(
            "postgres-checkpoint",
            task="How does PostgreSQL Ledger checkpoint safely resume?",
        )
        assert resumed.continuation_ref == "postgres-continuation"

        with _raw_connection(dsn) as connection:
            connection.execute(
                f"UPDATE {quoted_schema}.memory_recall_checkpoints "
                "SET continuation_ref = %s WHERE checkpoint_id = %s",
                ("tampered-continuation", "postgres-checkpoint"),
            )
        with pytest.raises(LedgerCheckpointError, match="integrity seal"):
            planner.resume_checkpoint(
                "postgres-checkpoint",
                task="How does PostgreSQL Ledger checkpoint safely resume?",
            )
        with _raw_connection(dsn) as connection:
            connection.execute(
                f"UPDATE {quoted_schema}.memory_recall_checkpoints "
                "SET continuation_ref = %s WHERE checkpoint_id = %s",
                ("postgres-continuation", "postgres-checkpoint"),
            )

        writer.forget(
            active.record_id,
            expected_revision=active.revision,
            event_id="forget:checkpoint-document",
        )
        with _raw_connection(dsn) as connection:
            checkpoint_row = connection.execute(
                f"SELECT state FROM {quoted_schema}.memory_recall_checkpoints "
                "WHERE checkpoint_id = %s",
                ("postgres-checkpoint",),
            ).fetchone()
            selection_row = connection.execute(
                f"SELECT COUNT(*) AS count FROM {quoted_schema}."
                "memory_recall_checkpoint_selections WHERE checkpoint_id = %s",
                ("postgres-checkpoint",),
            ).fetchone()
        assert checkpoint_row["state"] == "invalidated"
        assert selection_row["count"] == 0
        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            planner.resume_checkpoint(
                "postgres-checkpoint",
                task="How does PostgreSQL Ledger checkpoint safely resume?",
            )
    finally:
        _close(reopened)
