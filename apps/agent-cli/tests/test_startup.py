"""Тесты стартового меню без терминала."""

from __future__ import annotations

from pathlib import Path

from _mocks import FakeWriter

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
