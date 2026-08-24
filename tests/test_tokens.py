from __future__ import annotations

import pytest

from protoprompt.injector import ContextBuilder
from protoprompt.store.memory import InMemStore
from protoprompt.tokens import RegexTokenCounter, TokenCounter

from _mocks import MockLLM


def test_regex_counter_is_protocol():
    counter = RegexTokenCounter()
    assert isinstance(counter, TokenCounter)


def test_regex_counter_empty():
    assert RegexTokenCounter().count("") == 0


def test_regex_counter_ascii():
    n = RegexTokenCounter().count("hello world foo bar")
    assert n > 0
    assert n <= 5


def test_regex_counter_cyrillic_denser():
    counter = RegexTokenCounter()
    ascii_count = counter.count("hello world")
    cyr_count = counter.count("привет мир")
    assert cyr_count >= ascii_count


def test_regex_counter_cjk_densest():
    counter = RegexTokenCounter()
    ascii_count = counter.count("hello world")
    cjk_count = counter.count("你好世界你好世界")
    assert cjk_count > ascii_count


def test_regex_counter_punctuation():
    n = RegexTokenCounter().count("Hello, world!")
    assert n >= 4


def test_regex_counter_messages_overhead():
    counter = RegexTokenCounter()
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
    ]
    n = counter.count_messages(msgs)
    assert n >= 2 * 4  # at least the per-message overhead


def test_regex_counter_messages_non_string_content():
    counter = RegexTokenCounter()
    msgs = [{"role": "user", "content": 12345}]
    n = counter.count_messages(msgs)
    assert n > 0


@pytest.mark.asyncio
async def test_context_builder_uses_single_embed_call():
    """The query must be embedded once even when both RAG and session are on."""

    store = InMemStore()
    store.add("1", ["Paris is the capital of France"], [[0.5] * 16])
    store.add(
        "session_chat_x",
        ["User asked about weather"],
        [[0.5] * 16],
        {"chat_id": "chat_x"},
    )
    llm = MockLLM()
    builder = ContextBuilder(store, llm)
    inp = {
        "query": "What is the capital of France?",
        "system_prompt": "You are helpful",
        "chat_id": "chat_x",
        "doc_ids": [1],
    }
    from protoprompt import ContextInput
    await builder.build(ContextInput(**inp))

    assert len(llm.embed_calls) == 1
    assert llm.embed_calls[0]["texts"] == [inp["query"]]
