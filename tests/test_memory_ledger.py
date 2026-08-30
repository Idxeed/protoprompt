from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from protoprompt.ledger import (
    LedgerConflictError,
    LedgerNotReadyError,
    LedgerStateError,
    MemoryKind,
    MemoryRelation,
    MemoryRelationType,
    MemoryState,
    MemoryTrust,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.types import canonical_json, command_hash, content_hash, scope_dict
from protoprompt.scope import MemoryScope
from protoprompt.store.sqlite import SqliteStore


T0 = datetime(2034, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def scope_a() -> MemoryScope:
    return MemoryScope(tenant="acme", user="alice", thread="t-1")


@pytest.fixture
def scope_b() -> MemoryScope:
    return MemoryScope(tenant="acme", user="bob", thread="t-1")


@pytest.fixture
def ledger() -> SqliteMemoryLedger:
    store = SqliteMemoryLedger()
    store.setup()
    try:
        yield store
    finally:
        store.close()


def _writer(ledger: SqliteMemoryLedger, scope: MemoryScope, *, now: datetime = T0) -> MemoryWriter:
    return MemoryWriter(ledger, scope=scope, actor="host-review", clock=lambda: now)


def _candidate(
    writer: MemoryWriter,
    *,
    record_id: str = "fact-language",
    source_ref: str = "turn:001",
    content: str = "Пользователь предпочитает русский язык.",
    valid_until: datetime | None = None,
    event_id: str = "evt-observed",
):
    return writer.propose(
        kind=MemoryKind.PREFERENCE,
        content=content,
        source_ref=source_ref,
        evidence_refs=("turn:001:line:1",),
        confidence=0.8,
        record_id=record_id,
        valid_until=valid_until,
        event_id=event_id,
    )


def test_setup_is_explicit_and_dry_run_does_not_create_schema(tmp_path, scope_a):
    path = tmp_path / "ledger.db"
    store = SqliteMemoryLedger(str(path))
    try:
        assert store.schema_version() == 0
        assert store.dry_run_setup() == {
            "component": "memory_ledger",
            "from_version": 0,
            "to_version": 4,
            "changes_required": True,
            "actions": ["create isolated memory-ledger tables"],
        }
        with pytest.raises(LedgerNotReadyError, match="setup"):
            store.observe(
                scope_a,
                kind="fact",
                content="not yet",
                source_ref="turn:before-setup",
            )
        assert store.schema_version() == 0

        store.setup()
        assert store.schema_version() == 4
        assert store.dry_run_setup()["changes_required"] is False
    finally:
        store.close()


def test_setup_keeps_legacy_sqlite_chunks_untouched_and_backup_is_readable(tmp_path, scope_a):
    path = tmp_path / "shared.db"
    legacy = SqliteStore(str(path))
    legacy.add("legacy-doc", ["legacy sentinel"], [[1.0]], {"kind": "legacy"})
    legacy.close()

    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer, record_id="backed-up", event_id="evt-backup")
    active = writer.confirm(candidate.record_id, expected_revision=1, event_id="evt-backup-confirm")
    backup_path = tmp_path / "shared-backup.db"
    ledger.backup(str(backup_path))
    ledger.close()

    legacy_reopened = SqliteStore(str(path))
    try:
        assert legacy_reopened.get("legacy-doc")["document"] == "legacy sentinel"
    finally:
        legacy_reopened.close()

    copied = SqliteMemoryLedger(str(backup_path))
    try:
        restored = copied.get(scope_a, active.record_id)
        assert restored is not None
        assert restored.content == "Пользователь предпочитает русский язык."
    finally:
        copied.close()


def test_setup_fails_closed_when_an_unrelated_database_uses_a_reserved_table_case_insensitively(tmp_path):
    path = tmp_path / "foreign-memory-events.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE MEMORY_EVENTS (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO MEMORY_EVENTS (value) VALUES ('foreign sentinel')")
        connection.commit()
    finally:
        connection.close()

    ledger = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="reserved schema names"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="reserved schema names"):
            ledger.setup()
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM memory_events").fetchone()[0] == "foreign sentinel"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_records'"
        ).fetchone() is None
    finally:
        connection.close()


def test_explicit_v1_to_v4_migration_adds_erasure_and_source_revocation_guards(tmp_path):
    path = tmp_path / "v1-ledger.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_erasure_tombstones")
        connection.execute("DROP TABLE memory_erasure_receipts")
        connection.execute("DROP TABLE memory_erased_event_tombstones")
        connection.execute("DROP TABLE memory_hard_erase_receipts")
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute("UPDATE ledger_schema SET version = 1 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        assert upgraded.dry_run_setup() == {
            "component": "memory_ledger",
            "from_version": 1,
            "to_version": 4,
            "changes_required": True,
            "actions": [
                "add v2 erasure receipts and replay tombstones",
                "add v3 erased-command replay barriers",
                "add v4 source-revocation barriers and scrub legacy event fingerprints",
            ],
        }
        upgraded.setup()
        assert upgraded.schema_version() == 4
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_erasure_tombstones'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_erasure_receipts'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_erased_event_tombstones'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_hard_erase_receipts'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'memory_source_revocation_tombstones'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_v3_to_v4_migration_scrubs_legacy_fingerprints_through_its_old_guard(
    tmp_path,
    scope_a,
):
    path = tmp_path / "v3-ledger.db"
    seeded = SqliteMemoryLedger(str(path))
    seeded.setup()
    _candidate(
        _writer(seeded, scope_a),
        record_id="legacy-fingerprint",
        content="Legacy candidate plaintext.",
        event_id="evt-legacy-fingerprint",
    )
    seeded.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute(
            "DROP TRIGGER protoprompt_memory_ledger_events_reject_update_v1"
        )
        connection.execute(
            "UPDATE memory_events SET content_hash = ?, payload_hash = ? "
            "WHERE event_id = ?",
            ("f" * 64, "legacy-content-derived-command", "evt-legacy-fingerprint"),
        )
        connection.execute(
            "UPDATE memory_records SET last_event_sequence = 91 "
            "WHERE record_id = ?",
            ("legacy-fingerprint",),
        )
        connection.execute(
            "CREATE TRIGGER memory_events_reject_update BEFORE UPDATE ON memory_events "
            "BEGIN SELECT RAISE(ABORT, 'memory_events are append-only'); END"
        )
        connection.execute("UPDATE ledger_schema SET version = 3 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        assert upgraded.dry_run_setup()["actions"] == [
            "add v4 source-revocation barriers and scrub legacy event fingerprints"
        ]
        upgraded.setup()
        assert upgraded.schema_version() == 4
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        event = connection.execute(
            "SELECT content_hash, payload_hash FROM memory_events WHERE event_id = ?",
            ("evt-legacy-fingerprint",),
        ).fetchone()
        assert event[0] is None
        assert event[1] != "legacy-content-derived-command"
        assert connection.execute(
            "SELECT last_event_sequence FROM memory_records WHERE record_id = ?",
            ("legacy-fingerprint",),
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE memory_events SET actor = 'attacker' WHERE sequence = 1")
    finally:
        connection.close()


def test_migration_fails_closed_before_adopting_an_incompatible_future_table(tmp_path):
    path = tmp_path / "incompatible-future-table.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_erasure_tombstones")
        connection.execute("DROP TABLE memory_erasure_receipts")
        connection.execute("DROP TABLE memory_erased_event_tombstones")
        connection.execute("DROP TABLE memory_hard_erase_receipts")
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute("CREATE TABLE memory_erasure_receipts (foreign_value TEXT)")
        connection.execute("UPDATE ledger_schema SET version = 1 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.dry_run_setup()
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.setup()
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_erasure_receipts'"
        ).fetchone()[0].startswith("CREATE TABLE memory_erasure_receipts (foreign_value TEXT)")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'memory_erased_event_tombstones'"
        ).fetchone() is None
    finally:
        connection.close()


