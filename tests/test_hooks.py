"""Tests for observability hooks on builders and the pipeline."""

from __future__ import annotations

from protoprompt import (
    ContextBuilder,
    ContextHooks,
    ContextInput,
    InMemStore,
    Pipeline,
    PipelineHooks,
    RegexTokenCounter,
    Session,
    TokenBudgetedContextBuilder,
)

from _mocks import MockLLM


def _budgeted(hooks) -> TokenBudgetedContextBuilder:
    return TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=20,
        hooks=hooks,
    )


async def test_budgeted_hooks_section_and_build():
    sections: list[tuple[str, int]] = []
    done: list[object] = []
    hooks = ContextHooks(
        on_section_used=lambda label, tokens: sections.append((label, tokens)),
        on_build_done=lambda report: done.append(report),
    )
    builder = _budgeted(hooks)
    await builder.build(ContextInput(query="q", system_prompt="S"))
    assert sections[0] == ("system", 1)
    assert len(done) == 1 and done[0] is not None
    assert done[0].section_tokens["system"] == 1


async def test_budgeted_hook_drop_events():
    dropped: list[tuple[str, str]] = []
    hooks = ContextHooks(on_block_dropped=lambda l, r: dropped.append((l, r)))
    builder = _budgeted(hooks)
    await builder.build(ContextInput(
        query="q",
        system_prompt="S",
        include_profile=True,
        # spaced tokens: ~100 words ≫ the 20-token budget
        profile_text="token " * 100,
    ))
    assert ("profile", "over_budget") in dropped


async def test_base_builder_reports_none_on_done():
    done: list[object] = []
    hooks = ContextHooks(on_build_done=lambda report: done.append(report))
    builder = ContextBuilder(InMemStore(), MockLLM(embed_dim=2), hooks=hooks)
    await builder.build(ContextInput(query="q", system_prompt="S"))
    assert done == [None]


async def test_failing_hook_does_not_break_build():
    def boom(*args):
        raise ValueError("hook exploded")

    hooks = ContextHooks(on_section_used=boom, on_build_done=boom)
    builder = _budgeted(hooks)
    out = await builder.build(ContextInput(query="q", system_prompt="S"))
    assert out.system_prompt


async def test_pipeline_hooks_flow():
    events: list[str] = []
    store = InMemStore()
    pipeline = Pipeline(
        store, MockLLM(embed_dim=4),
        compress_every_n=6,
        hooks=PipelineHooks(
            on_skip_compress=lambda s: events.append("skip"),
            on_before_compress=lambda s: events.append("before"),
            on_after_compress=lambda s, b: events.append("after"),
        ),
    )
    short = Session(chat_id="s1", messages=[{"role": "user", "content": "hi"}])
    await pipeline.compress_and_store(short)
    assert events == ["skip"]

    long_msgs = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"сообщение {i} хочу план задача"}
        for i in range(8)
    ]
    blocks = await pipeline.compress_and_store(Session(chat_id="s2", messages=long_msgs))
    assert blocks
    assert events == ["skip", "before", "after"]
