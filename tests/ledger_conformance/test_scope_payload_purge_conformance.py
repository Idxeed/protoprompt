"""SQLite execution wrapper for the shared exact-scope purge contract."""

from __future__ import annotations

from ledger_conformance.scope_payload_purge import (
    SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS,
    run_scope_payload_purge_conformance_v1,
)
from protoprompt.ledger import SqliteMemoryLedger


def test_sqlite_scope_payload_purge_conformance(tmp_path) -> None:
    path = tmp_path / "ledger-scope-payload-purge-conformance.db"

    def reopenable_sqlite() -> SqliteMemoryLedger:
        return SqliteMemoryLedger(str(path))

    report = run_scope_payload_purge_conformance_v1(reopenable_sqlite)

    assert report == {
        "report_schema_version": 1,
        "contract_id": "memory_writer_scope_payload_purge",
        "contract_version": 1,
        "check_ids": list(SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS),
        "passed_check_count": len(SCOPE_PAYLOAD_PURGE_CONFORMANCE_V1_CHECK_IDS),
        "status": "passed",
    }