def test_migration_fails_closed_on_a_future_table_with_incompatible_constraints(tmp_path):
    path = tmp_path / "incompatible-future-table-constraints.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_erasure_tombstones")
        connection.execute("DROP TABLE memory_erasure_receipts")
        connection.execute("DROP TABLE memory_erased_event_tombstones")
        connection.execute("DROP TABLE memory_hard_erase_receipts")
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute(
            "CREATE TABLE memory_erasure_receipts ("
            "scope_id TEXT NOT NULL, scope_json TEXT NOT NULL, event_id TEXT NOT NULL, "
            "record_id TEXT NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL, "
            "payload_deleted INTEGER NOT NULL CHECK (payload_deleted = 0), "
            "source_refs_deleted INTEGER NOT NULL, relations_deleted INTEGER NOT NULL, "
            "PRIMARY KEY (scope_id, scope_json, event_id))"
        )
        connection.execute("UPDATE ledger_schema SET version = 1 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.dry_run_setup()
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.setup()
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
        ).fetchone()[0] == 1
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_erasure_receipts'"
        ).fetchone()[0]
        assert "CHECK (payload_deleted = 0)" in table_sql
    finally:
        connection.close()


def test_migration_dry_run_rejects_a_future_reserved_table_name_owned_by_a_view(tmp_path):
    path = tmp_path / "future-table-view.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_erasure_tombstones")
        connection.execute("DROP TABLE memory_erasure_receipts")
        connection.execute("DROP TABLE memory_erased_event_tombstones")
        connection.execute("DROP TABLE memory_hard_erase_receipts")
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute(
            "CREATE VIEW memory_erasure_receipts AS SELECT 'foreign' AS foreign_value"
        )
        connection.execute("UPDATE ledger_schema SET version = 1 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.dry_run_setup()
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.setup()
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'memory_erasure_receipts'"
        ).fetchone()[0] == "view"
    finally:
        connection.close()


def test_setup_fails_closed_on_an_altered_explicit_ledger_index(tmp_path):
    path = tmp_path / "incompatible-ledger-index.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX idx_memory_events_scope_record")
        connection.execute(
            "CREATE UNIQUE INDEX idx_memory_events_scope_record "
            "ON memory_events (scope_id, scope_json, record_id)"
        )
        connection.commit()
    finally:
        connection.close()

    reopened = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="idx_memory_events_scope_record"):
            reopened.dry_run_setup()
        with pytest.raises(LedgerStateError, match="idx_memory_events_scope_record"):
            reopened.setup()
    finally:
        reopened.close()


def test_fresh_setup_rejects_a_reserved_ledger_index_name_on_another_table(tmp_path):
    path = tmp_path / "reserved-ledger-index.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_marker (value TEXT)")
        connection.execute("CREATE INDEX idx_memory_events_scope_record ON legacy_marker (value)")
        connection.commit()
    finally:
        connection.close()

    ledger = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="idx_memory_events_scope_record"):
            ledger.dry_run_setup()
        with pytest.raises(LedgerStateError, match="idx_memory_events_scope_record"):
            ledger.setup()
    finally:
        ledger.close()


def test_setup_rejects_an_unowned_index_targeting_an_uppercase_ledger_table(tmp_path):
    path = tmp_path / "unexpected-uppercase-ledger-index.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE INDEX foreign_payload_index ON MEMORY_PAYLOADS (content)")
        connection.commit()
    finally:
        connection.close()

    reopened = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="foreign_payload_index"):
            reopened.dry_run_setup()
        with pytest.raises(LedgerStateError, match="foreign_payload_index"):
            reopened.setup()
    finally:
        reopened.close()


def test_setup_rechecks_migration_target_schema_after_acquiring_write_lock(tmp_path):
    class BeginProxy:
        def __init__(self, connection, inject):
            self._connection = connection
            self._inject = inject
            self._injected = False

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE" and not self._injected:
                self._injected = True
                self._inject()
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    path = tmp_path / "migration-preflight-race.db"
    fresh = SqliteMemoryLedger(str(path))
    fresh.setup()
    fresh.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE memory_erasure_tombstones")
        connection.execute("DROP TABLE memory_erasure_receipts")
        connection.execute("DROP TABLE memory_erased_event_tombstones")
        connection.execute("DROP TABLE memory_hard_erase_receipts")
        connection.execute("DROP TABLE memory_source_revocation_tombstones")
        connection.execute("UPDATE ledger_schema SET version = 1 WHERE component = 'memory_ledger'")
        connection.commit()
    finally:
        connection.close()

    upgraded = SqliteMemoryLedger(str(path))

    def inject_foreign_future_table():
        foreign = sqlite3.connect(path)
        try:
            foreign.execute("CREATE TABLE memory_erasure_receipts (foreign_value TEXT)")
            foreign.commit()
        finally:
            foreign.close()

    upgraded._conn = BeginProxy(upgraded._conn, inject_foreign_future_table)
    try:
        with pytest.raises(LedgerStateError, match="memory_erasure_receipts"):
            upgraded.setup()
    finally:
        upgraded.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT version FROM ledger_schema WHERE component = 'memory_ledger'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT foreign_value FROM memory_erasure_receipts").fetchall() == []
    finally:
        connection.close()


