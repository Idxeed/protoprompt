"""SQLite regression coverage for the public exact-scope payload purge API."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from protoprompt.ledger import (
    LedgerConflictError,
    LedgerStateError,
    MemoryAdmissionPolicy,
    MemoryEventType,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryWriter,
    SqliteMemoryLedger,
    StaleMemoryReviewError,
)
from protoprompt.ledger.recall import (
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.ledger.types import canonical_json, scope_dict
from protoprompt.scope import MemoryScope


_NOW = datetime(2041, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def _writer(ledger: SqliteMemoryLedger, scope: MemoryScope) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=scope,
        actor="scope-purge-host",
        clock=lambda: _NOW,
    )


def _candidate(
    writer: MemoryWriter,
    *,
    record_id: str,
    content: str,
    source_ref: str | None = None,
) -> object:
    return writer.propose(
        kind=MemoryKind.FACT,
        content=content,
        source_ref=source_ref or f"source:{record_id}",
        evidence_refs=(f"evidence:{record_id}",),
        confidence=0.9,
        record_id=record_id,
        event_id=f"observe:{record_id}",
    )


def _scope_storage(scope: MemoryScope) -> tuple[str, str]:
    """Rebuild the exact private storage key for controlled DB assertions."""

    return scope.correlation_id(), canonical_json(scope_dict(scope))


def test_purge_payloads_covers_every_payload_bearing_state_in_exact_scope(
    tmp_path,
):
    """One transaction removes all live payloads without widening to a sibling."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-states.db"))
    ledger.setup()
    try:
        target_scope = MemoryScope(
            tenant="tenant-purge-target",
            user="user-purge-target",
            thread="thread-purge-target",
            kind="memory-purge-target",
        )
        sibling_scope = MemoryScope(
            tenant="tenant-purge-sibling",
            user="user-purge-sibling",
            thread="thread-purge-sibling",
            kind="memory-purge-sibling",
        )
        writer = _writer(ledger, target_scope)
        sibling = _writer(ledger, sibling_scope)

        candidate = _candidate(
            writer,
            record_id="state-candidate",
            content="CONTENT-MUST-NOT-LEAK-candidate",
        )
        active_candidate = _candidate(
            writer,
            record_id="state-active",
            content="CONTENT-MUST-NOT-LEAK-active",
        )
        active = writer.confirm(
            active_candidate.record_id,
            expected_revision=active_candidate.revision,
            event_id="confirm:state-active",
        )
        superseded_candidate = _candidate(
            writer,
            record_id="state-superseded",
            content="CONTENT-MUST-NOT-LEAK-superseded",
        )
        superseded_active = writer.confirm(
            superseded_candidate.record_id,
            expected_revision=superseded_candidate.revision,
            event_id="confirm:state-superseded",
        )
        replacement_candidate = _candidate(
            writer,
            record_id="state-replacement",
            content="CONTENT-MUST-NOT-LEAK-replacement",
        )
        replacement = writer.confirm(
            replacement_candidate.record_id,
            expected_revision=replacement_candidate.revision,
            event_id="confirm:state-replacement",
        )
        superseded = writer.supersede(
            superseded_active.record_id,
            replacement_record_id=replacement.record_id,
            expected_revision=superseded_active.revision,
            expected_replacement_revision=replacement.revision,
            event_id="supersede:state-superseded",
        )
        retracted_candidate = _candidate(
            writer,
            record_id="state-retracted",
            content="CONTENT-MUST-NOT-LEAK-retracted",
        )
        retracted_active = writer.confirm(
            retracted_candidate.record_id,
            expected_revision=retracted_candidate.revision,
            event_id="confirm:state-retracted",
        )
        retracted = writer.retract(
            retracted_active.record_id,
            expected_revision=retracted_active.revision,
            reason_code="host-correction",
            event_id="retract:state-retracted",
        )
        expired_candidate = _candidate(
            writer,
            record_id="state-expired",
            content="CONTENT-MUST-NOT-LEAK-expired",
        )
        expired = writer.expire(
            expired_candidate.record_id,
            expected_revision=expired_candidate.revision,
            event_id="expire:state-expired",
        )
        quarantined_candidate = _candidate(
            writer,
            record_id="state-quarantined",
            content="CONTENT-MUST-NOT-LEAK-quarantined",
        )
        quarantined = writer.quarantine(
            quarantined_candidate.record_id,
            expected_revision=quarantined_candidate.revision,
            reason_code="host-review-needed",
            event_id="quarantine:state-quarantined",
        )

        sibling_record = _candidate(
            sibling,
            record_id="state-candidate",
            content="CONTENT-MUST-STAY-IN-SIBLING",
            source_ref="source:sibling-only",
        )
        records = (
            candidate,
            active,
            superseded,
            replacement,
            retracted,
            expired,
            quarantined,
        )
        assert {record.state for record in records} == {
            MemoryState.CANDIDATE,
            MemoryState.ACTIVE,
            MemoryState.SUPERSEDED,
            MemoryState.RETRACTED,
            MemoryState.EXPIRED,
            MemoryState.QUARANTINED,
        }
        assert writer.payload_readback().payload_record_count == len(records)
        assert sibling.payload_readback().payload_record_count == 1

        receipt = writer.purge_payloads("purge-all-states")

        assert receipt.records_forgotten == len(records)
        assert receipt.payload_rows_deleted == len(records)
        assert receipt.source_refs_deleted == len(records)
        assert receipt.relations_deleted >= 1
        assert receipt.readback.is_empty
        assert writer.payload_readback().is_empty
        for record in records:
            after = writer.get(record.record_id)
            assert after is not None
            assert after.state is MemoryState.RETRACTED
            assert after.content is None
            assert after.content_available is False

        # The sibling uses the same logical ID as one target record, proving
        # that the public writer's pinned scope is the deletion boundary.
        sibling_after = sibling.get(sibling_record.record_id)
        assert sibling_after is not None
        assert sibling_after.content == "CONTENT-MUST-STAY-IN-SIBLING"
        assert sibling.payload_readback().payload_record_count == 1

        # The public receipt is intentionally aggregate-only: it must not
        # become an accidental content, source, record-ID, or scope export.
        serialized = json.dumps(receipt.to_dict(), sort_keys=True)
        for forbidden in (
            "CONTENT-MUST-NOT-LEAK-candidate",
            "CONTENT-MUST-NOT-LEAK-active",
            "state-candidate",
            "source:state-candidate",
            "evidence:state-candidate",
            target_scope.tenant,
            target_scope.user,
            target_scope.thread,
            target_scope.kind,
        ):
            assert forbidden not in serialized
    finally:
        ledger.close()


