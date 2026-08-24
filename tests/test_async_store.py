"""Tests for async store support: AsyncInMemStore, AsyncStoreWrapper,
as_async dispatch, and sync/async transparency in builders/pipeline."""

from __future__ import annotations

import pytest

from protoprompt import (
    ContextBuilder,
    ContextInput,
    InMemStore,
    Pipeline,
)
from protoprompt.store import (
    AsyncInMemStore,
    AsyncStoreWrapper,
    as_async,
    await_if_needed,
    is_async_store,
)

from _mocks import MockLLM


async def _populate(store, llm=None) -> None:
    texts = ["alpha text", "beta text"]
    if llm is not None:
        embs = await llm.embed(texts)
    else:
        embs = [[1.0, 0.0], [0.0, 1.0]]
    await await_if_needed(store.add("doc-1", texts, embs, {"kind": "fact"}))


async def test_await_if_needed_passthrough_sync():
    assert await await_if_needed(42) == 42
    assert await await_if_needed("x") == "x"


async def test_is_async_store_detection():
    assert not is_async_store(InMemStore())
    assert is_async_store(AsyncInMemStore())
    assert is_async_store(AsyncStoreWrapper(InMemStore()))


async def test_as_async_returns_wrapper_for_sync():
    sync = InMemStore()
    wrapped = as_async(sync)
    assert isinstance(wrapped, AsyncStoreWrapper)
    assert wrapped.sync_store is sync


async def test_as_async_passthrough_for_async():
    store = AsyncInMemStore()
    assert as_async(store) is store


async def test_async_in_mem_store_semantics():
    store = AsyncInMemStore()
    await _populate(store)
    assert await store.count() == 2
    hits = await store.query([1.0, 0.0], top_k=1)
    assert hits[0]["document"] == "alpha text"
    await store.delete("doc-1")
    assert await store.count() == 0


async def test_wrapper_matches_sync_results():
    sync = InMemStore()
    await _populate(sync)
    wrapped = AsyncStoreWrapper(sync)
    hits = await wrapped.query([0.0, 1.0], top_k=2, where={"kind": "fact"})
    assert {h["document"] for h in hits} == {"alpha text", "beta text"}


async def test_context_builder_with_async_store():
    llm = MockLLM(embed_dim=2)
    store = AsyncInMemStore()
    await _populate(store, llm)
    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(query="beta text", doc_ids=["doc-1"]))
    assert out.rag_blocks[0] == "beta text"


async def test_context_builder_with_wrapped_sync_store():
    llm = MockLLM(embed_dim=2)
    store = as_async(InMemStore())
    await _populate(store, llm)
    builder = ContextBuilder(store, llm)
    out = await builder.build(ContextInput(query="alpha text", doc_ids=["doc-1"]))
    assert out.rag_blocks[0] == "alpha text"


async def test_pipeline_with_async_store():
    store = AsyncInMemStore()
    llm = MockLLM(embed_dim=4)
    pipeline = Pipeline(store, llm, compress_every_n=2)
    session_msgs = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} хочу план"}
        for i in range(6)
    ]
    from protoprompt import Session

    blocks = await pipeline.compress_and_store(Session(chat_id="c1", messages=session_msgs))
    assert blocks
    docs = await store.query([1.0] * 4, top_k=10, where={"doc_id": "session_c1"})
    assert len(docs) == len(blocks)
