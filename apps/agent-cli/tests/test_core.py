"""Тесты AgentCore: цикл хода, память, лимиты, хвост диалога."""

from __future__ import annotations

import pytest

from protoprompt import (
    CachedLLMClient,
    InMemStore,
    RegexTokenCounter,
    SqliteStore,
    TokenBudgetedContextBuilder,
    TokenBudgetExceededError,
)

from _mocks import MockLLM


def _w(n: int) -> str:
    return " ".join(["tok"] * n)


# ── простой ход без инструментов ─────────────────────────────────


async def test_turn_without_actions_returns_reply(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["привет, я тут"])
    core = core_factory(mem, llm=llm)
    result = await core.turn("пока")
    assert result.reply == "привет, я тут"
    assert result.actions_run == 0
    assert result.iterations == 1


async def test_user_message_lands_in_memory(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    await core.turn("важное указание")
    kinds = [i.kind for i in mem.items.values()]
    texts = [i.text for i in mem.items.values()]
    assert "tool_output" in kinds
    assert "важное указание" in texts


async def test_first_turn_sets_goal_automatically(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    assert mem.goal.text == ""
    await core.turn("почини падающий тест в retry.py")
    assert mem.goal.text == "почини падающий тест в retry.py"


async def test_goal_not_overwritten_on_later_turns(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    await core.turn("первая задача")
    goal_before = mem.goal.text
    await core.turn("совсем другая тема")
    assert mem.goal.text == goal_before


async def test_assistant_reply_lands_in_memory(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem, llm=MockLLM(responses=["ответ модели"]))
    await core.turn("спроси что-то")
    assert any(i.text == "ответ модели" for i in mem.items.values())


async def test_system_prompt_contains_assembled_context(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    await core.turn("hello there")
    messages = core.llm.chat_calls[0]["messages"]
    system = messages[0]["content"]
    assert "Ты — тестовый агент." in system
    assert messages[-1] == {"role": "user", "content": "hello there"}
    assert "hello there" not in system


async def test_tail_contains_user_and_assistant(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem, llm=MockLLM(responses=["ок"]))
    await core.turn("первый ход")
    roles = [m["role"] for m in core.tail]
    assert roles == ["user", "assistant"]


async def test_tail_is_capped(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem, tail_size=2)
    for _ in range(5):
        await core.turn("шаг")
    assert len(core.tail) <= 2


# ── ход с инструментами ──────────────────────────────────────────


async def test_write_action_creates_edit_item_and_runs(core_factory, mem_factory, tmp_path):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="write" path="x.py">print(1)</action>',
        "готово",
    ])
    core = core_factory(mem, llm=llm, root=str(tmp_path))
    core.tools.perms["write"] = "allow"
    result = await core.turn("создай файл")
    assert result.actions_run == 1
    assert result.reply == "готово"
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "print(1)"
    edits = [i for i in mem.items.values() if i.kind == "edit"]
    assert edits, "правка должна попасть в память"
    assert edits[0].pinned, "правки пинятся"


async def test_bash_action_feeds_tool_output_memory(core_factory, mem_factory, tmp_path):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="bash">echo 42</action>',
        "сделано",
    ])
    tools = core_factory(mem, llm=llm, root=str(tmp_path)).tools
    tools.perms["bash"] = "allow"
    core = core_factory(mem, llm=llm, root=str(tmp_path), tools=tools)
    result = await core.turn("вычисли 42")
    assert result.actions_run == 1
    assert any("42" in i.text for i in mem.items.values())


async def test_denied_tool_does_not_crash(core_factory, mem_factory, tmp_path):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="bash">echo secret</action>',
        "не вышло",
    ])
    tools = core_factory(mem, llm=llm, root=str(tmp_path)).tools
    tools.perms["bash"] = "deny"
    core = core_factory(mem, llm=llm, root=str(tmp_path), tools=tools)
    result = await core.turn("запусти")
    assert result.actions_run == 1
    assert any("permission denied" in i.text for i in mem.items.values())
    assert result.reply == "не вышло"


async def test_iteration_limit_stops_action_loop(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=['<action name="bash">echo x</action>'] * 10)
    core = core_factory(mem, llm=llm, max_iterations=3)
    result = await core.turn("цикл")
    assert result.iterations == 3
    assert result.actions_run == 3
    assert result.reply == ""