def test_completed_operation_retries_after_reopen_without_purging_later_payload(
    tmp_path,
):
    """A durable receipt binds one completed operation to its original scope."""

    path = tmp_path / "scope-purge-retry.db"
    scope = MemoryScope(tenant="retry-tenant", user="retry-user", thread="retry-thread")
    first_ledger = SqliteMemoryLedger(str(path))
    first_ledger.setup()
    try:
        first_writer = _writer(first_ledger, scope)
        _candidate(
            first_writer,
            record_id="before-restart",
            content="initial payload removed by original operation",
        )
        first_receipt = first_writer.purge_payloads("operation-survives-restart")
        assert first_receipt.records_forgotten == 1
        assert first_receipt.readback.is_empty
    finally:
        first_ledger.close()

    reopened = SqliteMemoryLedger(str(path))
    reopened.setup()
    try:
        writer = _writer(reopened, scope)
        later = _candidate(
            writer,
            record_id="created-after-original-operation",
            content="later payload must not be selected by an old receipt",
        )

        retry = writer.purge_payloads("operation-survives-restart")

        assert retry == first_receipt
        assert retry.readback.is_empty  # snapshot sealed at the original commit
        later_after = writer.get(later.record_id)
        assert later_after is not None
        assert later_after.content == "later payload must not be selected by an old receipt"
        assert writer.payload_readback().payload_record_count == 1

        with pytest.raises(LedgerConflictError, match="operation_id"):
            writer.purge_payloads(
                "operation-survives-restart",
                reason_code="different-reason-is-command-drift",
            )
        assert writer.payload_readback().payload_record_count == 1
    finally:
        reopened.close()


