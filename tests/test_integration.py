from __future__ import annotations

import pytest

from protoprompt import ContextBuilder, ContextInput, Pipeline, Session
from protoprompt.profile.builder import ProfileBuilder
from protoprompt.session.strategy import HeuristicStrategy
from protoprompt.store.memory import InMemStore

from _mocks import MockLLM


@pytest.mark.asyncio
async def test_profile_builder_logs_and_returns_minimal(caplog):
    class BadLLM(MockLLM):
        async def chat(self, messages, model="", **options):
            return "not-json-at-all"

    llm = BadLLM()
    builder = ProfileBuilder(llm)
    profile = await builder.build("u1", [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "world"},
    ])
    assert profile.user_id == "u1"
    assert profile.summary == "Не удалось построить профиль"
    assert "Failed to build profile" in caplog.text


@pytest.mark.asyncio
async def test_pipeline_overwrite_survives_no_state_loss():
    """Two consecutive compressions must end with a clean state (no _new residue)."""

    store = InMemStore()
    llm = MockLLM()
    pipeline = Pipeline(store, llm, compress_every_n=3)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": f"msg {i}"} for i in range(5)
    ])
    await pipeline.compress_and_store(session)
    after_first = store.count()

    session.messages.extend([
        {"role": "user", "content": f"more {i}"} for i in range(3)
    ])
    await pipeline.compress_and_store(session)
    after_second = store.count()

    # No `_new` residue should remain.
    assert all("_new" not in str(k) for k in store._chunks.keys())


@pytest.mark.asyncio
async def test_heuristic_min_messages_short_circuits():
    strat = HeuristicStrategy(min_messages=10)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "msg"} for _ in range(5)
    ])
    blocks = await strat.compress(session, MockLLM())
    assert blocks == []


@pytest.mark.asyncio
async def test_context_builder_full_flow(mock_llm):
    store = InMemStore()
    store.add("1", ["doc alpha"], [[0.5] * 16])
    store.add(
        "session_c1",
        ["user asked about alpha"],
        [[0.5] * 16],
        {"chat_id": "c1"},
    )
    builder = ContextBuilder(store, mock_llm)
    out = await builder.build(ContextInput(
        query="q",
        system_prompt="sys",
        chat_id="c1",
        doc_ids=[1],
        include_profile=True,
        profile_text="expert",
    ))
    assert "sys" in out.system_prompt
    assert "alpha" in out.system_prompt
    assert out.profile_used is True
    assert out.rag_blocks
    assert out.session_blocks
