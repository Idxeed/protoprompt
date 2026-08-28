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


def test_parser_defaults():
    args = main_mod.build_parser().parse_args([])
    assert args.prompt is None
    assert args.backend is None
    assert args.budget is None
    assert args.stream is None
    assert args.no_menu is False


def test_parser_flags():
    args = main_mod.build_parser().parse_args(
        ["--backend", "httpx", "--budget", "700", "--trace", "-p", "go",
         "--output-format", "json", "--session", "s1", "--plan", "dir"]
    )
    assert args.backend == "httpx"
    assert args.budget == 700
    assert args.trace is True
    assert args.prompt == "go"
    assert args.output_format == "json"
    assert args.session == "s1"
    assert args.plan is True
    assert args.path == "dir"


def test_parser_resume_aliases_and_no_stream():
    args = main_mod.build_parser().parse_args(
        ["--resume", "bugfix", "--no-stream", "project"]
    )
    assert args.resume_session == "bugfix"
    assert args.stream is False


def test_parser_no_menu():
    args = main_mod.build_parser().parse_args(["--no-menu"])
    assert args.no_menu is True


def test_json_and_stream_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "make_llm", lambda *a, **k: MockLLM())
    args = main_mod.build_parser().parse_args(
        ["-p", "hi", "--output-format", "json", "--stream", str(tmp_path)]
    )
    with pytest.raises(ValueError, match="stream"):
        import asyncio
        asyncio.run(main_mod._run(args))
