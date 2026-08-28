"""Тесты ToolRunner: инструменты, jail, права."""

from __future__ import annotations

import sys

import pytest

from protoprompt_cli.actions import Action
from protoprompt_cli.tools import (
    DEFAULT_PERMS,
    PERM_ALLOW,
    PERM_ASK,
    PERM_DENY,
    ToolRunner,
)


def _action(name, body="", **kwargs):
    return Action(name=name, body=body, kwargs=kwargs)


@pytest.fixture
def runner(tmp_path):
    perms = {"bash": "allow", "write": "allow", "edit": "allow"}
    return ToolRunner(tmp_path, perms=perms)


# ── базовые права ────────────────────────────────────────────────


def test_default_perms_reading_allowed_writing_asked():
    assert DEFAULT_PERMS["read"] == PERM_ALLOW
    assert DEFAULT_PERMS["bash"] == PERM_ASK
    assert DEFAULT_PERMS["write"] == PERM_ASK
    assert DEFAULT_PERMS["edit"] == PERM_ASK


async def test_unknown_tool_fails(runner):
    result = await runner.run(_action("teleport"))
    assert result.ok is False
    assert "unknown tool" in result.error


async def test_deny_permission_blocks(runner):
    runner.perms["bash"] = PERM_DENY
    result = await runner.run(_action("bash", "echo hi"))
    assert result.ok is False
    assert "permission denied" in result.error


async def test_ask_without_callback_denies(runner):
    runner.perms["write"] = PERM_ASK
    result = await runner.run(_action("write", "data", path="f.txt"))
    assert result.ok is False
    assert "permission denied" in result.error


async def test_ask_callback_grants(runner):
    runner.perms["write"] = PERM_ASK
    decisions = []
    runner.ask_callback = lambda action: _record(decisions, action)
    result = await runner.run(_action("write", "data", path="f.txt"))
    assert result.ok is True
    assert decisions[0].name == "write"


async def test_ask_callback_denies(runner):
    runner.perms["bash"] = PERM_ASK
    runner.ask_callback = lambda action: False
    result = await runner.run(_action("bash", "echo hi"))
    assert result.ok is False


def _record(lst, value):
    lst.append(value)
    return True


# ── bash ──────────────────────────────────────────────────────────


async def test_bash_runs_and_reports_exit(runner, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hello')", encoding="utf-8")
    result = await runner.run(
        _action("bash", f'"{sys.executable}" "{script}"')
    )
    assert result.ok is True
    assert "hello" in result.output
    assert "exit=0" in result.output


async def test_bash_reports_nonzero_exit(runner):
    result = await runner.run(_action("bash", "exit 3"))
    assert result.ok is False
    assert "exit=3" in result.output


async def test_bash_empty_command_fails(runner):
    result = await runner.run(_action("bash", "   "))
    assert result.ok is False
    assert "empty" in result.error


async def test_bash_truncates_long_output(runner, tmp_path):
    runner.max_output = 100
    script = tmp_path / "gen.py"
    script.write_text("print('x' * 500)", encoding="utf-8")
    result = await runner.run(
        _action("bash", f'"{sys.executable}" "{script}"')
    )
    assert result.ok is True
    assert "обрезано" in result.output


# ── read / write / edit ──────────────────────────────────────────


async def test_read_returns_content(runner, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    result = await runner.run(_action("read", path="a.py"))
    assert result.ok is True
    assert "def f()" in result.output


async def test_read_relative_path_resolves_under_root(runner, tmp_path):
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "b.py").write_text("x = 1", encoding="utf-8")
    result = await runner.run(_action("read", path="src/b.py"))
    assert result.ok is True
    assert "x = 1" in result.output


async def test_read_missing_file_fails(runner):
    result = await runner.run(_action("read", path="missing.py"))
    assert result.ok is False
    assert "no such file" in result.error


async def test_read_outside_root_is_jailed(runner, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = await runner.run(_action("read", path=str(outside)))
    assert result.ok is False
    assert "outside project root" in result.error


async def test_write_creates_file_and_parents(runner, tmp_path):
    result = await runner.run(_action("write", "hello world", path="src/x.txt"))
    assert result.ok is True
    assert (tmp_path / "src" / "x.txt").read_text(encoding="utf-8") == "hello world"


async def test_write_requires_path(runner):
    result = await runner.run(_action("write", "data"))
    assert result.ok is False
    assert "path" in result.error


async def test_write_outside_root_denied(runner, tmp_path):
    result = await runner.run(
        _action("write", "x", path=str(tmp_path.parent / "evil.txt"))
    )
    assert result.ok is False


async def test_edit_replaces_once(runner, tmp_path):
    (tmp_path / "a.py").write_text("alpha beta alpha", encoding="utf-8")
    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )
    assert result.ok is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "OMEGA beta alpha"


async def test_edit_missing_pattern_soft_fails(runner, tmp_path):
    (tmp_path / "a.py").write_text("nothing here", encoding="utf-8")
    result = await runner.run(
        _action("edit", body="", path="a.py", old="zzz", new="yyy")
    )
    assert result.ok is False
    assert "pattern not found" in result.error


async def test_edit_missing_file_fails(runner):
    result = await runner.run(
        _action("edit", body="", path="nope.py", old="a", new="b")
    )
    assert result.ok is False


# ── glob / grep ──────────────────────────────────────────────────


async def test_glob_returns_matches(runner, tmp_path):
    (tmp_path / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "two.txt").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "one.py" in result.output
    assert "two.txt" not in result.output


async def test_glob_recursive(runner, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep.py").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "**/*.py"))
    assert result.ok is True
    assert "src/deep.py" in result.output


async def test_grep_finds_lines_with_location(runner, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nfoo = 2\n", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="foo"))
    assert result.ok is True
    assert "a.py:2" in result.output
    assert "foo = 2" in result.output


async def test_grep_bad_regex_fails(runner):
    result = await runner.run(_action("grep", pattern="("))
    assert result.ok is False
    assert "regex" in result.error


async def test_grep_no_matches(runner, tmp_path):
    (tmp_path / "a.py").write_text("nothing", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="zzz"))
    assert result.ok is True
    assert "no matches" in result.output


# ── jail отключён ────────────────────────────────────────────────


async def test_jail_disabled_allows_outside_read(tmp_path):
    runner = ToolRunner(tmp_path, jail=False)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = await runner.run(_action("read", path=str(outside)))
    assert result.ok is True
    assert "secret" in result.output
