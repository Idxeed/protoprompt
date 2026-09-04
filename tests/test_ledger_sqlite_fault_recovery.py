"""Crash-recovery conformance for SQLite Ledger write units.

These are deliberately process-boundary tests rather than mock exceptions.
The worker terminates with :func:`os._exit` from SQLite's trace callback after
one operation has performed several writes but before its enclosing
``BEGIN IMMEDIATE`` can commit.  A new Ledger connection must therefore see
the exact pre-command state; it must never inherit an event, tombstone,
receipt, checkpoint state, or other sidecar from only part of the command.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import pytest

from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import (
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.scope import MemoryScope


ROOT = Path(__file__).resolve().parents[1]
_CRASH_EXIT = 86
_T0 = datetime(2039, 4, 5, 6, 7, 8, tzinfo=timezone.utc)


# Keep the worker self-contained.  In particular, a normal ``close()`` or a
# Python exception would exercise the Ledger's exception handler rather than
# SQLite's process-death recovery path that an operator needs to trust.
_CRASH_WORKER = r'''
from datetime import datetime, timezone
import os
import sys

from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.scope import MemoryScope


EXIT = 86
T0 = datetime(2039, 4, 5, 6, 7, 8, tzinfo=timezone.utc)
path, case = sys.argv[1:3]
scope = MemoryScope(tenant="fault", user="alice", thread="sqlite")
ledger = SqliteMemoryLedger(path)
ledger.setup()
writer = MemoryWriter(ledger, scope=scope, actor="fault-host", clock=lambda: T0)


def active(record_id, source_ref):
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content="fault-recovery payload for " + record_id,
        source_ref=source_ref,
        evidence_refs=("evidence:" + record_id,),
        confidence=0.9,
        record_id=record_id,
        event_id="observe-" + record_id,
    )
    return writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="confirm-" + record_id,
    )


def abort_on(marker):
    def trace(statement):
        normalized = " ".join(statement.upper().split())
        if marker in normalized:
            # Do not raise: the enclosing context manager would catch it and
            # roll back normally.  This models a process/connection death.
            os._exit(EXIT)

    ledger._conn.set_trace_callback(trace)


if case == "observe":
    abort_on("INSERT INTO MEMORY_PAYLOADS")
    writer.propose(
        kind=MemoryKind.FACT,
        content="new record must not partially survive",
        source_ref="source:observe",
        evidence_refs=("evidence:observe",),
        confidence=0.9,
        record_id="observe-record",
        event_id="observe-record-event",
    )
elif case == "transition":
    record = active("transition-record", "source:transition")
    abort_on("UPDATE MEMORY_RECORDS SET STATE")
    writer.retract(
        record.record_id,
        expected_revision=record.revision,
        reason_code="fault-retract",
        event_id="transition-retract-event",
    )
elif case == "forget_by_source":
    active("source-first", "source:revoked")
    active("source-second", "source:revoked")
    abort_on("DELETE FROM MEMORY_PAYLOADS")
    writer.forget_by_source("source:revoked")
elif case == "hard_erase":
    record = active("erase-record", "source:erase")
    # This delete occurs after payload/source deletion, replay tombstones, and
    # event-reference scrubbing, so it proves those preceding changes are one
    # all-or-nothing write unit too.
    abort_on("DELETE FROM MEMORY_EVENTS WHERE")
    writer.erase(
        record.record_id,
        expected_revision=record.revision,
        event_id="hard-erase-event",
    )
elif case == "checkpoint_invalidation":
    candidate = writer._assert_candidate_with_origin(
        origin=MemoryOrigin.HOST_ASSERTION,
        kind=MemoryKind.FACT,
        content="checkpoint selection must remain coherent",
        source_ref="source:checkpoint",
        evidence_refs=("evidence:checkpoint",),
        confidence=0.9,
        record_id="checkpoint-record",
        event_id="checkpoint-observed",
    )
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.HOST_ASSERTION,
        policy=MemoryAdmissionPolicy.safe_default(),
    )
    record = gate.confirm(gate.review(candidate.record_id), event_id="checkpoint-confirmed")
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        checkpoint_secret=b"c" * 32,
        clock=lambda: T0,
    )
    plan = planner.plan(task="checkpoint recovery", token_budget=512, byte_budget=4096)
    planner.checkpoint(
        plan,
        checkpoint_id="fault-checkpoint",
        continuation_ref="fault-continuation",
    )
    # The checkpoint state UPDATE has already happened when this later
    # statement is traced.  A fresh process must not see invalidated state
    # paired with retained selections, or the reverse.
    abort_on("DELETE FROM MEMORY_RECALL_CHECKPOINT_SELECTIONS")
    writer.retract(
        record.record_id,
        expected_revision=record.revision,
        reason_code="fault-checkpoint-retract",
        event_id="checkpoint-retract-event",
    )
elif case in {
    "scope_payload_purge",
    "scope_payload_purge_receipt_insert",
    "scope_payload_purge_after_commit",
}:
    active("scope-purge-first", "source:scope-purge:first")
    active("scope-purge-second", "source:scope-purge:second")
    if case == "scope_payload_purge":
        # This happens after the first lifecycle event has been appended but
        # before the encompassing aggregate receipt can be inserted/committed.
        abort_on("DELETE FROM MEMORY_PAYLOADS")
    elif case == "scope_payload_purge_receipt_insert":
        # Every per-record lifecycle transition and checkpoint invalidation has
        # already run at this point.  The aggregate receipt is deliberately
        # written last, so its failure must still roll the entire batch back.
        abort_on("INSERT INTO MEMORY_SCOPE_PAYLOAD_PURGE_RECEIPTS")
    writer.purge_payloads("fault-scope-purge-operation")
    if case == "scope_payload_purge_after_commit":
        # Model a host process dying after the transaction committed but before
        # it could return the receipt over its own RPC/request boundary.
        os._exit(EXIT)
else:
    raise SystemExit("unknown fault case: " + case)
'''


def _scope() -> MemoryScope:
    return MemoryScope(tenant="fault", user="alice", thread="sqlite")


def _writer(ledger: SqliteMemoryLedger) -> MemoryWriter:
    return MemoryWriter(ledger, scope=_scope(), actor="fault-host", clock=lambda: _T0)


def _run_crash_worker(path: Path, case: str) -> None:
    environment = os.environ.copy()
    original_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT)
        if not original_pythonpath
        else str(ROOT) + os.pathsep + original_pythonpath
    )
    completed = subprocess.run(
        [sys.executable, "-c", _CRASH_WORKER, str(path), case],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == _CRASH_EXIT, (
        f"fault worker for {case!r} did not terminate at its SQL crash point\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _table_count(ledger: SqliteMemoryLedger, table: str) -> int:
    # Table names are static test constants, never test input.
    return int(ledger._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _active(writer: MemoryWriter, *, record_id: str, source_ref: str):
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content="fault-recovery payload for " + record_id,
        source_ref=source_ref,
        evidence_refs=("evidence:" + record_id,),
        confidence=0.9,
        record_id=record_id,
        event_id="observe-" + record_id,
    )
    return writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="confirm-" + record_id,
    )


def test_observe_connection_abort_recovers_no_partial_creation_sidecars(tmp_path):
    path = tmp_path / "observe-abort.db"
    _run_crash_worker(path, "observe")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        assert writer.get("observe-record") is None
        assert {
            table: _table_count(ledger, table)
            for table in (
                "memory_events",
                "memory_records",
                "memory_payloads",
                "memory_sources",
                "memory_record_admission_metadata",
            )
        } == {
            "memory_events": 0,
            "memory_records": 0,
            "memory_payloads": 0,
            "memory_sources": 0,
            "memory_record_admission_metadata": 0,
        }

        # The uncommitted event identity did not become a replay barrier.
        retried = writer.propose(
            kind=MemoryKind.FACT,
            content="new record must not partially survive",
            source_ref="source:observe",
            evidence_refs=("evidence:observe",),
            confidence=0.9,
            record_id="observe-record",
            event_id="observe-record-event",
        )
        assert retried.content_available
    finally:
        ledger.close()


def test_lifecycle_transition_connection_abort_recovers_exact_pretransition_state(tmp_path):
    path = tmp_path / "transition-abort.db"
    _run_crash_worker(path, "transition")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        record = writer.get("transition-record")
        assert record is not None
        assert record.state is MemoryState.ACTIVE
        assert record.revision == 2
        assert record.content_available
        assert [event.event_type.value for event in writer.events(record.record_id)] == [
            "observed",
            "confirmed",
        ]

        retracted = writer.retract(
            record.record_id,
            expected_revision=record.revision,
            reason_code="fault-retract",
            event_id="transition-retract-event",
        )
        assert retracted.state is MemoryState.RETRACTED
    finally:
        ledger.close()


def test_forget_by_source_connection_abort_recovers_batch_and_revocation_tombstone(tmp_path):
    path = tmp_path / "forget-source-abort.db"
    _run_crash_worker(path, "forget_by_source")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        for record_id in ("source-first", "source-second"):
            record = writer.get(record_id)
            assert record is not None
            assert record.state is MemoryState.ACTIVE
            assert record.revision == 2
            assert record.content_available
            assert [event.event_type.value for event in writer.events(record_id)] == [
                "observed",
                "confirmed",
            ]
        assert _table_count(ledger, "memory_source_revocation_tombstones") == 0
        assert _table_count(ledger, "memory_erasure_receipts") == 0
        assert _table_count(ledger, "memory_payloads") == 2
        assert _table_count(ledger, "memory_sources") == 2

        receipts = writer.forget_by_source("source:revoked")
        assert [receipt.record_id for receipt in receipts] == ["source-first", "source-second"]
        assert _table_count(ledger, "memory_source_revocation_tombstones") == 1
    finally:
        ledger.close()


def test_hard_erase_connection_abort_recovers_primary_rows_and_all_erasure_sidecars(tmp_path):
    path = tmp_path / "hard-erase-abort.db"
    _run_crash_worker(path, "hard_erase")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        record = writer.get("erase-record")
        assert record is not None
        assert record.state is MemoryState.ACTIVE
        assert record.revision == 2
        assert record.content_available
        assert [event.event_type.value for event in writer.events(record.record_id)] == [
            "observed",
            "confirmed",
        ]
        assert {
            table: _table_count(ledger, table)
            for table in (
                "memory_erasure_tombstones",
                "memory_erased_event_tombstones",
                "memory_hard_erase_receipts",
            )
        } == {
            "memory_erasure_tombstones": 0,
            "memory_erased_event_tombstones": 0,
            "memory_hard_erase_receipts": 0,
        }

        receipt = writer.erase(
            record.record_id,
            expected_revision=record.revision,
            event_id="hard-erase-event",
        )
        assert receipt.payload_deleted
        assert writer.get(record.record_id) is None
    finally:
        ledger.close()


def test_checkpoint_invalidation_connection_abort_preserves_coherent_active_manifest(tmp_path):
    path = tmp_path / "checkpoint-invalidation-abort.db"
    _run_crash_worker(path, "checkpoint_invalidation")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        record = writer.get("checkpoint-record")
        assert record is not None
        assert record.state is MemoryState.ACTIVE
        assert record.revision == 2
        assert [event.event_type.value for event in writer.events(record.record_id)] == [
            "asserted",
            "confirmed",
        ]
        row = ledger._conn.execute(
            "SELECT state, invalidated_at, invalidation_reason, selected_count "
            "FROM memory_recall_checkpoints WHERE checkpoint_id = ?",
            ("fault-checkpoint",),
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "state": "active",
            "invalidated_at": None,
            "invalidation_reason": None,
            "selected_count": 1,
        }
        assert _table_count(ledger, "memory_recall_checkpoint_selections") == 1

        planner = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.admission_safe_default(),
            checkpoint_secret=b"c" * 32,
            clock=lambda: _T0,
        )
        resumed = planner.resume_checkpoint("fault-checkpoint", task="checkpoint recovery")
        assert resumed.continuation_ref == "fault-continuation"

        writer.retract(
            record.record_id,
            expected_revision=record.revision,
            reason_code="fault-checkpoint-retract",
            event_id="checkpoint-retract-event",
        )
        row = ledger._conn.execute(
            "SELECT state, invalidated_at, invalidation_reason, selected_count "
            "FROM memory_recall_checkpoints WHERE checkpoint_id = ?",
            ("fault-checkpoint",),
        ).fetchone()
        assert row is not None
        assert row["state"] == "invalidated"
        assert row["invalidated_at"] is not None
        assert row["invalidation_reason"] == "selected_record_changed"
        assert row["selected_count"] == 1
        assert _table_count(ledger, "memory_recall_checkpoint_selections") == 0
        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            planner.resume_checkpoint("fault-checkpoint", task="checkpoint recovery")
    finally:
        ledger.close()


def test_scope_payload_purge_connection_abort_rolls_back_every_affected_record_and_receipt(tmp_path):
    path = tmp_path / "scope-payload-purge-abort.db"
    _run_crash_worker(path, "scope_payload_purge")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        assert writer.payload_readback().payload_record_count == 2
        for record_id in ("scope-purge-first", "scope-purge-second"):
            record = writer.get(record_id)
            assert record is not None
            assert record.state is MemoryState.ACTIVE
            assert record.revision == 2
            assert record.content_available
            assert [event.event_type.value for event in writer.events(record_id)] == [
                "observed",
                "confirmed",
            ]
        assert _table_count(ledger, "memory_scope_payload_purge_receipts") == 0

        receipt = writer.purge_payloads("fault-scope-purge-operation")
        assert receipt.records_forgotten == 2
        assert receipt.readback.is_empty
    finally:
        ledger.close()


def test_scope_payload_purge_receipt_insert_abort_rolls_back_prior_batch_mutations(tmp_path):
    path = tmp_path / "scope-payload-purge-receipt-abort.db"
    _run_crash_worker(path, "scope_payload_purge_receipt_insert")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        assert writer.payload_readback().payload_record_count == 2
        for record_id in ("scope-purge-first", "scope-purge-second"):
            record = writer.get(record_id)
            assert record is not None
            assert record.state is MemoryState.ACTIVE
            assert record.revision == 2
            assert record.content_available
            assert [event.event_type.value for event in writer.events(record_id)] == [
                "observed",
                "confirmed",
            ]
        assert _table_count(ledger, "memory_scope_payload_purge_receipts") == 0
    finally:
        ledger.close()


def test_scope_payload_purge_postcommit_crash_replays_durable_receipt_without_new_mutation(tmp_path):
    path = tmp_path / "scope-payload-purge-postcommit.db"
    _run_crash_worker(path, "scope_payload_purge_after_commit")

    ledger = SqliteMemoryLedger(str(path))
    try:
        writer = _writer(ledger)
        assert writer.payload_readback().is_empty
        assert _table_count(ledger, "memory_scope_payload_purge_receipts") == 1
        first_event_counts = {
            record_id: len(writer.events(record_id))
            for record_id in ("scope-purge-first", "scope-purge-second")
        }
        receipt = writer.purge_payloads("fault-scope-purge-operation")
        assert receipt.records_forgotten == 2
        assert receipt.readback.is_empty
        assert {
            record_id: len(writer.events(record_id))
            for record_id in ("scope-purge-first", "scope-purge-second")
        } == first_event_counts
    finally:
        ledger.close()
