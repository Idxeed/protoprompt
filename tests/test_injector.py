from __future__ import annotations

import pytest

from protoprompt.context import ContextOutput
from protoprompt.injector import ContextBuilder, ContextInput
from protoprompt.store.memory import InMemStore


class MockLLM:
    async def chat(self, messages, model="", **options):
        return "mocked"

    async def embed(self, texts, model=""):
        return [[float(ord(c) % 10) / 10.0 for c in t[:10].ljust(10, "a")] for t in texts]


def test_context_output_positional_constructor_is_backward_compatible():
    out = ContextOutput("sys", ["rag"], ["session"], True, None)
    assert out.rag_blocks == ["rag"]
    assert out.session_blocks == ["session"]
    assert out.rag_chunks == []


@pytest.mark.asyncio
async def test_context_without_rag_and_session():
    store = InMemStore()
    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    inp = ContextInput(
        query="hello",
        system_prompt="You are helpful",
        include_rag=False,
        include_session=False,
    )
    out = await builder.build(inp)
    assert out.system_prompt == "You are helpful"
    assert out.rag_blocks == []
    assert out.session_blocks == []


@pytest.mark.asyncio
async def test_context_with_rag_injection():
    store = InMemStore()
    store.add("1", ["Paris is the capital of France"], [[0.5] * 10])

    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    inp = ContextInput(
        query="What is the capital of France?",
        system_prompt="You are helpful",
        doc_ids=[1],
        include_rag=True,
        include_session=False,
    )
    out = await builder.build(inp)
    assert "Paris" in out.system_prompt
    assert len(out.rag_blocks) == 1


@pytest.mark.asyncio
async def test_context_with_session_injection():
    store = InMemStore()
    store.add(
        "session_chat_42",
        ["User asked about weather, assistant provided forecast for Moscow"],
        [[0.5] * 10],
        {"chat_id": "chat_42", "strategy": "heuristic"},
    )

    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    inp = ContextInput(
        query="What about tomorrow?",
        system_prompt="You are helpful",
        chat_id="chat_42",
        include_rag=False,
        include_session=True,
        top_k_session=3,
    )
    out = await builder.build(inp)
    assert "Moscow" in out.system_prompt
    assert len(out.session_blocks) == 1


@pytest.mark.asyncio
async def test_context_empty_store_graceful():
    store = InMemStore()
    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    inp = ContextInput(
        query="hello",
        system_prompt="base",
        include_rag=True,
        include_session=True,
    )
    out = await builder.build(inp)
    assert out.system_prompt == "base"
    assert out.rag_blocks == []
    assert out.session_blocks == []
