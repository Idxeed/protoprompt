from __future__ import annotations

import pytest

from protoprompt.pipeline import Pipeline
from protoprompt.session.types import Session
from protoprompt.session.strategy import HeuristicStrategy
from protoprompt.store.memory import InMemStore


class MockLLM:
    async def chat(self, messages, model="", **options):
        return "mocked"

    async def embed(self, texts, model=""):
        return [[float(ord(c) % 10) / 10.0 for c in t[:10].ljust(10, "a")] for t in texts]


@pytest.mark.asyncio
async def test_pipeline_should_not_compress_under_threshold():
    store = InMemStore()
    llm = MockLLM()
    pipeline = Pipeline(store, llm, compress_every_n=10)
    assert pipeline.should_compress(5) is False
    assert pipeline.should_compress(10) is True
    assert pipeline.should_compress(15) is True


@pytest.mark.asyncio
async def test_pipeline_compress_and_store():
    store = InMemStore()
    llm = MockLLM()
    pipeline = Pipeline(store, llm, compress_every_n=5, embedding_model="test")

    session = Session(
        chat_id="chat_1",
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(8)],
        strategy="heuristic",
    )
    blocks = await pipeline.compress_and_store(session)

    assert len(blocks) >= 1
    doc_id = f"session_{session.chat_id}"
    assert store.count() == len(blocks)

    results = store.query([0.1] * 10, top_k=5)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_pipeline_empty_session_noop():
    store = InMemStore()
    llm = MockLLM()
    pipeline = Pipeline(store, llm, compress_every_n=2)

    session = Session(chat_id="empty", messages=[])
    blocks = await pipeline.compress_and_store(session)
    assert blocks == []
    assert store.count() == 0


@pytest.mark.asyncio
async def test_pipeline_replaces_previous_compression():
    store = InMemStore()
    llm = MockLLM()
    pipeline = Pipeline(store, llm, compress_every_n=3)

    session = Session(
        chat_id="chat_r",
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(5)],
    )
    await pipeline.compress_and_store(session)
    count_first = store.count()

    session2 = Session(
        chat_id="chat_r",
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(7)],
    )
    await pipeline.compress_and_store(session2)

    assert store.count() > 0
