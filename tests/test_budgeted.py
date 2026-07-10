from __future__ import annotations

import pytest

from protoprompt import (
    ContextInput,
    TokenBudgetedContextBuilder,
    TokenBudgetExceededError,
)
from protoprompt.store.memory import InMemStore
from protoprompt.tokens import RegexTokenCounter

from _mocks import MockLLM


@pytest.mark.asyncio
async def test_system_prompt_overflow_raises():
    store = InMemStore()
    llm = MockLLM()
    builder = TokenBudgetedContextBuilder(store, llm, max_tokens=3)
    inp = ContextInput(
        query="q",
        system_prompt="this system prompt is far too long for the budget",
    )
    with pytest.raises(TokenBudgetExceededError) as exc:
        await builder.build(inp)
    assert exc.value.section == "system"


@pytest.mark.asyncio
async def test_no_query_embedding_when_not_needed():
    store = InMemStore()
    llm = MockLLM()
    builder = TokenBudgetedContextBuilder(store, llm, max_tokens=1000)
    inp = ContextInput(
        query="q",
        system_prompt="You are helpful",
        include_rag=False,
        include_session=False,
    )
    out = await builder.build(inp)
    assert llm.embed_calls == []
    assert out.system_prompt == "You are helpful"


@pytest.mark.asyncio
async def test_rag_blocks_truncated_to_budget():
    store = InMemStore()
    for i in range(4):
        store.add(
            "1",
            [f"document number {i} has some content " * 5],
            [[0.5] * 16],
        )
    llm = MockLLM()
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        store, llm, counter=counter, max_tokens=40
    )
    inp = ContextInput(
        query="q",
        system_prompt="sys",
        doc_ids=[1],
        top_k_rag=4,
    )
    out = await builder.build(inp)
    total = counter.count(out.system_prompt)
    assert total <= 40
    assert out.rag_blocks  # at least one RAG block fits


@pytest.mark.asyncio
async def test_profile_dropped_when_over_budget():
    store = InMemStore()
    llm = MockLLM()
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        store, llm, counter=counter, max_tokens=10
    )
    inp = ContextInput(
        query="q",
        system_prompt="sys",
        include_profile=True,
        profile_text="very long profile " * 50,
    )
    out = await builder.build(inp)
    assert out.profile_used is False
    assert "profile" in (out.budget_report.dropped_blocks if out.budget_report else [])


@pytest.mark.asyncio
async def test_budget_report_attached():
    store = InMemStore()
    llm = MockLLM()
    builder = TokenBudgetedContextBuilder(store, llm, max_tokens=200)
    out = await builder.build(ContextInput(query="q", system_prompt="sys"))
    assert out.budget_report is not None
    assert out.budget_report.budget == 200
    assert out.budget_report.used_tokens >= 1
    assert out.budget_report.remaining_tokens >= 0


@pytest.mark.asyncio
async def test_priorities_order_changes_allocation():
    store = InMemStore()
    store.add("1", ["rag " * 30], [[0.9] * 16])
    store.add(
        "session_c1",
        ["session " * 30],
        [[0.1] * 16],
        {"chat_id": "c1"},
    )
    llm = MockLLM()

    # Session first: the higher-scored RAG block is dropped.
    b1 = TokenBudgetedContextBuilder(
        store, llm, max_tokens=20,
        priorities=("system", "session", "rag"),
    )
    out1 = await b1.build(ContextInput(
        query="q", system_prompt="", chat_id="c1", doc_ids=[1]
    ))

    # RAG first: session is dropped.
    b2 = TokenBudgetedContextBuilder(
        store, llm, max_tokens=20,
        priorities=("system", "rag", "session"),
    )
    out2 = await b2.build(ContextInput(
        query="q", system_prompt="", chat_id="c1", doc_ids=[1]
    ))

    assert out1.rag_blocks == []
    assert out2.session_blocks == []


@pytest.mark.asyncio
async def test_truncate_handles_zero_budget():
    store = InMemStore()
    llm = MockLLM()
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(store, llm, counter=counter, max_tokens=2)
    store.add("1", ["some long document"], [[0.5] * 16])
    out = await builder.build(ContextInput(
        query="q", system_prompt="x", doc_ids=[1], top_k_rag=1
    ))
    # system "x" already consumed the entire 2-token budget, RAG dropped.
    assert out.rag_blocks == []
