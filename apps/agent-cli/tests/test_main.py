"""Тесты точки входа __main__: неинтерактивный режим и авто-resume."""

from __future__ import annotations

import pytest

from protoprompt.agent import WorkingMemory

from _mocks import MockLLM

from protoprompt_cli import __main__ as main_mod
from protoprompt_cli import persistence


async def test_print_mode_runs_and_saves_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(["-p", "привет", str(tmp_path)])
    code = await main_mod._run(args)
    assert code == 0
    state = persistence.load_json(persistence.session_file(tmp_path, "default"))
    assert state, "сессия должна сохраниться после прогона"
    texts = [i["text"] for i in state["items"]]
    assert "привет" in texts


async def test_print_mode_resumes_previous_session(tmp_path, monkeypatch):
    first = WorkingMemory(max_tokens=200)
    old_note = await first.note("старая заметка про проект", pin=True)
    persistence.save_session(first, tmp_path, "default")

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(["-p", "новый вопрос", str(tmp_path)])
    await main_mod._run(args)

    state = persistence.load_json(persistence.session_file(tmp_path, "default"))
    ids = [i["id"] for i in state["items"]]
    assert old_note in ids, "горячий набор предыдущей сессии должен восстановиться"


async def test_print_mode_uses_named_session(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["-p", "тест", "--session", "fix-task", str(tmp_path)]
    )
    await main_mod._run(args)
    assert persistence.session_file(tmp_path, "fix-task").is_file()


async def test_continue_ignores_a_corrupt_newer_session(tmp_path, monkeypatch):
    default = WorkingMemory(max_tokens=200)
    await default.note("default session memory", pin=True)
    persistence.save_session(default, tmp_path, "default")
    broken = persistence.session_file(tmp_path, "broken")
    broken.write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["--continue", "-p", "new default question", str(tmp_path)]
    )
    await main_mod._run(args)

    state = persistence.load_json(persistence.session_file(tmp_path, "default"))
    assert any("new default question" in item["text"] for item in state["items"])
    assert broken.read_text(encoding="utf-8") == "{not valid json"


async def test_print_mode_json_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["-p", "вопрос", "--output-format", "json", str(tmp_path)]
    )
    await main_mod._run(args)
    captured = capsys.readouterr().out
    payload = __import__("json").loads(captured)
    assert "reply" in payload
    assert "usage" in payload
    assert isinstance(payload["usage"]["input_tokens"], int)


async def test_print_and_trace_output_are_terminal_safe(tmp_path, monkeypatch, capsys):
    unsafe = "reply\x1b]52;c;clipboard\x07\r\u202e"
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM(responses=[unsafe]))
    args = main_mod.build_parser().parse_args(
        ["--trace", "-p", "question", str(tmp_path)]
    )

    assert await main_mod._run(args) == 0

    captured = capsys.readouterr().out
    assert unsafe not in captured
    assert "\x1b" not in captured
    assert "\x07" not in captured
    assert "\r" not in captured
    assert "\u202e" not in captured
    assert r"\u001b]52;c;clipboard\u0007\u000d\u202e" in captured


async def test_print_mode_streaming_output_is_terminal_safe(tmp_path, monkeypatch, capsys):
    class StreamingMockLLM(MockLLM):
        async def chat_stream(self, messages, model="", on_token=None, **options):
            self.chat_calls.append({"messages": list(messages), "model": model, **options})
            reply = self.responses.pop(0)
            on_token(reply)
            return reply

    unsafe = "stream\x1b[8m\x9b31m\u200b"
    monkeypatch.setattr(
        main_mod, "make_llm", lambda *a, **k: StreamingMockLLM(responses=[unsafe])
    )
    args = main_mod.build_parser().parse_args(
        ["--stream", "-p", "question", str(tmp_path)]
    )

    assert await main_mod._run(args) == 0

    captured = capsys.readouterr().out
    assert unsafe not in captured
    assert "\x1b" not in captured
    assert "\x9b" not in captured
    assert "\u200b" not in captured
    assert r"stream\u001b[8m\u009b31m\u200b" in captured


