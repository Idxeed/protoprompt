"""Тесты ToolRunner: инструменты, jail, права."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from protoprompt_cli import tools as tools_module
from protoprompt_cli import persistence
from protoprompt_cli.actions import Action
from protoprompt_cli.tools import (
    DEFAULT_PERMS,
    PERM_ALLOW,
    PERM_ASK,
    PERM_DENY,
    ToolRunner,
)


_WINDOWS_JAILED_TREE_UNAVAILABLE = pytest.mark.skipif(
    os.name == "nt", reason="Windows jailed tree traversal fails closed"
)
_LINUX_JAILED_SHELL_AVAILABLE = pytest.mark.skipif(
    sys.platform != "linux",
    reason="descriptor-pinned jailed shell requires Linux /proc",
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


def test_persisted_permissions_cannot_add_tools_or_invalid_modes(tmp_path):
    runner = ToolRunner(
        tmp_path,
        perms={"bash": "allow", "read": "unexpected", "teleport": "allow"},
    )
    assert runner.perms["bash"] == PERM_ALLOW
    assert runner.perms["read"] == PERM_ALLOW
    assert "teleport" not in runner.perms


def test_non_mapping_permissions_are_ignored(tmp_path):
    runner = ToolRunner(tmp_path, perms=["bash", "allow"])  # type: ignore[arg-type]
    assert runner.perms == DEFAULT_PERMS


def test_runner_accepts_matching_project_identity(tmp_path):
    identity = persistence.capture_project_identity(tmp_path)
    runner = ToolRunner(
        tmp_path, perms={"bash": "allow"}, project_identity=identity
    )
    assert runner.root == tmp_path.resolve()
    assert runner.perms["bash"] == PERM_ALLOW


async def test_runner_rechecks_identity_after_approval_wait(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    identity = persistence.capture_project_identity(project)
    retired = tmp_path / "retired-project"

    async def swap_during_approval(action):
        project.rename(retired)
        project.mkdir()
        return True

    runner = ToolRunner(
        project, project_identity=identity, ask_callback=swap_during_approval
    )
    result = await runner.run(_action("bash", "echo must-not-run"))
    assert result.ok is False
    assert "project root identity changed" in result.error


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


@_LINUX_JAILED_SHELL_AVAILABLE
async def test_bash_runs_and_reports_exit(runner, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text("print('hello')", encoding="utf-8")
    result = await runner.run(
        _action("bash", f'"{sys.executable}" "{script}"')
    )
    assert result.ok is True
    assert "hello" in result.output
    assert "exit=0" in result.output


@_LINUX_JAILED_SHELL_AVAILABLE
async def test_bash_reports_nonzero_exit(runner):
    result = await runner.run(_action("bash", "exit 3"))
    assert result.ok is False
    assert "exit=3" in result.output


async def test_bash_empty_command_fails(runner):
    result = await runner.run(_action("bash", "   "))
    assert result.ok is False
    assert "empty" in result.error


@_LINUX_JAILED_SHELL_AVAILABLE
async def test_bash_truncates_long_output(runner, tmp_path):
    runner.max_output = 100
    script = tmp_path / "gen.py"
    script.write_text("print('x' * 500)", encoding="utf-8")
    result = await runner.run(
        _action("bash", f'"{sys.executable}" "{script}"')
    )
    assert result.ok is True
    assert "обрезано" in result.output


@_LINUX_JAILED_SHELL_AVAILABLE
async def test_bash_keeps_a_descriptor_pinned_cwd_if_root_is_replaced(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    identity = persistence.capture_project_identity(project)
    runner = ToolRunner(project, perms={"bash": "allow"}, project_identity=identity)
    retired = tmp_path / "retired-project"
    observed: dict[str, object] = {}

    def run_after_replacement(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        observed["cwd"] = cwd
        observed["pass_fds"] = kwargs["pass_fds"]
        project.rename(retired)
        project.mkdir()
        # The inherited descriptor still names the retired, approved root,
        # not the same-path replacement selected by the attacker.
        assert cwd.resolve() == retired.resolve()
        return subprocess.CompletedProcess(args[0], 0, stdout="safe\n", stderr="")

    monkeypatch.setattr(tools_module.subprocess, "run", run_after_replacement)
    result = await runner.run(_action("bash", "echo safe"))

    assert result.ok is False
    assert "project root identity changed" in result.error
    assert str(observed["cwd"]).startswith("/proc/self/fd/")
    assert observed["pass_fds"]


@pytest.mark.skipif(os.name != "nt", reason="Windows jailed shell policy")
async def test_windows_jailed_bash_fails_closed(runner):
    result = await runner.run(_action("bash", "echo must-not-run"))

    assert result.ok is False
    assert "safe jailed shell cwd is unavailable on Windows" in result.error


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
    if os.name == "nt":
        assert "project-relative" in result.error
    else:
        assert "outside project root" in result.error


async def test_read_rejects_a_hard_link_to_an_external_inode(runner, tmp_path):
    outside = tmp_path.parent / "hard-link-read-sentinel.txt"
    outside.write_text("TOP_SECRET", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip("hard links are unavailable on this host")

    result = await runner.run(_action("read", path="linked.txt"))

    assert result.ok is False
    assert "hard-linked" in result.error
    assert "TOP_SECRET" not in result.output


async def test_read_never_follows_an_external_file_symlink(runner, tmp_path):
    outside = tmp_path.parent / "read-symlink-sentinel.txt"
    outside.write_text("TOP_SECRET", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    result = await runner.run(_action("read", path="linked.txt"))

    assert result.ok is False
    assert "TOP_SECRET" not in result.output


async def test_read_is_bounded_before_decoding(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_READ_BYTES", 16)
    (tmp_path / "large.txt").write_text("x" * 100, encoding="utf-8")
    result = await runner.run(_action("read", path="large.txt"))
    assert result.ok is True
    assert "file truncated at inspection limit" in result.output


@pytest.mark.skipif(os.name != "nt", reason="Windows native root-relative read")
async def test_windows_read_uses_a_native_root_relative_handle(
    runner, tmp_path, monkeypatch
):
    (tmp_path / "inside.txt").write_text("safe", encoding="utf-8")

    def fail_path_canonicalization(handle):
        raise AssertionError("path canonicalization must not implement the jail")

    monkeypatch.setattr(runner, "_opened_file_path", fail_path_canonicalization)
    monkeypatch.setattr(
        runner,
        "_resolve_existing_file",
        lambda path: (_ for _ in ()).throw(
            AssertionError("Windows jailed read must not resolve a pathname")
        ),
    )
    result = await runner.run(_action("read", path="inside.txt"))
    assert result.ok is True
    assert "safe" in result.output


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


@pytest.mark.skipif(os.name != "nt", reason="Windows UNICODE_STRING boundary")
async def test_write_rejects_a_windows_component_that_would_overflow_unicode_string(
    runner, tmp_path
):
    outside = tmp_path.parent / f"{tmp_path.name}-unicode-overflow.txt"
    assert not outside.exists()
    # If a native UNICODE_STRING length wraps from 65540 to 4 bytes, this
    # component becomes `..` and the final name would be committed outside
    # the jailed root.  The validation must reject before opening anything.
    overflow_component = ".." + ("a" * 32768)
    result = await runner.run(
        _action("write", "must-not-escape", path=f"{overflow_component}\\{outside.name}")
    )

    assert result.ok is False
    assert not outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
def test_windows_mutation_rejects_superscript_device_aliases(runner):
    for name in (
        "COM¹.txt",
        "COM²",
        "COM³.log",
        "LPT¹",
        "LPT².md",
        "LPT³",
        "CONIN$",
        "CONOUT$.txt",
    ):
        with pytest.raises(tools_module.OutOfProject):
            runner._mutation_parts(name)
        with pytest.raises(tools_module.OutOfProject):
            runner._validate_windows_inspection_path(name)
    with pytest.raises(tools_module.OutOfProject):
        runner._validate_windows_inspection_path(r"\\attacker.invalid\share\file.txt")


async def test_write_never_follows_a_final_external_symlink(runner, tmp_path):
    outside = tmp_path.parent / "write-symlink-sentinel.txt"
    outside.write_text("DO_NOT_TOUCH", encoding="utf-8")
    target = tmp_path / "target.txt"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    await runner.run(_action("write", "replacement", path="target.txt"))

    assert outside.read_text(encoding="utf-8") == "DO_NOT_TOUCH"


async def test_write_rejects_an_external_parent_symlink(runner, tmp_path):
    outside = tmp_path.parent / "write-parent-external"
    outside.mkdir()
    link = tmp_path / "linked-parent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    result = await runner.run(
        _action("write", "must-stay-jailed", path="linked-parent/new.txt")
    )

    assert result.ok is False
    assert not (outside / "new.txt").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux openat2 mount boundary")
def test_jailed_tools_reject_a_bind_mount_crossing(tmp_path):
    """Exercise RESOLVE_NO_XDEV inside a disposable user/mount namespace."""
    if not shutil.which("unshare") or not shutil.which("mount"):
        pytest.skip("unshare or mount is unavailable")
    source_root = Path(__file__).resolve().parents[3]
    source_cli = source_root / "apps" / "agent-cli" / "src"
    child = textwrap.dedent(
        """
        import asyncio
        import tempfile
        from pathlib import Path
        from protoprompt_cli.actions import Action
        from protoprompt_cli.tools import ToolRunner

        async def main():
            with tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "project"
                outside = base / "outside"
                mountpoint = root / "mounted"
                root.mkdir()
                outside.mkdir()
                mountpoint.mkdir()
                (outside / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
                import subprocess
                subprocess.run(["mount", "--bind", str(outside), str(mountpoint)], check=True)
                try:
                    runner = ToolRunner(root, perms={"write": "allow"})
                    read = await runner.run(Action(name="read", body="", kwargs={"path": "mounted/secret.txt"}))
                    write = await runner.run(Action(name="write", body="escape", kwargs={"path": "mounted/new.txt"}))
                    assert not read.ok, read
                    assert "TOP_SECRET" not in read.output
                    assert not write.ok, write
                    assert not (outside / "new.txt").exists()
                finally:
                    subprocess.run(["umount", str(mountpoint)], check=True)

        asyncio.run(main())
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(source_root), str(source_cli)))
    result = subprocess.run(
        ["unshare", "-Urnm", sys.executable, "-c", child],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}"
    if "Operation not permitted" in output or "unshare failed" in output:
        pytest.skip("unprivileged mount namespaces are unavailable")
    raise AssertionError(output)


