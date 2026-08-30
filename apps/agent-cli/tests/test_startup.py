"""Тесты стартового меню без терминала."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _mocks import FakeWriter

from protoprompt_cli import startup
from protoprompt_cli.startup import choose_project


def test_default_choice_opens_current(tmp_path):
    output = FakeWriter()
    selected = choose_project(tmp_path, readline=lambda prompt: "", write=output)
    assert selected == tmp_path.resolve()
    assert "pp-agent" in output.text


def test_menu_can_choose_other_directory(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    answers = iter(["2", str(other)])
    selected = choose_project(tmp_path, readline=lambda prompt: next(answers))
    assert selected == other.resolve()


def test_menu_quit_returns_none(tmp_path):
    assert choose_project(tmp_path, readline=lambda prompt: "q") is None


def test_invalid_choice_falls_back_to_current(tmp_path):
    assert choose_project(tmp_path, readline=lambda prompt: "wat") == tmp_path.resolve()


def test_missing_other_directory_returns_none(tmp_path):
    answers = iter(["2", str(tmp_path / "missing")])
    assert choose_project(tmp_path, readline=lambda prompt: next(answers)) is None


def test_eof_returns_none(tmp_path):
    def eof(prompt):
        raise EOFError

    assert choose_project(tmp_path, readline=eof) is None


def test_launcher_escapes_an_unsafe_project_path_before_repl(monkeypatch):
    unsafe = Path("unsafe-\x1b]52;c;clipboard\x07\u202e")
    output = FakeWriter()

    monkeypatch.setattr(startup, "safe_project_directory", lambda current: unsafe)

    assert choose_project("ignored", readline=lambda prompt: "q", write=output) is None

    assert "\x1b" not in output.text
    assert "\x07" not in output.text
    assert "\u202e" not in output.text
    assert r"unsafe-\u001b]52;c;clipboard\u0007\u202e" in output.text


def test_launcher_escapes_an_unavailable_user_path(monkeypatch, tmp_path):
    output = FakeWriter()
    unsafe = "unsafe-\x1b]52;c;clipboard\x07"
    answers = iter(["2", unsafe])

    def fake_safe_project_directory(path):
        if str(path) == unsafe:
            raise OSError("nope")
        return tmp_path

    monkeypatch.setattr(startup, "safe_project_directory", fake_safe_project_directory)

    assert choose_project(tmp_path, readline=lambda prompt: next(answers), write=output) is None

    assert "\x1b" not in output.text
    assert "\x07" not in output.text
    assert r"unsafe-\u001b]52;c;clipboard\u0007" in output.text


@pytest.mark.skipif(os.name != "nt", reason="Windows project selection only")
def test_windows_menu_does_not_resolve_a_project_path(tmp_path, monkeypatch):
    def resolve_must_not_run(*args, **kwargs):
        raise AssertionError("project chooser must preserve the no-reparse boundary")

    monkeypatch.setattr(Path, "resolve", resolve_must_not_run)
    selected = choose_project(tmp_path, readline=lambda prompt: "", write=lambda _: None)
    assert selected == tmp_path