def test_same_opaque_operation_id_is_isolated_by_the_writer_scope(tmp_path):
    """The durable key is exact-scope, while hosts still mint IDs globally."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-same-operation-id.db"))
    ledger.setup()
    try:
        first = _writer(
            ledger,
            MemoryScope(tenant="same-operation", user="first", thread="purge"),
        )
        second = _writer(
            ledger,
            MemoryScope(tenant="same-operation", user="second", thread="purge"),
        )
        _candidate(first, record_id="shared-id", content="first exact-scope payload")
        _candidate(second, record_id="shared-id", content="second exact-scope payload")

        first_receipt = first.purge_payloads("same-host-operation-id")
        second_receipt = second.purge_payloads("same-host-operation-id")

        assert first_receipt.records_forgotten == second_receipt.records_forgotten == 1
        assert first_receipt.scope_fingerprint != second_receipt.scope_fingerprint
        assert first.payload_readback().is_empty
        assert second.payload_readback().is_empty
    finally:
        ledger.close()


def test_purge_makes_admission_review_stale_and_cannot_resurrect_candidate(tmp_path):
    """A review minted before deletion cannot later turn erased payload active."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-admission.db"))
    ledger.setup()
    try:
        scope = MemoryScope(tenant="admission-tenant", user="admission-user", thread="review")
        writer = _writer(ledger, scope)
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="source:admission-candidate",
            evidence_refs=("evidence:admission-candidate",),
            confidence=0.95,
            asserted=True,
        ).submit("candidate payload must not be resurrected after purge")
        review = gate.review(candidate.record_id)

        receipt = writer.purge_payloads("purge-before-admission-confirm")
        assert receipt.records_forgotten == 1
        with pytest.raises(StaleMemoryReviewError, match="no longer eligible"):
            gate.review(candidate.record_id)
        with pytest.raises(LedgerConflictError, match="candidate changed"):
            gate.confirm(review, event_id="confirm-after-purge-must-fail")

        after = writer.get(candidate.record_id)
        assert after is not None
        assert after.state is MemoryState.RETRACTED
        assert after.content is None
        assert writer.list_active() == []
        assert MemoryEventType.CONFIRMED not in {
            event.event_type for event in writer.events(candidate.record_id)
        }
    finally:
        ledger.close()


