from __future__ import annotations

import pytest

from protoprompt.session.strategy import LLMSummaryStrategy
from protoprompt.session.types import Session

from _mocks import MockLLM


@pytest.mark.asyncio
async def test_short_session_falls_back_to_heuristic():
    from protoprompt.session.strategy import HeuristicStrategy
    llm = MockLLM()
    # The fallback HeuristicStrategy needs a permissive min_messages.
    strat = LLMSummaryStrategy(
        min_messages=6,
        window_size=2,
        max_blocks=2,
        fallback=HeuristicStrategy(min_messages=1),
    )
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    blocks = await strat.compress(session, llm)
    # Heuristic fallback yields at least the head block.
    assert len(blocks) >= 1
    assert blocks[0].metadata.get("segment") == "head"


@pytest.mark.asyncio
async def test_long_session_uses_llm():
    llm = MockLLM()
    strat = LLMSummaryStrategy(min_messages=4, window_size=3, max_blocks=2)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": f"msg {i} with some content " * 3}
        for i in range(8)
    ])
    blocks = await strat.compress(session, llm)
    assert blocks
    assert all(b.metadata.get("segment") == "summary" for b in blocks)
    assert llm.chat_calls, "LLM must be called at least once for long sessions"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_heuristic():
    class FailingLLM(MockLLM):
        async def chat(self, messages, model="", **options):
            raise RuntimeError("simulated LLM outage")

    llm = FailingLLM()
    strat = LLMSummaryStrategy(min_messages=4, window_size=2, max_blocks=2)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "что-то важное про решение задачи " * 5}
        for _ in range(6)
    ])
    blocks = await strat.compress(session, llm)
    # Heuristic still produces a head block for a 6-message session.
    assert blocks
    segments = {b.metadata.get("segment") for b in blocks}
    assert "head" in segments


@pytest.mark.asyncio
async def test_window_count_capped_by_max_blocks():
    llm = MockLLM()
    strat = LLMSummaryStrategy(min_messages=2, window_size=2, max_blocks=2)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": f"msg {i}"} for i in range(10)
    ])
    blocks = await strat.compress(session, llm)
    assert len(blocks) <= 2


@pytest.mark.asyncio
async def test_prompt_uses_russian_template_by_default():
    llm = MockLLM()
    strat = LLMSummaryStrategy(min_messages=2, window_size=2, max_blocks=1)
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "abc " * 20},
        {"role": "user", "content": "def " * 20},
        {"role": "user", "content": "ghi " * 20},
        {"role": "user", "content": "jkl " * 20},
    ])
    await strat.compress(session, llm)
    sent = llm.chat_calls[0]["messages"][0]["content"]
    assert "Сжатие" in sent or "сжимает" in sent.lower()


@pytest.mark.asyncio
async def test_prompt_uses_english_template():
    llm = MockLLM()
    strat = LLMSummaryStrategy(min_messages=2, window_size=2, max_blocks=1, language="en")
    session = Session(chat_id="c1", messages=[
        {"role": "user", "content": "abc " * 20},
        {"role": "user", "content": "def " * 20},
        {"role": "user", "content": "ghi " * 20},
        {"role": "user", "content": "jkl " * 20},
    ])
    await strat.compress(session, llm)
    sent = llm.chat_calls[0]["messages"][0]["content"]
    assert "Summary" in sent
