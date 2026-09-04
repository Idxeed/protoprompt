from __future__ import annotations

import re

import pytest

from protoprompt.injector import ContextBuilder
from protoprompt.store.memory import InMemStore
from protoprompt.tokens import ProviderTokenCounter, RegexTokenCounter, TokenCounter

from _mocks import MockLLM


def _pre_fast_path_count(text: str) -> int:
    """Mirror the former Unicode-aware loop for equivalence coverage."""

    word = re.compile(r"\w+|[^\w\s]", re.UNICODE)
    cyrillic = re.compile(r"[\u0400-\u04FF]")
    cjk = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")
    total = 0
    for match in word.finditer(text):
        chunk = match.group(0)
        if cjk.search(chunk):
            total += max(1, int(len(chunk) * 1.5))
        elif cyrillic.search(chunk):
            total += max(1, int(len(chunk) * 0.6 * 1.3))
        else:
            total += 1
    return total


def test_regex_counter_is_protocol():
    counter = RegexTokenCounter()
    assert isinstance(counter, TokenCounter)


def test_regex_counter_empty():
    assert RegexTokenCounter().count("") == 0


def test_regex_counter_ascii():
    n = RegexTokenCounter().count("hello world foo bar")
    assert n > 0
    assert n <= 5


@pytest.mark.parametrize(
    "text",
    [
        "plain ASCII words and 12345",
        "JSON: {\"kind\":\"fact\",\"content\":\"hello\"}",
        "punctuation !?.,;:-_()[]{}<>/\\\\\"'",
        "tabs\tnewlines\nand\x00controls",
    ],
)
def test_regex_counter_ascii_fast_path_is_identical_to_the_prior_loop(text: str):
    """The ASCII specialization must retain the exact pre-optimization count."""

    assert RegexTokenCounter().count(text) == _pre_fast_path_count(text)


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


def test_message_counters_include_tool_calls_and_rich_content_payloads():
    args = "argument " * 200
    tool_call = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": args},
        }],
    }]
    rich_content = [{
        "role": "user",
        "content": [{
            "type": "input_text",
            "text": "hello",
            "metadata": {"source": "attachment"},
        }],
    }]
    regex = RegexTokenCounter()
    provider = ProviderTokenCounter("openai", fallback=regex)

    assert regex.count_messages(tool_call) > 200
    assert provider.count_messages(tool_call) > 200
    assert regex.count_messages(rich_content) > regex.count("hello") + 4


def test_message_counters_reject_non_json_provider_payloads():
    with pytest.raises(TypeError, match="JSON-compatible"):
        RegexTokenCounter().count_messages([
            {"role": "assistant", "content": None, "tool_calls": object()},
        ])


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