def test_purge_invalidates_a_selected_checkpoint_within_the_same_write_boundary(tmp_path):
    """A receipt cannot coexist with a resumable checkpoint for erased data."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-checkpoint.db"))
    ledger.setup()
    try:
        scope = MemoryScope(tenant="checkpoint-tenant", user="checkpoint-user", thread="purge")
        writer = _writer(ledger, scope)
        policy = MemoryAdmissionPolicy(
            policy_id="scope-purge-checkpoint-document-policy",
            policy_version="1",
            allowed_origins=(MemoryOrigin.DOCUMENT,),
            minimum_confidence=0.5,
        )
        gate = MemoryReviewGate(writer, origin=MemoryOrigin.DOCUMENT, policy=policy)
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="source:checkpoint-document",
            evidence_refs=("evidence:checkpoint-document",),
            confidence=0.95,
        ).submit("checkpoint-selected payload is erased by the scope purge")
        active = gate.confirm(gate.review(candidate.record_id), event_id="confirm:checkpoint")
        planner = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.admission_safe_default(),
            checkpoint_secret=b"p" * 32,
            clock=lambda: _NOW,
        )
        plan = planner.plan(task="checkpoint selected payload", token_budget=512, byte_budget=4096)
        checkpoint = planner.checkpoint(
            plan,
            checkpoint_id="scope-purge-checkpoint",
            continuation_ref="scope-purge-continuation",
        )
        assert checkpoint.selected_count == 1

        receipt = writer.purge_payloads("purge-selected-checkpoint")

        assert receipt.records_forgotten == 1
        erased = writer.get(active.record_id)
        assert erased is not None and erased.content is None
        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            planner.resume_checkpoint(
                "scope-purge-checkpoint",
                task="checkpoint selected payload",
            )
    finally:
        ledger.close()


def test_purge_rolls_back_every_record_and_receipt_when_one_forget_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A mid-batch exception leaves no partial lifecycle or receipt evidence."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-rollback.db"))
    ledger.setup()
    try:
        scope = MemoryScope(tenant="rollback-tenant", user="rollback-user", thread="all-or-none")
        writer = _writer(ledger, scope)
        first = _candidate(
            writer,
            record_id="atomic-first",
            content="first payload remains after injected failure",
        )
        second = _candidate(
            writer,
            record_id="atomic-second",
            content="second payload remains after injected failure",
        )
        original_forget_locked = ledger._forget_locked
        calls = 0

        def fail_on_second_forget(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected scope purge failure")
            return original_forget_locked(*args, **kwargs)

        monkeypatch.setattr(ledger, "_forget_locked", fail_on_second_forget)
        with pytest.raises(RuntimeError, match="injected scope purge failure"):
            writer.purge_payloads("purge-must-be-atomic")

        assert writer.payload_readback().payload_record_count == 2
        for record in (first, second):
            after = writer.get(record.record_id)
            assert after is not None
            assert after.state is MemoryState.CANDIDATE
            assert after.content is not None
            assert len(writer.events(record.record_id)) == 1

        # This is a controlled direct assertion: a failed transaction must not
        # leave a replay receipt that could disguise the incomplete purge.
        scope_id, scope_json = _scope_storage(scope)
        receipt_count = ledger._conn.execute(
            "SELECT COUNT(*) FROM memory_scope_payload_purge_receipts "
            "WHERE scope_id = ? AND scope_json = ?",
            (scope_id, scope_json),
        ).fetchone()[0]
        assert receipt_count == 0

        monkeypatch.setattr(ledger, "_forget_locked", original_forget_locked)
        retry = writer.purge_payloads("purge-must-be-atomic")
        assert retry.records_forgotten == 2
        assert writer.payload_readback().is_empty
    finally:
        ledger.close()


def test_payload_readback_and_purge_fail_closed_on_an_orphan_payload_row(tmp_path):
    """An invalid row cannot turn a scope-wide deletion claim into a false zero."""

    ledger = SqliteMemoryLedger(str(tmp_path / "scope-purge-orphan.db"))
    ledger.setup()
    try:
        scope = MemoryScope(tenant="orphan-tenant", user="orphan-user", thread="orphan")
        writer = _writer(ledger, scope)
        scope_id, scope_json = _scope_storage(scope)
        # ``memory_payloads`` deliberately has no DB-level FK so historic
        # SQLite files edited with constraints disabled are a realistic repair
        # boundary.  The public command must stop rather than skip the row.
        ledger._conn.execute(
            "INSERT INTO memory_payloads "
            "(scope_id, scope_json, record_id, content, source_refs_json, evidence_refs_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                scope_id,
                scope_json,
                "orphan-payload-record",
                "must not be silently skipped",
                "[]",
                "[]",
            ),
        )
        ledger._conn.commit()

        with pytest.raises(LedgerStateError, match="orphaned"):
            writer.payload_readback()
        with pytest.raises(LedgerStateError, match="orphaned"):
            writer.purge_payloads("orphan-must-not-receive-a-receipt")
        assert ledger._conn.execute(
            "SELECT COUNT(*) FROM memory_scope_payload_purge_receipts "
            "WHERE scope_id = ? AND scope_json = ?",
            (scope_id, scope_json),
        ).fetchone()[0] == 0
    finally:
        ledger.close()
