"""ToolRunner: исполнение инструментов агента + модель прав.

Файловые инструменты ``read``, ``write``, ``edit``, ``glob`` и ``grep``
ограничены корнем проекта (jail, по умолчанию включён). ``bash`` запускается
из корня проекта, но не является sandbox. Права:
``ask`` (спросить через колбэк) / ``allow`` / ``deny``.
"""

from __future__ import annotations

import inspect
import asyncio
import errno
import fnmatch
import hashlib
import os
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from protoprompt_cli.persistence import (
    ProjectIdentity,
    ProjectIdentityChanged,
    capture_project_identity,
)

TOOLS = ("bash", "read", "write", "edit", "glob", "grep")

PERM_ASK = "ask"
PERM_ALLOW = "allow"
PERM_DENY = "deny"

DEFAULT_PERMS: dict[str, str] = {
    "read": PERM_ALLOW,
    "glob": PERM_ALLOW,
    "grep": PERM_ALLOW,
    "bash": PERM_ASK,
    "write": PERM_ASK,
    "edit": PERM_ASK,
}

MAX_OUTPUT = 8000
BASH_TIMEOUT = 120
MAX_READ_BYTES = 64 * 1024
MAX_GLOB_MATCHES = 1000
MAX_GLOB_ENTRIES = 2000
MAX_GLOB_DEPTH = 12
MAX_GLOB_PATTERN_LENGTH = 1024
MAX_GLOB_PATTERN_PARTS = 64
MAX_GLOB_WILDCARDS = 128
MAX_GREP_ENTRIES = 2000
MAX_GREP_FILES = 500
MAX_GREP_FILE_BYTES = 64 * 1024
MAX_GREP_MATCHES = 500
MAX_GREP_DEPTH = 12
_VALID_PERMISSION_MODES = frozenset({PERM_ASK, PERM_ALLOW, PERM_DENY})


class PermissionDenied(RuntimeError):
    """Право на инструмент не выдано."""


class OutOfProject(RuntimeError):
    """Путь за пределами корня проекта."""


class SafeJailUnavailable(RuntimeError):
    """The host cannot provide the no-follow filesystem boundary we require."""


@dataclass
class ToolResult:
    """Результат одного вызова инструмента."""

    ok: bool
    output: str
    tool: str = ""
    error: str = ""


@dataclass
class _TraversalBudget:
    """Mutable limits shared with one lazy directory walk."""

    entries: int = 0
    limited: bool = False