async def test_print_mode_plan_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["-p", "задача", "--plan", str(tmp_path)]
    )
    await main_mod._run(args)
    state = persistence.load_json(persistence.session_file(tmp_path, "default"))
    texts = " ".join(i.get("text", "") for i in state["items"])
    assert "план" in texts, "план-режим должен сохранить план"


async def test_print_mode_uses_git_root(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(["-p", "тест", str(project)])
    await main_mod._run(args)
    assert persistence.session_file(project, "default").is_file()


async def test_project_local_config_and_permissions_are_ignored_by_default(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    legacy = project / ".protoprompt"
    legacy.mkdir()
    (legacy / "config.toml").write_text('[llm]\nbackend = "httpx"\n', encoding="utf-8")
    (legacy / "perms.json").write_text('{"bash": "allow"}', encoding="utf-8")
    captured: dict[str, object] = {}
    real_tool_runner = main_mod.ToolRunner

    def make_llm(cfg):
        captured["backend"] = cfg["llm"]["backend"]
        return MockLLM()

    class CapturingToolRunner(real_tool_runner):
        def __init__(self, *args, **kwargs):
            captured["perms"] = dict(kwargs.get("perms") or {})
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_mod, "make_llm", make_llm)
    monkeypatch.setattr(main_mod, "ToolRunner", CapturingToolRunner)
    args = main_mod.build_parser().parse_args(["-p", "привет", str(project)])
    assert await main_mod._run(args) == 0

    assert captured["backend"] == "ollama"
    assert captured["perms"] == {}
    assert persistence.session_file(project, "default").is_file()
    assert not (legacy / "agent.db").exists()


async def test_malformed_user_permissions_are_ignored(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    persistence.ensure_state_dir(project)
    persistence.save_json(persistence.perms_json_path(project), ["bash", "allow"])
    captured: dict[str, object] = {}
    real_tool_runner = main_mod.ToolRunner

    class CapturingToolRunner(real_tool_runner):
        def __init__(self, *args, **kwargs):
            captured["perms"] = dict(kwargs.get("perms") or {})
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    monkeypatch.setattr(main_mod, "ToolRunner", CapturingToolRunner)
    args = main_mod.build_parser().parse_args(["-p", "привет", str(project)])
    assert await main_mod._run(args) == 0
    assert captured["perms"] == {}


async def test_user_owned_permissions_restore_only_durable_denials(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    persistence.ensure_state_dir(project)
    persistence.save_json(
        persistence.perms_json_path(project),
        {"bash": "allow", "write": "deny"},
    )
    captured: dict[str, object] = {}
    real_tool_runner = main_mod.ToolRunner

    class CapturingToolRunner(real_tool_runner):
        def __init__(self, *args, **kwargs):
            captured["perms"] = dict(kwargs.get("perms") or {})
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    monkeypatch.setattr(main_mod, "ToolRunner", CapturingToolRunner)
    args = main_mod.build_parser().parse_args(["-p", "привет", str(project)])
    assert await main_mod._run(args) == 0
    assert captured["perms"] == {"write": "deny"}


async def test_replaced_project_does_not_inherit_user_permissions(tmp_path, monkeypatch):
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("PROTOPROMPT_AGENT_STATE_DIR", str(state_home))
    project = tmp_path / "project"
    project.mkdir()
    persistence.ensure_state_dir(project)
    old_perms = persistence.perms_json_path(project)
    persistence.save_json(old_perms, {"bash": "allow"})

    project.rename(tmp_path / "retired-project")
    project.mkdir()
    assert persistence.perms_json_path(project) != old_perms

    captured: dict[str, object] = {}
    real_tool_runner = main_mod.ToolRunner

    class CapturingToolRunner(real_tool_runner):
        def __init__(self, *args, **kwargs):
            captured["perms"] = dict(kwargs.get("perms") or {})
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    monkeypatch.setattr(main_mod, "ToolRunner", CapturingToolRunner)
    args = main_mod.build_parser().parse_args(["-p", "привет", str(project)])
    assert await main_mod._run(args) == 0
    assert captured["perms"] == {}


async def test_root_swap_between_perms_load_and_runner_is_rejected(
    tmp_path, monkeypatch
):
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("PROTOPROMPT_AGENT_STATE_DIR", str(state_home))
    project = tmp_path / "project"
    project.mkdir()
    persistence.ensure_state_dir(project)
    persistence.save_json(persistence.perms_json_path(project), {"bash": "allow"})
    retired = tmp_path / "retired-project"
    captured: dict[str, object] = {}
    real_tool_runner = main_mod.ToolRunner

    class SwappingToolRunner(real_tool_runner):
        def __init__(self, *args, **kwargs):
            captured["perms"] = dict(kwargs.get("perms") or {})
            captured["identity"] = kwargs.get("project_identity")
            project.rename(retired)
            project.mkdir()
            super().__init__(*args, **kwargs)
            captured["constructed"] = True

    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    monkeypatch.setattr(main_mod, "ToolRunner", SwappingToolRunner)
    args = main_mod.build_parser().parse_args(["-p", "привет", str(project)])
    with pytest.raises(persistence.ProjectIdentityChanged):
        await main_mod._run(args)

    assert captured["perms"] == {}
    assert isinstance(captured["identity"], persistence.ProjectIdentity)
    assert "constructed" not in captured


async def test_explicit_config_remains_supported(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted.toml"
    project = tmp_path / "project"
    project.mkdir()
    trusted.write_text('[llm]\nbackend = "httpx"\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def make_llm(cfg):
        captured["backend"] = cfg["llm"]["backend"]
        return MockLLM()

    monkeypatch.setattr(main_mod, "make_llm", make_llm)
    args = main_mod.build_parser().parse_args(
        ["--config", str(trusted), "-p", "привет", str(project)]
    )
    assert await main_mod._run(args) == 0
    assert captured["backend"] == "httpx"


def test_parser_defaults():
    args = main_mod.build_parser().parse_args([])
    assert args.prompt is None
    assert args.backend is None
    assert args.budget is None
    assert args.request_max_tokens is None
    assert args.output_reserve is None
    assert args.stream is None
    assert args.no_menu is False


def test_parser_flags():
    args = main_mod.build_parser().parse_args(
        ["--backend", "httpx", "--budget", "700", "--request-max-tokens", "4096",
         "--output-reserve", "512", "--trace", "-p", "go",
         "--output-format", "json", "--session", "s1", "--plan", "dir"]
    )
    assert args.backend == "httpx"
    assert args.budget == 700
    assert args.request_max_tokens == 4096
    assert args.output_reserve == 512
    assert args.trace is True
    assert args.prompt == "go"
    assert args.output_format == "json"
    assert args.session == "s1"
    assert args.plan is True
    assert args.path == "dir"


async def test_print_mode_applies_request_limits_separately_from_memory(
    tmp_path, monkeypatch
):
    llm = MockLLM()
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: llm)
    args = main_mod.build_parser().parse_args([
        "-p", "вопрос", "--budget", "700", "--request-max-tokens", "4096",
        "--output-reserve", "41", str(tmp_path),
    ])

    await main_mod._run(args)

    assert llm.chat_calls[0]["max_tokens"] == 41


def test_parser_resume_aliases_and_no_stream():
    args = main_mod.build_parser().parse_args(
        ["--resume", "bugfix", "--no-stream", "project"]
    )
    assert args.resume_session == "bugfix"
    assert args.stream is False


def test_parser_no_menu():
    args = main_mod.build_parser().parse_args(["--no-menu"])
    assert args.no_menu is True


def test_parser_errors_do_not_render_terminal_control_arguments(capsys):
    parser = main_mod.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["first-path", "second-path", "\x1b]52;c;clipboard\x07"])

    captured = capsys.readouterr().err
    assert "\x1b" not in captured
    assert "\x07" not in captured
    assert r"\u001b]52;c;clipboard\u0007" in captured


def test_json_and_stream_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["-p", "hi", "--output-format", "json", "--stream", str(tmp_path)]
    )
    with pytest.raises(ValueError, match="stream"):
        import asyncio
        asyncio.run(main_mod._run(args))
