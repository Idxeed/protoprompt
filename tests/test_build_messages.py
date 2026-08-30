"""Tests for build_messages(): base assembly and budget-aware history
trimming in TokenBudgetedContextBuilder."""

from __future__ import annotations

import pytest

from protoprompt import (
    ContextBuilder,
    ContextInput,
    InMemStore,
    RegexTokenCounter,
    TokenBudgetedContextBuilder,
    TokenBudgetExceededError,
)
from protoprompt.tokens import ProviderTokenCounter

from _mocks import MockLLM


async def test_base_build_messages_ordering():
    builder = ContextBuilder(InMemStore(), MockLLM(embed_dim=2))
    msgs = await builder.build_messages(
        ContextInput(query="q", system_prompt="SYS"),
        history=[{"role": "user", "content": "h1"}, {"role": "assistant", "content": "a1"}],
        user_message="final question",
    )
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "SYS"
    assert msgs[-1]["content"] == "final question"


async def test_base_build_messages_empty_system():
    builder = ContextBuilder(InMemStore(), MockLLM(embed_dim=2))
    msgs = await builder.build_messages(
        ContextInput(query="q", system_prompt=""),
        user_message="hi",
    )
    assert [m["role"] for m in msgs] == ["user"]


async def test_budgeted_trims_old_history_first():
    llm = MockLLM(embed_dim=2)
    builder = TokenBudgetedContextBuilder(
        InMemStore(), llm,
        counter=RegexTokenCounter(),
        max_tokens=20,
    )
    history = [
        {"role": "user", "content": "word " * 10},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "tiny"},
    ]
    msgs = await builder.build_messages(
        ContextInput(query="q", system_prompt="S"),
        history=history,
        user_message="go",
    )
    contents = [m["content"] for m in msgs]
    # system + kept history + final user message
    assert contents[0] == "S"
    assert contents[-1] == "go"
    # the long oldest message must not survive a 30-token budget
    all_content = " ".join(contents)
    assert "word" not in all_content
    assert any(c == "tiny" for c in contents)


async def test_budgeted_report_counts_history():
    builder = TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=1000,
    )
    history = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
    msgs = await builder.build_messages(
        ContextInput(query="q", system_prompt="S"),
        history=history,
    )
    assert len(msgs) == 5  # system + 4 kept (budget is large enough)


async def test_budgeted_user_message_overflow_raises_instead_of_exceeding_budget():
    builder = TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=5,
    )
    with pytest.raises(TokenBudgetExceededError) as exc:
        await builder.build_messages(
            ContextInput(query="q", system_prompt=""),
            history=[{"role": "user", "content": "x" * 500}],
            user_message="answer me",
        )
    assert exc.value.section == "user"


async def test_budgeted_messages_include_final_turn_tool_history_and_output_reserve():
    # Use provider-specific framing (3 tokens/message for OpenAI) while
    # keeping the text estimate deterministic for this regression test.
    counter = ProviderTokenCounter("openai", fallback=RegexTokenCounter())
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=35,
        output_reserve=6,
    )
    history = [
        {"role": "user", "content": "old turn " * 8},
        {"role": "tool", "content": "tool result with useful data"},
        {"role": "assistant", "content": "recent answer"},
    ]

    messages = await builder.build_messages(
        ContextInput(
            query="q",
            system_prompt="System instructions",
            include_rag=False,
            include_session=False,
        ),
        history=history,
        user_message="final question",
    )

    report = builder.last_report
    assert counter.count_messages(messages) + 6 <= 35
    assert messages[-1] == {"role": "user", "content": "final question"}
    assert report.used_tokens == counter.count_messages(messages)
    assert report.output_reserve_tokens == 6
    assert report.user_message_tokens == counter.count_messages([messages[-1]])
    assert report.remaining_tokens == 35 - report.used_tokens - 6


async def test_budgeted_messages_allow_per_request_output_reserve_override():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=20,
    )

    messages = await builder.build_messages(
        ContextInput(
            query="q",
            system_prompt="S",
            include_rag=False,
            include_session=False,
        ),
        user_message="go",
        output_reserve=7,
    )

    assert counter.count_messages(messages) + 7 <= 20
    assert builder.last_report.output_reserve_tokens == 7


