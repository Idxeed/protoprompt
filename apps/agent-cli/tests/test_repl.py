"""Тесты REPL: диспетчер слэш-команд и цикл ввода."""

from __future__ import annotations

import sys

import pytest

from _mocks import FakeReader, FakeWriter, MockLLM

from protoprompt import SqliteStore
from protoprompt.agent import WorkingMemory

from protoprompt_cli import persistence, render, tools as tools_module
from protoprompt_cli.core import AgentCore
from protoprompt_cli.repl import HELP_TEXT, Repl
from protoprompt_cli.tools import ToolRunner


# ── служебные ────────────────────────────────────────────────────


def _build(lines, *, root=".", mem=None, llm=None, readline=None):
    mem = mem or WorkingMemory(max_tokens=200, namespace="t")
    llm = llm or MockLLM()
    tools = ToolRunner(root)
    core = AgentCore(mem, llm, tools, system_prompt="sys")
    writer = FakeWriter()
    repl = Repl(core, mem, tools, root=root, write=writer,
                readline=readline or FakeReader(list(lines)))
    return repl, mem, writer


# ── отдельные команды ────────────────────────────────────────────


async def test_help_prints_help():
    repl, _, writer = _build([])
    await repl.dispatch("/help")
    assert writer.text == HELP_TEXT


async def test_unknown_command():
    repl, _, writer = _build([])
    await repl.dispatch("/nope")
    assert "неизвестная команда" in writer.text


async def test_goal_set_and_get():
    repl, mem, writer = _build([])
    await repl.dispatch("/goal починить тест")
    assert mem.goal.text == "починить тест"
    assert "установлена" in writer.text
    await repl.dispatch("/goal")
    assert "починить тест" in writer.text


async def test_budget_changes_max_tokens():
    repl, mem, writer = _build([])
    await repl.dispatch("/budget 777")
    assert mem._max_tokens == 777
    assert "777" in writer.text


async def test_budget_without_arg_is_usage():
    repl, _, writer = _build([])
    await repl.dispatch("/budget")
    assert "использование" in writer.text


async def test_pin_unpin_forget():
    repl, mem, writer = _build([])
    item_id = await mem.add("log", "что-то")
    await repl.dispatch(f"/pin {item_id}")
    assert mem.items[item_id].pinned
    await repl.dispatch(f"/unpin {item_id}")
    assert not mem.items[item_id].pinned
    await repl.dispatch(f"/forget {item_id}")
    assert item_id not in mem.items
    assert mem.manifest.entries


async def test_pin_unknown_id():
    repl, _, writer = _build([])
    await repl.dispatch("/pin m999999")
    assert "нет такого элемента" in writer.text


async def test_recall_keyword_restores_cold():
    repl, mem, writer = _build([])
    note_id = await mem.note("важное про ретраи и бэкофф")
    await repl.dispatch(f"/forget {note_id}")
    assert note_id not in mem.items
    await repl.dispatch("/recall ретраи")
    assert "восстановлено" in writer.text or "[R]" in writer.text


async def test_recall_without_query():
    repl, _, writer = _build([])
    await repl.dispatch("/recall")
    assert "использование" in writer.text


async def test_memory_command_prints_table():
    repl, mem, writer = _build([])
    await mem.add("edit", "def f(): return 1", summary="правка")
    await repl.dispatch("/memory")
    assert "правка" in writer.text
    assert "used=" in writer.text


async def test_cold_command_prints_manifest():
    repl, mem, writer = _build([])
    item_id = await mem.add("log", "шум")
    await repl.dispatch(f"/forget {item_id}")
    await repl.dispatch("/cold")
    assert "шум" in writer.text


async def test_trace_toggle():
    repl, mem, writer = _build([])
    await repl.dispatch("/trace on")
    assert mem._trace is not None
    await repl.dispatch("/trace off")
    assert mem._trace is None


async def test_trace_prints_events():
    repl, mem, writer = _build([])
    await repl.dispatch("/trace on")
    await mem.add("edit", "def a(): pass", summary="правка")
    assert "[+]" in writer.text


async def test_clear_requires_confirmation():
    repl, mem, writer = _build([])
    await mem.add("log", "данные")
    await repl.dispatch("/clear")
    assert len(mem.items) == 1
    await repl.dispatch("/clear yes")
    assert len(mem.items) == 0


