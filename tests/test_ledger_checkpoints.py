"""Durable boundary tests for sealed Ledger recall checkpoints.

The checkpoint contract deliberately stores only an opaque continuation
reference and selection markers.  These tests exercise that contract through
its host-facing APIs, including a true SQLite restart and the request's final
composition boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from _mocks import MockLLM
from protoprompt import ContextInput, InMemStore
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    LedgerStateError,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import (
    CheckpointContractMismatchError,
    LedgerCheckpointError,
    LedgerContextComposer,
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
    StaleMemoryPlanError,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


T0 = datetime(2037, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"v012-test-secret-for-sealed-ledger-checkpoints"
_TASK = "How does the sealed checkpoint resume safely?"


@pytest.fixture
def scope() -> MemoryScope:
    return MemoryScope(tenant="checkpoint-acme", user="alice", thread="resume-1")


def _writer(ledger: SqliteMemoryLedger, scope: MemoryScope) -> MemoryWriter:
    return MemoryWriter(ledger, scope=scope, actor="checkpoint-host", clock=lambda: T0)


def _document_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="checkpoint-document-policy-v1",
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
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id=f"admission:{record_id}",
    )


def _planner(writer: MemoryWriter, counter: RegexTokenCounter) -> LedgerRecallPlanner:
    return LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )


def _input(*, include_rag: bool = False) -> ContextInput:
    return ContextInput(
        query=_TASK,
        system_prompt="The host system contract remains authoritative.",
        include_rag=include_rag,
        include_session=False,
    )


def _seal(
    planner: LedgerRecallPlanner,
    *,
    checkpoint_id: str,
    continuation_ref: str,
):
    plan = planner.plan(task=_TASK, token_budget=300, byte_budget=10_000)
    return planner.checkpoint(
        plan,
        checkpoint_id=checkpoint_id,
        continuation_ref=continuation_ref,
    )


async def test_checkpoint_survives_restart_and_composes_only_freshly_resumed_data(
    tmp_path,
    scope,
):
    path = tmp_path / "checkpoint-restart.db"
    content = "Resume only after the sealed selected-record manifest verifies."
    first_ledger = SqliteMemoryLedger(str(path))
    first_ledger.setup()
    try:
        writer = _writer(first_ledger, scope)
        active = _admitted_document(
            writer,
            record_id="restart-memory",
            content=content,
        )
        checkpoint = _seal(
            _planner(writer, RegexTokenCounter()),
            checkpoint_id="restart-checkpoint",
            continuation_ref="restart-continuation",
        )

        checkpoint_receipt = json.dumps(checkpoint.explain(), ensure_ascii=False)
        assert checkpoint.checkpoint_id == "restart-checkpoint"
        assert checkpoint.continuation_ref == "restart-continuation"
        assert content not in checkpoint_receipt
        assert active.record_id not in checkpoint_receipt
        assert checkpoint.checkpoint_id not in checkpoint_receipt
        assert checkpoint.continuation_ref not in checkpoint_receipt
    finally:
        first_ledger.close()

    restarted_ledger = SqliteMemoryLedger(str(path))
    restarted_ledger.setup()
    try:
        restarted_writer = _writer(restarted_ledger, scope)
        counter = RegexTokenCounter()
        restarted_planner = _planner(restarted_writer, counter)
        resume = restarted_planner.resume_checkpoint(
            "restart-checkpoint",
            task=_TASK,
        )
        assert resume.continuation_ref == "restart-continuation"
        assert "restart-continuation" not in json.dumps(resume.explain())

        builder = TokenBudgetedContextBuilder(
            InMemStore(),
            MockLLM(),
            counter=counter,
            max_tokens=500,
            scope=scope,
        )
        composer = LedgerContextComposer(builder, restarted_planner)
        with pytest.raises(LedgerCheckpointError, match="resume task"):
            await composer.plan_checkpoint_messages(
                resume,
                ContextInput(
                    query="unrelated current host request",
                    system_prompt="The host system contract remains authoritative.",
                    include_rag=False,
                    include_session=False,
                ),
                user_message="This request must not reuse the checkpoint lane.",
            )
        request = await composer.plan_checkpoint_messages(
            resume,
            _input(),
            user_message="What is the safe resume rule?",
        )
        messages = request.render_messages()

        assert json.loads(messages[2]["content"])["records"] == [{
            "content": content,
            "kind": "fact",
        }]
        explained = json.dumps(request.explain(), ensure_ascii=False)
        assert content not in explained
        assert active.record_id not in explained
        assert scope.correlation_id() not in explained
    finally:
        restarted_ledger.close()


def test_checkpoint_resume_rejects_a_tampered_hmac_manifest(tmp_path, scope):
    path = tmp_path / "checkpoint-tamper.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        _admitted_document(
            writer,
            record_id="tamper-memory",
            content="The continuation reference is covered by the HMAC seal.",
        )
        _seal(
            _planner(writer, RegexTokenCounter()),
            checkpoint_id="tamper-checkpoint",
            continuation_ref="original-continuation",
        )
    finally:
        ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_recall_checkpoints "
            "SET continuation_ref = ? WHERE checkpoint_id = ?",
            ("tampered-continuation", "tamper-checkpoint"),
        )

    restarted_ledger = SqliteMemoryLedger(str(path))
    restarted_ledger.setup()
    try:
        planner = _planner(_writer(restarted_ledger, scope), RegexTokenCounter())
        with pytest.raises(LedgerCheckpointError, match="integrity seal"):
            planner.resume_checkpoint("tamper-checkpoint", task=_TASK)
    finally:
        restarted_ledger.close()


def test_checkpoint_resume_rejects_policy_counter_and_scope_drift(tmp_path, scope):
    path = tmp_path / "checkpoint-contract.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        _admitted_document(
            writer,
            record_id="contract-memory",
            content="A resume must retain its strict policy and counter contract.",
        )
        _seal(
            _planner(writer, RegexTokenCounter()),
            checkpoint_id="contract-checkpoint",
            continuation_ref="contract-continuation",
        )

        stricter_policy = replace(
            LedgerRecallPolicy.admission_safe_default(),
            minimum_confidence=0.95,
        )
        policy_drift = LedgerRecallPlanner(
            writer,
            policy=stricter_policy,
            counter=RegexTokenCounter(),
            checkpoint_secret=_CHECKPOINT_SECRET,
            clock=lambda: T0,
        )
        with pytest.raises(CheckpointContractMismatchError, match="policy and counter"):
            policy_drift.resume_checkpoint("contract-checkpoint", task=_TASK)

        counter_drift = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.admission_safe_default(),
            counter=RegexTokenCounter(),
            counter_id="another-counter-contract-v1",
            checkpoint_secret=_CHECKPOINT_SECRET,
            clock=lambda: T0,
        )
        with pytest.raises(CheckpointContractMismatchError, match="policy and counter"):
            counter_drift.resume_checkpoint("contract-checkpoint", task=_TASK)

        other_scope = MemoryScope(
            tenant="checkpoint-acme",
            user="bob",
            thread="resume-1",
        )
        other_planner = _planner(_writer(ledger, other_scope), RegexTokenCounter())
        with pytest.raises(KeyError, match="contract-checkpoint"):
            other_planner.resume_checkpoint("contract-checkpoint", task=_TASK)
    finally:
        ledger.close()


def test_checkpoint_requires_host_hmac_key_and_strict_admission_policy(tmp_path, scope):
    path = tmp_path / "checkpoint-host-authority.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        strict_without_key = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.admission_safe_default(),
            counter=RegexTokenCounter(),
            clock=lambda: T0,
        )
        strict_plan = strict_without_key.plan(task=_TASK, token_budget=300)
        with pytest.raises(LedgerCheckpointError, match="checkpoint_secret"):
            strict_without_key.checkpoint(
                strict_plan,
                checkpoint_id="missing-key-checkpoint",
                continuation_ref="missing-key-continuation",
            )

        permissive = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.safe_default(),
            counter=RegexTokenCounter(),
            checkpoint_secret=_CHECKPOINT_SECRET,
            clock=lambda: T0,
        )
        permissive_plan = permissive.plan(task=_TASK, token_budget=300)
        with pytest.raises(LedgerCheckpointError, match="admission evidence"):
            permissive.checkpoint(
                permissive_plan,
                checkpoint_id="permissive-checkpoint",
                continuation_ref="permissive-continuation",
            )

        identity_planner = _planner(writer, RegexTokenCounter())
        identity_plan = identity_planner.plan(task=_TASK, token_budget=300)
        first = identity_planner.checkpoint(
            identity_plan,
            checkpoint_id="identity-checkpoint-one",
            continuation_ref="identity-continuation-one",
        )
        second = identity_planner.checkpoint(
            identity_plan,
            checkpoint_id="identity-checkpoint-two",
            continuation_ref="identity-continuation-two",
        )
        assert first != second
        assert len({first, second}) == 2
    finally:
        ledger.close()


def test_setup_fails_closed_on_a_corrupted_checkpoint_sidecar(tmp_path, scope):
    path = tmp_path / "checkpoint-corrupt-sidecar.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        _admitted_document(
            writer,
            record_id="schema-memory",
            content="Schema validation must reject malformed checkpoint metadata.",
        )
        _seal(
            _planner(writer, RegexTokenCounter()),
            checkpoint_id="schema-checkpoint",
            continuation_ref="schema-continuation",
        )
    finally:
        ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_recall_checkpoints SET schema_version = 99 "
            "WHERE checkpoint_id = ?",
            ("schema-checkpoint",),
        )

    corrupted = SqliteMemoryLedger(str(path))
    try:
        with pytest.raises(LedgerStateError, match="sealed recall checkpoint"):
            corrupted.dry_run_setup()
    finally:
        corrupted.close()


def test_selected_record_lifecycle_change_invalidates_and_scrubs_checkpoint(
    tmp_path,
    scope,
):
    path = tmp_path / "checkpoint-lifecycle.db"
    ledger = SqliteMemoryLedger(str(path))
    ledger.setup()
    try:
        writer = _writer(ledger, scope)
        active = _admitted_document(
            writer,
            record_id="lifecycle-memory",
            content="A forgotten selected record cannot be resumed from a manifest.",
        )
        planner = _planner(writer, RegexTokenCounter())
        _seal(
            planner,
            checkpoint_id="lifecycle-checkpoint",
            continuation_ref="lifecycle-continuation",
        )

        writer.forget(
            active.record_id,
            expected_revision=active.revision,
            event_id="forget:lifecycle-memory",
        )

        with sqlite3.connect(path) as connection:
            state = connection.execute(
                "SELECT state FROM memory_recall_checkpoints WHERE checkpoint_id = ?",
                ("lifecycle-checkpoint",),
            ).fetchone()
            selection_count = connection.execute(
                "SELECT COUNT(*) FROM memory_recall_checkpoint_selections "
                "WHERE checkpoint_id = ?",
                ("lifecycle-checkpoint",),
            ).fetchone()
        assert state == ("invalidated",)
        assert selection_count == (0,)

        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            planner.resume_checkpoint("lifecycle-checkpoint", task=_TASK)
    finally:
        ledger.close()


async def test_checkpoint_composition_fails_closed_when_forget_wins_during_rag(
    tmp_path,
    scope,
):
    class BlockingLLM(MockLLM):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts, model=""):
            self.started.set()
            await self.release.wait()
            return await super().embed(texts, model=model)

    path = tmp_path / "checkpoint-composition-race.db"
    first_ledger = SqliteMemoryLedger(str(path))
    first_ledger.setup()
    second_ledger = SqliteMemoryLedger(str(path))
    llm = BlockingLLM()
    try:
        writer = _writer(first_ledger, scope)
        concurrent_writer = _writer(second_ledger, scope)
        active = _admitted_document(
            writer,
            record_id="race-memory",
            content="The final request boundary must reject forgotten checkpoint data.",
        )
        counter = RegexTokenCounter()
        planner = _planner(writer, counter)
        _seal(
            planner,
            checkpoint_id="race-checkpoint",
            continuation_ref="race-continuation",
        )
        resume = planner.resume_checkpoint("race-checkpoint", task=_TASK)
        builder = TokenBudgetedContextBuilder(
            InMemStore(),
            llm,
            counter=counter,
            max_tokens=500,
            scope=scope,
        )
        composer = LedgerContextComposer(builder, planner)

        task = asyncio.create_task(composer.plan_checkpoint_messages(
            resume,
            _input(include_rag=True),
            user_message="Use the sealed durable fact.",
        ))
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        concurrent_writer.forget(
            active.record_id,
            expected_revision=active.revision,
            event_id="forget:checkpoint-race-memory",
        )
        llm.release.set()

        with pytest.raises(StaleMemoryPlanError, match="replan"):
            await task
    finally:
        llm.release.set()
        second_ledger.close()
        first_ledger.close()
