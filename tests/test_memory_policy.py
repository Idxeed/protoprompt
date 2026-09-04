"""Regression tests for the v1-candidate combined memory policy contract."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from protoprompt import MemoryScope, RegexTokenCounter
from protoprompt.ledger import (
    MEMORY_POLICY_SCHEMA_VERSION,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryPolicy,
    MemoryPolicyError,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy


_NOW = datetime(2048, 1, 1, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _NOW


def _document_admission(*, minimum_confidence: float = 0.8) -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="memory-policy-document-admission-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.DOCUMENT,),
        allowed_kinds=(MemoryKind.FACT, MemoryKind.DECISION),
        minimum_confidence=minimum_confidence,
    )


def _document_recall(
    *,
    origins: tuple[MemoryOrigin, ...] = (MemoryOrigin.DOCUMENT,),
    kinds: tuple[MemoryKind, ...] = (MemoryKind.FACT,),
    minimum_confidence: float = 0.9,
    require_admission_audit: bool = True,
) -> LedgerRecallPolicy:
    return LedgerRecallPolicy(
        policy_id="memory-policy-document-recall-v1",
        allowed_origins=origins,
        allowed_kinds=kinds,
        minimum_confidence=minimum_confidence,
        require_admission_audit=require_admission_audit,
    )


def test_safe_default_is_audited_and_has_a_stable_content_free_receipt():
    policy = MemoryPolicy.safe_default()

    assert policy.schema_version == MEMORY_POLICY_SCHEMA_VERSION == 1
    assert policy.admission.allowed_origins == (MemoryOrigin.HOST_ASSERTION,)
    assert policy.recall.allowed_origins == (MemoryOrigin.HOST_ASSERTION,)
    assert policy.recall.require_admission_audit is True
    assert policy.recall.minimum_confidence == policy.admission.minimum_confidence == 0.75

    first = policy.explain()
    second = policy.explain()
    assert first == second
    assert first["fingerprint"] == policy.fingerprint
    assert first["admission"]["policy_id"] == "ledger-admission-safe-v1"
    assert first["recall"]["policy_id"] == "memory-policy-safe-recall-v1"

    # Explanations are caller-owned snapshots, not a mutable hidden policy.
    first["admission"]["allowed_origins"].append("document")
    assert policy.explain() == second


@pytest.mark.parametrize(
    ("admission", "recall", "message"),
    [
        (
            _document_admission(),
            _document_recall(require_admission_audit=False),
            "immutable admission audit",
        ),
        (
            _document_admission(),
            LedgerRecallPolicy(
                policy_id="memory-policy-unrestricted-recall-v1",
                require_admission_audit=True,
            ),
            "concrete origins",
        ),
        (
            MemoryAdmissionPolicy(
                policy_id="memory-policy-unknown-admission-v1",
                allowed_origins=(MemoryOrigin.UNKNOWN,),
            ),
            LedgerRecallPolicy(
                policy_id="memory-policy-unknown-recall-v1",
                allowed_origins=(MemoryOrigin.UNKNOWN,),
                require_admission_audit=True,
            ),
            "exclude unknown",
        ),
        (
            MemoryAdmissionPolicy(
                policy_id="memory-policy-legacy-unknown-admission-v1",
                allowed_origins=(MemoryOrigin.LEGACY_UNKNOWN,),
            ),
            LedgerRecallPolicy(
                policy_id="memory-policy-legacy-unknown-recall-v1",
                allowed_origins=(MemoryOrigin.LEGACY_UNKNOWN,),
                require_admission_audit=True,
            ),
            "exclude unknown",
        ),
        (
            _document_admission(),
            _document_recall(origins=(MemoryOrigin.HOST_ASSERTION,)),
            "allowed_origins",
        ),
        (
            _document_admission(),
            _document_recall(kinds=(MemoryKind.PREFERENCE,)),
            "allowed_kinds",
        ),
        (
            _document_admission(minimum_confidence=0.9),
            _document_recall(minimum_confidence=0.8),
            "minimum_confidence",
        ),
    ],
)
def test_policy_rejects_recall_that_is_weaker_than_its_admission_component(
    admission: MemoryAdmissionPolicy,
    recall: LedgerRecallPolicy,
    message: str,
):
    with pytest.raises(MemoryPolicyError, match=message):
        MemoryPolicy(
            policy_id="memory-policy-invalid-v1",
            admission=admission,
            recall=recall,
        )


def test_policy_requires_typed_components_and_known_schema():
    for schema_version in (True, 1.0, 2):
        with pytest.raises(ValueError, match="schema version"):
            MemoryAdmissionPolicy(schema_version=schema_version)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="schema version"):
            LedgerRecallPolicy(schema_version=schema_version)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="admission"):
        MemoryPolicy(admission=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="recall"):
        MemoryPolicy(recall=object())  # type: ignore[arg-type]
    with pytest.raises(MemoryPolicyError, match="schema"):
        MemoryPolicy(schema_version=2)
    with pytest.raises(MemoryPolicyError, match="schema"):
        MemoryPolicy(schema_version=True)
    with pytest.raises(MemoryPolicyError, match="schema"):
        MemoryPolicy(schema_version=1.0)


def test_matching_components_drive_explicit_host_admission_and_recall():
    policy = MemoryPolicy(
        policy_id="memory-policy-document-v1",
        policy_version="1",
        admission=_document_admission(),
        recall=_document_recall(),
    )
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        writer = MemoryWriter(
            ledger,
            scope=MemoryScope(tenant="policy", user="alice", thread="docs"),
            actor="policy-host",
            clock=_clock,
        )
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.DOCUMENT,
            policy=policy.admission,
        )
        candidate = gate.ingress(
            kind=MemoryKind.FACT,
            source_ref="document:policy:1",
            evidence_refs=("document:policy:1:page:1",),
            confidence=0.95,
        ).submit("The project uses the approved memory policy.")
        review = gate.review(candidate.record_id)
        active = gate.confirm(review, event_id="policy-document-confirmed")

        planner = LedgerRecallPlanner(
            writer,
            policy=policy.recall,
            counter=RegexTokenCounter(),
            counter_id="policy-test-counter-v1",
            clock=_clock,
        )
        plan = planner.plan(
            task="approved project memory policy",
            token_budget=512,
            byte_budget=4_096,
        )
        resolved = planner.resolve(plan)

        assert resolved.record_count == 1
        assert "approved memory policy" in resolved.render_data()
        assert active.record_id not in resolved.render_data()
        assert plan.policy_id == policy.recall.policy_id
        assert plan.policy_fingerprint != policy.fingerprint
        assert policy.explain()["admission"]["policy_id"] == policy.admission.policy_id
        assert policy.explain()["recall"]["policy_id"] == policy.recall.policy_id
    finally:
        ledger.close()