async def test_write_parent_swap_cannot_redirect_the_commit(runner, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    retired = tmp_path / "retired-work"
    outside = tmp_path.parent / "write-race-external"
    outside.mkdir()
    original_token_hex = tools_module.secrets.token_hex
    swapped = False

    def swap_parent(size):
        nonlocal swapped
        if not swapped:
            swapped = True
            try:
                work.rename(retired)
                work.symlink_to(outside, target_is_directory=True)
            except OSError:
                # Windows directory handles may intentionally prevent the
                # rename. That is itself a safe outcome for this race.
                pass
        return original_token_hex(size)

    monkeypatch.setattr(tools_module.secrets, "token_hex", swap_parent)
    await runner.run(_action("write", "safe payload", path="work/note.txt"))

    assert not (outside / "note.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux renameat2 verification")
async def test_write_does_not_claim_success_after_staged_inode_substitution(
    runner, tmp_path, monkeypatch
):
    original_write = runner._write_all
    substituted = False

    def substitute_staged_inode(descriptor, payload):
        nonlocal substituted
        original_write(descriptor, payload)
        if substituted:
            return
        substituted = True
        staged = next(tmp_path.glob(".protoprompt-write-*.tmp"))
        attacker = tmp_path / "attacker-inode.txt"
        attacker.write_text("attacker content", encoding="utf-8")
        attacker.replace(staged)

    monkeypatch.setattr(runner, "_write_all", substitute_staged_inode)
    target = tmp_path / "new.txt"
    result = await runner.run(_action("write", "agent content", path="new.txt"))

    assert result.ok is False
    assert "commit is uncertain" in result.error
    assert target.read_text(encoding="utf-8") == "attacker content"


@pytest.mark.skipif(os.name == "nt", reason="Linux renameat2 verification")
async def test_overwrite_restores_original_after_staged_inode_substitution(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    original_write = runner._write_all
    substituted = False

    def substitute_staged_inode(descriptor, payload):
        nonlocal substituted
        original_write(descriptor, payload)
        if substituted:
            return
        substituted = True
        staged = next(tmp_path.glob(".protoprompt-write-*.tmp"))
        attacker = tmp_path / "attacker-inode.txt"
        attacker.write_text("attacker content", encoding="utf-8")
        attacker.replace(staged)

    monkeypatch.setattr(runner, "_write_all", substitute_staged_inode)
    result = await runner.run(
        _action("write", "agent replacement", path="existing.txt")
    )

    assert result.ok is False
    assert "changed during atomic replacement" in result.error
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name != "nt", reason="Windows native create-only commit")
async def test_windows_write_does_not_replace_a_target_created_during_staging(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "appeared-during-write.txt"
    original_token_hex = tools_module.secrets.token_hex
    created = False

    def create_target_during_staging(size):
        nonlocal created
        if not created:
            created = True
            target.write_text("concurrent content", encoding="utf-8")
        return original_token_hex(size)

    monkeypatch.setattr(
        tools_module.secrets, "token_hex", create_target_during_staging
    )
    result = await runner.run(
        _action("write", "agent content", path="appeared-during-write.txt")
    )

    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "concurrent content"


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_write_replaces_a_hard_link_instead_of_mutating_its_target(runner, tmp_path):
    outside = tmp_path.parent / "hard-link-sentinel.txt"
    outside.write_text("DO_NOT_TOUCH", encoding="utf-8")
    target = tmp_path / "linked.txt"
    try:
        os.link(outside, target)
    except OSError:
        pytest.skip("hard links are unavailable on this host")

    result = await runner.run(_action("write", "inside replacement", path="linked.txt"))

    assert result.ok is True
    assert outside.read_text(encoding="utf-8") == "DO_NOT_TOUCH"
    assert target.read_text(encoding="utf-8") == "inside replacement"


@pytest.mark.skipif(os.name != "nt", reason="Windows jailed overwrite policy")
async def test_windows_write_to_an_existing_file_fails_closed(runner, tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")

    result = await runner.run(_action("write", "replacement", path="existing.txt"))

    assert result.ok is False
    assert "overwrite" in result.error
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode preservation")
async def test_write_preserves_private_posix_mode_on_an_existing_file(runner, tmp_path):
    target = tmp_path / "private.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o600)

    result = await runner.run(_action("write", "new", path="private.txt"))

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_edit_replaces_once(runner, tmp_path):
    (tmp_path / "a.py").write_text("alpha beta alpha", encoding="utf-8")
    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )
    assert result.ok is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "OMEGA beta alpha"


@pytest.mark.skipif(os.name != "nt", reason="Windows jailed overwrite policy")
async def test_windows_edit_of_an_existing_file_fails_closed(runner, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("alpha beta alpha", encoding="utf-8")

    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )

    assert result.ok is False
    assert "overwrite" in result.error
    assert target.read_text(encoding="utf-8") == "alpha beta alpha"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode preservation")
async def test_edit_preserves_private_posix_mode(runner, tmp_path):
    target = tmp_path / "private-edit.txt"
    target.write_text("alpha", encoding="utf-8")
    target.chmod(0o600)

    result = await runner.run(
        _action("edit", body="", path="private-edit.txt", old="alpha", new="OMEGA")
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "OMEGA"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_edit_rejects_a_target_replaced_after_its_snapshot(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "a.py"
    target.write_text("alpha", encoding="utf-8")
    replacement = tmp_path / "concurrent.py"
    original_snapshot = runner._read_existing_file_snapshot

    def replace_after_snapshot(*args, **kwargs):
        snapshot = original_snapshot(*args, **kwargs)
        replacement.write_text("concurrent change", encoding="utf-8")
        replacement.replace(target)
        return snapshot

    monkeypatch.setattr(runner, "_read_existing_file_snapshot", replace_after_snapshot)
    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )

    assert result.ok is False
    assert "changed during operation" in result.error
    assert target.read_text(encoding="utf-8") == "concurrent change"


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_edit_rejects_a_same_inode_change_after_its_snapshot(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "a.py"
    target.write_text("alpha", encoding="utf-8")
    original_snapshot = runner._read_existing_file_snapshot

    def rewrite_after_snapshot(*args, **kwargs):
        snapshot = original_snapshot(*args, **kwargs)
        # Same pathname and inode, same byte length, different content.
        target.write_text("omega", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(runner, "_read_existing_file_snapshot", rewrite_after_snapshot)
    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )

    assert result.ok is False
    assert "changed during operation" in result.error
    assert target.read_text(encoding="utf-8") == "omega"


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_edit_rolls_back_when_its_post_exchange_version_check_fails(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "a.py"
    target.write_text("alpha", encoding="utf-8")
    original_matches = runner._matches_file_version
    calls = 0

    def fail_after_exchange(descriptor, expected):
        nonlocal calls
        calls += 1
        return calls == 1 and original_matches(descriptor, expected)

    monkeypatch.setattr(runner, "_matches_file_version", fail_after_exchange)
    result = await runner.run(
        _action("edit", body="", path="a.py", old="alpha", new="OMEGA")
    )

    assert result.ok is False
    assert "changed during operation" in result.error
    assert target.read_text(encoding="utf-8") == "alpha"


@pytest.mark.skipif(os.name == "nt", reason="Windows jailed overwrites fail closed")
async def test_write_rolls_back_after_a_post_exchange_metadata_failure(
    runner, tmp_path, monkeypatch
):
    target = tmp_path / "a.py"
    target.write_text("old", encoding="utf-8")

    def fail_metadata(source_fd, destination_fd):
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(runner, "_copy_posix_security_metadata", fail_metadata)
    result = await runner.run(_action("write", "new", path="a.py"))

    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "old"


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


async def test_edit_never_follows_a_final_external_symlink(runner, tmp_path):
    outside = tmp_path.parent / "edit-symlink-sentinel.txt"
    outside.write_text("alpha SECRET", encoding="utf-8")
    target = tmp_path / "target.txt"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")

    result = await runner.run(
        _action("edit", body="", path="target.txt", old="alpha", new="OMEGA")
    )

    assert result.ok is False
    assert outside.read_text(encoding="utf-8") == "alpha SECRET"


# ── glob / grep ──────────────────────────────────────────────────


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_returns_matches(runner, tmp_path):
    (tmp_path / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "two.txt").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "one.py" in result.output
    assert "two.txt" not in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_recursive(runner, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep.py").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "**/*.py"))
    assert result.ok is True
    assert "src/deep.py" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_recursive_wildcard_matches_project_root(runner):
    result = await runner.run(_action("glob", "**"))
    assert result.ok is True
    assert "." in result.output.splitlines()


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_recursive_wildcard_matches_zero_or_more_directories(
    runner, tmp_path
):
    src = tmp_path / "src"
    nested = src / "nested"
    nested.mkdir(parents=True)
    (src / "leaf.py").write_text("", encoding="utf-8")
    (nested / "leaf.py").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "src/**/leaf.py"))
    assert result.ok is True
    assert "src/leaf.py" in result.output
    assert "src/nested/leaf.py" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_uses_lazy_scandir_not_pathlib_glob(runner, tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("", encoding="utf-8")

    def fail_glob(*args, **kwargs):
        raise AssertionError("Path.glob must not be used")

    monkeypatch.setattr(Path, "glob", fail_glob)
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "one.py" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_stops_at_a_bounded_match_count(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_GLOB_MATCHES", 2)
    for index in range(3):
        (tmp_path / f"{index}.py").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "glob inspection limit reached" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_stops_at_the_depth_cap(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_GLOB_DEPTH", 1)
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "deep.py").write_text("", encoding="utf-8")
    result = await runner.run(_action("glob", "**/*.py"))
    assert result.ok is True
    assert "deep.py" not in result.output
    assert "glob inspection limit reached" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_nonrecursive_glob_does_not_report_a_depth_limit(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setattr(tools_module, "MAX_GLOB_DEPTH", 1)
    (tmp_path / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested").mkdir()
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "one.py" in result.output
    assert "glob inspection limit reached" not in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_caps_entries_before_rejected_candidates(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setattr(tools_module, "MAX_GLOB_ENTRIES", 1)
    (tmp_path / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "two.py").write_text("", encoding="utf-8")

    def reject_candidate(path):
        raise tools_module.OutOfProject(f"outside project root: {path}")

    monkeypatch.setattr(runner, "_resolve_existing", reject_candidate)
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is True
    assert "glob inspection limit reached" in result.output


async def test_glob_rejects_parent_traversal(runner):
    result = await runner.run(_action("glob", "../*.py"))
    assert result.ok is False
    assert "outside project root" in result.error


async def test_glob_rejects_an_overly_complex_pattern(runner, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_GLOB_PATTERN_LENGTH", 3)
    result = await runner.run(_action("glob", "*.py"))
    assert result.ok is False
    assert "too complex" in result.error


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_glob_does_not_follow_external_directory_symlinks(runner, tmp_path):
    external = tmp_path.parent / "external-tree"
    external.mkdir()
    (external / "secret.py").write_text("secret", encoding="utf-8")
    link = tmp_path / "linked-tree"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    result = await runner.run(_action("glob", "**/*.py"))
    assert result.ok is True
    assert "secret.py" not in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_finds_lines_with_location(runner, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nfoo = 2\n", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="foo"))
    assert result.ok is True
    assert "a.py:2" in result.output
    assert "foo = 2" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_uses_lazy_scandir_not_pathlib_rglob(runner, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    def fail_rglob(*args, **kwargs):
        raise AssertionError("Path.rglob must not be used")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    result = await runner.run(_action("grep", pattern="needle"))
    assert result.ok is True
    assert "a.py:1" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_treats_patterns_as_literal_text(runner, tmp_path):
    (tmp_path / "a.py").write_text("call(thing)\n", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="("))
    assert result.ok is True
    assert "call(thing)" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_no_matches(runner, tmp_path):
    (tmp_path / "a.py").write_text("nothing", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="zzz"))
    assert result.ok is True
    assert "no matches" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_does_not_follow_external_file_symlinks(runner, tmp_path):
    external = tmp_path.parent / "outside-secret.txt"
    external.write_text("TOP_SECRET", encoding="utf-8")
    link = tmp_path / "linked-secret.txt"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    result = await runner.run(_action("grep", pattern="TOP_SECRET"))
    assert result.ok is True
    assert "TOP_SECRET" not in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_omits_a_hard_link_to_an_external_inode(runner, tmp_path):
    external = tmp_path.parent / "hard-link-grep-sentinel.txt"
    external.write_text("TOP_SECRET", encoding="utf-8")
    linked = tmp_path / "linked-secret.txt"
    try:
        os.link(external, linked)
    except OSError:
        pytest.skip("hard links are unavailable on this host")

    result = await runner.run(_action("grep", pattern="TOP_SECRET"))

    assert result.ok is True
    assert "linked-secret.txt:" not in result.output


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux tree boundary")
async def test_grep_with_a_subdirectory_path_uses_root_relative_validation(
    runner, tmp_path
):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("needle\n", encoding="utf-8")

    result = await runner.run(_action("grep", pattern="needle", path="src"))

    assert result.ok is True
    assert "src/a.py:1" in result.output


@pytest.mark.skipif(os.name != "nt", reason="Windows tree traversal policy")
async def test_windows_jailed_glob_and_grep_fail_closed(runner, tmp_path):
    (tmp_path / "inside.txt").write_text("needle", encoding="utf-8")

    glob = await runner.run(_action("glob", "*.txt"))
    grep = await runner.run(_action("grep", pattern="needle"))

    assert glob.ok is False
    assert grep.ok is False
    assert "tree traversal is unavailable" in glob.error
    assert "tree traversal is unavailable" in grep.error


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_stops_at_a_bounded_match_count(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_GREP_MATCHES", 2)
    (tmp_path / "a.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="hit"))
    assert result.ok is True
    assert "grep inspection limit reached" in result.output


@_WINDOWS_JAILED_TREE_UNAVAILABLE
async def test_grep_stops_at_the_depth_cap(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_GREP_DEPTH", 1)
    (tmp_path / "top.txt").write_text("needle\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "deep.txt").write_text("needle\n", encoding="utf-8")
    result = await runner.run(_action("grep", pattern="needle"))
    assert result.ok is True
    assert "top.txt:1" in result.output
    assert "deep.txt:1" not in result.output
    assert "grep inspection limit reached" in result.output


# ── jail отключён ────────────────────────────────────────────────


async def test_jail_disabled_allows_outside_read(tmp_path):
    runner = ToolRunner(tmp_path, jail=False)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = await runner.run(_action("read", path=str(outside)))
    assert result.ok is True
    assert "secret" in result.output
