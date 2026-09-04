"""Focused contract tests for the trusted-host task-resume demo bridge."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from protoprompt import ContextInput, InMemStore, RegexTokenCounter
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import MemoryKind, MemoryOrigin, MemoryWriter, task_resume_scope
from protoprompt.ledger.recall import StaleMemoryCheckpointError
from protoprompt.scope import MemoryScope
from protoprompt_ollama_chat.task_resume_demo import (
    TASK_RESUME_DEMO_SCHEMA_VERSION,
    TaskResumeDemoError,
    TaskResumeDemoHost,
    TaskResumeDemoReceipt,
    TaskResumeDemoSeed,
)
from protoprompt_ollama_chat.task_resume_state import TaskResumeBindingState


_CHECKPOINT_SECRET = b"ollama-chat-task-resume-demo-host-secret-0001"


class _NoopEmbeddingClient:
    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0] for _ in texts]


class _RecordingBuilder(TokenBudgetedContextBuilder):
    """Keep the live query observable without adding a second test API."""

    def __init__(self, scope: MemoryScope) -> None:
        super().__init__(
            InMemStore(),
            _NoopEmbeddingClient(),
            counter=RegexTokenCounter(),
            max_tokens=1_024,
            output_reserve=64,
            scope=scope,
        )
        self.seen_queries: list[str] = []

    async def _plan_messages_with_host_prefix(self, inp: ContextInput, *args: Any, **kwargs: Any):
        self.seen_queries.append(inp.query)
        return await super()._plan_messages_with_host_prefix(inp, *args, **kwargs)


class _BlockingBuilder(_RecordingBuilder):
    """Stop after binding lookup to exercise close-vs-compose revalidation."""

    def __init__(
        self,
        scope: MemoryScope,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(scope)
        self._started = started
        self._release = release

    async def _plan_messages_with_host_prefix(
        self,
        inp: ContextInput,
        *args: Any,
        **kwargs: Any,
    ):
        self._started.set()
        await self._release.wait()
        return await super()._plan_messages_with_host_prefix(inp, *args, **kwargs)


def _host(tmp_path: Path) -> TaskResumeDemoHost:
    return TaskResumeDemoHost(
        state_path=tmp_path / "chat.db",
        ledger_path=tmp_path / "task-resume-ledger.db",
        checkpoint_secret=_CHECKPOINT_SECRET,
    )


def _seed(conversation_id: str = "demo-conversation") -> TaskResumeDemoSeed:
    return TaskResumeDemoSeed(
        conversation_id=conversation_id,
        task_descriptor="DESCRIPTOR_NOT_FOR_MODEL: safely resume launch demonstration",
        goal="GOAL_FOR_MODEL: confirm the approved local launch state",
        completed_action_refs=("ACTION_REF_NOT_FOR_MODEL:prepare-001",),
        outcome="interrupted",
        next_action="NEXT_ACTION_FOR_MODEL: inspect the local service status",
        lesson="LESSON_FOR_MODEL: retain a bounded host-confirmed episode",
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("conversation_id", "not allowed\n", "conversation_id"),
        ("task_descriptor", "   ", "task_descriptor"),
        ("goal", ["not text"], "goal"),
        ("completed_action_refs", ("one", "one"), "duplicates"),
        ("outcome", "unknown", "unknown"),
    ],
)
def test_seed_rejects_malformed_host_values(
    field: str,
    value: object,
    error: str,
) -> None:
    values: dict[str, object] = {
        "conversation_id": "valid-conversation",
        "task_descriptor": "valid descriptor",
        "goal": "valid goal",
        "completed_action_refs": ("action:one",),
        "outcome": "interrupted",
        "next_action": "valid next action",
        "lesson": "valid lesson",
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError), match=error):
        TaskResumeDemoSeed(**values)  # type: ignore[arg-type]


def _builder_factory(created: list[_RecordingBuilder]):
    def factory(scope: MemoryScope) -> TokenBudgetedContextBuilder:
        builder = _RecordingBuilder(scope)
        created.append(builder)
        return builder

    return factory


def _context(query: str = "CURRENT_RAG_QUERY: what does the uploaded PDF say?") -> ContextInput:
    return ContextInput(
        query=query,
        system_prompt="Answer from bounded local reference data.",
        include_rag=False,
        include_session=False,
    )


async def test_host_seed_admits_one_episode_and_model_messages_are_safe(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    created: list[_RecordingBuilder] = []
    seed = _seed()
    try:
        receipt = host.seed(seed, builder_factory=_builder_factory(created))

        assert receipt == TaskResumeDemoReceipt(
            schema_version=TASK_RESUME_DEMO_SCHEMA_VERSION,
            contract_id="ledger-task-episode-resume-v1",
            operation="seeded",
            active=True,
            selected_episode_count=1,
        )
        assert host.status(seed.conversation_id).active
        assert host.explain()["browser_api"] is False
        assert host.explain()["auto_admission"] is False

        # Host internals are inspected only here to prove that one strict,
        # confirmed Episode exists; no public host method returns this binding.
        binding = host._state.load_active(seed.conversation_id)
        assert binding is not None
        writer = MemoryWriter(
            host._ledger,
            scope=task_resume_scope(binding.parent_scope, task_ref=binding.task_ref),
            actor="test-host",
        )
        records = writer.list_active()
        assert len(records) == 1
        assert records[0].kind is MemoryKind.EPISODE
        assert records[0].origin is MemoryOrigin.HOST_ASSERTION

        request = await host.compose_active(
            seed.conversation_id,
            inp=_context(),
            builder_factory=_builder_factory(created),
            user_message="CURRENT_USER_MESSAGE: summarize the live PDF query",
        )
        assert request is not None
        assert created[-1].seen_queries == [
            "CURRENT_RAG_QUERY: what does the uploaded PDF say?"
        ]
        assert request.render_messages()[-1] == {
            "role": "user",
            "content": "CURRENT_USER_MESSAGE: summarize the live PDF query",
        }

        model_payload = json.dumps(
            request.render_messages(),
            ensure_ascii=False,
            sort_keys=True,
        )
        forbidden = (
            binding.task_ref,
            binding.task_descriptor,
            binding.checkpoint_id,
            seed.completed_action_refs[0],
            host._source_ref(binding.task_ref),
        )
        assert all(value not in model_payload for value in forbidden)
        assert seed.goal in model_payload
        assert seed.next_action in model_payload
        assert seed.lesson in model_payload
    finally:
        host.close()


async def test_active_mapping_survives_restart_without_persisting_raw_secret(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "chat.db"
    ledger_path = tmp_path / "task-resume-ledger.db"
    seed = _seed()
    created: list[_RecordingBuilder] = []

    first = TaskResumeDemoHost(
        state_path=state_path,
        ledger_path=ledger_path,
        checkpoint_secret=_CHECKPOINT_SECRET,
    )
    first.seed(seed, builder_factory=_builder_factory(created))
    first.close()

    restarted = TaskResumeDemoHost(
        state_path=state_path,
        ledger_path=ledger_path,
        checkpoint_secret=_CHECKPOINT_SECRET,
    )
    try:
        assert restarted.status(seed.conversation_id).active
        request = await restarted.compose_active(
            seed.conversation_id,
            inp=_context("CURRENT_RAG_QUERY: resume after restart"),
            builder_factory=_builder_factory(created),
            user_message="CURRENT_USER_MESSAGE: continue",
        )
        assert request is not None
        assert created[-1].seen_queries == ["CURRENT_RAG_QUERY: resume after restart"]
    finally:
        restarted.close()

    assert _CHECKPOINT_SECRET not in state_path.read_bytes()
    assert _CHECKPOINT_SECRET not in ledger_path.read_bytes()


def test_failed_state_create_rolls_back_the_unbound_admitted_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host(tmp_path)
    created: list[_RecordingBuilder] = []
    seed = _seed()
    admitted_task_refs: list[str] = []
    original_admit = host._admit_one_episode

    def remember_admission(
        writer: MemoryWriter,
        *,
        task_ref: str,
        episode: Any,
    ) -> None:
        admitted_task_refs.append(task_ref)
        original_admit(writer, task_ref=task_ref, episode=episode)

    def fail_state_create(**_: Any) -> None:
        raise RuntimeError("simulated state create failure")

    try:
        monkeypatch.setattr(host, "_admit_one_episode", remember_admission)
        monkeypatch.setattr(host._state, "create", fail_state_create)
        with pytest.raises(RuntimeError, match="state create failure"):
            host.seed(seed, builder_factory=_builder_factory(created))

        assert len(admitted_task_refs) == 1
        assert not host.status(seed.conversation_id).active
        parent_scope = host._state.parent_scope_for(seed.conversation_id)
        writer = MemoryWriter(
            host._ledger,
            scope=task_resume_scope(parent_scope, task_ref=admitted_task_refs[0]),
            actor="test-host",
        )
        # The raw record may remain as an operational receipt, but it is no
        # longer active/recallable and the sealed checkpoint was invalidated by
        # the source-forget rollback.
        assert writer.list_active() == []
    finally:
        host.close()


async def test_changed_episode_lifecycle_fails_before_any_model_composition(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    created: list[_RecordingBuilder] = []
    seed = _seed()
    try:
        host.seed(seed, builder_factory=_builder_factory(created))
        binding = host._state.load_active(seed.conversation_id)
        assert binding is not None
        writer = MemoryWriter(
            host._ledger,
            scope=task_resume_scope(binding.parent_scope, task_ref=binding.task_ref),
            actor="test-host",
        )
        writer.forget_by_source(host._source_ref(binding.task_ref))

        with pytest.raises(StaleMemoryCheckpointError, match="no longer active"):
            await host.compose_active(
                seed.conversation_id,
                inp=_context(),
                builder_factory=_builder_factory(created),
                user_message="CURRENT_USER_MESSAGE: should not compose",
            )
        assert host.status(seed.conversation_id).active
        # The builder exists because the trusted factory is instantiated, but
        # checkpoint preflight fails before it receives the current request.
        assert created[-1].seen_queries == []
    finally:
        host.close()


async def test_close_failure_leaves_closing_mapping_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host(tmp_path)
    created: list[_RecordingBuilder] = []
    seed = _seed()
    host.seed(seed, builder_factory=_builder_factory(created))

    def fail_cleanup(self: MemoryWriter, source_ref: str, *, reason_code: str = "source_revoked"):
        raise RuntimeError("simulated ledger cleanup failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(MemoryWriter, "forget_by_source", fail_cleanup)
            with pytest.raises(RuntimeError, match="cleanup failure"):
                host.close_binding(seed.conversation_id)

        # The state row is no longer active, even though cleanup failed.  The
        # host does not compose a closing binding or silently make it active.
        assert not host.status(seed.conversation_id).active
        closing = host._state.begin_close(seed.conversation_id)
        assert closing is not None
        assert closing.state is TaskResumeBindingState.CLOSING
        assert await host.compose_active(
            seed.conversation_id,
            inp=_context(),
            builder_factory=_builder_factory(created),
            user_message="CURRENT_USER_MESSAGE: must not resume while closing",
        ) is None

        receipt = host.close_binding(seed.conversation_id)
        assert receipt.operation == "closed"
        assert not receipt.active
        assert not host.status(seed.conversation_id).active
    finally:
        host.close()


async def test_closing_binding_rejects_an_inflight_provider_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan paused at retrieval cannot return after its mapping closes."""

    host = _host(tmp_path)
    seed = _seed()
    started = asyncio.Event()
    release = asyncio.Event()
    try:
        host.seed(seed, builder_factory=_builder_factory([]))

        def blocking_factory(scope: MemoryScope) -> TokenBudgetedContextBuilder:
            return _BlockingBuilder(scope, started=started, release=release)

        pending = asyncio.create_task(
            host.compose_active(
                seed.conversation_id,
                inp=_context(),
                builder_factory=blocking_factory,
                user_message="CURRENT_USER_MESSAGE: must not be sent after close",
            )
        )
        await started.wait()

        def fail_cleanup(
            self: MemoryWriter,
            source_ref: str,
            *,
            reason_code: str = "source_revoked",
        ) -> None:
            raise RuntimeError("simulated cleanup failure")

        with monkeypatch.context() as patch:
            patch.setattr(MemoryWriter, "forget_by_source", fail_cleanup)
            with pytest.raises(RuntimeError, match="cleanup failure"):
                host.close_binding(seed.conversation_id)

        release.set()
        with pytest.raises(TaskResumeDemoError, match="binding changed"):
            await pending
    finally:
        release.set()
        if not host._closed:
            host.close()
