"""Тесты персистентности: корень проекта, namespace, state."""

from __future__ import annotations

import os
from dataclasses import replace
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


def test_safe_project_directory_requires_an_existing_directory(tmp_path):
    missing = tmp_path / "missing"
    file_path = tmp_path / "not-a-directory.txt"
    file_path.write_text("not a project", encoding="utf-8")

    with pytest.raises(OSError):
        persistence.safe_project_directory(missing)
    with pytest.raises(OSError):
        persistence.safe_project_directory(file_path)


def test_namespace_is_deterministic_and_distinct(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    a = persistence.namespace_for(one)
    b = persistence.namespace_for(one)
    c = persistence.namespace_for(two)
    assert a == b
    assert a != c
    assert len(a) == 12


def test_captured_identity_is_stable_and_validates_current_root(tmp_path):
    identity = persistence.capture_project_identity(tmp_path)
    try:
        assert persistence.namespace_for(identity) == identity.namespace
        assert persistence.state_dir(identity) == persistence.state_dir(tmp_path)
        # Creating a normal child changes the directory's ctime.  Identity
        # must remain valid: it is pinned to stable birth generation instead.
        (tmp_path / "ordinary-work.txt").write_text("ok", encoding="utf-8")
        assert identity.assert_current(tmp_path) == tmp_path.resolve()
    finally:
        identity.close()


def test_captured_identity_exposes_a_pinned_root_descriptor(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    identity = persistence.capture_project_identity(project)
    descriptor = identity.duplicate_root_fd()
    try:
        pinned = os.fstat(descriptor)
        assert pinned.st_dev == identity.device
        assert pinned.st_ino == identity.inode

        project.rename(tmp_path / "retired-project")
        project.mkdir()

        # The held descriptor still names the original object, while the
        # replacement must never be accepted as its project namespace.
        assert os.fstat(descriptor).st_ino == identity.inode
        with pytest.raises(persistence.ProjectIdentityChanged):
            identity.assert_current(project)
    finally:
        os.close(descriptor)
        identity.close()


def test_identity_validates_descriptor_generation(tmp_path):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    identity = persistence.capture_project_identity(project)
    project_descriptor = identity.duplicate_root_fd()
    other_capture = persistence._capture_root(other)
    try:
        identity.assert_root_descriptor(project_descriptor)
        with pytest.raises(persistence.ProjectIdentityChanged):
            identity.assert_root_descriptor(other_capture.fd)
    finally:
        os.close(project_descriptor)
        other_capture.close()
        identity.close()


def test_identity_rejects_reused_inode_with_a_new_birth_generation(
    tmp_path, monkeypatch
):
    """Generation prevents state reuse even if a filesystem recycles an inode."""
    identity = persistence.capture_project_identity(tmp_path)
    real_capture = persistence._capture_root

    def capture_with_reused_inode(root):
        capture = real_capture(root)
        capture.snapshot = replace(
            capture.snapshot, generation=capture.snapshot.generation + 1
        )
        return capture

    monkeypatch.setattr(persistence, "_capture_root", capture_with_reused_inode)
    try:
        with pytest.raises(persistence.ProjectIdentityChanged):
            identity.assert_current(tmp_path)
    finally:
        identity.close()


def test_namespace_material_includes_stable_birth_generation(tmp_path):
    identity = persistence.capture_project_identity(tmp_path)
    try:
        snapshot = persistence._RootSnapshot(
            root=identity.root,
            canonical_path=identity.canonical_path,
            device=identity.device,
            inode=identity.inode,
            generation=identity.generation,
        )
        assert persistence._namespace_from_snapshot(snapshot) == identity.namespace
        assert (
            persistence._namespace_from_snapshot(
                replace(snapshot, generation=snapshot.generation + 1)
            )
            != identity.namespace
        )
    finally:
        identity.close()


def test_path_namespace_releases_its_temporary_root_pin(tmp_path, monkeypatch):
    calls = 0
    original_close = persistence.ProjectIdentity.close

    def record_close(self):
        nonlocal calls
        calls += 1
        return original_close(self)

    monkeypatch.setattr(persistence.ProjectIdentity, "close", record_close)
    persistence.namespace_for(tmp_path)
    persistence.state_dir(tmp_path)
    assert calls == 2


def test_identity_close_is_idempotent_and_disables_descriptor_access(tmp_path):
    identity = persistence.capture_project_identity(tmp_path)
    identity.close()
    identity.close()
    with pytest.raises(OSError, match="closed"):
        identity.duplicate_root_fd()
    with pytest.raises(persistence.ProjectIdentityChanged, match="unavailable"):
        identity.assert_current(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows native root probe only")
def test_windows_identity_recheck_does_not_resolve_root_path(tmp_path, monkeypatch):
    """A repeated identity check must not follow a swapped junction by path."""
    identity = persistence.capture_project_identity(tmp_path)

    def resolve_must_not_run(*args, **kwargs):
        raise AssertionError("identity recheck must use the native no-reparse walk")

    monkeypatch.setattr(Path, "resolve", resolve_must_not_run)
    assert identity.assert_current(tmp_path) == identity.root


@pytest.mark.skipif(os.name != "nt", reason="Windows native root probe only")
def test_windows_identity_rejects_a_reparse_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")

    with pytest.raises(OSError, match="reparse"):
        persistence.capture_project_identity(alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows native root discovery only")
def test_windows_find_root_uses_native_marker_probe_without_resolve(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    # A worktree's .git is a regular file, not a directory.
    (project / ".git").write_text("gitdir: ../metadata", encoding="utf-8")

    def resolve_must_not_run(*args, **kwargs):
        raise AssertionError("Windows root discovery must not resolve by pathname")

    monkeypatch.setattr(Path, "resolve", resolve_must_not_run)
    assert persistence.find_root(nested) == project


@pytest.mark.skipif(os.name != "nt", reason="Windows native root discovery only")
def test_windows_find_root_rejects_unc_before_discovery():
    with pytest.raises(OSError, match="local Windows drive"):
        persistence.find_root(r"\\server\share\project")


@pytest.mark.skipif(os.name != "nt", reason="Windows native root discovery only")
def test_windows_find_root_rejects_a_reparse_start(tmp_path):
    target = tmp_path / "target"
    (target / ".git").mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")

    with pytest.raises(OSError, match="reparse"):
        persistence.find_root(alias)


def test_captured_identity_rejects_a_replaced_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    identity = persistence.capture_project_identity(project)

    project.rename(tmp_path / "retired-project")
    project.mkdir()

    with pytest.raises(persistence.ProjectIdentityChanged):
        identity.assert_current(project)


def test_namespace_changes_when_same_path_is_recreated(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    before = persistence.namespace_for(project)

    project.rename(tmp_path / "retired-project")
    project.mkdir()

    assert persistence.namespace_for(project) != before


def test_namespace_fails_closed_when_root_identity_is_unavailable(tmp_path):
    with pytest.raises(OSError):
        persistence.namespace_for(tmp_path / "missing-project")


def test_state_paths_are_user_owned(tmp_path, monkeypatch):
    state_home = tmp_path / "user-state"
    monkeypatch.setenv("PROTOPROMPT_AGENT_STATE_DIR", str(state_home))
    expected = state_home / persistence.namespace_for(tmp_path)
    assert persistence.state_dir(tmp_path) == expected
    assert persistence.cold_db_path(tmp_path) == expected / "agent.db"
    assert persistence.state_json_path(tmp_path) == expected / "state.json"
    assert persistence.perms_json_path(tmp_path) == expected / "perms.json"
    assert persistence.legacy_state_dir(tmp_path) == tmp_path / ".protoprompt"


def test_legacy_project_state_symlink_is_never_opened(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    legacy = project / ".protoprompt"
    try:
        legacy.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    directory = persistence.ensure_state_dir(project)
    assert directory == persistence.state_dir(project)
    assert directory.is_dir()
    assert list(external.iterdir()) == []


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
    path = persistence.session_file(".", "my session!")
    assert path == persistence.session_dir(".") / "my_session.json"
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
