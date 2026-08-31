"""Tests for the host-side typed task-resume reference-data contract."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from _mocks import MockLLM
from protoprompt import ContextInput, InMemStore
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import (
    LedgerRecallPlanner,
    LedgerRecallPolicy,
    StaleMemoryCheckpointError,
)
import protoprompt.ledger.task_resume as task_resume
from protoprompt.ledger.task_resume import (
    MAX_PROCEDURE_STEP_CHARS,
    MAX_TASK_GOAL_CHARS,
    TaskEpisode,
    TaskOutcome,
    TaskProcedure,
    TaskResumePayloadError,
    decode_task_resume_payload,
    encode_task_resume_payload,
)
from protoprompt.ledger.task_resume_planner import (
    TASK_RESUME_SCOPE_KIND,
    TaskResumeBindingError,
    TaskResumeConfigurationError,
    TaskResumePayloadBindingError,
    TaskResumePlanner,
    task_resume_scope,
)
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter


T0 = datetime(2038, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
_CHECKPOINT_SECRET = b"v017-task-resume-host-checkpoint-secret"
_TASK_DESCRIPTOR = "Resume the checked deployment task safely."


@pytest.fixture
def resume_ledger() -> SqliteMemoryLedger:
    ledger = SqliteMemoryLedger()
    ledger.setup()
    try:
        yield ledger
    finally:
        ledger.close()


@pytest.fixture
def parent_scope() -> MemoryScope:
    return MemoryScope(
        tenant="task-resume-acme",
        user="alice",
        thread="conversation-17",
        kind="chat",
    )


def _writer(
    ledger: SqliteMemoryLedger,
    parent: MemoryScope,
    task_ref: str,
) -> tuple[MemoryWriter, MemoryScope]:
    scope = task_resume_scope(parent, task_ref=task_ref)
    return (
        MemoryWriter(
            ledger,
            scope=scope,
            actor="task-resume-host",
            clock=lambda: T0,
        ),
        scope,
    )


def _episode_admission_policy() -> MemoryAdmissionPolicy:
    return MemoryAdmissionPolicy(
        policy_id="task-resume-host-episode-admission-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        allowed_kinds=(MemoryKind.EPISODE,),
        minimum_confidence=0.75,
    )


def _admit_episode(
    writer: MemoryWriter,
    *,
    record_id: str,
    payload: TaskEpisode,
):
    return _admit_episode_content(
        writer,
        record_id=record_id,
        content=payload.to_json(),
    )


def _admit_episode_content(
    writer: MemoryWriter,
    *,
    record_id: str,
    content: str,
):
    gate = MemoryReviewGate(
        writer,
        origin=MemoryOrigin.HOST_ASSERTION,
        policy=_episode_admission_policy(),
    )
    candidate = gate.ingress(
        kind=MemoryKind.EPISODE,
        source_ref=f"task-source:{record_id}",
        evidence_refs=(f"task-evidence:{record_id}",),
        confidence=0.9,
        asserted=True,
    ).submit(content)
    return gate.confirm(
        gate.review(candidate.record_id),
        event_id=f"task-admission:{record_id}",
    )


def _adapter(
    writer: MemoryWriter,
    parent: MemoryScope,
    task_ref: str,
    scope: MemoryScope,
) -> tuple[TaskResumePlanner, LedgerRecallPlanner]:
    counter = RegexTokenCounter()
    planner = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.task_resume_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=600,
        scope=scope,
    )
    return (
        TaskResumePlanner(
            builder,
            planner,
            parent_scope=parent,
            task_ref=task_ref,
            task_descriptor=_TASK_DESCRIPTOR,
        ),
        planner,
    )


def _input(query: str = "What should the host check after this restart?") -> ContextInput:
    return ContextInput(
        query=query,
        system_prompt="The host system contract remains authoritative.",
        include_rag=False,
        include_session=False,
    )


def test_episode_round_trip_is_canonical_typed_and_non_executable_data():
    episode = TaskEpisode(
        task_ref="task:deploy-42",
        goal="Resume the checked deployment after the environment is available.",
        completed_action_refs=("action:prepare", "artifact:manifest"),
        outcome="interrupted",
        next_action="Check the host-owned deployment status before continuing.",
        lesson="Persist immutable artifact references before a restart.",
    )

    encoded = encode_task_resume_payload(episode)

    assert encoded == episode.to_json()
    assert encoded == (
        '{"completed_action_refs":["action:prepare","artifact:manifest"],'
        '"goal":"Resume the checked deployment after the environment is available.",'
        '"kind":"episode","lesson":"Persist immutable artifact references before a restart.",'
        '"next_action":"Check the host-owned deployment status before continuing.",'
        '"outcome":"interrupted","schema_version":1,"task_ref":"task:deploy-42"}'
    )
    restored = decode_task_resume_payload(encoded)
    assert restored == episode
    assert restored.outcome is TaskOutcome.INTERRUPTED
    assert "not tool instructions" in (task_resume.__doc__ or "")
    assert not hasattr(task_resume, "SqliteMemoryLedger")
    assert not hasattr(task_resume, "MemoryWriter")


def test_procedure_round_trip_preserves_ordered_steps_and_episode_provenance():
    procedure = TaskProcedure(
        task_ref="task:deploy-42",
        procedure_ref="procedure:checked-deploy-v1",
        steps=[
            "Verify the host-owned deployment status.",
            "Apply the approved continuation only after verification.",
        ],
        supporting_episode_refs=["episode:31", "episode:37"],
    )

    restored = decode_task_resume_payload(procedure.to_json())

    assert restored == procedure
    assert restored.steps == (
        "Verify the host-owned deployment status.",
        "Apply the approved continuation only after verification.",
    )
    assert restored.supporting_episode_refs == ("episode:31", "episode:37")
    assert restored.to_dict()["kind"] == "procedure"


def test_task_ref_uses_the_checkpoint_compatible_identifier_bound():
    with pytest.raises(ValueError, match="task_ref.*128"):
        TaskEpisode(
            task_ref="t" * 129,
            goal="Valid goal.",
            completed_action_refs=(),
            outcome=TaskOutcome.SUCCEEDED,
        )


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: TaskEpisode(
                task_ref="task ref with spaces",
                goal="Valid goal.",
                completed_action_refs=(),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "task_ref",
        ),
        (
            lambda: TaskEpisode(
                task_ref="task:one",
                goal="x" * (MAX_TASK_GOAL_CHARS + 1),
                completed_action_refs=(),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "goal",
        ),
        (
            lambda: TaskEpisode(
                task_ref="task:one",
                goal="Valid goal.",
                completed_action_refs=("action:one", "action:one"),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "duplicates",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=(),
                supporting_episode_refs=("episode:one",),
            ),
            "at least one step",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=("Valid step.",),
                supporting_episode_refs=(),
            ),
            "at least 1 reference",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=("x" * (MAX_PROCEDURE_STEP_CHARS + 1),),
                supporting_episode_refs=("episode:one",),
            ),
            r"steps\[0\]",
        ),
    ],
)
def test_direct_construction_rejects_invalid_or_unbounded_data(factory, match):
    with pytest.raises((TypeError, ValueError), match=match):
        factory()


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not JSON", "invalid task-resume JSON"),
        ("[]", "JSON object"),
        (
            '{"schema_version":1,"kind":"episode","task_ref":"task:one",'
            '"goal":"one","goal":"two","completed_action_refs":[],'
            '"outcome":"succeeded","next_action":null,"lesson":null}',
            "duplicate JSON field",
        ),
        ("{\"schema_version\":NaN}", "non-finite JSON constant"),
        (
            json.dumps({
                "schema_version": 2,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "invalid task episode payload",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
                "unexpected": True,
            }),
            "unknown field",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "procedure",
                "task_ref": "task:one",
                "goal": "An episode field cannot be accepted as a procedure.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "missing required field",
        ),
        (
            json.dumps({
                "schema_version": True,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "invalid task episode payload",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "procedure",
                "task_ref": "task:one",
                "procedure_ref": "procedure:one",
                "steps": ["Valid step."],
                "supporting_episode_refs": [],
            }),
            "invalid task procedure payload",
        ),
    ],
)
def test_decoder_fails_closed_on_unknown_or_malformed_payloads(payload, match):
    with pytest.raises(TaskResumePayloadError, match=match):
        decode_task_resume_payload(payload)


def test_decoder_rejects_unknown_kind_and_non_string_transport():
    with pytest.raises(TaskResumePayloadError, match="unsupported task-resume payload kind"):
        decode_task_resume_payload('{"schema_version":1,"kind":"fact"}')
    with pytest.raises(TypeError, match="JSON string"):
        decode_task_resume_payload(b"{}")  # type: ignore[arg-type]


def test_decoder_converts_deep_json_recursion_to_a_typed_payload_error():
    with pytest.raises(TaskResumePayloadError):
        decode_task_resume_payload("[" * 2_000 + "]" * 2_000)


def test_equivalent_json_is_normalized_to_the_canonical_wire_shape():
    raw = (
        '{ "task_ref" : "task:one", "lesson" : null, "next_action" : null, '
        '"outcome" : "succeeded", "completed_action_refs" : [], '
        '"goal" : "  Keep this task scoped.  ", "kind" : "episode", '
        '"schema_version" : 1 }'
    )

    restored = decode_task_resume_payload(raw)

    assert isinstance(restored, TaskEpisode)
    assert restored.goal == "Keep this task scoped."
    assert encode_task_resume_payload(restored) == (
        '{"completed_action_refs":[],"goal":"Keep this task scoped.",'
        '"kind":"episode","lesson":null,"next_action":null,'
        '"outcome":"succeeded","schema_version":1,"task_ref":"task:one"}'
    )


def test_task_resume_scope_is_backend_specific_and_requires_tenant_user(parent_scope):
    first = task_resume_scope(parent_scope, task_ref="task:deployment-a")
    second = task_resume_scope(parent_scope, task_ref="task:deployment-b")

    assert first != second
    assert first.tenant == parent_scope.tenant
    assert first.user == parent_scope.user
    assert first.thread == (
        f"task:{parent_scope.correlation_id()}:task:deployment-a"
    )
    assert first.kind == TASK_RESUME_SCOPE_KIND
    assert first.correlation_id() != second.correlation_id()
    sibling_parent = MemoryScope(
        tenant=parent_scope.tenant,
        user=parent_scope.user,
        thread="another-conversation",
        kind=parent_scope.kind,
    )
    assert task_resume_scope(
        sibling_parent,
        task_ref="task:deployment-a",
    ) != first
    with pytest.raises(TaskResumeConfigurationError, match="tenant and user"):
        task_resume_scope(MemoryScope(tenant="tenant-only"), task_ref="task:one")
    with pytest.raises(ValueError, match="task_ref"):
        task_resume_scope(parent_scope, task_ref="not an opaque task ref")


def test_adapter_rejects_a_scope_or_policy_that_widens_episode_resume(
    resume_ledger,
    parent_scope,
):
    writer, scope = _writer(resume_ledger, parent_scope, "task:policy")
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=600,
        scope=scope,
    )
    broad = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.admission_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )

    with pytest.raises(TaskResumeConfigurationError, match="host_assertion"):
        TaskResumePlanner(
            builder,
            broad,
            parent_scope=parent_scope,
            task_ref="task:policy",
            task_descriptor=_TASK_DESCRIPTOR,
        )

    strict = LedgerRecallPlanner(
        writer,
        policy=LedgerRecallPolicy.task_resume_safe_default(),
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )
    wrong_scope_builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=600,
        scope=MemoryScope(tenant="task-resume-acme", user="alice", thread="wrong"),
    )
    with pytest.raises(TaskResumeConfigurationError, match="request_builder"):
        TaskResumePlanner(
            wrong_scope_builder,
            strict,
            parent_scope=parent_scope,
            task_ref="task:policy",
            task_descriptor=_TASK_DESCRIPTOR,
        )


@pytest.mark.parametrize(
    ("policy_change", "match"),
    [
        ({"require_admission_audit": False}, "admission evidence"),
        (
            {"allowed_origins": (MemoryOrigin.HOST_ASSERTION, MemoryOrigin.DOCUMENT)},
            "host_assertion",
        ),
        (
            {"allowed_kinds": (MemoryKind.EPISODE, MemoryKind.PROCEDURE)},
            "episode memory kind",
        ),
        ({"minimum_confidence": 0.74}, "confidence"),
    ],
)
def test_adapter_rejects_every_policy_widening(
    resume_ledger,
    parent_scope,
    policy_change,
    match,
):
    writer, scope = _writer(resume_ledger, parent_scope, "task:policy-negative")
    counter = RegexTokenCounter()
    policy = replace(LedgerRecallPolicy.task_resume_safe_default(), **policy_change)
    planner = LedgerRecallPlanner(
        writer,
        policy=policy,
        counter=counter,
        checkpoint_secret=_CHECKPOINT_SECRET,
        clock=lambda: T0,
    )
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=600,
        scope=scope,
    )

    with pytest.raises(TaskResumeConfigurationError, match=match):
        TaskResumePlanner(
            builder,
            planner,
            parent_scope=parent_scope,
            task_ref="task:policy-negative",
            task_descriptor=_TASK_DESCRIPTOR,
        )


async def test_adapter_seals_and_resumes_only_matching_typed_host_episode(
    resume_ledger,
    parent_scope,
):
    task_ref = "task:deployment-42"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    episode = TaskEpisode(
        task_ref=task_ref,
        goal="Resume deployment after the host verifies its current state.",
        completed_action_refs=("action:prepare", "artifact:manifest"),
        outcome=TaskOutcome.INTERRUPTED,
        next_action="Ask the host for the current deployment status.",
        lesson="Retain artifact references before a controlled restart.",
    )
    active = _admit_episode(writer, record_id="episode-42", payload=episode)
    adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)

    checkpoint = adapter.seal_checkpoint(
        checkpoint_id="checkpoint-deployment-42",
        token_budget=400,
        byte_budget=10_000,
    )
    request = await adapter.compose_checkpoint(
        checkpoint_id=checkpoint.checkpoint_id,
        inp=_input("Current PDF question: which source should I inspect now?"),
        user_message="Continue only after checking the host-owned status.",
    )

    rendered = request.render_messages()
    envelope = json.loads(rendered[2]["content"])
    assert envelope["type"] == "protoprompt.ledger-recall"
    assert len(envelope["records"]) == 1
    restored = decode_task_resume_payload(envelope["records"][0]["content"])
    assert restored == episode
    assert envelope["records"][0]["kind"] == "episode"

    explained = json.dumps({
        "adapter": adapter.explain(),
        "checkpoint": checkpoint.explain(),
        "request": request.explain(),
    }, ensure_ascii=False)
    for private_value in (
        task_ref,
        checkpoint.checkpoint_id,
        checkpoint.continuation_ref,
        active.record_id,
        scope.correlation_id(),
        _TASK_DESCRIPTOR,
        episode.goal,
        episode.next_action,
        episode.lesson,
        *episode.completed_action_refs,
        "task-source:episode-42",
        "task-evidence:episode-42",
    ):
        assert private_value not in explained


async def test_adapter_reconstructs_from_host_mapping_after_sqlite_restart(
    tmp_path,
    parent_scope,
):
    path = tmp_path / "task-resume-restart.db"
    task_ref = "task:restart"
    checkpoint_id = "task-restart-checkpoint"
    episode = TaskEpisode(
        task_ref=task_ref,
        goal="Recreate a host-only task-resume boundary after a restart.",
        completed_action_refs=("artifact:restart-manifest",),
        outcome=TaskOutcome.INTERRUPTED,
    )
    first = SqliteMemoryLedger(str(path))
    first.setup()
    try:
        writer, scope = _writer(first, parent_scope, task_ref)
        _admit_episode(writer, record_id="restart-episode", payload=episode)
        adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)
        checkpoint = adapter.seal_checkpoint(
            checkpoint_id=checkpoint_id,
            token_budget=400,
        )
        assert checkpoint.checkpoint_id == checkpoint_id
    finally:
        first.close()

    restarted = SqliteMemoryLedger(str(path))
    restarted.setup()
    try:
        writer, scope = _writer(restarted, parent_scope, task_ref)
        adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)
        request = await adapter.compose_checkpoint(
            checkpoint_id=checkpoint_id,
            inp=_input("What does the current PDF retrieval say after restart?"),
            user_message="Continue only from the restored host task mapping.",
        )
        envelope = json.loads(request.render_messages()[2]["content"])
        assert decode_task_resume_payload(envelope["records"][0]["content"]) == episode
    finally:
        restarted.close()


def test_adapter_fails_closed_before_sealing_on_malformed_or_cross_task_payload(
    resume_ledger,
    parent_scope,
):
    task_ref = "task:typed-boundary"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    wrong_payload = TaskEpisode(
        task_ref="task:other-boundary",
        goal="This payload belongs to a different task.",
        completed_action_refs=(),
        outcome=TaskOutcome.INTERRUPTED,
    )
    _admit_episode(writer, record_id="wrong-task-payload", payload=wrong_payload)
    adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)

    with pytest.raises(TaskResumePayloadBindingError, match="task_ref"):
        adapter.seal_checkpoint(
            checkpoint_id="cross-task-checkpoint",
            token_budget=400,
        )


@pytest.mark.parametrize(
    "content",
    [
        "unstructured host assertion is not a task-resume payload",
        TaskProcedure(
            task_ref="task:invalid-episode-shape",
            procedure_ref="procedure:must-not-pass",
            steps=("A procedure cannot masquerade as an episode.",),
            supporting_episode_refs=("episode:one",),
        ).to_json(),
    ],
)
def test_adapter_rejects_malformed_or_procedure_payload_in_an_episode_record(
    resume_ledger,
    parent_scope,
    content,
):
    task_ref = "task:invalid-episode-shape"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    _admit_episode_content(
        writer,
        record_id="invalid-episode-content",
        content=content,
    )
    adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)

    with pytest.raises(TaskResumePayloadBindingError, match="typed payload|kind"):
        adapter.seal_checkpoint(
            checkpoint_id="invalid-episode-checkpoint",
            token_budget=400,
        )


async def test_adapter_rejects_checkpoint_continuation_ref_mismatch(
    resume_ledger,
    parent_scope,
):
    task_ref = "task:continuation-boundary"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    _admit_episode(
        writer,
        record_id="continuation-episode",
        payload=TaskEpisode(
            task_ref=task_ref,
            goal="Check the existing task before a continuation.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    adapter, planner = _adapter(writer, parent_scope, task_ref, scope)
    plan = planner.plan(task=_TASK_DESCRIPTOR, token_budget=400, byte_budget=10_000)
    checkpoint = planner.checkpoint(
        plan,
        checkpoint_id="wrong-continuation-checkpoint",
        continuation_ref="task:another-boundary",
    )

    with pytest.raises(TaskResumeBindingError, match="continuation_ref"):
        await adapter.compose_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            inp=_input(),
            user_message="Do not compose a mismatched continuation.",
        )


async def test_adapter_preflights_direct_checkpoint_payloads_before_composition(
    resume_ledger,
    parent_scope,
    monkeypatch,
):
    task_ref = "task:preflight"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    _admit_episode_content(
        writer,
        record_id="preflight-malformed-episode",
        content="This host-confirmed episode is intentionally not typed JSON.",
    )
    adapter, planner = _adapter(writer, parent_scope, task_ref, scope)
    plan = planner.plan(task=_TASK_DESCRIPTOR, token_budget=400, byte_budget=10_000)
    checkpoint = planner.checkpoint(
        plan,
        checkpoint_id="preflight-malformed-checkpoint",
        continuation_ref=task_ref,
    )

    async def composition_must_not_run(*args, **kwargs):
        raise AssertionError("malformed payload reached checkpoint composition")

    monkeypatch.setattr(
        adapter._composer,
        "plan_checkpoint_messages",
        composition_must_not_run,
    )
    with pytest.raises(TaskResumePayloadBindingError, match="typed payload"):
        await adapter.compose_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            inp=_input(),
            user_message="Malformed task data must fail before composition.",
        )


async def test_task_scopes_cannot_resume_each_others_checkpoint(
    resume_ledger,
    parent_scope,
):
    writer_a, scope_a = _writer(resume_ledger, parent_scope, "task:scope-a")
    writer_b, scope_b = _writer(resume_ledger, parent_scope, "task:scope-b")
    _admit_episode(
        writer_a,
        record_id="scope-a-episode",
        payload=TaskEpisode(
            task_ref="task:scope-a",
            goal="Keep task A in its own backend scope.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    _admit_episode(
        writer_b,
        record_id="scope-b-episode",
        payload=TaskEpisode(
            task_ref="task:scope-b",
            goal="Keep task B in its own backend scope.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    adapter_a, _planner_a = _adapter(writer_a, parent_scope, "task:scope-a", scope_a)
    adapter_b, _planner_b = _adapter(writer_b, parent_scope, "task:scope-b", scope_b)
    checkpoint_b = adapter_b.seal_checkpoint(
        checkpoint_id="scope-b-checkpoint",
        token_budget=400,
    )

    with pytest.raises(KeyError, match="scope-b-checkpoint"):
        await adapter_a.compose_checkpoint(
            checkpoint_id=checkpoint_b.checkpoint_id,
            inp=_input(),
            user_message="Task A must not read task B's checkpoint.",
        )


async def test_same_task_ref_in_different_parent_threads_cannot_cross_resume(
    resume_ledger,
    parent_scope,
):
    task_ref = "task:shared-host-id"
    sibling_parent = MemoryScope(
        tenant=parent_scope.tenant,
        user=parent_scope.user,
        thread="conversation-18",
        kind=parent_scope.kind,
    )
    writer_a, scope_a = _writer(resume_ledger, parent_scope, task_ref)
    writer_b, scope_b = _writer(resume_ledger, sibling_parent, task_ref)
    assert scope_a != scope_b
    _admit_episode(
        writer_b,
        record_id="sibling-parent-episode",
        payload=TaskEpisode(
            task_ref=task_ref,
            goal="Keep this resume record inside its parent conversation.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    adapter_a, _planner_a = _adapter(writer_a, parent_scope, task_ref, scope_a)
    adapter_b, _planner_b = _adapter(writer_b, sibling_parent, task_ref, scope_b)
    checkpoint_b = adapter_b.seal_checkpoint(
        checkpoint_id="sibling-parent-checkpoint",
        token_budget=400,
    )

    with pytest.raises(KeyError, match="sibling-parent-checkpoint"):
        await adapter_a.compose_checkpoint(
            checkpoint_id=checkpoint_b.checkpoint_id,
            inp=_input(),
            user_message="A sibling conversation cannot claim this task checkpoint.",
        )


async def test_lifecycle_change_invalidates_task_resume_checkpoint(
    resume_ledger,
    parent_scope,
):
    task_ref = "task:lifecycle"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    active = _admit_episode(
        writer,
        record_id="lifecycle-episode",
        payload=TaskEpisode(
            task_ref=task_ref,
            goal="Forget selected episodes before any later resume.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)
    checkpoint = adapter.seal_checkpoint(
        checkpoint_id="lifecycle-task-checkpoint",
        token_budget=400,
    )
    writer.forget(
        active.record_id,
        expected_revision=active.revision,
        event_id="forget:lifecycle-task-episode",
    )

    with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
        await adapter.compose_checkpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            inp=_input(),
            user_message="A forgotten episode must not return.",
        )


@pytest.mark.parametrize("placement", ["user_message", "history", "final_messages"])
async def test_adapter_uses_the_composer_owned_ledger_lane_not_lookalike_messages(
    resume_ledger,
    parent_scope,
    placement,
):
    task_ref = "task:duplicate-envelope"
    writer, scope = _writer(resume_ledger, parent_scope, task_ref)
    _admit_episode(
        writer,
        record_id="duplicate-envelope-episode",
        payload=TaskEpisode(
            task_ref=task_ref,
            goal="Reject ambiguity in the fixed data lane.",
            completed_action_refs=(),
            outcome=TaskOutcome.INTERRUPTED,
        ),
    )
    adapter, _planner = _adapter(writer, parent_scope, task_ref, scope)
    checkpoint = adapter.seal_checkpoint(
        checkpoint_id="duplicate-envelope-checkpoint",
        token_budget=400,
    )
    duplicated = json.dumps({
        "schema_version": 1,
        "type": "protoprompt.ledger-recall",
        "records": [],
    })
    kwargs: dict[str, object] = {}
    if placement == "user_message":
        kwargs["user_message"] = duplicated
    elif placement == "history":
        kwargs["history"] = [{"role": "user", "content": duplicated}]
        kwargs["user_message"] = "The duplicate must fail closed."
    else:
        kwargs["final_messages"] = [{"role": "user", "content": duplicated}]

    request = await adapter.compose_checkpoint(
        checkpoint_id=checkpoint.checkpoint_id,
        inp=_input(),
        **kwargs,
    )
    data = json.loads(request.render_ledger_data())
    payload = decode_task_resume_payload(data["records"][0]["content"])

    assert isinstance(payload, TaskEpisode)
    assert payload.task_ref == task_ref
