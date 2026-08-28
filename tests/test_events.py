from __future__ import annotations

import json

import pytest

from protoprompt import (
    CacheEvent,
    CachedLLMClient,
    ContextBuilder,
    ContextEvent,
    ContextInput,
    EventDispatcher,
    InMemStore,
    MemoryScope,
    Pipeline,
    PipelineHooks,
    ProfileEvent,
    RecallEvent,
    RetrieveEvent,
    Session,
)
from protoprompt.agent import WorkingMemory
from protoprompt.profile.manager import ProfileManager
from protoprompt.profile.store import InMemoryProfileStore
from protoprompt.profile.types import Signal

from _mocks import MockLLM


def test_dispatcher_redacts_content_recursively_and_keeps_metrics():
    received = []
    dispatcher = EventDispatcher(received.append)
    dispatcher.emit(ContextEvent(
        action="completed",
        attributes={
            "prompt": "private prompt",
            "nested": {"content": "private document"},
            "token_count": 17,
        },
    ))

    payload = received[0].to_dict()
    rendered = json.dumps(payload)
    assert "private prompt" not in rendered
    assert "private document" not in rendered
    assert payload["attributes"]["prompt"] == "[REDACTED]"
    assert payload["attributes"]["token_count"] == 17


@pytest.mark.asyncio
async def test_context_and_retrieve_events_share_trace_without_raw_content():
    events = []
    store = InMemStore()
    store.add("doc", ["private document body"], [[1.0, 0.0]], {"kind": "document"})
    scope = MemoryScope(tenant="private-tenant", user="private-user")
    # Re-index into the scoped physical namespace through the public API.
    from protoprompt.rag import DocumentIndexer

    llm = MockLLM(embed_dim=2)
    await DocumentIndexer(store, llm, scope=scope).index("scoped", "private scoped body")
    builder = ContextBuilder(store, llm, scope=scope, event_sink=events.append)
    out = await builder.build(ContextInput(query="private question", doc_ids=["scoped"]))

    assert out.rag_blocks
    context_event = next(event for event in events if isinstance(event, ContextEvent))
    retrieve_event = next(event for event in events if isinstance(event, RetrieveEvent))
    assert context_event.trace_id == retrieve_event.trace_id
    assert context_event.scope_id == scope.correlation_id()
    rendered = json.dumps([event.to_dict() for event in events])
    assert "private question" not in rendered
    assert "private scoped body" not in rendered
    assert "private-tenant" not in rendered
    assert "private-user" not in rendered


@pytest.mark.asyncio
async def test_pipeline_typed_events_wrap_legacy_hooks():
    order: list[str] = []
    pipeline = Pipeline(
        InMemStore(),
        MockLLM(embed_dim=4),
        compress_every_n=6,
        event_sink=lambda event: order.append(f"event:{event.action}"),
        hooks=PipelineHooks(
            on_skip_compress=lambda session: order.append("hook:skip"),
            on_before_compress=lambda session: order.append("hook:before"),
            on_after_compress=lambda session, blocks: order.append("hook:after"),
        ),
    )
    await pipeline.compress_and_store(Session(chat_id="short", messages=[]))
    assert order == ["event:skipped", "hook:skip"]

    messages = [
        {"role": "user", "content": f"private message {index} plan"}
        for index in range(8)
    ]
    await pipeline.compress_and_store(Session(chat_id="long", messages=messages))
    assert order[-4:] == [
        "event:started",
        "hook:before",
        "event:completed",
        "hook:after",
    ]


@pytest.mark.asyncio
async def test_profile_cache_recall_and_evict_events_have_safe_shapes():
    events = []
    scope = MemoryScope(tenant="acme", user="u1")
    manager = ProfileManager(
        InMemoryProfileStore(),
        scope=scope,
        event_sink=events.append,
    )
    await manager.update(
        "u1",
        [Signal(user_id="u1", kind="message", role="user", text="private profile")],
    )

    cached = CachedLLMClient(MockLLM(embed_dim=4), scope=scope, event_sink=events.append)
    await cached.embed(["private cached text"], model="m")
    await cached.embed(["private cached text"], model="m")

    memory = WorkingMemory(
        store=InMemStore(),
        max_tokens=30,
        scope=scope,
        event_sink=events.append,
    )
    item_id = await memory.add("file", "def private_symbol(): pass")
    await memory.forget(item_id)
    await memory.recall("private_symbol")

    assert any(isinstance(event, ProfileEvent) for event in events)
    cache_events = [event for event in events if isinstance(event, CacheEvent)]
    assert [event.attributes["hit_count"] for event in cache_events] == [0, 1]
    assert any(event.event_name == "evict" for event in events)
    assert any(isinstance(event, RecallEvent) for event in events)
    rendered = json.dumps([event.to_dict() for event in events])
    assert "private profile" not in rendered
    assert "private cached text" not in rendered
    assert "private_symbol" not in rendered


def test_failing_event_sink_is_non_fatal(caplog):
    def fail(event):
        raise RuntimeError("telemetry unavailable")

    dispatcher = EventDispatcher(fail)
    dispatcher.emit(CacheEvent(action="lookup", attributes={"hit_count": 1}))
    assert "event sink failed" in caplog.text
