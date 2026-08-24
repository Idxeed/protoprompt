from __future__ import annotations

import pytest

from protoprompt.session.types import Session
from protoprompt.session.strategy import HeuristicStrategy


class MockLLM:
    async def chat(self, messages, model="", **options):
        return "mocked response"

    async def embed(self, texts, model=""):
        return [[0.1] * 384 for _ in texts]


@pytest.mark.asyncio
async def test_heuristic_empty_session():
    strategy = HeuristicStrategy(min_messages=1)
    session = Session(chat_id="c1", messages=[])
    llm = MockLLM()
    blocks = await strategy.compress(session, llm)
    assert blocks == []


@pytest.mark.asyncio
async def test_heuristic_short_session_returns_head():
    strategy = HeuristicStrategy(head_count=3, tail_count=5, min_messages=1)
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
    session = Session(chat_id="c1", messages=msgs)
    llm = MockLLM()
    blocks = await strategy.compress(session, llm)
    assert len(blocks) >= 1
    assert "Начало диалога" in blocks[0].text


@pytest.mark.asyncio
async def test_heuristic_long_session_splits_head_and_tail():
    strategy = HeuristicStrategy(head_count=2, tail_count=3, min_messages=1)
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    session = Session(chat_id="c1", messages=msgs)
    llm = MockLLM()
    blocks = await strategy.compress(session, llm)
    segments = {b.metadata.get("segment") for b in blocks}
    assert "head" in segments
    assert "tail" in segments


@pytest.mark.asyncio
async def test_heuristic_detects_keywords():
    strategy = HeuristicStrategy(head_count=1, tail_count=1, min_length=10, min_messages=1, keywords=("цель",))
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "моя цель — построить ракету"},
        {"role": "user", "content": "ok"},
        {"role": "user", "content": "bye"},
    ]
    session = Session(chat_id="c1", messages=msgs)
    llm = MockLLM()
    blocks = await strategy.compress(session, llm)
    text = "\n".join(b.text for b in blocks)
    assert "построить ракету" in text or any("построить ракету" in b.text for b in blocks)