def test_writer_requires_a_non_empty_host_scope(ledger):
    with pytest.raises(ValueError, match="non-empty"):
        MemoryWriter(ledger, scope=MemoryScope())


def test_candidate_requires_host_confirmation_before_default_recall(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer)

    assert candidate.state is MemoryState.CANDIDATE
    assert candidate.trust is MemoryTrust.UNTRUSTED
    assert candidate.is_recallable(now=T0) is False
    assert writer.list_active() == []

    active = writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-confirmed",
    )
    assert active.state is MemoryState.ACTIVE
    assert active.trust is MemoryTrust.HOST_CONFIRMED
    assert active.is_recallable(now=T0) is True
    assert [record.record_id for record in writer.list_active()] == ["fact-language"]

    receipts = writer.events(candidate.record_id)
    assert [event.event_type.value for event in receipts] == ["observed", "confirmed"]
    assert all("Пользователь предпочитает" not in json.dumps(event.to_dict()) for event in receipts)
    assert all("turn:001" not in json.dumps(event.to_dict()) for event in receipts)

    with pytest.raises(ValueError, match="host_confirmed"):
        replace(candidate, state=MemoryState.ACTIVE)


def test_event_sequences_are_local_to_the_record_and_never_expose_content_hashes(
    ledger,
    scope_a,
    scope_b,
):
    alice = _writer(ledger, scope_a)
    bob = _writer(ledger, scope_b)
    alice_candidate = _candidate(alice, record_id="alice-sequence", event_id="evt-alice-sequence")
    bob_candidate = _candidate(
        bob,
        record_id="bob-sequence",
        source_ref="turn:bob-sequence",
        content="Bob sequence sentinel.",
        event_id="evt-bob-sequence",
    )
    alice_active = alice.confirm(
        alice_candidate.record_id,
        expected_revision=alice_candidate.revision,
        event_id="evt-alice-sequence-confirm",
    )

    assert [event.sequence for event in alice.events(alice_active.record_id)] == [1, 2]
    assert [event.sequence for event in bob.events(bob_candidate.record_id)] == [1]
    assert alice_active.last_event_sequence == 2
    assert bob_candidate.last_event_sequence == 1
    assert all(
        "content_hash" not in event.to_dict()
        for event in alice.events(alice_active.record_id)
    )


def test_default_reader_defensively_filters_a_record_that_stops_being_recallable(
    ledger,
    scope_a,
    monkeypatch,
):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer, record_id="reader-race", event_id="evt-reader-race")
    writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-reader-race-confirm",
    )
    original_load = ledger._load_record_locked

    def return_nonrecallable_snapshot(scope, record_id):
        record = original_load(scope, record_id)
        return replace(record, state=MemoryState.RETRACTED) if record is not None else None

    monkeypatch.setattr(ledger, "_load_record_locked", return_nonrecallable_snapshot)
    assert writer.list_active() == []


def test_get_uses_one_snapshot_when_a_related_record_is_hard_erased(tmp_path, scope_a):
    class CursorProxy:
        def __init__(self, cursor, trigger):
            self._cursor = cursor
            self._trigger = trigger

        def fetchone(self):
            row = self._cursor.fetchone()
            self._trigger(row)
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class ConnectionProxy:
        def __init__(self, connection, trigger):
            self._connection = connection
            self._trigger = trigger

        def execute(self, sql, parameters=()):
            return CursorProxy(self._connection.execute(sql, parameters), lambda row: self._trigger(sql, row))

        def __getattr__(self, name):
            return getattr(self._connection, name)

    path = tmp_path / "snapshot-get.db"
    writer_ledger = SqliteMemoryLedger(str(path))
    writer_ledger.setup()
    reader_ledger = SqliteMemoryLedger(str(path))
    writer_ledger._conn.execute("PRAGMA journal_mode=WAL")
    reader_ledger._conn.execute("PRAGMA journal_mode=WAL")
    writer = _writer(writer_ledger, scope_a)
    try:
        old = writer.confirm(
            _candidate(writer, record_id="snapshot-old", event_id="evt-snapshot-old").record_id,
            expected_revision=1,
            event_id="evt-snapshot-old-confirm",
        )
        new_candidate = _candidate(
            writer,
            record_id="snapshot-new",
            source_ref="turn:snapshot-new",
            content="Snapshot replacement.",
            event_id="evt-snapshot-new",
        )
        new = writer.confirm(
            new_candidate.record_id,
            expected_revision=new_candidate.revision,
            event_id="evt-snapshot-new-confirm",
        )
        superseded = writer.supersede(
            old.record_id,
            replacement_record_id=new.record_id,
            expected_revision=old.revision,
            expected_replacement_revision=new.revision,
            event_id="evt-snapshot-supersede",
        )
        fired = False

        def erase_after_record_row(sql, row):
            nonlocal fired
            if row is not None and not fired and "SELECT r.*, p.content" in sql:
                fired = True
                writer.erase(
                    superseded.record_id,
                    expected_revision=superseded.revision,
                    event_id="evt-snapshot-hard-erase",
                )

        reader_ledger._conn = ConnectionProxy(reader_ledger._conn, erase_after_record_row)
        snapshot = reader_ledger.get(scope_a, new.record_id)
        assert snapshot is not None
        assert snapshot.relations == (
            MemoryRelation(MemoryRelationType.SUPERSEDES, old.record_id),
        )
        after = writer.get(new.record_id)
        assert after is not None
        assert after.relations == ()
    finally:
        reader_ledger.close()
        writer_ledger.close()


