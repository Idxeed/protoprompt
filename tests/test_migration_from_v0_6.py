"""Evidence for the non-destructive v0.6 -> 1.0 Ledger cutover path.

The v0.6.1 package did not contain a Memory Ledger.  Its SQLite vector,
session, and profile stores therefore remain legacy authoritative data rather
than rows a modern Ledger may silently adopt.  This fixture reproduces their
published v0.6.1 table shapes from the tagged source, then proves that a new
Ledger in a separate file neither changes nor imports them.  Rollback is
selection of the preserved source database, never a destructive schema
downgrade.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3

from protoprompt import SqliteStore
from protoprompt.ledger import SqliteMemoryLedger
from protoprompt.profile import SqliteProfileStore


_FIXTURE = (
    Path(__file__).parent / "fixtures" / "migration" / "v0.6"
    / "legacy-sqlite-v0.6.1.sql"
)


def _materialize_v06_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_FIXTURE.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _source_snapshot(path: Path) -> dict[str, object]:
    """Read a source database through a read-only URI without side effects."""

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return {
            "chunks": connection.execute(
                "SELECT doc_id, chunk_index, document, metadata, hex(embedding) "
                "FROM chunks ORDER BY id"
            ).fetchall(),
            "profiles": connection.execute(
                "SELECT user_id, json, version FROM profiles ORDER BY user_id"
            ).fetchall(),
            "catalog": connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall(),
        }
    finally:
        connection.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_current_readers_can_open_preserved_copy(path: Path) -> None:
    """Use a disposable copy so legacy-source byte preservation stays exact."""

    store = SqliteStore(str(path))
    try:
        document = store.get("document-v06")
        session = store.get("session-v06")
        assert document is not None
        assert document["document"].startswith("Legacy PDF policy")
        assert document["metadata"]["name"] == "legacy-policy.pdf"
        assert session is not None
        assert session["metadata"]["kind"] == "session"
    finally:
        store.close()

    profiles = SqliteProfileStore(str(path))
    try:
        profile = profiles.get("legacy-user")
        assert profile is not None
        assert profile.version == 3
        assert profile.preferences.language == "ru"
        assert profile.facts == {"city": "Химки"}
    finally:
        profiles.close()


def test_v06_sqlite_cutover_keeps_legacy_source_untouched_and_rolls_back_by_selection(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "protoprompt-v0.6.1-source.db"
    _materialize_v06_source(source_path)
    before_snapshot = _source_snapshot(source_path)
    before_digest = _digest(source_path)

    ledger_path = tmp_path / "protoprompt-ledger-v1.db"
    ledger = SqliteMemoryLedger(str(ledger_path))
    try:
        assert ledger.dry_run_setup() == {
            "component": "memory_ledger",
            "from_version": 0,
            "to_version": 7,
            "changes_required": True,
            "actions": ["create isolated memory-ledger tables"],
        }
        ledger.setup()
        assert ledger.schema_version() == 7

        # Cutover never interprets vector/session/profile rows as admitted
        # Ledger records. A host must explicitly create and review any new
        # Ledger record after it has decided what legacy data is appropriate.
        row = ledger._conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()
        assert row is not None
        assert int(row[0]) == 0
        ledger.backup(str(tmp_path / "protoprompt-ledger-v1.backup.db"))
    finally:
        ledger.close()

    assert _digest(source_path) == before_digest
    assert _source_snapshot(source_path) == before_snapshot
    assert not any(
        str(name).startswith("memory_")
        for _kind, name, _table, _sql in before_snapshot["catalog"]
    )

    # A rollback retains the original file and selects it again. The new
    # Ledger file can be quarantined separately; nothing downgrades its v7
    # schema into the legacy source file.
    rollback_path = tmp_path / "rollback-v0.6.1-source.db"
    shutil.copy2(source_path, rollback_path)
    assert _digest(rollback_path) == before_digest
    assert _source_snapshot(rollback_path) == before_snapshot
    _assert_current_readers_can_open_preserved_copy(rollback_path)

    # Reader startup writes only to the disposable rollback copy (for example
    # its normal WAL choice), never to the preserved original source.
    assert _digest(source_path) == before_digest
    assert _source_snapshot(source_path) == before_snapshot
