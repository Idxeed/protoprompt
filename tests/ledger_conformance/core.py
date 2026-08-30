"""Shared, non-collected conformance checks for Memory Ledger backends.

The helpers deliberately exercise only the public host-facing Ledger/Writer
contract.  They know nothing about SQLite files, tables, triggers, SQL, or a
future PostgreSQL implementation.  Backend-specific migration, transaction,
and schema-tamper tests remain next to their respective adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
from typing import Any

import pytest

from protoprompt.ledger import (
    LedgerConflictError,
    LedgerStateError,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryState,
    MemoryTrust,
    MemoryWriter,
)
from protoprompt.ledger.recall import (
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


T0 = datetime(2039, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"ledger-conformance-checkpoint-secret-v1"
LedgerFactory = Callable[[], Any]


def _writer(ledger: Any, scope: MemoryScope) -> MemoryWriter:
    """Create one deterministic, scope-pinned host writer for a backend."""

    return MemoryWriter(
        ledger,
        scope=scope,
        actor="ledger-conformance-host",
        clock=lambda: T0,
    )


def _opened(factory: LedgerFactory) -> Any:
    ledger = factory()
    ledger.setup()
    return ledger


def _document_policy() -> MemoryAdmissionPolicy:
    """Return the fixed strict document policy used by parity checks."""

    return MemoryAdmissionPolicy(
        policy_id="ledger-conformance-document-policy-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        minimum_confidence=0.5,
    )


def _admitted_document(
    writer: MemoryWriter,
    *,
    content: str,
    source_ref: str,
):
    """Store one document through the public admission gate only."""

    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.DOCUMENT,
        policy=_document_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.FACT,
        source_ref=source_ref,
        evidence_refs=(f"{source_ref}:evidence",),
        confidence=0.9,
    ).submit(content)
    return gate, candidate


def _strict_planner(writer: MemoryWriter) -> LedgerRecallPlanner:
    """Create the restart-safe strict planner used by checkpoint checks."""

    return LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=RegexTokenCounter(),
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )


def assert_candidate_confirmation_and_content_free_events(factory: LedgerFactory) -> None:
    """Candidates must stay out of active recall until host confirmation."""

    ledger = _opened(factory)
    try:
        scope = MemoryScope(tenant="conformance", user="alice", thread="candidate")
        writer = _writer(ledger, scope)
        content = "Candidate content must not reach default active recall."
        source_ref = "conformance:candidate-source"
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content=content,
            source_ref=source_ref,
            evidence_refs=("conformance:candidate-evidence",),
            confidence=0.8,
            record_id="candidate-record",
            event_id="candidate-observed",
        )

        assert candidate.state is MemoryState.CANDIDATE
        assert candidate.trust is MemoryTrust.UNTRUSTED
        assert candidate.is_recallable(now=T0) is False
        assert writer.list_active() == []

        active = writer.confirm(
            candidate.record_id,
            expected_revision=candidate.revision,
            event_id="candidate-confirmed",
        )
        assert active.state is MemoryState.ACTIVE
        assert active.trust is MemoryTrust.HOST_CONFIRMED
        assert active.is_recallable(now=T0) is True
        assert [record.record_id for record in writer.list_active()] == [
            candidate.record_id
        ]

        events = writer.events(candidate.record_id)
        assert [event.event_type.value for event in events] == ["observed", "confirmed"]
        rendered_events = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
        assert content not in rendered_events
        assert source_ref not in rendered_events
    finally:
        ledger.close()


def assert_exact_scope_isolation_and_scoped_forget(factory: LedgerFactory) -> None:
    """Equal record IDs and a forget action must stay inside the host scope."""

    ledger = _opened(factory)
    try:
        alice_scope = MemoryScope(tenant="conformance", user="alice", thread="shared")
        bob_scope = MemoryScope(tenant="conformance", user="bob", thread="shared")
        alice = _writer(ledger, alice_scope)
        bob = _writer(ledger, bob_scope)
        alice_candidate = alice.propose(
            kind=MemoryKind.PREFERENCE,
            content="Alice scoped content.",
            source_ref="conformance:alice",
            record_id="shared-record-id",
            event_id="scope-observed",
        )
        bob_candidate = bob.propose(
            kind=MemoryKind.PREFERENCE,
            content="Bob scoped content.",
            source_ref="conformance:bob",
            record_id="shared-record-id",
            event_id="scope-observed",
        )
        alice_active = alice.confirm(
            alice_candidate.record_id,
            expected_revision=alice_candidate.revision,
            event_id="scope-confirmed",
        )
        bob_active = bob.confirm(
            bob_candidate.record_id,
            expected_revision=bob_candidate.revision,
            event_id="scope-confirmed",
        )

        assert [record.content for record in alice.list_active()] == ["Alice scoped content."]
        assert [record.content for record in bob.list_active()] == ["Bob scoped content."]

        receipt = alice.forget(
            alice_active.record_id,
            expected_revision=alice_active.revision,
            event_id="scope-forget",
        )
        assert receipt.payload_deleted is True
        assert alice.list_active() == []
        forgotten = alice.get(alice_active.record_id)
        assert forgotten is not None
        assert forgotten.content is None
        assert [record.content for record in bob.list_active()] == ["Bob scoped content."]
        bob_after = bob.get(bob_active.record_id)
        assert bob_after is not None
        assert bob_after.revision == bob_active.revision
    finally:
        ledger.close()


def assert_idempotent_retries_and_conflicting_event_reuse(factory: LedgerFactory) -> None:
    """Exact retry is stable; one event ID cannot mean two commands."""

    ledger = _opened(factory)
    try:
        writer = _writer(
            ledger,
            MemoryScope(tenant="conformance", user="alice", thread="retries"),
        )
        kwargs = {
            "kind": MemoryKind.FACT,
            "content": "Idempotent candidate content.",
            "source_ref": "conformance:retry",
            "record_id": "retry-record",
            "event_id": "retry-observed",
        }
        first = writer.propose(**kwargs)
        retried = writer.propose(**kwargs)
        assert retried == first
        assert len(writer.events(first.record_id)) == 1

        with pytest.raises(LedgerConflictError, match="event_id"):
            writer.propose(
                **{
                    **kwargs,
                    "content": "A distinct command must not reuse this event ID.",
                }
            )

        active = writer.confirm(
            first.record_id,
            expected_revision=first.revision,
            event_id="retry-confirmed",
        )
        retried_active = writer.confirm(
            first.record_id,
            expected_revision=first.revision,
            event_id="retry-confirmed",
        )
        assert retried_active == active
        assert len(writer.events(first.record_id)) == 2
    finally:
        ledger.close()


def assert_restart_and_setup_persistence(factory: LedgerFactory) -> None:
    """An explicit setup and clean client restart preserve active scoped data."""

    scope = MemoryScope(tenant="conformance", user="alice", thread="restart")
    first = _opened(factory)
    try:
        writer = _writer(first, scope)
        candidate = writer.propose(
            kind=MemoryKind.FACT,
            content="Active record survives an explicit backend restart.",
            source_ref="conformance:restart",
            record_id="restart-record",
            event_id="restart-observed",
        )
        active = writer.confirm(
            candidate.record_id,
            expected_revision=candidate.revision,
            event_id="restart-confirmed",
        )
    finally:
        first.close()

    restarted = _opened(factory)
    try:
        # Setup remains explicitly callable and idempotent after the reopen.
        restarted.setup()
        writer = _writer(restarted, scope)
        restored = writer.get(active.record_id)
        assert restored is not None
        assert restored.state is MemoryState.ACTIVE
        assert restored.content == "Active record survives an explicit backend restart."
        assert [record.record_id for record in writer.list_active()] == [active.record_id]
    finally:
        restarted.close()


def assert_admission_boundary_and_strict_recall(factory: LedgerFactory) -> None:
    """Concrete-origin documents require an audited review before strict recall.

    This uses only the host-facing admission, writer, and recall APIs.  It
    deliberately keeps the raw legacy confirmation path in the fixture too,
    so a backend must prove that strict recall distinguishes audited document
    data from an otherwise active ``unknown`` record.
    """

    ledger = _opened(factory)
    try:
        writer = _writer(
            ledger,
            MemoryScope(tenant="conformance", user="alice", thread="admission"),
        )
        content = "Audited document content is eligible for strict recall."
        source_ref = "conformance:document-admission"
        gate, candidate = _admitted_document(
            writer,
            content=content,
            source_ref=source_ref,
        )

        with pytest.raises(LedgerStateError, match="MemoryReviewGate"):
            writer.confirm(
                candidate.record_id,
                expected_revision=candidate.revision,
                event_id="admission-direct-confirm-denied",
            )
        untouched = writer.get(candidate.record_id)
        assert untouched is not None
        assert untouched.state is MemoryState.CANDIDATE
        assert writer.admission_audits(candidate.record_id) == []

        active = gate.confirm(
            gate.review(candidate.record_id),
            event_id="admission-reviewed-confirm",
        )
        assert active.state is MemoryState.ACTIVE
        assert active.trust is MemoryTrust.HOST_CONFIRMED
        audits = writer.admission_audits(active.record_id)
        assert len(audits) == 1
        audit_receipt = json.dumps(
            [audit.to_dict() for audit in audits],
            ensure_ascii=False,
        )
        assert content not in audit_receipt
        assert source_ref not in audit_receipt

        raw = writer.propose(
            kind=MemoryKind.FACT,
            content="Raw unknown-origin content must stay out of strict recall.",
            source_ref="conformance:raw-legacy",
            record_id="admission-raw-legacy",
            event_id="admission-raw-observed",
        )
        writer.confirm(
            raw.record_id,
            expected_revision=raw.revision,
            event_id="admission-raw-confirmed",
        )

        planner = _strict_planner(writer)
        context = planner.resolve(
            planner.plan(
                task="audited document content",
                token_budget=500,
                byte_budget=10_000,
            )
        )
        records = json.loads(context.render_data())["records"]
        assert records == [{"content": content, "kind": MemoryKind.FACT.value}]
    finally:
        ledger.close()


def assert_lifecycle_forget_source_and_hard_erase(factory: LedgerFactory) -> None:
    """Exercise the public lifecycle, erasure, and exact-scope contracts."""

    ledger = _opened(factory)
    try:
        lifecycle_scope = MemoryScope(
            tenant="conformance",
            user="alice",
            thread="lifecycle",
        )
        lifecycle = _writer(ledger, lifecycle_scope)

        quarantined_candidate = lifecycle.propose(
            kind=MemoryKind.FACT,
            content="Quarantined data is never active.",
            source_ref="conformance:lifecycle-quarantine",
            record_id="lifecycle-quarantine",
            event_id="lifecycle-quarantine-observed",
        )
        quarantined = lifecycle.quarantine(
            quarantined_candidate.record_id,
            expected_revision=quarantined_candidate.revision,
            reason_code="review_required",
            event_id="lifecycle-quarantined",
        )
        assert quarantined.state is MemoryState.QUARANTINED

        expiring_candidate = lifecycle.propose(
            kind=MemoryKind.FACT,
            content="This record expires at the Ledger boundary.",
            source_ref="conformance:lifecycle-expiry",
            valid_until=T0 + timedelta(seconds=1),
            record_id="lifecycle-expiry",
            event_id="lifecycle-expiry-observed",
        )
        lifecycle.confirm(
            expiring_candidate.record_id,
            expected_revision=expiring_candidate.revision,
            event_id="lifecycle-expiry-confirmed",
        )
        expired = lifecycle.expire_due(now=T0 + timedelta(seconds=1))
        assert [record.record_id for record in expired] == [
            expiring_candidate.record_id
        ]
        assert expired[0].state is MemoryState.EXPIRED

        old_candidate = lifecycle.propose(
            kind=MemoryKind.FACT,
            content="The old active fact is superseded.",
            source_ref="conformance:lifecycle-old",
            record_id="lifecycle-old",
            event_id="lifecycle-old-observed",
        )
        old = lifecycle.confirm(
            old_candidate.record_id,
            expected_revision=old_candidate.revision,
            event_id="lifecycle-old-confirmed",
        )
        replacement_candidate = lifecycle.propose(
            kind=MemoryKind.FACT,
            content="The replacement active fact remains current.",
            source_ref="conformance:lifecycle-replacement",
            record_id="lifecycle-replacement",
            event_id="lifecycle-replacement-observed",
        )
        replacement = lifecycle.confirm(
            replacement_candidate.record_id,
            expected_revision=replacement_candidate.revision,
            event_id="lifecycle-replacement-confirmed",
        )
        superseded = lifecycle.supersede(
            old.record_id,
            replacement_record_id=replacement.record_id,
            expected_revision=old.revision,
            expected_replacement_revision=replacement.revision,
            event_id="lifecycle-superseded",
        )
        assert superseded.state is MemoryState.SUPERSEDED
        assert superseded.superseded_by == replacement.record_id
        retracted = lifecycle.retract(
            replacement.record_id,
            expected_revision=replacement.revision,
            reason_code="lifecycle_retracted",
            event_id="lifecycle-retracted",
        )
        assert retracted.state is MemoryState.RETRACTED
        events_before_stale_transition = lifecycle.events(retracted.record_id)
        with pytest.raises(LedgerConflictError, match="revision"):
            lifecycle.retract(
                replacement.record_id,
                expected_revision=replacement.revision,
                reason_code="stale_transition",
                event_id="lifecycle-stale-retract",
            )
        assert lifecycle.events(retracted.record_id) == events_before_stale_transition
        assert lifecycle.list_active() == []

        source_scope = MemoryScope(
            tenant="conformance",
            user="alice",
            thread="source-revocation",
        )
        other_source_scope = MemoryScope(
            tenant="conformance",
            user="bob",
            thread="source-revocation",
        )
        source_writer = _writer(ledger, source_scope)
        other_source_writer = _writer(ledger, other_source_scope)
        source_ref = "conformance:revoked-source"
        source_records = []
        for record_id, content in (
            ("source-record-one", "First record from a revocable source."),
            ("source-record-two", "Second record from a revocable source."),
        ):
            candidate = source_writer.propose(
                kind=MemoryKind.FACT,
                content=content,
                source_ref=source_ref,
                record_id=record_id,
                event_id=f"{record_id}-observed",
            )
            source_records.append(
                source_writer.confirm(
                    candidate.record_id,
                    expected_revision=candidate.revision,
                    event_id=f"{record_id}-confirmed",
                )
            )
        other_candidate = other_source_writer.propose(
            kind=MemoryKind.FACT,
            content="The same source remains valid in a different scope.",
            source_ref=source_ref,
            record_id="source-record-other-scope",
            event_id="source-other-observed",
        )
        other_source_writer.confirm(
            other_candidate.record_id,
            expected_revision=other_candidate.revision,
            event_id="source-other-confirmed",
        )

        source_receipts = source_writer.forget_by_source(source_ref)
        assert [receipt.record_id for receipt in source_receipts] == [
            record.record_id for record in source_records
        ]
        assert all(receipt.payload_deleted for receipt in source_receipts)
        assert source_writer.forget_by_source(source_ref) == []
        for record in source_records:
            forgotten = source_writer.get(record.record_id)
            assert forgotten is not None
            assert forgotten.state is MemoryState.RETRACTED
            assert forgotten.content is None
            assert forgotten.source_refs == ()
        with pytest.raises(LedgerStateError, match="revoked"):
            source_writer.propose(
                kind=MemoryKind.FACT,
                content="Revoked source content must not re-enter this scope.",
                source_ref=source_ref,
                record_id="source-reingest-denied",
                event_id="source-reingest-denied",
            )
        assert [record.content for record in other_source_writer.list_active()] == [
            "The same source remains valid in a different scope."
        ]

        erase_scope = MemoryScope(
            tenant="conformance",
            user="alice",
            thread="hard-erase",
        )
        other_erase_scope = MemoryScope(
            tenant="conformance",
            user="bob",
            thread="hard-erase",
        )
        erasing_writer = _writer(ledger, erase_scope)
        other_erasing_writer = _writer(ledger, other_erase_scope)
        erased_candidate = erasing_writer.propose(
            kind=MemoryKind.FACT,
            content="Hard-erased content must not be replayed.",
            source_ref="conformance:hard-erase",
            record_id="shared-hard-erase-record",
            event_id="hard-erase-observed",
        )
        erased_active = erasing_writer.confirm(
            erased_candidate.record_id,
            expected_revision=erased_candidate.revision,
            event_id="hard-erase-confirmed",
        )
        other_erased_candidate = other_erasing_writer.propose(
            kind=MemoryKind.FACT,
            content="Other-scope content survives the neighbouring hard erase.",
            source_ref="conformance:hard-erase",
            record_id="shared-hard-erase-record",
            event_id="hard-erase-observed",
        )
        other_erasing_writer.confirm(
            other_erased_candidate.record_id,
            expected_revision=other_erased_candidate.revision,
            event_id="hard-erase-confirmed",
        )
        first_erase = erasing_writer.erase(
            erased_active.record_id,
            expected_revision=erased_active.revision,
            event_id="hard-erase-command",
        )
        retried_erase = erasing_writer.erase(
            erased_active.record_id,
            expected_revision=erased_active.revision,
            event_id="hard-erase-command",
        )
        assert retried_erase == first_erase
        assert first_erase.events_deleted == 2
        assert erasing_writer.get(erased_active.record_id) is None
        assert erasing_writer.events(erased_active.record_id) == []
        with pytest.raises(LedgerStateError, match="erased"):
            erasing_writer.propose(
                kind=MemoryKind.FACT,
                content="Hard-erased content must not be replayed.",
                source_ref="conformance:hard-erase",
                record_id="shared-hard-erase-record",
                event_id="hard-erase-observed",
            )
        assert [record.content for record in other_erasing_writer.list_active()] == [
            "Other-scope content survives the neighbouring hard erase."
        ]
    finally:
        ledger.close()


def assert_checkpoint_reopen_resume_and_selected_record_invalidation(
    factory: LedgerFactory,
) -> None:
    """A strict sealed checkpoint survives reopen and dies with its selection.

    The helper intentionally avoids raw database inspection and tampering.  A
    backend proves the durable public contract through checkpoint creation,
    a real client reopen, resume, and a public lifecycle change to the
    selected record.
    """

    scope = MemoryScope(tenant="conformance", user="alice", thread="checkpoint")
    content = "A sealed selection must be freshly revalidated after reopen."
    source_ref = "conformance:checkpoint-document"
    task = "sealed selection revalidation"
    checkpoint_id = "conformance-checkpoint"
    continuation_ref = "conformance-continuation"

    first = _opened(factory)
    try:
        first_writer = _writer(first, scope)
        gate, candidate = _admitted_document(
            first_writer,
            content=content,
            source_ref=source_ref,
        )
        active = gate.confirm(
            gate.review(candidate.record_id),
            event_id="checkpoint-admission-confirmed",
        )
        planner = _strict_planner(first_writer)
        checkpoint = planner.checkpoint(
            planner.plan(task=task, token_budget=500, byte_budget=10_000),
            checkpoint_id=checkpoint_id,
            continuation_ref=continuation_ref,
        )
        assert checkpoint.selected_count == 1
        checkpoint_receipt = json.dumps(checkpoint.explain(), ensure_ascii=False)
        for private_value in (
            content,
            active.record_id,
            checkpoint_id,
            continuation_ref,
            scope.correlation_id(),
        ):
            assert private_value not in checkpoint_receipt
    finally:
        first.close()

    restarted = _opened(factory)
    try:
        restarted_writer = _writer(restarted, scope)
        restarted_planner = _strict_planner(restarted_writer)
        resume = restarted_planner.resume_checkpoint(checkpoint_id, task=task)
        assert resume.continuation_ref == continuation_ref
        resume_receipt = json.dumps(resume.explain(), ensure_ascii=False)
        for private_value in (
            content,
            active.record_id,
            checkpoint_id,
            continuation_ref,
            scope.correlation_id(),
        ):
            assert private_value not in resume_receipt

        selected = restarted_writer.get(active.record_id)
        assert selected is not None
        restarted_writer.forget(
            selected.record_id,
            expected_revision=selected.revision,
            event_id="checkpoint-selected-record-forgotten",
        )
        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            restarted_planner.resume_checkpoint(checkpoint_id, task=task)
    finally:
        restarted.close()