def test_retries_are_idempotent_but_reused_event_id_with_other_payload_conflicts(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    first = _candidate(writer, event_id="evt-001")
    retried = _candidate(writer, event_id="evt-001")

    assert retried == first
    assert len(writer.events(first.record_id)) == 1
    with pytest.raises(LedgerConflictError, match="event_id"):
        _candidate(
            writer,
            content="Другой текст не должен пройти с тем же idempotency key.",
            event_id="evt-001",
        )

    active = writer.confirm(
        first.record_id,
        expected_revision=1,
        event_id="evt-002",
    )
    retried_active = writer.confirm(
        first.record_id,
        expected_revision=1,
        event_id="evt-002",
    )
    assert retried_active == active
    assert len(writer.events(first.record_id)) == 2


def test_candidate_retry_conflicts_when_the_audited_host_actor_changes(ledger, scope_a):
    ledger.observe(
        scope_a,
        kind="fact",
        content="Actor-bound candidate.",
        source_ref="turn:actor",
        record_id="actor-bound",
        actor="reviewer-a",
        event_id="evt-actor-bound",
        occurred_at=T0,
    )
    with pytest.raises(LedgerConflictError, match="different command"):
        ledger.observe(
            scope_a,
            kind="fact",
            content="Actor-bound candidate.",
            source_ref="turn:actor",
            record_id="actor-bound",
            actor="reviewer-b",
            event_id="evt-actor-bound",
            occurred_at=T0 + timedelta(seconds=1),
        )


def test_idempotency_key_survives_a_retry_with_a_later_execution_timestamp(ledger, scope_a):
    times = iter((T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=20), T0 + timedelta(seconds=30)))
    writer = MemoryWriter(ledger, scope=scope_a, clock=lambda: next(times))
    first = _candidate(writer, event_id="evt-retry-later")
    retried = _candidate(writer, event_id="evt-retry-later")

    assert retried == first
    assert len(writer.events(first.record_id)) == 1
    active = writer.confirm(first.record_id, expected_revision=1, event_id="evt-retry-confirm")
    retried_active = writer.confirm(first.record_id, expected_revision=1, event_id="evt-retry-confirm")
    assert retried_active == active


def test_implicit_record_id_can_retry_with_the_same_event_id(ledger, scope_a):
    times = iter((T0, T0 + timedelta(seconds=1)))
    writer = MemoryWriter(ledger, scope=scope_a, clock=lambda: next(times))
    first = writer.propose(
        kind="fact",
        content="Anonymous-idempotent candidate.",
        source_ref="turn:implicit",
        event_id="evt-implicit",
    )
    retried = writer.propose(
        kind="fact",
        content="Anonymous-idempotent candidate.",
        source_ref="turn:implicit",
        event_id="evt-implicit",
    )
    assert retried == first
    assert ledger.count(scope_a) == 1


