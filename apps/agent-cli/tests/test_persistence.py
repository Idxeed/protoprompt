"""Тесты персистентности: корень проекта, namespace, state."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoprompt.agent import WorkingMemory

from protoprompt_cli import persistence


def test_find_root_returns_git_root(tmp_path):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    assert persistence.find_root(sub) == project.resolve()


def test_find_root_falls_back_to_dir(tmp_path):
    bare = tmp_path / "no_git"
    bare.mkdir()
    assert persistence.find_root(bare) == bare.resolve()


def test_find_root_resolves_file_to_parent(tmp_path):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    f = project / "a.py"
    f.write_text("", encoding="utf-8")
    assert persistence.find_root(f) == project.resolve()


def test_namespace_is_deterministic_and_distinct():
    a = persistence.namespace_for("C:/proj/one")
    b = persistence.namespace_for("C:/proj/one")
    c = persistence.namespace_for("C:/proj/two")
    assert a == b
    assert a != c
    assert len(a) == 12


def test_state_paths(tmp_path):
    assert persistence.state_dir(tmp_path) == tmp_path / ".protoprompt"
    assert persistence.cold_db_path(tmp_path) == tmp_path / ".protoprompt" / "agent.db"
    assert persistence.state_json_path(tmp_path) == tmp_path / ".protoprompt" / "state.json"
    assert persistence.perms_json_path(tmp_path) == tmp_path / ".protoprompt" / "perms.json"


def test_load_json_missing_returns_default(tmp_path):
    assert persistence.load_json(tmp_path / "x.json", default=[]) == []


def test_load_json_bad_content_returns_default(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{broken", encoding="utf-8")
    assert persistence.load_json(p, default=None) is None


def test_json_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    persistence.save_json(p, {"a": [1, 2], "rus": "текст"})
    assert persistence.load_json(p) == {"a": [1, 2], "rus": "текст"}


async def test_save_load_state_roundtrip(tmp_path):
    mem = WorkingMemory(max_tokens=200)
    await mem.set_goal("цель сессии")
    e = await mem.add("edit", "def fix(): return True", summary="фикс")
    note_id = await mem.note("заметка про структуру", pin=True)

    persistence.save_state(mem, tmp_path)
    assert persistence.state_json_path(tmp_path).is_file()

    fresh = WorkingMemory(max_tokens=200)
    assert persistence.load_state(fresh, tmp_path) is True
    assert set(fresh.items) == {e, note_id}
    assert fresh.goal.text == "цель сессии"
    assert fresh.step == mem.step


def test_load_state_missing_returns_false(tmp_path):
    mem = WorkingMemory(max_tokens=200)
    assert persistence.load_state(mem, tmp_path) is False


# ── сессии ───────────────────────────────────────────────────────


def test_session_file_sanitizes_name():
    assert persistence.session_file(".", "my session!") == \
        Path(".protoprompt/sessions/my_session.json")
    assert persistence._sanitize_session("  ") == "default"
    assert persistence._sanitize_session("a/b\\c") == "a_b_c"


async def test_save_load_session_roundtrip(tmp_path):
    mem = WorkingMemory(max_tokens=200)
    item_id = await mem.note("заметка в сессии", pin=True)
    persistence.save_session(mem, tmp_path, "feature-x")

    fresh = WorkingMemory(max_tokens=200)
    assert persistence.session_exists(tmp_path, "feature-x")
    assert persistence.load_session(fresh, tmp_path, "feature-x") is True
    assert item_id in fresh.items


async def test_list_sessions_metadata(tmp_path):
    a = WorkingMemory(max_tokens=200)
    await a.note("сессия A", pin=True)
    persistence.save_session(a, tmp_path, "a")

    b = WorkingMemory(max_tokens=200)
    await b.set_goal("цель сессии B")
    await b.add("file", "def work(): pass", summary="файл B")
    persistence.save_session(b, tmp_path, "b")

    sessions = persistence.list_sessions(tmp_path)
    names = {s["name"] for s in sessions}
    assert names == {"a", "b"}
    meta_b = next(s for s in sessions if s["name"] == "b")
    assert meta_b["goal"].startswith("цель сессии B")
    assert meta_b["items"] >= 1


def test_list_sessions_empty_when_no_dir(tmp_path):
    assert persistence.list_sessions(tmp_path) == []


def test_load_missing_session_returns_false(tmp_path):
    assert persistence.load_session(WorkingMemory(max_tokens=200), tmp_path, "nope") is False


async def test_load_malformed_session_preserves_active_memory(tmp_path):
    mem = WorkingMemory(max_tokens=200)
    await mem.set_goal("keep active session")
    item_id = await mem.note("active memory", pin=True)
    before = mem.export_state()
    persistence.save_json(
        persistence.session_file(tmp_path, "broken"),
        {"items": [{"not": "a memory item"}]},
    )

    assert persistence.load_session(mem, tmp_path, "broken") is False
    assert mem.export_state() == before
    assert item_id in mem.items


def test_latest_session_returns_newest(tmp_path):
    import os
    import time

    persistence.save_json(persistence.session_file(tmp_path, "older"), {"items": []})
    old_time = time.time() - 10
    os.utime(persistence.session_file(tmp_path, "older"), (old_time, old_time))
    persistence.save_json(persistence.session_file(tmp_path, "newer"), {"items": [{"id": "m1"}]})
    assert persistence.latest_session(tmp_path) == "newer"