async def test_exhausted_loop_falls_back_to_stripped_reply(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="bash">echo 1</action>',
        '<action name="bash">echo 2</action>',
        "итог: <action name=\"bash\">echo 3</action> тут всё",
    ])
    core = core_factory(mem, llm=llm, max_iterations=3)
    result = await core.turn("петля")
    assert result.iterations == 3
    assert "итог:" in result.reply
    assert "<action" not in result.reply
    assert "тут всё" in result.reply


async def test_multiple_actions_in_one_reply(core_factory, mem_factory, tmp_path):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="write" path="a.txt">A</action>'
        '<action name="write" path="b.txt">B</action>',
        "оба файла созданы",
    ])
    core = core_factory(mem, llm=llm, root=str(tmp_path))
    core.tools.perms["write"] = "allow"
    result = await core.turn("два файла")
    assert result.actions_run == 2
    assert (tmp_path / "a.txt").is_file()
    assert (tmp_path / "b.txt").is_file()


# ── recall в начале хода ─────────────────────────────────────────


async def test_recall_query_restores_cold_items(tmp_path):
    from protoprompt.agent import WorkingMemory

    from protoprompt_cli.core import AgentCore
    from protoprompt_cli.tools import ToolRunner

    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=MockLLM(), max_tokens=80, namespace="rc")
    junk = await mem.add("log", _w(30))
    await mem.forget(junk)

    llm = MockLLM()
    core = AgentCore(mem, llm, ToolRunner(tmp_path), system_prompt="sys")
    result = await core.turn("вопрос", recall_query=_w(30))
    assert result.restored, "recall должен что-то вернуть"