def test_writer_rejects_a_raw_string_where_evidence_ids_are_expected(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    with pytest.raises(TypeError, match="not a string"):
        writer.propose(
            kind="fact",
            content="not an evidence list",
            source_ref="turn:bad-evidence",
            evidence_refs="raw document text",
        )


def test_same_logical_id_isolated_by_exact_host_scope(ledger, scope_a, scope_b):
    alice = _writer(ledger, scope_a)
    bob = _writer(ledger, scope_b)
    alice_record = _candidate(alice, record_id="shared-id", content="alice only")
    bob_record = _candidate(
        bob,
        record_id="shared-id",
        source_ref="turn:bob-001",
        content="bob only",
        event_id="evt-bob-observed",
    )

    alice_active = alice.confirm(
        alice_record.record_id,
        expected_revision=alice_record.revision,
        event_id="evt-alice-confirmed",
    )
    bob_active = bob.confirm(
        bob_record.record_id,
        expected_revision=bob_record.revision,
        event_id="evt-bob-confirmed",
    )
    assert [record.content for record in alice.list_active()] == ["alice only"]
    assert [record.content for record in bob.list_active()] == ["bob only"]

    receipt = alice.forget(alice_active.record_id, expected_revision=alice_active.revision)
    assert receipt.payload_deleted is True
    assert alice.list_active() == []
    assert [record.content for record in bob.list_active()] == ["bob only"]
    assert bob.get("shared-id").revision == bob_active.revision


def test_supersede_requires_two_active_versions_and_creates_scoped_relation(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    old = writer.confirm(
        _candidate(writer, record_id="fact-old", event_id="evt-old-observed").record_id,
        expected_revision=1,
        event_id="evt-old-confirmed",
    )
    new_candidate = _candidate(
        writer,
        record_id="fact-new",
        content="Пользователь предпочитает английский язык.",
        source_ref="turn:002",
        event_id="evt-new-observed",
    )
    new = writer.confirm(
        new_candidate.record_id,
        expected_revision=new_candidate.revision,
        event_id="evt-new-confirmed",
    )

    superseded = writer.supersede(
        old.record_id,
        replacement_record_id=new.record_id,
        expected_revision=old.revision,
        expected_replacement_revision=new.revision,
        event_id="evt-superseded",
    )
    assert superseded.state is MemoryState.SUPERSEDED
    assert superseded.superseded_by == new.record_id
    assert [record.record_id for record in writer.list_active()] == [new.record_id]
    replacement = writer.get(new.record_id)
    assert replacement is not None
    assert replacement.relations[0].relation is MemoryRelationType.SUPERSEDES
    assert replacement.relations[0].record_id == old.record_id

    with pytest.raises(LedgerStateError, match="active record"):
        writer.supersede(
            old.record_id,
            replacement_record_id=new.record_id,
            expected_revision=superseded.revision,
            expected_replacement_revision=new.revision,
        )


def test_forget_clears_a_dangling_supersede_pointer(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    old_candidate = _candidate(writer, record_id="old", event_id="evt-old")
    old = writer.confirm(old_candidate.record_id, expected_revision=1, event_id="evt-old-confirm")
    new_candidate = _candidate(
        writer,
        record_id="new",
        source_ref="turn:new",
        content="Updated fact.",
        event_id="evt-new",
    )
    new = writer.confirm(new_candidate.record_id, expected_revision=1, event_id="evt-new-confirm")
    writer.supersede(
        old.record_id,
        replacement_record_id=new.record_id,
        expected_revision=old.revision,
        expected_replacement_revision=new.revision,
        event_id="evt-supersede",
    )

    writer.forget(new.record_id, expected_revision=new.revision, event_id="evt-forget-new")
    old_after = writer.get(old.record_id)
    assert old_after is not None
    assert old_after.state is MemoryState.SUPERSEDED
    assert old_after.superseded_by is None


def test_quarantine_expiry_and_retraction_never_reach_default_reader(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    quarantined_candidate = _candidate(writer, record_id="quarantine-me", event_id="evt-q-obs")
    quarantined = writer.quarantine(
        quarantined_candidate.record_id,
        expected_revision=quarantined_candidate.revision,
        reason_code="untrusted_tool_output",
        event_id="evt-q",
    )
    assert quarantined.state is MemoryState.QUARANTINED

    expiring_candidate = _candidate(
        writer,
        record_id="expire-me",
        source_ref="turn:expire",
        valid_until=T0 + timedelta(seconds=60),
        event_id="evt-expire-obs",
    )
    active = writer.confirm(
        expiring_candidate.record_id,
        expected_revision=expiring_candidate.revision,
        event_id="evt-expire-confirm",
    )
    assert [record.record_id for record in writer.list_active(now=T0 + timedelta(seconds=59))] == [
        active.record_id
    ]
    assert writer.expire_due(now=T0 + timedelta(seconds=59)) == []
    expired = writer.expire_due(now=T0 + timedelta(seconds=60))
    assert [record.state for record in expired] == [MemoryState.EXPIRED]
    assert writer.expire_due(now=T0 + timedelta(seconds=61)) == []

    retracted_candidate = _candidate(
        writer,
        record_id="retract-me",
        source_ref="turn:retract",
        event_id="evt-retract-obs",
    )
    retracted_active = writer.confirm(
        retracted_candidate.record_id,
        expected_revision=retracted_candidate.revision,
        event_id="evt-retract-confirm",
    )
    retracted = writer.retract(
        retracted_active.record_id,
        expected_revision=retracted_active.revision,
        reason_code="user_correction",
        event_id="evt-retract",
    )
    assert retracted.state is MemoryState.RETRACTED
    assert retracted.content_available is True
    assert writer.list_active(now=T0 + timedelta(seconds=61)) == []


def test_expire_due_skips_a_record_hard_erased_during_the_batch(tmp_path, scope_a, monkeypatch):
    path = tmp_path / "expire-race.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    other = SqliteMemoryLedger(str(path))
    writer = _writer(ledger, scope_a)
    try:
        gone = _candidate(
            writer,
            record_id="due-gone",
            source_ref="turn:due-gone",
            valid_until=T0,
            event_id="evt-due-gone",
        )
        remaining = _candidate(
            writer,
            record_id="due-remaining",
            source_ref="turn:due-remaining",
            content="Second due candidate.",
            valid_until=T0,
            event_id="evt-due-remaining",
        )
        original_expire = ledger.expire
        erased = False

        def erase_then_expire(scope, record_id, **kwargs):
            nonlocal erased
            if record_id == gone.record_id and not erased:
                erased = True
                other.erase(
                    scope,
                    gone.record_id,
                    expected_revision=gone.revision,
                    event_id="evt-due-hard-erase",
                )
            return original_expire(scope, record_id, **kwargs)

        monkeypatch.setattr(ledger, "expire", erase_then_expire)
        expired = ledger.expire_due(scope_a, now=T0)
        assert [record.record_id for record in expired] == [remaining.record_id]
        assert ledger.get(scope_a, gone.record_id) is None
        remaining_after = ledger.get(scope_a, remaining.record_id)
        assert remaining_after is not None
        assert remaining_after.state is MemoryState.EXPIRED
    finally:
        other.close()
        ledger.close()


def test_forget_by_source_erases_payload_and_related_projection_metadata(ledger, scope_a, scope_b):
    alice = _writer(ledger, scope_a)
    bob = _writer(ledger, scope_b)
    first = alice.confirm(
        _candidate(alice, record_id="first", source_ref="pdf:contract-1", event_id="evt-1").record_id,
        expected_revision=1,
        event_id="evt-1-confirm",
    )
    second_candidate = _candidate(
        alice,
        record_id="second",
        source_ref="pdf:contract-1",
        content="Второй факт из PDF.",
        event_id="evt-2",
    )
    second = alice.confirm(
        second_candidate.record_id,
        expected_revision=second_candidate.revision,
        event_id="evt-2-confirm",
    )
    bob_candidate = _candidate(
        bob,
        record_id="first",
        source_ref="pdf:contract-1",
        content="Bob private source.",
        event_id="evt-bob-1",
    )
    bob.confirm(bob_candidate.record_id, expected_revision=bob_candidate.revision, event_id="evt-bob-confirm")

    receipts = alice.forget_by_source("pdf:contract-1")
    assert [receipt.record_id for receipt in receipts] == [first.record_id, second.record_id]
    assert all(receipt.payload_deleted for receipt in receipts)
    assert alice.list_active() == []
    forgotten = alice.get(first.record_id)
    assert forgotten is not None
    assert forgotten.content is None
    assert forgotten.source_refs == ()
    assert forgotten.state is MemoryState.RETRACTED
    assert [record.content for record in bob.list_active()] == ["Bob private source."]


def test_forget_by_source_is_atomic_and_blocks_future_ingestion_in_its_scope(
    ledger,
    scope_a,
    scope_b,
    monkeypatch,
):
    alice = _writer(ledger, scope_a)
    bob = _writer(ledger, scope_b)
    first = alice.confirm(
        _candidate(
            alice,
            record_id="atomic-first",
            source_ref="pdf:revoked-source",
            event_id="evt-atomic-first",
        ).record_id,
        expected_revision=1,
        event_id="evt-atomic-first-confirm",
    )
    second_candidate = _candidate(
        alice,
        record_id="atomic-second",
        source_ref="pdf:revoked-source",
        content="Second source record.",
        event_id="evt-atomic-second",
    )
    second = alice.confirm(
        second_candidate.record_id,
        expected_revision=second_candidate.revision,
        event_id="evt-atomic-second-confirm",
    )
    original_forget_locked = ledger._forget_locked

    def fail_after_second_forget(*args, **kwargs):
        receipt = original_forget_locked(*args, **kwargs)
        if kwargs["record_id"] == "atomic-second":
            raise sqlite3.DatabaseError("injected source batch failure")
        return receipt

    monkeypatch.setattr(ledger, "_forget_locked", fail_after_second_forget)
    with pytest.raises(sqlite3.DatabaseError, match="injected source batch failure"):
        alice.forget_by_source("pdf:revoked-source")

    first_after_failure = alice.get(first.record_id)
    second_after_failure = alice.get(second.record_id)
    assert first_after_failure is not None and first_after_failure.content_available
    assert second_after_failure is not None and second_after_failure.content_available
    assert ledger._conn.execute(
        "SELECT COUNT(*) FROM memory_source_revocation_tombstones"
    ).fetchone()[0] == 0

    monkeypatch.setattr(ledger, "_forget_locked", original_forget_locked)
    receipts = alice.forget_by_source("pdf:revoked-source")
    assert [receipt.record_id for receipt in receipts] == ["atomic-first", "atomic-second"]
    with pytest.raises(LedgerStateError, match="revoked"):
        _candidate(
            alice,
            record_id="reingest-denied",
            source_ref="pdf:revoked-source",
            content="This source must stay revoked.",
            event_id="evt-reingest-denied",
        )
    bob_candidate = _candidate(
        bob,
        record_id="reingest-allowed-in-other-scope",
        source_ref="pdf:revoked-source",
        content="The other scope stays independent.",
        event_id="evt-bob-reingest",
    )
    assert bob_candidate.content_available
    source_key = ledger._conn.execute(
        "SELECT source_key FROM memory_source_revocation_tombstones"
    ).fetchone()["source_key"]
    assert "pdf:revoked-source" not in source_key


def test_forget_retries_return_the_original_erasure_receipt(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer, record_id="forget-retry", event_id="evt-forget-observed")
    active = writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-forget-confirmed",
    )

    first = writer.forget(
        active.record_id,
        expected_revision=active.revision,
        event_id="evt-forget-command",
    )
    retried = writer.forget(
        active.record_id,
        expected_revision=active.revision,
        event_id="evt-forget-command",
    )
    assert retried == first
    assert retried.payload_deleted is True
    assert writer.get(active.record_id).content is None


def test_forget_redacts_persisted_content_fingerprints_and_event_exports(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    content = "A low-entropy private preference."
    candidate = _candidate(
        writer,
        record_id="privacy-forget",
        source_ref="turn:privacy",
        content=content,
        event_id="evt-privacy-observed",
    )
    active = writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-privacy-confirmed",
    )
    legacy_content_derived_command = command_hash({
        "event_type": "observed",
        "record_id": "privacy-forget",
        "kind": "preference",
        "content_hash": content_hash(scope_a, content),
        "source_ref": "turn:privacy",
        "evidence_refs": ["turn:001:line:1"],
        "confidence": 0.8,
        "retention_policy": "default",
        "valid_from": None,
        "valid_until": None,
        "actor": "host-review",
    })
    raw_before = ledger._conn.execute(
        "SELECT content_hash, payload_hash FROM memory_events WHERE event_id = ?",
        ("evt-privacy-observed",),
    ).fetchone()
    assert raw_before["content_hash"] is None
    assert raw_before["payload_hash"] != legacy_content_derived_command

    writer.forget(
        active.record_id,
        expected_revision=active.revision,
        event_id="evt-privacy-forget",
    )
    forgotten = writer.get(active.record_id)
    assert forgotten is not None
    assert forgotten.content_hash == "0" * 64
    assert forgotten.to_dict()["content_hash"] == "0" * 64
    export = writer.export()
    assert all("content_hash" not in event for event in export["events"])

    raw_record = ledger._conn.execute(
        "SELECT content_hash FROM memory_records WHERE record_id = ?",
        (active.record_id,),
    ).fetchone()
    raw_event = ledger._conn.execute(
        "SELECT content_hash, payload_hash FROM memory_events WHERE event_id = ?",
        ("evt-privacy-observed",),
    ).fetchone()
    assert raw_record["content_hash"] == "0" * 64
    assert raw_event["content_hash"] is None
    assert raw_event["payload_hash"] != legacy_content_derived_command


def test_forget_advances_the_revision_but_preserves_the_first_retraction_time(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer, record_id="retract-then-forget", event_id="evt-rf-observed")
    active = writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-rf-confirmed",
    )
    retracted = ledger.retract(
        scope_a,
        active.record_id,
        expected_revision=active.revision,
        reason_code="pending_erasure",
        actor="host-review",
        event_id="evt-rf-retracted",
        occurred_at=T0 + timedelta(seconds=1),
    )
    forgotten = ledger.forget(
        scope_a,
        retracted.record_id,
        expected_revision=retracted.revision,
        actor="host-review",
        event_id="evt-rf-forgotten",
        occurred_at=T0 + timedelta(seconds=2),
    )
    after = writer.get(retracted.record_id)
    assert forgotten.state is MemoryState.RETRACTED
    assert after is not None
    assert after.revision == retracted.revision + 1
    assert after.retracted_at == retracted.retracted_at
    assert [event.event_type.value for event in writer.events(after.record_id)] == [
        "observed",
        "confirmed",
        "retracted",
        "forgotten",
    ]


def test_erasure_receipt_reserves_the_scope_local_event_id_namespace(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer, record_id="receipt-a", event_id="evt-receipt-a")
    active = writer.confirm(candidate.record_id, expected_revision=1, event_id="evt-receipt-a-confirm")
    retracted = writer.retract(
        active.record_id,
        expected_revision=active.revision,
        reason_code="reviewed",
        event_id="evt-receipt-a-retract",
    )
    writer.forget(
        retracted.record_id,
        expected_revision=retracted.revision,
        event_id="evt-reserved",
    )
    with pytest.raises(LedgerConflictError, match="different command"):
        _candidate(
            writer,
            record_id="receipt-b",
            source_ref="turn:receipt-b",
            content="must not reuse command id",
            event_id="evt-reserved",
        )


def test_hard_erase_receipt_also_reserves_the_scope_local_event_id_namespace(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    first = writer.confirm(
        _candidate(writer, record_id="hard-receipt-a", event_id="evt-hard-receipt-a").record_id,
        expected_revision=1,
        event_id="evt-hard-receipt-a-confirm",
    )
    second_candidate = _candidate(
        writer,
        record_id="hard-receipt-b",
        source_ref="turn:hard-receipt-b",
        content="Second hard receipt record.",
        event_id="evt-hard-receipt-b",
    )
    second = writer.confirm(
        second_candidate.record_id,
        expected_revision=second_candidate.revision,
        event_id="evt-hard-receipt-b-confirm",
    )
    writer.erase(
        first.record_id,
        expected_revision=first.revision,
        event_id="evt-shared-hard-command",
    )
    with pytest.raises(LedgerConflictError, match="hard erase command"):
        writer.forget(
            second.record_id,
            expected_revision=second.revision,
            event_id="evt-shared-hard-command",
        )


def test_erase_removes_local_record_events_payload_relations_and_is_scope_limited(tmp_path, scope_a, scope_b):
    path = tmp_path / "erase.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        alice = _writer(ledger, scope_a)
        bob = _writer(ledger, scope_b)
        candidate = _candidate(alice, record_id="erase-me", content="erase sentinel", event_id="evt-erase")
        active = alice.confirm(candidate.record_id, expected_revision=candidate.revision, event_id="evt-erase-confirm")
        bob_candidate = _candidate(
            bob,
            record_id="erase-me",
            source_ref="turn:bob",
            content="bob sentinel",
            event_id="evt-bob-erase",
        )
        bob.confirm(bob_candidate.record_id, expected_revision=bob_candidate.revision, event_id="evt-bob-erase-confirm")

        receipt = alice.erase(active.record_id, expected_revision=active.revision)
        assert receipt.events_deleted == 2
        assert ledger.get(scope_a, active.record_id) is None
        assert ledger.events(scope_a, active.record_id) == []
        assert [record.content for record in bob.list_active()] == ["bob sentinel"]
        with pytest.raises(LedgerStateError, match="erased"):
            _candidate(
                alice,
                record_id="erase-me",
                content="erase sentinel",
                event_id="evt-erase",
            )
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT content FROM memory_payloads WHERE content = ?", ("erase sentinel",)).fetchall() == []
        alice_event_rows = connection.execute(
            "SELECT * FROM memory_events WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
            (
                scope_a.correlation_id(),
                canonical_json(scope_dict(scope_a)),
                "erase-me",
            ),
        ).fetchall()
        bob_event_rows = connection.execute(
            "SELECT * FROM memory_events WHERE scope_id = ? AND scope_json = ? AND record_id = ?",
            (
                scope_b.correlation_id(),
                canonical_json(scope_dict(scope_b)),
                "erase-me",
            ),
        ).fetchall()
        assert alice_event_rows == []
        assert len(bob_event_rows) == 2
        tombstone = connection.execute("SELECT record_key FROM memory_erasure_tombstones").fetchone()
        assert tombstone is not None
        assert "erase-me" not in str(tombstone[0])
    finally:
        connection.close()


def test_hard_erase_redacts_other_records_event_references_in_its_scope(ledger, scope_a, scope_b):
    alice = _writer(ledger, scope_a)
    bob = _writer(ledger, scope_b)

    alice_old = alice.confirm(
        _candidate(alice, record_id="old", event_id="evt-alice-old").record_id,
        expected_revision=1,
        event_id="evt-alice-old-confirm",
    )
    alice_new_candidate = _candidate(
        alice,
        record_id="new",
        source_ref="turn:alice-new",
        content="Alice replacement.",
        event_id="evt-alice-new",
    )
    alice_new = alice.confirm(
        alice_new_candidate.record_id,
        expected_revision=alice_new_candidate.revision,
        event_id="evt-alice-new-confirm",
    )
    alice.supersede(
        alice_old.record_id,
        replacement_record_id=alice_new.record_id,
        expected_revision=alice_old.revision,
        expected_replacement_revision=alice_new.revision,
        event_id="evt-alice-supersede",
    )

    bob_old = bob.confirm(
        _candidate(
            bob,
            record_id="old",
            source_ref="turn:bob-old",
            content="Bob old value.",
            event_id="evt-bob-old",
        ).record_id,
        expected_revision=1,
        event_id="evt-bob-old-confirm",
    )
    bob_new_candidate = _candidate(
        bob,
        record_id="new",
        source_ref="turn:bob-new",
        content="Bob replacement.",
        event_id="evt-bob-new",
    )
    bob_new = bob.confirm(
        bob_new_candidate.record_id,
        expected_revision=bob_new_candidate.revision,
        event_id="evt-bob-new-confirm",
    )
    bob.supersede(
        bob_old.record_id,
        replacement_record_id=bob_new.record_id,
        expected_revision=bob_old.revision,
        expected_replacement_revision=bob_new.revision,
        event_id="evt-bob-supersede",
    )

    receipt = alice.erase(alice_new.record_id, expected_revision=alice_new.revision)
    alice_old_after = alice.get(alice_old.record_id)
    assert receipt.relations_deleted == 2
    assert alice_old_after is not None
    assert alice_old_after.superseded_by is None
    assert [event.related_record_id for event in alice.events(alice_old.record_id)] == [
        None,
        None,
        None,
    ]
    assert bob.events(bob_old.record_id)[-1].related_record_id == bob_new.record_id
    assert ledger._conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE related_record_id = ?",
        (alice_new.record_id,),
    ).fetchone()[0] == 1


def test_hard_erase_retries_and_blocks_an_implicit_original_command(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = writer.propose(
        kind="fact",
        content="Queued plaintext must not return.",
        source_ref="turn:queued",
        event_id="evt-queued-observed",
    )
    active = writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="evt-queued-confirmed",
    )
    first = writer.erase(
        active.record_id,
        expected_revision=active.revision,
        event_id="evt-hard-erase",
    )
    retried = writer.erase(
        active.record_id,
        expected_revision=active.revision,
        event_id="evt-hard-erase",
    )
    assert retried == first
    with pytest.raises(LedgerStateError, match="hard-erased"):
        writer.propose(
            kind="fact",
            content="Queued plaintext must not return.",
            source_ref="turn:queued",
            event_id="evt-queued-observed",
        )


def test_projection_persists_and_event_rows_cannot_be_updated(tmp_path, scope_a):
    path = tmp_path / "persist.db"
    first = SqliteMemoryLedger(str(path))
    first.setup()
    writer = _writer(first, scope_a)
    candidate = _candidate(writer, content="persist sentinel", event_id="evt-persist")
    active = writer.confirm(candidate.record_id, expected_revision=candidate.revision, event_id="evt-persist-confirm")
    first.close()

    second = SqliteMemoryLedger(str(path))
    try:
        assert second.schema_version() == 4
        persisted = second.get(scope_a, active.record_id)
        assert persisted is not None
        assert persisted.content == "persist sentinel"
        assert persisted.state is MemoryState.ACTIVE
    finally:
        second.close()

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE memory_events SET actor = 'attacker' WHERE sequence = 1")
    finally:
        connection.close()


def test_legacy_trigger_name_cannot_disable_event_update_guard(tmp_path, scope_a):
    path = tmp_path / "legacy-trigger.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute(
            "CREATE TRIGGER memory_events_reject_update BEFORE UPDATE ON legacy_marker "
            "BEGIN SELECT RAISE(ABORT, 'legacy guard'); END"
        )
        connection.commit()
    finally:
        connection.close()

    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope_a)
        _candidate(writer, record_id="trigger-check", event_id="evt-trigger")
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE memory_events SET actor = 'attacker' WHERE sequence = 1")
    finally:
        connection.close()