async def test_model_switch():
    repl, _, writer = _build([])
    await repl.dispatch("/model gpt-4o")
    assert repl.core.chat_model == "gpt-4o"
    await repl.dispatch("/model")
    assert "gpt-4o" in writer.text


async def test_save_writes_session(tmp_path):
    repl, mem, writer = _build([], root=str(tmp_path))
    await mem.add("note", "заметка", pin=True)
    await repl.dispatch("/save")
    assert persistence.session_file(tmp_path, "default").is_file()


async def test_resume_state_restores_state_json(tmp_path):
    first, mem, _ = _build([], root=str(tmp_path))
    item_id = await mem.add("note", "важное", pin=True)
    persistence.save_state(mem, tmp_path)

    second, fresh_mem, writer = _build([], root=str(tmp_path))
    await second.dispatch("/resume-state")
    assert item_id in fresh_mem.items
    assert "восстановлено" in writer.text


async def test_resume_state_without_state(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/resume-state")
    assert "состояния нет" in writer.text


# ── цикл run ─────────────────────────────────────────────────────


async def test_run_loop_handles_text_and_exit():
    repl, mem, writer = _build(["привет", "/exit"])
    await repl.run()
    assert "mocked response" in writer.text
    assert "bye" in writer.text


async def test_run_loop_ends_on_eof():
    repl, _, writer = _build(["a", "b"])
    await repl.run()
    assert len(writer.lines) >= 2


async def test_run_loop_skips_empty_lines():
    repl, _, writer = _build(["", "  ", "/exit"])
    await repl.run()
    assert "bye" in writer.text


async def test_run_loop_unknown_command_continues():
    repl, _, writer = _build(["/nope", "/exit"])
    await repl.run()
    assert "неизвестная команда" in writer.text
    assert "bye" in writer.text


# ── интерактивное разрешение инструментов ────────────────────────


async def test_ask_permission_grants_on_yes():
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([], root=".", readline=FakeReader(["y"]))
    assert await repl._ask_permission(Action(name="bash", body="ls")) is True


async def test_ask_permission_denies_on_no():
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([], root=".", readline=FakeReader(["n"]))
    assert await repl._ask_permission(Action(name="bash", body="ls")) is False


async def test_ask_permission_denies_on_eof():
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([], root=".", readline=FakeReader([]))
    assert await repl._ask_permission(Action(name="bash", body="ls")) is False


async def test_ask_permission_accepts_russian_da():
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([], root=".", readline=FakeReader(["д"]))
    assert await repl._ask_permission(Action(name="bash", body="ls")) is True


async def test_ask_permission_shows_full_escaped_payload_before_yes():
    from protoprompt_cli.actions import Action

    snapshots = []

    def reader(prompt):
        snapshots.append((prompt, writer.text))
        return "y"

    repl, _, writer = _build([], root=".", readline=reader)
    command = "echo safe\r\x1b]0;spoof\x07\nthen-visible-tail"

    assert await repl._ask_permission(Action(name="bash", body=command)) is True

    assert len(snapshots) == 1
    prompt, visible_before_answer = snapshots[0]
    assert 'разрешить "bash"?' in prompt
    assert "then-visible-tail" in visible_before_answer
    assert "\\r" in visible_before_answer
    assert "\\u001b" in visible_before_answer
    assert "\\u0007" in visible_before_answer
    assert "\r" not in visible_before_answer
    assert "\x1b" not in visible_before_answer
    assert "\x07" not in visible_before_answer


async def test_ask_permission_sanitizes_its_prompt_too():
    from protoprompt_cli.actions import Action

    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return "n"

    repl, _, writer = _build([], root=".", readline=reader)
    unsafe_name = "\x1b]52;c;spoof\x07"

    assert await repl._ask_permission(Action(name=unsafe_name, body="payload")) is False

    assert prompts
    assert "\x1b" not in prompts[0]
    assert "\x07" not in prompts[0]
    assert "\x1b" not in writer.text
    assert "\x07" not in writer.text


async def test_ask_permission_shows_structured_write_payload_before_always(tmp_path):
    from protoprompt_cli.actions import Action

    snapshots = []

    def reader(prompt):
        snapshots.append((prompt, writer.text))
        return "a"

    repl, _, writer = _build([], root=str(tmp_path), readline=reader)
    action = Action(
        name="write",
        body="complete new content",
        kwargs={"path": "src/\x1b]0;spoof\x07new.py"},
    )

    assert await repl._ask_permission(action) is True

    assert repl.tools.perms["write"] == "allow"
    assert len(snapshots) == 1
    _, visible_before_answer = snapshots[0]
    assert 'field["path"] = "src/\\u001b]0;spoof\\u0007new.py"' in visible_before_answer
    assert 'field["content"] = "complete new content"' in visible_before_answer
    assert "\x1b" not in visible_before_answer
    assert "\x07" not in visible_before_answer


async def test_ask_permission_rejects_payload_that_cannot_be_shown_completely():
    from protoprompt_cli.actions import MAX_APPROVAL_PREVIEW_BYTES, Action

    def fail_reader(prompt):
        raise AssertionError(f"must not ask approval for hidden payload: {prompt}")

    repl, _, writer = _build([], root=".", readline=fail_reader)
    hidden_tail = "MALICIOUS_SUFFIX_MUST_NOT_BE_APPROVED"
    action = Action(
        name="bash",
        body=("x" * MAX_APPROVAL_PREVIEW_BYTES) + hidden_tail,
    )

    assert await repl._ask_permission(action) is False

    assert "безопасный лимит показа" in writer.text
    assert "sha256=" in writer.text
    assert hidden_tail not in writer.text


async def test_ask_permission_rejects_payload_changed_during_confirmation():
    from protoprompt_cli.actions import Action

    action = Action(name="bash", body="echo reviewed")

    def reader(prompt):
        action.body = "echo replaced-after-preview"
        return "a"

    repl, _, writer = _build([], root=".", readline=reader)

    assert await repl._ask_permission(action) is False

    assert "payload изменился" in writer.text
    assert repl.tools.perms["bash"] != "allow"


async def test_tool_runner_gets_ask_callback():
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([])
    assert repl.tools.ask_callback is not None
    assert await repl.tools.ask_callback(Action(name="bash", body="ls")) is False


# ── права и их персистентность ──────────────────────────────────


async def test_allow_is_session_only(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/allow bash")
    assert repl.tools.perms["bash"] == "allow"
    saved = persistence.load_json(persistence.perms_json_path(tmp_path), {})
    assert "bash" not in saved
    assert "до конца сессии" in writer.text


async def test_deny_persists_perms(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/deny write")
    assert repl.tools.perms["write"] == "deny"
    assert persistence.load_json(persistence.perms_json_path(tmp_path))["write"] == "deny"


async def test_allow_unknown_tool_is_usage(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/allow teleport")
    assert "использование" in writer.text


async def test_perms_prints_table(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/allow bash")
    writer.lines.clear()
    await repl.dispatch("/perms")
    assert any("bash" in line and "allow" in line for line in writer.lines)


async def test_ask_always_option_is_session_only(tmp_path):
    from protoprompt_cli.actions import Action

    repl, _, writer = _build([], root=str(tmp_path), readline=FakeReader(["a"]))
    assert await repl._ask_permission(Action(name="bash", body="ls")) is True
    assert repl.tools.perms["bash"] == "allow"
    saved = persistence.load_json(persistence.perms_json_path(tmp_path), {})
    assert "bash" not in saved
    assert "до конца сессии" in writer.text


async def test_auto_save_every_n_turns(tmp_path):
    mem = WorkingMemory(max_tokens=200, namespace="t")
    llm = MockLLM()
    tools = ToolRunner(str(tmp_path))
    core = AgentCore(mem, llm, tools, system_prompt="sys")
    writer = FakeWriter()
    repl = Repl(core, mem, tools, root=str(tmp_path), write=writer,
                readline=FakeReader(["шаг один", "шаг два", "/exit"]),
                save_every=2)
    await repl.run()
    assert persistence.session_file(tmp_path, "default").is_file()


async def test_repl_reports_an_oversized_final_input_without_exiting(tmp_path):
    mem = WorkingMemory(max_tokens=20, namespace="oversized")
    core = AgentCore(
        mem,
        MockLLM(),
        ToolRunner(tmp_path),
        system_prompt="",
        request_max_tokens=24,
        output_reserve_tokens=12,
    )
    writer = FakeWriter()
    repl = Repl(
        core,
        mem,
        core.tools,
        root=str(tmp_path),
        write=writer,
        readline=FakeReader(["word " * 50, "/exit"]),
    )

    await repl.run()

    assert "контекст не помещается" in writer.text


async def test_compact_reports_a_request_budget_error_without_destroying_memory(tmp_path):
    mem = WorkingMemory(max_tokens=200, namespace="compact-overflow")
    await mem.add("log", "word " * 50)
    core = AgentCore(
        mem,
        MockLLM(),
        ToolRunner(tmp_path),
        system_prompt="",
        request_max_tokens=24,
        output_reserve_tokens=12,
    )
    writer = FakeWriter()
    repl = Repl(core, mem, core.tools, root=str(tmp_path), write=writer)

    await repl.dispatch("/compact")

    assert "контекст не помещается" in writer.text
    assert mem.items, "failed compaction must not remove the hot memory"


# ── новые команды: context / status / cost ──────────────────────


async def test_context_command_prints_assembled():
    repl, mem, writer = _build([])
    await mem.add("edit", "def f(): return 1", summary="правка")
    await repl.dispatch("/context")
    assert "бюджет" in writer.text
    assert "правка" in writer.text or "def f" in writer.text


async def test_status_prints_session_and_usage(tmp_path):
    repl, mem, writer = _build([], root=str(tmp_path))
    await mem.add("note", "заметка", pin=True)
    await repl.dispatch("/status")
    assert "сессия" in writer.text
    assert "default" in writer.text
    assert "вызовы" in writer.text


async def test_cost_prints_usage():
    repl, _, writer = _build([])
    await repl.core.turn("х")
    await repl.dispatch("/cost")
    assert "токенов входа" in writer.text
    assert "1" in writer.text or "0" in writer.text


# ── новые команды: note / add / plan / compact ──────────────────


async def test_note_pins_observation():
    repl, mem, writer = _build([])
    await repl.dispatch("/note важно: retry живёт в core.py")
    assert any("retry" in i.text for i in mem.items.values())
    note = next(i for i in mem.items.values() if "retry" in i.text)
    assert note.pinned


async def test_add_reads_file_into_memory(tmp_path):
    (tmp_path / "src.py").write_text("def helper(): pass\n", encoding="utf-8")
    repl, mem, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/add src.py")
    assert any("def helper" in i.text for i in mem.items.values())
    added = next(i for i in mem.items.values() if "def helper" in i.text)
    assert added.pinned


async def test_add_uses_the_bounded_file_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_READ_BYTES", 16)
    (tmp_path / "large.txt").write_text("x" * 100, encoding="utf-8")
    repl, mem, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/add large.txt")
    assert "обрезано" in writer.text
    assert any("file truncated at inspection limit" in item.text for item in mem.items.values())


async def test_add_does_not_follow_an_external_symlink(tmp_path):
    external = tmp_path.parent / "outside-add-secret.txt"
    external.write_text("TOP_SECRET", encoding="utf-8")
    link = tmp_path / "linked-secret.txt"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    repl, mem, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/add linked-secret.txt")
    assert "пропуск" in writer.text
    assert not mem.items


async def test_add_missing_file_reports():
    repl, _, writer = _build([])
    await repl.dispatch("/add nope.py")
    assert "пропуск" in writer.text


async def test_plan_toggle():
    repl, _, writer = _build([])
    await repl.dispatch("/plan on")
    assert repl.core.plan_mode is True
    await repl.dispatch("/plan off")
    assert repl.core.plan_mode is False


async def test_compact_command(tmp_path):
    repl, mem, writer = _build([], root=str(tmp_path))
    await mem.add("file", "def foo(): pass", summary="foo")
    repl.core.llm = MockLLM(responses=["сжатый обзор"])
    await repl.dispatch("/compact")
    assert "сжат" in writer.text
    assert len(mem.items) == 1


# ── новые команды: сессии / git ─────────────────────────────────


async def test_sessions_lists_and_marks_active(tmp_path):
    a = WorkingMemory(max_tokens=200, namespace="t")
    await a.note("заметка сессии A", pin=True)
    persistence.save_session(a, tmp_path, "feature")

    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/sessions")
    assert "feature" in writer.text


async def test_resume_switches_session(tmp_path):
    a = WorkingMemory(max_tokens=200, namespace="t")
    item_id = await a.note("заметка сессии A", pin=True)
    persistence.save_session(a, tmp_path, "alpha")

    repl, mem, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/resume alpha")
    assert repl.session == "alpha"
    assert item_id in mem.items


async def test_resume_clears_previous_raw_tail_before_the_next_request(tmp_path):
    saved = WorkingMemory(max_tokens=200, namespace="t")
    await saved.note("alpha session memory", pin=True)
    persistence.save_session(saved, tmp_path, "alpha")

    repl, _, _ = _build([], root=str(tmp_path))
    await repl.core.turn("OLD_RESUME_RAW_SENTINEL")
    assert repl.core.last_context_plan is not None

    await repl.dispatch("/resume alpha")
    assert repl.core.tail == []
    assert repl.core.last_context_plan is None
    await repl.core.turn("fresh after resume")

    sent_content = "\n".join(
        str(message.get("content", ""))
        for message in repl.core.llm.chat_calls[-1]["messages"]
    )
    assert "OLD_RESUME_RAW_SENTINEL" not in sent_content


async def test_resume_unknown_session(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/resume ghost")
    assert "нет сессии" in writer.text


async def test_resume_rejects_a_malformed_session_without_switching(tmp_path):
    repl, mem, writer = _build([], root=str(tmp_path))
    await mem.set_goal("keep current session")
    item_id = await mem.note("active memory", pin=True)
    before = mem.export_state()
    persistence.save_json(
        persistence.session_file(tmp_path, "broken"),
        {"items": [{"not": "a memory item"}]},
    )

    await repl.dispatch("/resume broken")

    assert repl.session == "default"
    assert mem.export_state() == before
    assert item_id in mem.items
    assert "поврежд" in writer.text


async def test_new_starts_fresh_session(tmp_path):
    repl, mem, writer = _build([], root=str(tmp_path))
    await mem.add("log", "данные старой сессии")
    await repl.dispatch("/new beta")
    assert repl.session == "beta"
    assert len(mem.items) == 0


async def test_new_clears_raw_tail_and_previous_manifest(tmp_path):
    repl, mem, _ = _build([], root=str(tmp_path))
    stale_id = await mem.add("log", "stale manifest record")
    await mem.forget(stale_id)
    await repl.core.turn("OLD_NEW_RAW_SENTINEL")
    assert mem.manifest.entries
    assert repl.core.last_context_plan is not None

    await repl.dispatch("/new beta")

    assert repl.core.tail == []
    assert repl.core.last_context_plan is None
    assert not mem.manifest.entries
    await repl.core.turn("fresh after new")
    sent_content = "\n".join(
        str(message.get("content", ""))
        for message in repl.core.llm.chat_calls[-1]["messages"]
    )
    assert "OLD_NEW_RAW_SENTINEL" not in sent_content


async def test_git_command_uses_bash_permission_path(tmp_path, monkeypatch):
    from protoprompt_cli.tools import ToolResult

    repl, _, writer = _build([], root=str(tmp_path))
    actions = []

    async def run(action):
        actions.append(action)
        return ToolResult(True, "git output", tool="bash")

    monkeypatch.setattr(repl.tools, "run", run)

    await repl.dispatch('/git log --format="%h %s" --author "Jane Doe"')

    assert len(actions) == 1
    assert actions[0].name == "bash"
    assert actions[0].body == "git log '--format=%h %s' --author 'Jane Doe'"
    assert writer.text == "git output"


@pytest.mark.parametrize("command", ["!echo unsafe", "/git status"])
async def test_tool_and_git_output_cannot_change_terminal_state(
    tmp_path, monkeypatch, command
):
    from protoprompt_cli.tools import ToolResult

    unsafe = "before\x1b]52;c;clipboard\x07\rafter\x9b31m"
    repl, _, writer = _build(
        [command, "/exit"] if command.startswith("!") else [],
        root=str(tmp_path),
    )

    async def run(action):
        return ToolResult(True, unsafe, tool="bash")

    monkeypatch.setattr(repl.tools, "run", run)
    if command.startswith("!"):
        await repl.run()
    else:
        await repl.dispatch(command)

    assert unsafe not in writer.text
    assert "\x1b" not in writer.text
    assert "\x07" not in writer.text
    assert "\r" not in writer.text
    assert "\x9b" not in writer.text
    assert r"\u001b]52;c;clipboard\u0007\u000dafter\u009b31m" in writer.text


async def test_nonstream_model_reply_and_memory_views_are_terminal_safe():
    unsafe = "answer\x1b]52;c;clipboard\x07\u202e"
    repl, mem, writer = _build(["question", "/exit"], llm=MockLLM(responses=[unsafe]))
    repl.stream = False

    await repl.run()
    await repl.dispatch("/memory")
    await repl.dispatch("/context")

    assert unsafe not in writer.text
    assert "\x1b" not in writer.text
    assert "\x07" not in writer.text
    assert "\u202e" not in writer.text
    assert r"\u001b]52;c;clipboard\u0007\u202e" in writer.text
    assert mem.items


async def test_streamed_model_text_is_safe_before_later_permission_prompt(tmp_path):
    class StreamingMockLLM(MockLLM):
        async def chat_stream(self, messages, model="", on_token=None, **options):
            self.chat_calls.append({"messages": list(messages), "model": model, **options})
            response = self.responses.pop(0)
            for token in ("\x1b[8mspoofed-question", response):
                on_token(token)
            return response

    prompts = []
    answers = iter(["run it", "n", "/exit"])

    def reader(prompt):
        prompts.append(prompt)
        return next(answers)

    llm = StreamingMockLLM(responses=[
        '<action name="bash">echo never-run</action>',
        "safe final reply",
    ])
    repl, _, writer = _build([], root=str(tmp_path), llm=llm, readline=reader)

    await repl.run()

    assert any('разрешить "bash"? [y/N/a] ' in prompt for prompt in prompts)
    assert "запрошенный payload" in writer.text
    assert "\x1b" not in writer.text
    assert r"\u001b[8mspoofed-question" in writer.text


async def test_git_command_quotes_shell_metacharacters_before_bash(tmp_path, monkeypatch):
    from protoprompt_cli.tools import ToolResult

    repl, _, _ = _build([], root=str(tmp_path))
    actions = []

    async def run(action):
        actions.append(action)
        return ToolResult(True, "", tool="bash")

    monkeypatch.setattr(repl.tools, "run", run)

    await repl.dispatch("/git status; echo injected")

    assert len(actions) == 1
    assert actions[0].body == "git 'status;' echo injected"


async def test_git_command_respects_bash_permission(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    repl.tools.perms["bash"] = "deny"

    await repl.dispatch("/git status")

    assert "permission denied: bash" in writer.text


async def test_git_command_rejects_malformed_shell_quoting(tmp_path, monkeypatch):
    repl, _, writer = _build([], root=str(tmp_path))

    async def fail_if_called(action):
        raise AssertionError(f"ToolRunner must not run malformed Git args: {action}")

    monkeypatch.setattr(repl.tools, "run", fail_if_called)

    await repl.dispatch('/git commit -m "unterminated')

    assert "некорректные аргументы git" in writer.text


async def test_git_without_args_usage():
    repl, _, writer = _build([])
    await repl.dispatch("/git")
    assert "использование" in writer.text


async def test_history_prints_recent_input():
    repl, _, writer = _build([])
    repl._remember("first question")
    repl._remember("second question")
    await repl.dispatch("/history 1")
    assert "second question" in writer.text
    assert "first question" not in writer.text


async def test_init_creates_user_owned_config(tmp_path):
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/init")
    from protoprompt_cli.config import user_config_path

    config = user_config_path(tmp_path)
    assert config.is_file()
    contents = config.read_text(encoding="utf-8")
    assert "backend" in contents
    assert "request_max_tokens" in contents


async def test_init_does_not_overwrite_existing_config(tmp_path):
    from protoprompt_cli.config import user_config_path

    config = user_config_path(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("custom = true", encoding="utf-8")
    repl, _, writer = _build([], root=str(tmp_path))
    await repl.dispatch("/init")
    assert config.read_text(encoding="utf-8") == "custom = true"
    assert "уже существует" in writer.text


async def test_shell_shorthand_uses_permission_and_memory(tmp_path):
    repl, mem, writer = _build(
        ["!echo hello", "y", "/exit"], root=str(tmp_path)
    )
    await repl.run()
    if sys.platform == "linux":
        assert "hello" in writer.text
    else:
        assert "safe jailed shell cwd" in writer.text
    assert any("shell:" in item.summary for item in mem.items.values())


# ── мультистрочный ввод ─────────────────────────────────────────


async def test_multiline_input_joins_lines():
    repl, _, writer = _build([])
    reader = FakeReader(["вторая", "}"])
    repl._readline = reader
    joined = await repl._read_multiline("первая {")
    assert joined == "первая {\nвторая\n}"


async def test_plain_line_not_multiline():
    repl, _, writer = _build(["простая строка"])
    joined = await repl._read_multiline("простая строка")
    assert joined == "простая строка"


async def test_run_processes_multiline_prompt():
    repl, _, writer = _build(["вопрос {"])
    # без закрывающей скобки дойдём до EOF и обработаем как обычный ход
    await repl.run()
    assert "mocked response" in writer.text