async def test_recall_query_without_cold_nothing(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    result = await core.turn("вопрос", recall_query="несуществующее")
    assert result.restored == []


async def test_oversized_final_input_cannot_mutate_memory_via_recall(tmp_path):
    from protoprompt.agent import WorkingMemory

    from protoprompt_cli.core import AgentCore
    from protoprompt_cli.tools import ToolRunner

    store = SqliteStore()
    llm = MockLLM()
    mem = WorkingMemory(
        store=store,
        llm=llm,
        max_tokens=80,
        namespace="recall-preflight",
    )
    cold_id = await mem.add(
        "log", "RARE_RECALL_SENTINEL alpha beta", summary="rare recall"
    )
    await mem.forget(cold_id)
    before = mem.export_state()
    core = AgentCore(
        mem,
        llm,
        ToolRunner(tmp_path),
        system_prompt="",
        request_max_tokens=24,
        output_reserve_tokens=12,
    )

    with pytest.raises(TokenBudgetExceededError):
        await core.turn(_w(50), recall_query="RARE_RECALL_SENTINEL")

    assert mem.export_state() == before
    assert core.tail == []
    assert core.last_context_plan is None
    assert llm.chat_calls == []


# ── учёт токенов ─────────────────────────────────────────────────


async def test_usage_tracked_across_turns(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["первый ответ", "второй ответ"])
    core = core_factory(mem, llm=llm)
    await core.turn("один")
    await core.turn("два")
    assert core.usage["chat_calls"] == 2
    assert core.usage["input_tokens"] > 0
    assert core.usage["output_tokens"] > 0


async def test_reset_usage(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    await core.turn("х")
    core.reset_usage()
    assert core.usage == {"chat_calls": 0, "input_tokens": 0, "output_tokens": 0}


# ── bounded final requests ─────────────────────────────────────


async def test_turn_uses_exact_bounded_request_receipt(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["готово"])
    core = core_factory(
        mem,
        llm=llm,
        request_max_tokens=256,
        output_reserve_tokens=17,
    )

    result = await core.turn("проверь лимит запроса")

    sent = llm.chat_calls[0]["messages"]
    receipt = result.request_receipts[0]
    counter = RegexTokenCounter()
    assert sent[-1] == {"role": "user", "content": "проверь лимит запроса"}
    assert receipt.input_tokens == counter.count_messages(sent)
    assert receipt.input_tokens + receipt.output_reserve_tokens <= 256
    assert receipt.output_reserve_tokens == 17
    assert llm.chat_calls[0]["max_tokens"] == 17
    assert core.usage["input_tokens"] == receipt.input_tokens


async def test_action_continuation_is_an_atomic_final_pair_with_tiny_tail(
    core_factory, mem_factory, tmp_path
):
    mem = mem_factory()
    action_reply = '<action name="bash">echo guarded</action>'
    llm = MockLLM(responses=[action_reply, "готово"])
    core = core_factory(
        mem,
        llm=llm,
        root=str(tmp_path),
        tail_size=1,
        request_max_tokens=256,
        output_reserve_tokens=17,
    )
    core.tools.perms["bash"] = "deny"

    result = await core.turn("выполни команду")

    sent = llm.chat_calls[1]["messages"]
    continuation = sent[-2:]
    assert continuation[0] == {"role": "assistant", "content": action_reply}
    assert continuation[1]["role"] == "user"
    assert "[bash]" in continuation[1]["content"]
    assert "permission denied: bash" in continuation[1]["content"]
    receipt = result.request_receipts[1]
    assert receipt.final_input_tokens == RegexTokenCounter().count_messages(
        continuation
    )
    assert receipt.input_tokens + receipt.output_reserve_tokens <= 256


async def test_tail_never_keeps_an_orphan_text_action_result(
    core_factory, mem_factory, tmp_path
):
    mem = mem_factory()
    action_reply = '<action name="bash">echo guarded</action>'
    llm = MockLLM(responses=[action_reply, "готово", "следующий ответ"])
    core = core_factory(
        mem,
        llm=llm,
        root=str(tmp_path),
        tail_size=3,
        request_max_tokens=256,
        output_reserve_tokens=17,
    )
    core.tools.perms["bash"] = "deny"

    await core.turn("выполни команду")
    await core.turn("следующий вопрос")

    sent = llm.chat_calls[2]["messages"]
    contents = [message.get("content", "") for message in sent]
    action_positions = [
        index for index, content in enumerate(contents) if content == action_reply
    ]
    result_positions = [
        index for index, content in enumerate(contents) if "[bash]" in content
    ]
    assert bool(action_positions) == bool(result_positions)
    if action_positions:
        assert result_positions == [action_positions[0] + 1]


async def test_budgeted_history_drops_whole_text_action_group(
    core_factory, mem_factory
):
    """A tight request must not retain a synthetic result without its action."""
    mem = mem_factory()
    llm = MockLLM(responses=["готово"])
    counter = RegexTokenCounter()
    final = {"role": "user", "content": "NEXT_SENTINEL"}
    action = {
        "role": "assistant",
        "content": "ACTION_SENTINEL " + _w(12),
    }
    tool_result = {
        "role": "user",
        "content": "RESULT_SENTINEL " + _w(3),
    }
    reserve = 10
    # ``system_prompt=''`` becomes a framed ``\n\n`` system message during a
    # normal turn.  Leave room for the result alone, but not the full pair:
    # the generic provider packer would otherwise select only the result.
    request_limit = (
        reserve
        + counter.count_messages([{"role": "system", "content": "\n\n"}])
        + counter.count_messages([final])
        + counter.count_messages([tool_result])
    )
    core = core_factory(
        mem,
        llm=llm,
        system_prompt="",
        request_max_tokens=request_limit,
        output_reserve_tokens=reserve,
    )
    core._push_tail_group([action, tool_result])

    result = await core.turn(final["content"])

    sent = llm.chat_calls[0]["messages"]
    sent_content = "\n".join(str(message.get("content", "")) for message in sent)
    assert "ACTION_SENTINEL" not in sent_content
    assert "RESULT_SENTINEL" not in sent_content
    assert sent[-1] == final
    assert result.request_receipts[0].history_tokens == 0


async def test_plan_and_compact_share_the_bounded_request_path(
    core_factory, mem_factory
):
    mem = mem_factory()
    llm = MockLLM(responses=["шаг 1", "краткий обзор"])
    core = core_factory(
        mem,
        llm=llm,
        request_max_tokens=512,
        output_reserve_tokens=19,
    )
    core.plan_mode = True

    result = await core.turn("составь план")

    assert result.request_receipts[0].output_reserve_tokens == 19
    assert llm.chat_calls[0]["max_tokens"] == 19
    core.plan_mode = False
    await core.compact()
    assert llm.chat_calls[1]["max_tokens"] == 19
    assert core.last_context_plan is not None
    assert core.last_context_plan.receipt is not None
    assert core.last_context_plan.receipt.input_tokens + 19 <= 512


async def test_oversized_final_input_never_reaches_provider(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM()
    core = core_factory(
        mem,
        llm=llm,
        system_prompt="",
        request_max_tokens=24,
        output_reserve_tokens=12,
    )

    with pytest.raises(TokenBudgetExceededError):
        await core._plan_request(
            system_prompt="",
            history=[],
            final_messages=[{"role": "user", "content": _w(50)}],
        )
    assert llm.chat_calls == []


def test_injected_request_builder_must_match_core_limits(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM()
    smaller_builder = TokenBudgetedContextBuilder(
        InMemStore(),
        llm,
        max_tokens=64,
        output_reserve=10,
    )

    with pytest.raises(ValueError, match="context_builder"):
        core_factory(
            mem,
            llm=llm,
            request_max_tokens=128,
            output_reserve_tokens=10,
            context_builder=smaller_builder,
        )


async def test_rejected_final_input_does_not_poison_memory_or_tail(
    core_factory, mem_factory
):
    llm = MockLLM(responses=["короткий ответ"])
    mem = mem_factory(max_tokens=200, llm=llm)
    core = core_factory(
        mem,
        llm=llm,
        system_prompt="",
        request_max_tokens=24,
        output_reserve_tokens=12,
    )

    with pytest.raises(TokenBudgetExceededError):
        await core.turn(_w(50))

    assert mem.items == {}
    assert mem.goal.text == ""
    assert core.tail == []
    assert llm.embed_calls == []
    result = await core.turn("ok")
    assert result.reply == "короткий ответ"
    assert len(llm.chat_calls) == 1


# ── план-режим ──────────────────────────────────────────────────


async def test_plan_mode_skips_tools(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=[
        '<action name="bash">echo dangerous</action>\nшаг 1: изучить код\nшаг 2: исправить'
    ])
    core = core_factory(mem, llm=llm)
    core.plan_mode = True
    result = await core.turn("почини баг")
    assert result.actions_run == 0
    assert result.plan
    assert "шаг 1" in result.plan
    assert result.reply == result.plan
    assert any("план" in i.text for i in mem.items.values()), \
        "план должен попасть в память пин-заметкой"


async def test_plan_mode_off_runs_normally(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["обычный ответ"])
    core = core_factory(mem, llm=llm)
    result = await core.turn("вопрос")
    assert result.plan == ""
    assert result.reply == "обычный ответ"


# ── compact ──────────────────────────────────────────────────────


async def test_compact_replaces_hot_set_with_summary(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["краткий обзор: файлы, решения, задачи"])
    core = core_factory(mem, llm=llm)
    await mem.add("file", "def foo(): pass", summary="foo")
    await mem.add("log", "шумный лог")
    await mem.note("важная заметка", pin=True)

    summary = await core.compact()
    assert summary.startswith("краткий обзор")
    assert len(mem.items) == 1
    kept = next(iter(mem.items.values()))
    assert kept.pinned
    assert "краткий обзор" in kept.text
    assert len(mem.manifest.entries) == 3


async def test_compact_empty_memory_returns_empty(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem)
    assert await core.compact() == ""


async def test_compact_empty_llm_reply_keeps_memory(core_factory, mem_factory):
    mem = mem_factory()
    llm = MockLLM(responses=["   "])
    core = core_factory(mem, llm=llm)
    await mem.add("log", "данные")
    assert await core.compact() == ""
    assert len(mem.items) == 1


# ── стриминг ─────────────────────────────────────────────────────


class StreamingMockLLM(MockLLM):
    async def chat_stream(self, messages, model="", on_token=None, **options):
        self.chat_calls.append({"messages": list(messages), "model": model, **options})
        text = "стрим-ответ"
        if on_token is not None:
            on_token("стрим-")
            on_token("ответ")
        return text


async def test_turn_streams_tokens(core_factory, mem_factory):
    mem = mem_factory()
    llm = StreamingMockLLM()
    core = core_factory(
        mem,
        llm=llm,
        request_max_tokens=256,
        output_reserve_tokens=23,
    )
    chunks: list[str] = []
    result = await core.turn("поток", stream_cb=chunks.append)
    assert result.streamed is True
    assert result.reply == "стрим-ответ"
    assert "".join(chunks) == "стрим-ответ"
    assert llm.chat_calls[0]["max_tokens"] == 23


async def test_turn_without_stream_cb_not_streamed(core_factory, mem_factory):
    mem = mem_factory()
    core = core_factory(mem, llm=StreamingMockLLM())
    result = await core.turn("поток")
    assert result.streamed is False
    assert result.reply == "mocked response"


async def test_cached_non_streaming_backend_falls_back_to_chat(
    core_factory, mem_factory
):
    mem = mem_factory()
    inner = MockLLM()
    core = core_factory(mem, llm=CachedLLMClient(inner))
    chunks: list[str] = []

    result = await core.turn("без стриминга", stream_cb=chunks.append)

    assert result.streamed is False
    assert chunks == []
    assert len(inner.chat_calls) == 1
