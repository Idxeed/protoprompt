from __future__ import annotations

import pytest

from protoprompt import InMemStore, MemoryScope
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.integrations.agents_sdk import (
    ProtoPromptSession,
    create_session_input_callback,
)
from protoprompt.rag import DocumentIndexer
from protoprompt.tokens.regex_counter import RegexTokenCounter

from _mocks import MockLLM

agents = pytest.importorskip("agents")
from agents.memory.session import Session as AgentsSession  # noqa: E402


@pytest.mark.asyncio
async def test_protoprompt_session_matches_upstream_protocol_and_tail_semantics(tmp_path):
    session = ProtoPromptSession("thread-1", tmp_path / "agents.db")
    assert isinstance(session, AgentsSession)
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    await session.add_items(items)
    assert await session.get_items(limit=2) == items[-2:]
    assert await session.pop_item() == items[-1]
    assert await session.get_items() == items[:-1]
    await session.clear_session()
    assert await session.get_items() == []
    assert await session.pop_item() is None


@pytest.mark.asyncio
async def test_budgeted_session_callback_injects_recall_and_preserves_new_turn():
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    scope = MemoryScope(tenant="acme", user="alice", thread="thread-1")
    await DocumentIndexer(store, llm, scope=scope).index(
        "contract",
        "The old contract renews in May",
    )
    builder = TokenBudgetedContextBuilder(
        store,
        llm,
        counter=RegexTokenCounter(),
        max_tokens=45,
        scope=scope,
    )
    callback = create_session_input_callback(
        builder,
        session_id="thread-1",
        system_prompt="You are a legal assistant.",
    )
    history = [
        {"role": "user", "content": f"old turn {index} " + "noise " * 8}
        for index in range(8)
    ]
    new_input = [{"role": "user", "content": "When does the contract renew?"}]

    prepared = await callback(history, new_input)

    assert prepared[-1] == new_input[0]
    assert prepared.count(new_input[0]) == 1
    assert prepared[0]["role"] == "system"
    assert "renews in May" in prepared[0]["content"]
    assert len(prepared) < len(history) + len(new_input) + 1
    assert builder.last_report.history_kept == len(prepared) - 2


@pytest.mark.asyncio
async def test_session_callback_reads_structured_agents_content_blocks():
    store = InMemStore()
    llm = MockLLM(embed_dim=4)
    builder = TokenBudgetedContextBuilder(
        store,
        llm,
        max_tokens=30,
    )
    seen_queries: list[str] = []

    def factory(query: str):
        from protoprompt import ContextInput

        seen_queries.append(query)
        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    new_input = [{
        "role": "user",
        "content": [{"type": "input_text", "text": "structured question"}],
    }]
    prepared = await callback([], new_input)
    assert seen_queries == ["structured question"]
    assert prepared[-1] == new_input[0]