def test_setup_does_not_drop_a_decoy_namespaced_trigger_on_another_table(tmp_path):
    path = tmp_path / "decoy-namespaced-trigger.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO legacy_marker (value) VALUES ('original')")
        connection.execute(
            "CREATE TRIGGER protoprompt_memory_ledger_events_reject_update_v1 "
            "BEFORE UPDATE ON legacy_marker "
            "BEGIN SELECT RAISE(ABORT, 'before update on memory_events: legacy guard'); END"
        )
        connection.commit()
    finally:
        connection.close()

    ledger = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="reserved schema names"):
            ledger.setup()
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="legacy guard"):
            connection.execute("UPDATE legacy_marker SET value = 'mutated' WHERE id = 1")
        assert connection.execute("SELECT value FROM legacy_marker WHERE id = 1").fetchone()[0] == "original"
    finally:
        connection.close()


def test_setup_rejects_an_unowned_trigger_that_copies_ledger_payloads(tmp_path, scope_a):
    path = tmp_path / "unowned-payload-trigger.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    ledger.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE foreign_copy (content TEXT NOT NULL)")
        connection.execute(
            "CREATE TRIGGER foreign_payload_copy AFTER INSERT ON MEMORY_PAYLOADS "
            "BEGIN INSERT INTO foreign_copy (content) VALUES (NEW.content); END"
        )
        connection.commit()
    finally:
        connection.close()

    reopened = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="foreign_payload_copy"):
            reopened.dry_run_setup()
        with pytest.raises(LedgerStateError, match="foreign_payload_copy"):
            reopened.setup()
        with pytest.raises(LedgerStateError, match="foreign_payload_copy"):
            _candidate(_writer(reopened, scope_a), record_id="must-not-copy", event_id="evt-copy")
    finally:
        reopened.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT content FROM foreign_copy").fetchall() == []
    finally:
        connection.close()