@dataclass(frozen=True)
class _FileVersion:
    """A bounded, content-addressed snapshot used by a jailed ``edit``."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: bytes

    @classmethod
    def from_stat_and_bytes(
        cls, file_stat: os.stat_result, content: bytes
    ) -> "_FileVersion":
        return cls(
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            ctime_ns=file_stat.st_ctime_ns,
            digest=hashlib.sha256(content).digest(),
        )


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(обрезано, всего {len(text)} символов)"


class ToolRunner:
    def __init__(
        self,
        root: str | Path,
        perms: dict[str, str] | None = None,
        *,
        jail: bool = True,
        ask_callback=None,
        timeout: float = BASH_TIMEOUT,
        max_output: int = MAX_OUTPUT,
        project_identity: ProjectIdentity | None = None,
    ) -> None:
        # Even direct programmatic callers receive a root-object binding.  A
        # pathname alone is not a useful jail capability: it can be replaced
        # after the runner is constructed.
        if project_identity is None and jail:
            project_identity = capture_project_identity(root)
        self.project_identity = project_identity
        self.root = (
            project_identity.assert_current(root)
            if project_identity is not None
            else Path(root).resolve()
        )
        merged = dict(DEFAULT_PERMS)
        if isinstance(perms, dict):
            # Persisted grants are untrusted input even in a user-owned state
            # directory: never create new tool names or malformed modes from
            # a JSON file.
            for name, mode in perms.items():
                if name in DEFAULT_PERMS and mode in _VALID_PERMISSION_MODES:
                    merged[name] = mode
        self.perms = merged
        self.jail = jail
        self.ask_callback = ask_callback
        self.timeout = timeout
        self.max_output = max_output

    def assert_project_identity(self) -> None:
        """Reject use of persisted authority after the project root changes."""
        if self.project_identity is not None:
            self.root = self.project_identity.assert_current(self.root)

    def _resolve(self, path: str | Path) -> Path:
        p = Path(str(path))
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if self.jail and not (p == self.root or self.root in p.parents):
            raise OutOfProject(f"path outside project root: {path}")
        return p

    def _resolve_existing(self, path: str | Path) -> Path:
        """Resolve one existing path and reapply the project containment rule."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            target = candidate.resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(f"no such file: {path}") from exc
        if self.jail and not (target == self.root or self.root in target.parents):
            raise OutOfProject(f"path outside project root: {path}")
        return target

    def _resolve_existing_file(self, path: str | Path) -> Path:
        target = self._resolve_existing(path)
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        return target

    @staticmethod
    def _validate_windows_path_component(part: str) -> None:
        """Reject DOS aliases and names unsafe for native relative lookup."""
        reserved = {
            "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
            "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
        }
        # ADS, device-style and normalization aliases complicate the native
        # object namespace.  Refuse them rather than trying to normalize a
        # security-sensitive name.  Device names remain reserved even with a
        # filename extension, hence the stem comparison.
        stem = part.split(".", 1)[0].upper()
        if ":" in part or part.rstrip(" .") != part or stem in reserved:
            raise OutOfProject("unsafe Windows path component")
        try:
            encoded = part.encode("utf-16-le")
        except UnicodeEncodeError as exc:
            raise OutOfProject("unsafe Windows path component") from exc
        # UNICODE_STRING is length-prefixed with USHORT fields.  An overflow
        # would be truncated by ctypes before NtCreateFile sees it.
        if len(encoded) + 2 > 0xFFFF:
            raise OutOfProject("Windows path component is too long")

    def _windows_inspection_parts(self, path: str | Path) -> tuple[str, ...]:
        """Return a strict relative Windows read capability without probing it.

        Unlike a mutation, historical read paths accepted absolute names and
        resolved them before checking containment.  A reparse/junction in that
        lookup can issue external I/O first.  Native RootDirectory reads need
        a purely lexical, project-relative component list instead.
        """
        raw = str(path)
        if "\x00" in raw:
            raise OutOfProject("inspection path contains NUL")
        # A UNC or device namespace can make Path.resolve()/is_file() perform
        # network or device I/O before the later containment check.  Jailed
        # inspection supports a local drive-root path or a relative path only.
        if raw.startswith(("\\\\", "//")):
            raise OutOfProject("jailed inspection does not support Windows UNC paths")
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.anchor:
            raise OutOfProject("jailed Windows reads require a project-relative path")
        parts = candidate.parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise OutOfProject("invalid Windows inspection path")
        for part in parts:
            self._validate_windows_path_component(part)
        return tuple(parts)

    def _validate_windows_inspection_path(self, path: str | Path) -> None:
        """Validate a Windows read path without triggering filesystem I/O."""
        self._windows_inspection_parts(path)

    def _mutation_parts(self, path: str | Path) -> tuple[str, ...]:
        """Validate a mutation path as a capability-relative component list.

        Mutating jailed tools must not resolve a path and later write through
        that pathname.  The returned components are subsequently resolved by
        an OS directory handle, so reject absolute, traversal and platform
        namespace forms before touching the filesystem.
        """
        raw = str(path)
        if "\x00" in raw:
            raise OutOfProject("mutation path contains NUL")
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.anchor:
            raise OutOfProject("jailed mutations require a project-relative path")
        parts = candidate.parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise OutOfProject("invalid project-relative mutation path")
        if os.name == "nt":
            for part in parts:
                self._validate_windows_path_component(part)
        return tuple(parts)

    def _assert_root_descriptor(self, descriptor: int) -> None:
        """Ensure an opened root descriptor is the startup project object."""
        root_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise SafeJailUnavailable("safe jail root is not a directory")
        identity = self.project_identity
        if identity is not None:
            # Device/inode alone can be recycled after a directory is deleted.
            # ProjectIdentity also verifies its stable creation generation.
            identity.assert_root_descriptor(descriptor)

    def _duplicate_jailed_posix_root(self) -> int:
        """Return one operation-owned descriptor for the approved root.

        The long-lived ProjectIdentity pin prevents a same-path replacement
        from recycling the original inode while this agent runs.  Every
        descriptor-relative Linux operation starts from a duplicate of that
        pin, never by reopening the mutable root pathname.
        """
        if self.project_identity is None:
            # ``jail=True`` constructs an identity in __init__; retain this
            # defensive fallback only for deliberately legacy callers.
            descriptor = os.open(self.root, self._posix_dir_flags())
        else:
            descriptor = self.project_identity.duplicate_root_fd()
        try:
            self._assert_root_descriptor(descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        """Write every byte without relying on a path-based file object."""
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write to jailed temporary file")
            offset += written
        os.fsync(descriptor)

    @staticmethod
    def _read_descriptor_bounded(descriptor: int, byte_limit: int) -> bytes:
        """Read at most ``byte_limit + 1`` bytes from a private descriptor.

        The caller owns the descriptor, so rewinding it cannot affect another
        operation.  One extra byte distinguishes an exact bounded file from a
        truncated inspection without a second pathname lookup.
        """
        if byte_limit < 0:
            raise ValueError("byte limit must be non-negative")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = byte_limit + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def _matches_file_version(
        cls, descriptor: int, expected: _FileVersion
    ) -> bool:
        """Check a descriptor against a prior complete bounded snapshot."""
        current = os.fstat(descriptor)
        if (
            current.st_dev != expected.device
            or current.st_ino != expected.inode
            or current.st_size != expected.size
            or current.st_mtime_ns != expected.mtime_ns
            or current.st_ctime_ns != expected.ctime_ns
        ):
            return False
        content = cls._read_descriptor_bounded(descriptor, expected.size)
        after = os.fstat(descriptor)
        return (
            after.st_dev == expected.device
            and after.st_ino == expected.inode
            and after.st_size == expected.size
            and after.st_mtime_ns == expected.mtime_ns
            and after.st_ctime_ns == expected.ctime_ns
            and len(content) == expected.size
            and hashlib.sha256(content).digest() == expected.digest
        )

    @staticmethod
    def _posix_dir_flags() -> int:
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if (
            sys.platform != "linux"
            or any(not hasattr(os, flag) for flag in required)
            or os.open not in os.supports_dir_fd
        ):
            raise SafeJailUnavailable(
                "safe jailed filesystem access requires Linux openat2"
            )
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @staticmethod
    def _openat2_beneath(parent_fd: int, name: str, flags: int, mode: int = 0) -> int:
        """Open one relative name without links, mounts or lexical escapes.

        ``openat`` plus ``O_NOFOLLOW`` blocks symlinks but still walks across
        bind mounts.  Linux ``openat2`` gives the kernel the complete
        resolution policy, including ``RESOLVE_NO_XDEV``.  There is no
        path-based fallback because this helper is the jail boundary.
        """
        if sys.platform != "linux":
            raise SafeJailUnavailable("safe jailed filesystem access requires Linux openat2")
        try:
            import ctypes

            class _OpenHow(ctypes.Structure):
                _fields_ = [
                    ("flags", ctypes.c_ulonglong),
                    ("mode", ctypes.c_ulonglong),
                    ("resolve", ctypes.c_ulonglong),
                ]

            encoded = os.fsencode(name)
            if b"\x00" in encoded:
                raise OutOfProject("mutation path contains NUL")
            # Linux assigns syscall 437 to openat2 on every architecture
            # where the interface is available.  Keep the policy constants
            # local so Python versions without os.SYS_openat2 remain usable.
            resolve_no_xdev = 0x01
            resolve_no_magiclinks = 0x02
            resolve_no_symlinks = 0x04
            resolve_beneath = 0x08
            how = _OpenHow(
                flags,
                mode,
                resolve_no_xdev
                | resolve_no_magiclinks
                | resolve_no_symlinks
                | resolve_beneath,
            )
            libc = ctypes.CDLL(None, use_errno=True)
            result = int(
                libc.syscall(
                    ctypes.c_long(437),
                    ctypes.c_int(parent_fd),
                    ctypes.c_char_p(encoded),
                    ctypes.byref(how),
                    ctypes.c_size_t(ctypes.sizeof(how)),
                )
            )
        except OutOfProject:
            raise
        except (AttributeError, OSError) as exc:
            raise SafeJailUnavailable(
                "safe jailed filesystem access requires Linux openat2"
            ) from exc
        if result >= 0:
            return result
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL):
            raise SafeJailUnavailable("safe jailed filesystem access requires Linux openat2")
        if error in (errno.EXDEV, errno.ELOOP):
            raise OutOfProject("unsafe Linux jailed path component")
        raise OSError(error, os.strerror(error), name)

    @staticmethod
    def _renameat2(parent_fd: int, source: str, destination: str, flags: int) -> None:
        """Rename two entries inside an already-open Linux parent directory."""
        if sys.platform != "linux":
            raise SafeJailUnavailable("safe jailed mutations require Linux renameat2")
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
        except (AttributeError, OSError) as exc:
            raise SafeJailUnavailable("safe jailed mutations require Linux renameat2") from exc
        result = renameat2(
            parent_fd,
            os.fsencode(source),
            parent_fd,
            os.fsencode(destination),
            flags,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL):
            raise SafeJailUnavailable("safe jailed mutations require Linux renameat2")
        raise OSError(error, os.strerror(error), destination)

    @classmethod
    def _copy_posix_security_metadata(cls, source_fd: int, destination_fd: int) -> None:
        """Copy access-relevant POSIX metadata through already-open handles.

        Replacing an inode must not silently turn a private file into a
        group/world-readable one.  ACLs live in extended attributes on Linux,
        so copying just ``st_mode`` is insufficient.  Unsupported metadata
        APIs or a failed copy are a fail-closed condition for an overwrite.
        """
        source = os.fstat(source_fd)
        destination = os.fstat(destination_fd)
        if not stat.S_ISREG(source.st_mode):
            raise SafeJailUnavailable("cannot preserve metadata of a non-regular file")
        try:
            if (destination.st_uid, destination.st_gid) != (
                source.st_uid,
                source.st_gid,
            ):
                os.fchown(destination_fd, source.st_uid, source.st_gid)
            os.fchmod(destination_fd, stat.S_IMODE(source.st_mode))
            source_attributes = set(os.listxattr(source_fd))
            destination_attributes = set(os.listxattr(destination_fd))
            for attribute in destination_attributes - source_attributes:
                os.removexattr(destination_fd, attribute)
            for attribute in source_attributes:
                os.setxattr(
                    destination_fd,
                    attribute,
                    os.getxattr(source_fd, attribute),
                )
        except (AttributeError, NotImplementedError, OSError) as exc:
            raise SafeJailUnavailable(
                "cannot preserve jailed file access metadata"
            ) from exc

    @staticmethod
    def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
        return first.st_dev == second.st_dev and first.st_ino == second.st_ino

    def _open_posix_file_under_root(
        self, parts: tuple[str, ...], flags: int
    ) -> int:
        """Open one non-empty relative path from the pinned Linux root."""
        if not parts:
            raise FileNotFoundError("no such regular file: project root")
        root_fd = self._duplicate_jailed_posix_root()
        try:
            return self._openat2_beneath(root_fd, os.path.join(*parts), flags)
        finally:
            os.close(root_fd)

    def _validate_posix_tree_entry(
        self, parts: tuple[str, ...], *, is_directory: bool
    ) -> None:
        """Reject links and bind-mount crossings before a tree walk descends."""
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if is_directory:
            flags |= os.O_DIRECTORY
        descriptor = self._open_posix_file_under_root(parts, flags)
        try:
            opened = os.fstat(descriptor)
            if is_directory and not stat.S_ISDIR(opened.st_mode):
                raise OutOfProject("unsafe Linux directory component")
            if not is_directory and not stat.S_ISREG(opened.st_mode):
                raise OutOfProject("unsafe Linux tree entry")
            if not is_directory and opened.st_nlink > 1:
                # A hard link can make an inode reachable both inside and
                # outside the project without crossing a pathname boundary.
                # Strict jailed inspection has no safe origin proof for it.
                raise OutOfProject("unsafe hard-linked Linux tree entry")
        finally:
            os.close(descriptor)

    def _open_posix_parent(
        self, parts: tuple[str, ...], *, create_parents: bool
    ) -> tuple[int, list[int]]:
        """Open a parent directory component-by-component without following links."""
        flags = self._posix_dir_flags()
        root_fd = self._duplicate_jailed_posix_root()
        descriptors = [root_fd]
        try:
            parent_fd = root_fd
            for part in parts[:-1]:
                try:
                    child_fd = self._openat2_beneath(parent_fd, part, flags)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    try:
                        os.mkdir(part, 0o755, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    child_fd = self._openat2_beneath(parent_fd, part, flags)
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    os.close(child_fd)
                    raise OutOfProject(f"unsafe directory component: {part}")
                descriptors.append(child_fd)
                parent_fd = child_fd
            return parent_fd, descriptors
        except Exception:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def _replace_text_posix(
        self,
        parts: tuple[str, ...],
        text: str,
        *,
        create_parents: bool,
        require_existing: bool,
        expected_version: _FileVersion | None = None,
    ) -> Path:
        """Atomically replace one jailed entry using descriptor-relative syscalls."""
        parent_fd, descriptors = self._open_posix_parent(
            parts, create_parents=create_parents
        )
        temporary_name: str | None = None
        temporary_fd: int | None = None
        temporary_stat: os.stat_result | None = None
        existing_fd: int | None = None
        source_stat: os.stat_result | None = None
        preserve_temporary = False
        try:
            leaf = parts[-1]
            existing_flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                existing_fd = self._openat2_beneath(
                    parent_fd, leaf, existing_flags
                )
            except FileNotFoundError:
                if require_existing:
                    raise FileNotFoundError(f"no such regular file: {leaf}") from None
            if existing_fd is not None:
                source_stat = os.fstat(existing_fd)
                if not stat.S_ISREG(source_stat.st_mode):
                    raise OutOfProject(f"unsafe file component: {leaf}")
                if expected_version is not None and not self._matches_file_version(
                    existing_fd, expected_version
                ):
                    raise SafeJailUnavailable("edit target changed during operation")
            for _ in range(16):
                candidate = f".protoprompt-write-{secrets.token_hex(16)}.tmp"
                try:
                    temporary_fd = os.open(
                        candidate,
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        # Keep a staged replacement private until the content
                        # and the original access metadata are both ready.
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                temporary_stat = os.fstat(temporary_fd)
                break
            else:  # pragma: no cover - cryptographic collision is unrealistic
                raise SafeJailUnavailable("could not allocate a jailed temporary file")
            self._write_all(temporary_fd, text.encode("utf-8"))

            if existing_fd is None:
                # A target that did not exist must still not be overwritten
                # if another actor creates it while the temporary file is
                # being prepared.
                self._renameat2(parent_fd, temporary_name, leaf, 0x01)
                try:
                    current_entry = os.stat(
                        leaf, dir_fd=parent_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    preserve_temporary = True
                    raise SafeJailUnavailable(
                        "jailed creation commit is uncertain; recovery entry retained"
                    ) from exc
                if temporary_stat is None or not self._same_file(
                    temporary_stat, current_entry
                ):
                    # The random source name could have been substituted
                    # before renameat2 consumed it.  Do not claim success for
                    # a target that is not the inode we staged.
                    preserve_temporary = True
                    raise SafeJailUnavailable(
                        "jailed creation commit is uncertain; recovery entry retained"
                    )
                temporary_name = None
            else:
                assert source_stat is not None
                # Exchange, rather than an unconditional replace, gives us a
                # post-commit identity check.  If the final entry changed
                # after we captured its permissions, swap back rather than
                # applying those permissions to an unrelated replacement.
                self._renameat2(parent_fd, temporary_name, leaf, 0x02)

                def restore_exchanged_entry() -> tuple[bool, bool]:
                    """Restore source when its temporary name is still pinned.

                    The final entry may be a substituted inode.  Swapping it
                    back into the random name is safe only when that name is
                    still the original source; retain an unknown displaced
                    inode rather than deleting it in the caller.
                    """
                    try:
                        old_entry = os.stat(
                            temporary_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        current_entry = os.stat(
                            leaf, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if not self._same_file(source_stat, old_entry):
                            return False, False
                        self._renameat2(parent_fd, temporary_name, leaf, 0x02)
                        restored_entry = os.stat(
                            leaf, dir_fd=parent_fd, follow_symlinks=False
                        )
                        staged_entry = os.stat(
                            temporary_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if not self._same_file(source_stat, restored_entry):
                            return False, False
                        return (
                            True,
                            temporary_stat is not None
                            and self._same_file(temporary_stat, staged_entry),
                        )
                    except OSError:
                        return False, False

                try:
                    previous_entry = os.stat(
                        temporary_name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    current_entry = os.stat(
                        leaf, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if temporary_stat is None or not (
                        self._same_file(source_stat, previous_entry)
                        and self._same_file(temporary_stat, current_entry)
                    ):
                        raise SafeJailUnavailable(
                            "jailed target changed during atomic replacement"
                        )
                    if expected_version is not None and not self._matches_file_version(
                        existing_fd, expected_version
                    ):
                        raise SafeJailUnavailable("edit target changed during operation")
                    # The source descriptor now refers to the old inode under
                    # the temporary name; the temporary descriptor still
                    # refers to staged content now reachable at ``leaf``.
                    # Copy security metadata only after identity validation so
                    # staged content is never made broad prematurely.
                    self._copy_posix_security_metadata(existing_fd, temporary_fd)
                    os.fsync(temporary_fd)
                except Exception as exc:
                    restored, staged_recovered = restore_exchanged_entry()
                    if not restored:
                        # Do not unlink either random entry after an uncertain
                        # post-exchange outcome: it is the only local recovery
                        # evidence and may still name the old inode.
                        preserve_temporary = True
                        raise SafeJailUnavailable(
                            "jailed replacement commit is uncertain; recovery entry retained"
                        ) from exc
                    if not staged_recovered:
                        # We restored the requested target, but the random
                        # name now holds an inode we did not stage.  It may be
                        # another actor's entry, so preserve it for recovery.
                        preserve_temporary = True
                    raise
                # The old inode is now reachable only through our random
                # temporary name.  Remove it only after checking its identity
                # so concurrent directory churn cannot make us unlink an
                # unrelated entry.
                try:
                    old_entry = os.stat(
                        temporary_name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if self._same_file(source_stat, old_entry):
                        os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
                temporary_name = None
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            return self.root.joinpath(*parts)
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if existing_fd is not None:
                try:
                    os.close(existing_fd)
                except OSError:
                    pass
            if temporary_name is not None and not preserve_temporary:
                try:
                    if temporary_stat is not None:
                        current = os.stat(
                            temporary_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if self._same_file(temporary_stat, current):
                            os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _replace_text_windows(
        self,
        parts: tuple[str, ...],
        text: str,
        *,
        create_parents: bool,
        require_existing: bool,
        expected_version: _FileVersion | None = None,
    ) -> Path:
        """Safely create a new Windows entry through relative native handles.

        `CreateFileW` validates only the final reparse point, which is not
        enough for a jailed creation.  `NtCreateFile` with RootDirectory and
        OBJ_DONT_REPARSE makes every component capability-relative and rejects
        reparse traversal.  Existing-entry replacement fails closed: Windows
        lacks the required handle-relative identity-CAS primitive here.  There
        is intentionally no pathname-based fallback.
        """
        import ctypes
        import msvcrt
        from ctypes import wintypes

        if str(self.root).startswith("\\\\"):
            raise SafeJailUnavailable("safe jailed mutations do not support UNC roots")

        class _UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class _ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(_UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = [
                ("Status", ctypes.c_long),
                ("Information", ctypes.c_size_t),
            ]

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("CreationTime", wintypes.FILETIME),
                ("LastAccessTime", wintypes.FILETIME),
                ("LastWriteTime", wintypes.FILETIME),
                ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD),
                ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD),
                ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD),
            ]

        class _FileRenameInfo(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", ctypes.c_byte),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
        get_info.restype = wintypes.BOOL
        nt_create = ntdll.NtCreateFile
        nt_create.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        )
        nt_create.restype = ctypes.c_long
        nt_set_info = ntdll.NtSetInformationFile
        nt_set_info.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_int,
        )
        nt_set_info.restype = ctypes.c_long

        file_read_attributes = 0x00000080
        file_list_directory = 0x00000001
        file_traverse = 0x00000020
        generic_read = 0x80000000
        generic_write = 0x40000000
        delete_access = 0x00010000
        read_control = 0x00020000
        synchronize = 0x00100000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        file_attribute_reparse_point = 0x00000400
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000
        obj_case_insensitive = 0x00000040
        obj_dont_reparse = 0x00001000
        file_open = 1
        file_create = 2
        file_directory_file = 0x00000001
        file_non_directory_file = 0x00000040
        file_synchronous_io_nonalert = 0x00000020
        file_open_reparse_point = 0x00200000
        file_rename_information = 10
        file_disposition_information = 13
        status_object_name_not_found = -1073741772  # 0xC0000034
        status_object_path_not_found = -1073741766  # 0xC000003A
        status_object_name_collision = -1073741771  # 0xC0000035
        invalid_handle = ctypes.c_void_p(-1).value

        def as_fd(handle: int, flags: int) -> int:
            return msvcrt.open_osfhandle(handle, flags | os.O_BINARY)

        def raw_handle(descriptor: int) -> int:
            return msvcrt.get_osfhandle(descriptor)

        def attributes(handle: int) -> int:
            info = _ByHandleFileInformation()
            if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), "cannot inspect jailed handle")
            return int(info.FileAttributes)

        def open_relative(
            parent: int,
            name: str,
            *,
            access: int,
            disposition: int,
            options: int,
            share: int = file_share_read | file_share_write,
        ) -> tuple[int, int]:
            text_buffer = ctypes.create_unicode_buffer(name)
            encoded = name.encode("utf-16-le")
            if len(encoded) + 2 > 0xFFFF:
                raise OutOfProject("Windows native path component is too long")
            unicode_name = _UnicodeString(
                len(encoded), len(encoded) + 2, ctypes.cast(text_buffer, wintypes.LPWSTR)
            )
            object_attributes = _ObjectAttributes(
                ctypes.sizeof(_ObjectAttributes),
                wintypes.HANDLE(parent),
                ctypes.pointer(unicode_name),
                obj_case_insensitive | obj_dont_reparse,
                None,
                None,
            )
            io_status = _IoStatusBlock()
            handle = wintypes.HANDLE()
            status = int(
                nt_create(
                    ctypes.byref(handle),
                    access,
                    ctypes.byref(object_attributes),
                    ctypes.byref(io_status),
                    None,
                    0x00000080,
                    share,
                    disposition,
                    options,
                    None,
                    0,
                )
            )
            return status, int(handle.value or 0)

        def ensure_directory(parent_fd: int, name: str) -> int:
            parent = raw_handle(parent_fd)
            status, handle = open_relative(
                parent,
                name,
                access=file_list_directory | file_read_attributes | synchronize,
                disposition=file_open,
                options=file_directory_file
                | file_synchronous_io_nonalert
                | file_open_reparse_point,
            )
            if status in (status_object_name_not_found, status_object_path_not_found):
                if not create_parents:
                    raise FileNotFoundError(f"no such directory: {name}")
                status, handle = open_relative(
                    parent,
                    name,
                    access=file_list_directory | file_read_attributes | synchronize,
                    disposition=file_create,
                    options=file_directory_file
                    | file_synchronous_io_nonalert
                    | file_open_reparse_point,
                )
                if status == status_object_name_collision:
                    status, handle = open_relative(
                        parent,
                        name,
                        access=file_list_directory | file_read_attributes | synchronize,
                        disposition=file_open,
                        options=file_directory_file
                        | file_synchronous_io_nonalert
                        | file_open_reparse_point,
                    )
            if status < 0 or not handle:
                raise SafeJailUnavailable(
                    f"cannot open safe Windows directory component {name!r}: 0x{status & 0xFFFFFFFF:08X}"
                )
            descriptor = as_fd(handle, os.O_RDONLY)
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode) or (
                    attributes(raw_handle(descriptor)) & file_attribute_reparse_point
                ):
                    raise OutOfProject(f"unsafe directory component: {name}")
                return descriptor
            except Exception:
                os.close(descriptor)
                raise

        root_handle = create_file(
            str(self.root),
            file_read_attributes | file_traverse | synchronize,
            file_share_read | file_share_write,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if int(root_handle or 0) == invalid_handle:
            raise SafeJailUnavailable("cannot open safe Windows jail root")
        root_fd = as_fd(int(root_handle), os.O_RDONLY)
        directory_fds = [root_fd]
        temporary_fd: int | None = None
        existing_fd: int | None = None
        committed = False
        try:
            self._assert_root_descriptor(root_fd)
            if attributes(raw_handle(root_fd)) & file_attribute_reparse_point:
                raise SafeJailUnavailable("safe jail root is a reparse point")
            parent_fd = root_fd
            for component in parts[:-1]:
                parent_fd = ensure_directory(parent_fd, component)
                directory_fds.append(parent_fd)
            leaf = parts[-1]
            status, existing_handle = open_relative(
                raw_handle(parent_fd),
                leaf,
                access=generic_read | delete_access | read_control | synchronize,
                disposition=file_open,
                options=file_non_directory_file
                | file_synchronous_io_nonalert
                | file_open_reparse_point,
                share=file_share_read | file_share_write | file_share_delete,
            )
            if status in (status_object_name_not_found, status_object_path_not_found):
                if require_existing:
                    raise FileNotFoundError(f"no such file: {leaf}")
            elif status < 0 or not existing_handle:
                raise SafeJailUnavailable(
                    f"cannot open safe Windows file component {leaf!r}: "
                    f"0x{status & 0xFFFFFFFF:08X}"
                )
            else:
                existing_fd = as_fd(existing_handle, os.O_RDONLY)
                existing_attributes = attributes(raw_handle(existing_fd))
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode) or (
                    existing_attributes & file_attribute_reparse_point
                ):
                    raise OutOfProject(f"unsafe file component: {leaf}")
                # Windows does not expose a portable, handle-relative
                # compare-and-swap rename equivalent to Linux RENAME_EXCHANGE.
                # Releasing this descriptor before a replacing rename would
                # let a concurrent actor swap the final entry; copying only a
                # DACL also misses SACL/integrity metadata.  Creation is safe
                # with ReplaceIfExists=0 below, but overwriting an existing
                # entry must fail closed until such a primitive is available.
                raise SafeJailUnavailable(
                    "safe jailed overwrite of an existing file is unavailable on Windows"
                )
            for _ in range(16):
                temporary_name = f".protoprompt-write-{secrets.token_hex(16)}.tmp"
                status, temporary_handle = open_relative(
                    raw_handle(parent_fd),
                    temporary_name,
                    access=(
                        generic_read
                        | generic_write
                        | delete_access
                        | read_control
                        | synchronize
                    ),
                    disposition=file_create,
                    options=file_non_directory_file
                    | file_synchronous_io_nonalert
                    | file_open_reparse_point,
                    share=file_share_read | file_share_write | file_share_delete,
                )
                if status == status_object_name_collision:
                    continue
                if status < 0 or not temporary_handle:
                    raise SafeJailUnavailable(
                        f"cannot create safe Windows temporary file: 0x{status & 0xFFFFFFFF:08X}"
                    )
                temporary_fd = as_fd(temporary_handle, os.O_RDWR)
                break
            else:  # pragma: no cover - cryptographic collision is unrealistic
                raise SafeJailUnavailable("could not allocate a jailed temporary file")
            self._write_all(temporary_fd, text.encode("utf-8"))
            name_bytes = leaf.encode("utf-16-le")
            size = _FileRenameInfo.FileName.offset + len(name_bytes)
            rename_buffer = (ctypes.c_byte * size)()
            rename = ctypes.cast(
                rename_buffer, ctypes.POINTER(_FileRenameInfo)
            ).contents
            # A target can appear after the first safe lookup.  Creation must
            # not turn that race into an overwrite of a new entry.
            rename.ReplaceIfExists = 0
            rename.RootDirectory = wintypes.HANDLE(raw_handle(parent_fd))
            rename.FileNameLength = len(name_bytes)
            ctypes.memmove(
                ctypes.addressof(rename_buffer) + _FileRenameInfo.FileName.offset,
                name_bytes,
                len(name_bytes),
            )
            rename_status = int(
                nt_set_info(
                    wintypes.HANDLE(raw_handle(temporary_fd)),
                    ctypes.byref(_IoStatusBlock()),
                    ctypes.cast(rename_buffer, wintypes.LPVOID),
                    size,
                    file_rename_information,
                )
            )
            if rename_status < 0:
                raise SafeJailUnavailable(
                    "cannot commit safe Windows mutation: "
                    f"0x{rename_status & 0xFFFFFFFF:08X}"
                )
            committed = True
            return self.root.joinpath(*parts)
        finally:
            if temporary_fd is not None:
                if not committed:
                    delete_file = ctypes.c_byte(1)
                    nt_set_info(
                        wintypes.HANDLE(raw_handle(temporary_fd)),
                        ctypes.byref(_IoStatusBlock()),
                        ctypes.byref(delete_file),
                        ctypes.sizeof(delete_file),
                        file_disposition_information,
                    )
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if existing_fd is not None:
                try:
                    os.close(existing_fd)
                except OSError:
                    pass
            for descriptor in reversed(directory_fds):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _replace_text_jailed(
        self,
        path: str | Path,
        text: str,
        *,
        create_parents: bool,
        require_existing: bool,
        expected_version: _FileVersion | None = None,
    ) -> Path:
        """Replace one file through a no-follow, handle-relative jail."""
        self.assert_project_identity()
        parts = self._mutation_parts(path)
        if os.name == "nt":
            target = self._replace_text_windows(
                parts,
                text,
                create_parents=create_parents,
                require_existing=require_existing,
                expected_version=expected_version,
            )
        else:
            target = self._replace_text_posix(
                parts,
                text,
                create_parents=create_parents,
                require_existing=require_existing,
                expected_version=expected_version,
            )
        self.assert_project_identity()
        return target

    @staticmethod
    def _opened_file_path(handle) -> Path:
        """Return the canonical path held by an already-open file handle.

        Checking this handle, rather than the path again, closes the window
        where a project file can be replaced with an external symlink between
        containment validation and opening it.
        """
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            get_final_name = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            get_final_name.argtypes = (
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            get_final_name.restype = wintypes.DWORD
            raw_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
            size = get_final_name(raw_handle, None, 0, 0)
            if not size:
                raise OSError(ctypes.get_last_error(), "cannot resolve opened file")
            buffer = ctypes.create_unicode_buffer(size + 1)
            written = get_final_name(raw_handle, buffer, len(buffer), 0)
            if not written or written >= len(buffer):
                raise OSError(ctypes.get_last_error(), "cannot resolve opened file")
            raw_path = buffer.value
            if raw_path.startswith("\\\\?\\UNC\\"):
                raw_path = "\\\\" + raw_path[8:]
            elif raw_path.startswith("\\\\?\\"):
                raw_path = raw_path[4:]
            return Path(os.path.normpath(raw_path))

        for descriptor_root in ("/proc/self/fd", "/dev/fd"):
            try:
                raw_path = os.readlink(
                    os.path.join(descriptor_root, str(handle.fileno()))
                )
            except OSError:
                continue
            return Path(os.path.normpath(raw_path))
        # A platform without a descriptor-to-path API must fail closed rather
        # than fall back to a second path lookup with a symlink race.
        raise OSError("cannot resolve opened file handle")

    def _open_windows_file_under_root(self, parts: tuple[str, ...]) -> int:
        """Open a regular file from the pinned Windows root handle.

        Lexically checking ``GetFinalPathNameByHandleW`` cannot distinguish a
        same-path root that was swapped out and restored during a read.  This
        helper instead gives ``NtCreateFile`` the startup root handle as its
        ``RootDirectory`` and forbids every reparse traversal in the kernel.
        """
        import ctypes
        import msvcrt
        from ctypes import wintypes

        if not parts:
            raise FileNotFoundError("no such regular file: project root")
        if str(self.root).startswith("\\\\"):
            raise SafeJailUnavailable("safe jailed reads do not support UNC roots")
        for part in parts:
            self._validate_windows_path_component(part)

        class _UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class _ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(_UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", wintypes.LPVOID),
                ("SecurityQualityOfService", wintypes.LPVOID),
            ]

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = [
                ("Status", ctypes.c_long),
                ("Information", ctypes.c_size_t),
            ]

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("FileAttributes", wintypes.DWORD),
                ("CreationTime", wintypes.FILETIME),
                ("LastAccessTime", wintypes.FILETIME),
                ("LastWriteTime", wintypes.FILETIME),
                ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD),
                ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD),
                ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
        get_info.restype = wintypes.BOOL
        nt_create = ntdll.NtCreateFile
        nt_create.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        )
        nt_create.restype = ctypes.c_long

        file_read_data = 0x00000001
        file_read_attributes = 0x00000080
        file_traverse = 0x00000020
        synchronize = 0x00100000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_existing = 3
        file_attribute_reparse_point = 0x00000400
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000
        obj_case_insensitive = 0x00000040
        obj_dont_reparse = 0x00001000
        file_open = 1
        file_non_directory_file = 0x00000040
        file_synchronous_io_nonalert = 0x00000020
        file_open_reparse_point = 0x00200000
        status_object_name_not_found = -1073741772  # 0xC0000034
        status_object_path_not_found = -1073741766  # 0xC000003A
        invalid_handle = ctypes.c_void_p(-1).value

        def as_fd(handle: int, flags: int) -> int:
            return msvcrt.open_osfhandle(handle, flags | os.O_BINARY)

        def raw_handle(descriptor: int) -> int:
            return msvcrt.get_osfhandle(descriptor)

        def attributes(handle: int) -> int:
            info = _ByHandleFileInformation()
            if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), "cannot inspect jailed handle")
            return int(info.FileAttributes)

        root_handle = create_file(
            str(self.root),
            file_read_attributes | file_traverse | synchronize,
            file_share_read | file_share_write,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if int(root_handle or 0) == invalid_handle:
            raise SafeJailUnavailable("cannot open safe Windows jail root")
        root_fd = as_fd(int(root_handle), os.O_RDONLY)
        try:
            self._assert_root_descriptor(root_fd)
            if attributes(raw_handle(root_fd)) & file_attribute_reparse_point:
                raise SafeJailUnavailable("safe jail root is a reparse point")

            name = "\\".join(parts)
            encoded = name.encode("utf-16-le")
            if len(encoded) + 2 > 0xFFFF:
                raise OutOfProject("Windows native path is too long")
            text_buffer = ctypes.create_unicode_buffer(name)
            unicode_name = _UnicodeString(
                len(encoded), len(encoded) + 2, ctypes.cast(text_buffer, wintypes.LPWSTR)
            )
            object_attributes = _ObjectAttributes(
                ctypes.sizeof(_ObjectAttributes),
                wintypes.HANDLE(raw_handle(root_fd)),
                ctypes.pointer(unicode_name),
                obj_case_insensitive | obj_dont_reparse,
                None,
                None,
            )
            io_status = _IoStatusBlock()
            file_handle = wintypes.HANDLE()
            status = int(
                nt_create(
                    ctypes.byref(file_handle),
                    file_read_data | file_read_attributes | synchronize,
                    ctypes.byref(object_attributes),
                    ctypes.byref(io_status),
                    None,
                    0x00000080,
                    file_share_read | file_share_write,
                    file_open,
                    file_non_directory_file
                    | file_synchronous_io_nonalert
                    | file_open_reparse_point,
                    None,
                    0,
                )
            )
            if status in (status_object_name_not_found, status_object_path_not_found):
                raise FileNotFoundError(f"no such file: {name}")
            if status < 0 or not file_handle.value:
                raise SafeJailUnavailable(
                    "cannot open safe Windows jailed file: "
                    f"0x{status & 0xFFFFFFFF:08X}"
                )
            descriptor = as_fd(int(file_handle.value), os.O_RDONLY)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode) or (
                    attributes(raw_handle(descriptor)) & file_attribute_reparse_point
                ):
                    raise OutOfProject("unsafe Windows jailed file component")
                return descriptor
            except Exception:
                os.close(descriptor)
                raise
        finally:
            os.close(root_fd)

    def _read_existing_file_snapshot(
        self, path: str | Path, byte_limit: int
    ) -> tuple[Path, str, bool, _FileVersion | None]:
        """Read one regular file through the jail and capture a bounded version.

        The version is available only when the complete file fits the caller's
        inspection budget.  It lets ``edit`` reject a replacement that no
        longer applies to the exact content the model inspected.
        """
        if self.jail and os.name == "nt":
            relative_parts = self._windows_inspection_parts(path)
            # This display path is lexical only.  The native RootDirectory
            # handle below is the authority for both existence and content.
            target = self.root.joinpath(*relative_parts)
            descriptor = self._open_windows_file_under_root(relative_parts)
        else:
            target = self._resolve_existing_file(path)
            flags = os.O_RDONLY
            if os.name == "nt":
                flags |= getattr(os, "O_BINARY", 0)
            else:
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_NONBLOCK", 0)
            if self.jail:
                try:
                    relative_parts = target.relative_to(self.root).parts
                except ValueError as exc:  # defensive: _resolve_existing_file checked this
                    raise OutOfProject(f"path outside project root: {path}") from exc
                descriptor = self._open_posix_file_under_root(relative_parts, flags)
            else:
                descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise FileNotFoundError(f"no such regular file: {path}")
            if self.jail and opened_stat.st_nlink > 1:
                # A project path can be a hard link to an externally-named
                # inode.  There is no portable safe proof that its content is
                # project-owned, so strict inspection refuses it.
                raise OutOfProject(f"unsafe hard-linked file: {path}")
            raw = self._read_descriptor_bounded(handle.fileno(), byte_limit)
            final_stat = os.fstat(handle.fileno())
        if (
            not self._same_file(opened_stat, final_stat)
            or opened_stat.st_size != final_stat.st_size
            or opened_stat.st_mtime_ns != final_stat.st_mtime_ns
            or opened_stat.st_ctime_ns != final_stat.st_ctime_ns
        ):
            raise SafeJailUnavailable("file changed during safe inspection")
        truncated = len(raw) > byte_limit
        content = raw[:byte_limit].decode("utf-8", errors="replace")
        if truncated:
            version = None
        else:
            if len(raw) != final_stat.st_size:
                raise SafeJailUnavailable("file changed during safe inspection")
            version = _FileVersion.from_stat_and_bytes(final_stat, raw)
        return target, content, truncated, version

    def _read_existing_file_limited(
        self, path: str | Path, byte_limit: int
    ) -> tuple[Path, str, bool]:
        """Read a bounded file while hiding edit-only snapshot metadata."""
        target, content, truncated, _ = self._read_existing_file_snapshot(
            path, byte_limit
        )
        return target, content, truncated

    async def read_file_bounded(self, path: str | Path) -> tuple[Path, str, bool]:
        """Read an existing project file through the common bounded boundary."""
        self.assert_project_identity()
        result = await asyncio.to_thread(
            self._read_existing_file_limited, path, MAX_READ_BYTES
        )
        self.assert_project_identity()
        return result

    @staticmethod
    def _parse_glob_pattern(pattern: str) -> tuple[tuple[str, ...], bool]:
        """Validate a relative glob and return component-wise match metadata."""
        if "\x00" in pattern:
            raise ValueError("glob pattern contains NUL")
        if len(pattern) > MAX_GLOB_PATTERN_LENGTH:
            raise ValueError("glob pattern too complex")

        pattern_path = Path(pattern)
        if (
            pattern_path.is_absolute()
            or pattern_path.anchor
            or ".." in pattern_path.parts
        ):
            raise OutOfProject(f"glob pattern outside project root: {pattern}")

        parts = tuple(part for part in pattern_path.parts if part not in ("", "."))
        if len(parts) > MAX_GLOB_PATTERN_PARTS:
            raise ValueError("glob pattern too complex")
        if any("**" in part and part != "**" for part in parts):
            raise ValueError("invalid recursive glob pattern")
        wildcard_count = sum(
            part.count("*") + part.count("?") + part.count("[")
            for part in parts
        )
        if wildcard_count > MAX_GLOB_WILDCARDS:
            raise ValueError("glob pattern too complex")

        separators = tuple(sep for sep in (os.sep, os.altsep) if sep)
        return parts, pattern.endswith(separators)

    @staticmethod
    def _glob_parts_match(
        path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]
    ) -> bool:
        """Match path components while making ``**`` span zero or more parts."""
        pending = [(0, 0)]
        seen: set[tuple[int, int]] = set()
        while pending:
            pattern_index, path_index = pending.pop()
            state = (pattern_index, path_index)
            if state in seen:
                continue
            seen.add(state)
            if pattern_index == len(pattern_parts):
                if path_index == len(path_parts):
                    return True
                continue

            part = pattern_parts[pattern_index]
            if part == "**":
                pending.append((pattern_index + 1, path_index))
                if path_index < len(path_parts):
                    pending.append((pattern_index, path_index + 1))
            elif (
                path_index < len(path_parts)
                and fnmatch.fnmatch(path_parts[path_index], part)
            ):
                pending.append((pattern_index + 1, path_index + 1))
        return False

    def _iter_bounded_tree(
        self,
        base: Path,
        *,
        max_entries: int,
        max_depth: int,
        mark_depth_limit: bool,
        budget: _TraversalBudget,
    ):
        """Yield contained tree entries lazily without following symlink dirs."""
        base_root_parts: tuple[str, ...] = ()
        if self.jail and os.name != "nt":
            try:
                base_root_parts = base.resolve(strict=True).relative_to(self.root).parts
            except (OSError, ValueError) as exc:
                raise OutOfProject(f"path outside project root: {base}") from exc
        stack: list[tuple[Path, tuple[str, ...], int]] = [(base, (), 0)]
        while stack:
            directory, relative_parts, depth = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        budget.entries += 1
                        if budget.entries > max_entries:
                            budget.limited = True
                            return

                        child_depth = depth + 1
                        if child_depth > max_depth:
                            budget.limited = True
                            continue
                        child_parts = relative_parts + (entry.name,)
                        candidate = Path(entry.path)
                        try:
                            target = self._resolve_existing(candidate)
                        except (FileNotFoundError, OutOfProject, RuntimeError):
                            continue
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if (
                            not is_directory
                            and stat.S_ISREG(entry_stat.st_mode)
                            and entry_stat.st_nlink > 1
                        ):
                            # See _read_existing_file_snapshot: a hard link
                            # gives one inode names outside this project too.
                            continue
                        if self.jail and os.name != "nt":
                            try:
                                self._validate_posix_tree_entry(
                                    base_root_parts + child_parts,
                                    is_directory=is_directory,
                                )
                            except (
                                FileNotFoundError,
                                OutOfProject,
                                SafeJailUnavailable,
                                OSError,
                            ):
                                # A bind mount, symlink or other unsafe entry
                                # is omitted rather than followed during a
                                # recursive inspection.
                                continue

                        yield candidate, target, child_parts, is_directory
                        if is_directory:
                            if child_depth < max_depth:
                                stack.append((target, child_parts, child_depth))
                            elif mark_depth_limit:
                                budget.limited = True
            except OSError:
                continue

    async def _check_permission(self, action) -> str:
        mode = self.perms.get(action.name, PERM_ASK)
        if mode == PERM_ALLOW:
            return PERM_ALLOW
        if mode == PERM_DENY:
            return PERM_DENY
        if self.ask_callback is None:
            return PERM_DENY
        granted = self.ask_callback(action)
        if inspect.isawaitable(granted):
            granted = await granted
        return PERM_ALLOW if granted else PERM_DENY

    async def run(self, action) -> ToolResult:
        handler = getattr(self, f"_tool_{action.name}", None)
        if handler is None:
            return ToolResult(False, "", error=f"unknown tool: {action.name}")
        try:
            # The first check binds persisted grants to the captured startup
            # identity.  The second one covers an asynchronous approval wait.
            self.assert_project_identity()
            decision = await self._check_permission(action)
            if decision == PERM_DENY:
                return ToolResult(
                    False, "", error=f"permission denied: {action.name}",
                )
            self.assert_project_identity()
            output = await handler(action)
            # Tree-based inspection on Windows uses the platform directory
            # APIs, which cannot be pinned to one root descriptor here.  A
            # final identity check prevents a result collected after a root
            # replacement from crossing the tool-result boundary.
            self.assert_project_identity()
            if isinstance(output, ToolResult):
                return output
            return ToolResult(True, output, tool=action.name)
        except ProjectIdentityChanged as exc:
            return ToolResult(False, "", error=str(exc))
        except PermissionDenied as exc:
            return ToolResult(False, "", error=str(exc))
        except OutOfProject as exc:
            return ToolResult(False, "", error=str(exc))
        except Exception as exc:
            return ToolResult(False, "", error=f"{type(exc).__name__}: {exc}")

    def _run_jailed_shell(self, command: str):
        """Run a shell only from the descriptor-pinned Linux project root.

        A pathname ``cwd`` can be swapped after an approval but before the
        child process starts.  Linux exposes an inherited directory descriptor
        through ``/proc/self/fd`` in the child, so it is the only supported
        jailed shell launch here.  Windows and non-Linux POSIX hosts fail
        closed rather than launch an approved command in a replacement root.
        ``bash`` is still not a general process sandbox: the command can use
        any paths the user could use once it is explicitly approved.
        """
        self.assert_project_identity()
        if os.name == "nt":
            raise SafeJailUnavailable(
                "safe jailed shell cwd is unavailable on Windows"
            )
        if sys.platform != "linux" or not os.path.isdir("/proc/self/fd"):
            raise SafeJailUnavailable(
                "safe jailed shell cwd requires Linux /proc descriptor paths"
            )
        root_fd = self._duplicate_jailed_posix_root()
        try:
            return subprocess.run(
                command,
                shell=True,
                cwd=f"/proc/self/fd/{root_fd}",
                pass_fds=(root_fd,),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                errors="replace",
            )
        finally:
            os.close(root_fd)

    async def _tool_bash(self, action) -> str:
        cmd = action.body.strip()
        if not cmd:
            raise ValueError("empty bash command")
        if self.jail:
            proc = await asyncio.to_thread(self._run_jailed_shell, cmd)
        else:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                errors="replace",
            )
        combined = (proc.stdout or "") + (proc.stderr or "")
        status = f"exit={proc.returncode}"
        output = _clip(f"$ {cmd}\n{status}\n{combined}", self.max_output)
        return ToolResult(
            ok=proc.returncode == 0,
            output=output,
            tool=action.name,
            error=output if proc.returncode != 0 else "",
        )

    async def _tool_read(self, action) -> str:
        path = action.kwargs.get("path", action.body.strip())
        if not path:
            raise ValueError("read requires a path")
        target, content, truncated = await self.read_file_bounded(path)
        suffix = "\n…(file truncated at inspection limit)" if truncated else ""
        return _clip(f"# {target}\n{content}{suffix}", self.max_output)

    async def _tool_write(self, action) -> str:
        path = action.kwargs.get("path", "")
        if not path:
            raise ValueError("write requires a path attribute")
        if self.jail:
            target = await asyncio.to_thread(
                self._replace_text_jailed,
                path,
                action.body,
                create_parents=True,
                require_existing=False,
            )
        else:
            target = self._resolve(path)
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, action.body, encoding="utf-8")
        return f"wrote {target} ({len(action.body)} chars)"

    async def _tool_edit(self, action) -> str:
        path = action.kwargs.get("path", "")
        old = action.kwargs.get("old", "")
        new = action.kwargs.get("new", "")
        if not path:
            raise ValueError("edit requires a path attribute")
        if not old:
            raise ValueError("edit requires an old attribute")
        expected_version: _FileVersion | None = None
        if self.jail:
            # Reject unsafe name forms before the inspection read.  The read
            # itself is descriptor-validated; the later replacement is atomic
            # relative to a fresh no-follow parent handle.
            self._mutation_parts(path)
            self.assert_project_identity()
            target, content, truncated, expected_version = await asyncio.to_thread(
                self._read_existing_file_snapshot, path, MAX_READ_BYTES
            )
            self.assert_project_identity()
            if truncated:
                raise ValueError("edit target exceeds the safe inspection limit")
            if expected_version is None:  # defensive: a complete snapshot has one
                raise SafeJailUnavailable("cannot capture edit target version")
        else:
            target = self._resolve(path)
            if not await asyncio.to_thread(target.is_file):
                raise FileNotFoundError(f"no such file: {path}")
            content = await asyncio.to_thread(
                target.read_text, encoding="utf-8", errors="replace"
            )
        if old not in content:
            message = f"edit failed: pattern not found in {target}"
            return ToolResult(False, message, tool=action.name, error=message)
        updated = content.replace(old, new, 1)
        if self.jail:
            target = await asyncio.to_thread(
                self._replace_text_jailed,
                path,
                updated,
                create_parents=False,
                require_existing=True,
                expected_version=expected_version,
            )
        else:
            await asyncio.to_thread(target.write_text, updated, encoding="utf-8")
        return f"edited {target} (1 replacement)"

    async def _tool_glob(self, action) -> str:
        pattern = action.kwargs.get("pattern", action.body.strip())
        if not pattern:
            raise ValueError("glob requires a pattern")
        pattern_parts, directory_only = self._parse_glob_pattern(pattern)
        if self.jail and os.name == "nt":
            # Python exposes Windows directory scans only by pathname.  That
            # cannot bind an entire recursive walk to the startup root object
            # across a same-path replacement, so do not return unpinned names.
            raise SafeJailUnavailable(
                "safe jailed tree traversal is unavailable on Windows"
            )

        def collect() -> tuple[list[str], bool]:
            matches: list[str] = []
            budget = _TraversalBudget()
            if not pattern_parts:
                return ["."], False

            if self._glob_parts_match((), pattern_parts) and (
                not directory_only or self.root.is_dir()
            ):
                matches.append(".")

            max_depth = (
                MAX_GLOB_DEPTH
                if "**" in pattern_parts
                else min(MAX_GLOB_DEPTH, len(pattern_parts))
            )
            for candidate, _, relative_parts, is_directory in self._iter_bounded_tree(
                self.root,
                max_entries=MAX_GLOB_ENTRIES,
                max_depth=max_depth,
                mark_depth_limit="**" in pattern_parts,
                budget=budget,
            ):
                if directory_only and not is_directory:
                    continue
                if not self._glob_parts_match(relative_parts, pattern_parts):
                    continue
                if len(matches) >= MAX_GLOB_MATCHES:
                    budget.limited = True
                    break
                relative = candidate.relative_to(self.root)
                matches.append(str(relative).replace("\\", "/"))
            return sorted(matches), budget.limited

        matches, limited = await asyncio.to_thread(collect)
        if not matches:
            suffix = "\n…(glob inspection limit reached)" if limited else ""
            return f"no matches for {pattern!r}{suffix}"
        suffix = "\n…(glob inspection limit reached)" if limited else ""
        return _clip("\n".join(matches) + suffix, self.max_output)

    async def _tool_grep(self, action) -> str:
        pattern = action.kwargs.get("pattern", "")
        sub = action.kwargs.get("path", "")
        if not pattern:
            raise ValueError("grep requires a pattern")
        if self.jail and os.name == "nt":
            # See _tool_glob: recursive inspection needs a native
            # handle-relative directory enumerator, not pathlib/scandir paths.
            raise SafeJailUnavailable(
                "safe jailed tree traversal is unavailable on Windows"
            )
        base = self._resolve_existing(sub) if sub else self.root

        def search() -> tuple[list[str], bool]:
            lines: list[str] = []
            files = 0
            budget = _TraversalBudget()
            for candidate, target, _, is_directory in self._iter_bounded_tree(
                base,
                max_entries=MAX_GREP_ENTRIES,
                max_depth=MAX_GREP_DEPTH,
                mark_depth_limit=True,
                budget=budget,
            ):
                if is_directory or not target.is_file():
                    continue
                files += 1
                if files > MAX_GREP_FILES:
                    budget.limited = True
                    break
                try:
                    _, text, _ = self._read_existing_file_limited(
                        target, MAX_GREP_FILE_BYTES
                    )
                except (OSError, OutOfProject, SafeJailUnavailable):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if pattern in line:
                        rel = str(candidate.relative_to(self.root)).replace("\\", "/")
                        lines.append(f"{rel}:{lineno}: {line.strip()}")
                        if len(lines) >= MAX_GREP_MATCHES:
                            budget.limited = True
                            break
                if budget.limited:
                    break
            return lines, budget.limited

        lines, limited = await asyncio.to_thread(search)
        if not lines:
            suffix = "\n…(grep inspection limit reached)" if limited else ""
            return f"no matches for {pattern!r}{suffix}"
        suffix = "\n…(grep inspection limit reached)" if limited else ""
        return _clip("\n".join(lines) + suffix, self.max_output)