async def test_budgeted_messages_reject_overflowing_reserve_before_retrieval():
    llm = MockLLM(embed_dim=2)
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        llm,
        counter=RegexTokenCounter(),
        max_tokens=10,
    )

    with pytest.raises(TokenBudgetExceededError) as exc:
        await builder.build_messages(
            ContextInput(query="q", system_prompt=""),
            user_message="go",
            output_reserve=11,
        )

    assert exc.value.section == "output_reserve"
    assert llm.embed_calls == []


async def test_budgeted_messages_skip_retrieval_when_tail_uses_context_budget():
    llm = MockLLM(embed_dim=2)
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        llm,
        counter=counter,
        max_tokens=9,
    )
    tail = "one two three four five"

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt=""),
        user_message=tail,
    )

    assert messages == [{"role": "user", "content": tail}]
    assert llm.embed_calls == []
    assert "rag" in builder.last_report.dropped_blocks


async def test_budgeted_history_drops_tool_call_pairs_atomically_when_they_do_not_fit():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=35,
    )
    history = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": "argument " * 30,
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": "older safe context"},
    ]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        user_message="continue",
    )

    assert {message["role"] for message in messages} == {"user"}
    assert all("tool_calls" not in message for message in messages)
    assert "history[0]" in builder.last_report.dropped_blocks
    assert "history[1]" in builder.last_report.dropped_blocks
    assert counter.count_messages(messages) <= 35


async def test_budgeted_history_drops_mcp_approval_pairs_atomically_when_they_do_not_fit():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=35,
    )
    history = [
        {
            "type": "mcp_approval_request",
            "id": "approval_1",
            "name": "dangerous",
            "server_label": "mcp",
            "arguments": "argument " * 30,
        },
        {
            "type": "mcp_approval_response",
            "approval_request_id": "approval_1",
            "approve": True,
        },
    ]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        user_message="continue",
    )

    assert messages == [{"role": "user", "content": "continue"}]
    assert {"history[0]", "history[1]"} <= set(builder.last_report.dropped_blocks)
    assert counter.count_messages(messages) <= 35


async def test_budgeted_history_keeps_complete_tool_call_pairs_in_order():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )
    history = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": "what next?"},
    ]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        user_message="continue",
    )

    assert messages[:-1] == history
    assert messages[-1] == {"role": "user", "content": "continue"}


async def test_budgeted_messages_keep_trailing_chat_tool_call_for_final_tool_result():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )
    history = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": "{}"},
        }],
    }]
    final_messages = [{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "result",
    }]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        final_messages=final_messages,
    )

    assert messages == [*history, *final_messages]
    assert builder.last_report.history_kept == 1


async def test_budgeted_messages_keep_hosted_mcp_approval_for_final_response():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )
    history = [{
        "type": "hosted_tool_call",
        "call_id": "wrapper_id",
        "provider_data": {
            "type": "mcp_approval_request",
            "id": "approval_1",
            "name": "dangerous",
            "server_label": "mcp",
            "arguments": "{}",
        },
    }]
    final_messages = [{
        "type": "mcp_approval_response",
        "approval_request_id": "approval_1",
        "approve": True,
    }]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        final_messages=final_messages,
    )

    assert messages == [*history, *final_messages]
    assert builder.last_report.history_kept == 1


async def test_budgeted_history_keeps_normal_assistant_item_with_null_tool_calls():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=100,
    )
    history = [{"role": "assistant", "content": "normal reply", "tool_calls": None}]

    messages = await builder.build_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        history=history,
        user_message="continue",
    )

    assert messages[:-1] == history


async def test_budgeted_history_drops_reflected_in_report():
    builder = TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=40,
    )
    history = [
        {"role": "user", "content": "word " * 20},   # too big to fit
        {"role": "assistant", "content": "short one"},  # fits
    ]
    await builder.build_messages(
        ContextInput(query="q", system_prompt="S"),
        history=history,
    )