def test_write_rechecks_the_schema_after_acquiring_the_sqlite_write_lock(tmp_path, scope_a):
    class BeginProxy:
        def __init__(self, connection, inject):
            self._connection = connection
            self._inject = inject
            self._injected = False

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE" and not self._injected:
                self._injected = True
                self._inject()
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    path = tmp_path / "write-schema-race.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE foreign_copy (content TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()

    def inject_payload_copy_trigger():
        foreign = sqlite3.connect(path)
        try:
            foreign.execute(
                "CREATE TRIGGER race_payload_copy AFTER INSERT ON MEMORY_PAYLOADS "
                "BEGIN INSERT INTO foreign_copy (content) VALUES (NEW.content); END"
            )
            foreign.commit()
        finally:
            foreign.close()

    ledger._conn = BeginProxy(ledger._conn, inject_payload_copy_trigger)
    try:
        with pytest.raises(LedgerStateError, match="race_payload_copy"):
            _candidate(
                _writer(ledger, scope_a),
                record_id="race-sentinel",
                content="RACE_EXFIL_SENTINEL",
                event_id="evt-race-sentinel",
            )
    finally:
        ledger.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT content FROM foreign_copy").fetchall() == []
    finally:
        connection.close()


def test_setup_repairs_a_poisoned_namespaced_event_update_trigger(tmp_path, scope_a):
    path = tmp_path / "poisoned-trigger.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    _candidate(_writer(ledger, scope_a), record_id="poisoned-trigger", event_id="evt-poisoned")
    ledger.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "DROP TRIGGER protoprompt_memory_ledger_events_reject_update_v1"
        )
        connection.execute(
            "CREATE TRIGGER protoprompt_memory_ledger_events_reject_update_v1 "
            "BEFORE UPDATE ON memory_events WHEN 0 "
            "BEGIN SELECT RAISE(ABORT, 'memory_events are append-only'); END"
        )
        connection.commit()
    finally:
        connection.close()

    repaired = SqliteMemoryLedger(str(path))
    try:
        repaired.setup()
    finally:
        repaired.close()

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE memory_events SET actor = 'attacker' WHERE sequence = 1")
    finally:
        connection.close()


def test_stale_transition_is_rejected_without_extra_event(ledger, scope_a):
    writer = _writer(ledger, scope_a)
    candidate = _candidate(writer)
    active = writer.confirm(candidate.record_id, expected_revision=1, event_id="evt-confirm")
    with pytest.raises(LedgerConflictError, match="revision"):
        writer.retract(
            active.record_id,
            expected_revision=1,
            reason_code="stale_command",
        )
    assert [event.event_type.value for event in writer.events(active.record_id)] == [
        "observed",
        "confirmed",
    ]
