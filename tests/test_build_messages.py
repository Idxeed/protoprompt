"""Tests for build_messages(): base assembly and budget-aware history
trimming in TokenBudgetedContextBuilder."""

from __future__ import annotations

from protoprompt import (
    ContextBuilder,
    ContextInput,
    InMemStore,
    RegexTokenCounter,
    TokenBudgetedContextBuilder,
)

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


async def test_budgeted_user_message_always_kept():
    builder = TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=5,
    )
    msgs = await builder.build_messages(
        ContextInput(query="q", system_prompt=""),
        history=[{"role": "user", "content": "x" * 500}],
        user_message="answer me",
    )
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "answer me"


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
