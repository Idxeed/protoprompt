"""User-owned persistence for a project-scoped agent namespace.

The selected repository is untrusted input.  Mutable agent state therefore
lives in a per-user application-data directory, keyed by the canonical project
root, rather than in ``<project>/.protoprompt``.  This keeps sessions,
permissions and local configuration out of repository-controlled files.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEGACY_STATE_DIR = ".protoprompt"
COLD_DB = "agent.db"
STATE_JSON = "state.json"
PERMS_JSON = "perms.json"
SESSION_DIR = "sessions"
DEFAULT_SESSION = "default"


class ProjectIdentityChanged(RuntimeError):
    """A project path no longer names the identity captured at startup."""


@dataclass(frozen=True)
class _RootSnapshot:
    """Filesystem identity observed from one already-open root descriptor.

    ``generation`` is a stable object-creation value, not directory ``ctime``.
    Directory ``ctime`` changes whenever children are created or removed and is
    therefore unsuitable for an identity that must survive ordinary work.
    """

    root: Path
    canonical_path: str
    device: int
    inode: int
    generation: int


@dataclass
class _RootCapture:
    """A checked root snapshot and the descriptor that produced it."""

    snapshot: _RootSnapshot
    fd: int

    def detach_fd(self) -> int:
        """Transfer ownership of the captured descriptor to a root pin."""
        if self.fd < 0:
            raise OSError("project root descriptor is already closed")
        descriptor = self.fd
        self.fd = -1
        return descriptor

    def close(self) -> None:
        if self.fd >= 0:
            descriptor = self.fd
            self.fd = -1
            try:
                os.close(descriptor)
            except OSError:
                pass


class _RootPin:
    """Own one non-inheritable descriptor for the startup root object."""

    __slots__ = ("_fd",)

    def __init__(self, descriptor: int) -> None:
        self._fd = descriptor
        try:
            os.set_inheritable(descriptor, False)
        except OSError:
            self.close()
            raise

    @property
    def fd(self) -> int:
        if self._fd < 0:
            raise OSError("project root descriptor is closed")
        return self._fd

    def duplicate(self) -> int:
        """Return a caller-owned duplicate of the pinned root descriptor."""
        descriptor = os.dup(self.fd)
        try:
            os.set_inheritable(descriptor, False)
        except OSError:
            os.close(descriptor)
            raise
        return descriptor

    def close(self) -> None:
        if self._fd >= 0:
            descriptor = self._fd
            self._fd = -1
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown order
        try:
            self.close()
        except Exception:
            # Module globals can already be torn down during interpreter exit.
            pass


@dataclass(frozen=True)
class ProjectIdentity:
    """Immutable binding between a project path and its filesystem object."""

    root: Path
    canonical_path: str
    device: int
    inode: int
    generation: int
    namespace: str
    _root_pin: _RootPin = field(repr=False, compare=False)

    def duplicate_root_fd(self) -> int:
        """Return a caller-owned descriptor for the original root object.

        The descriptor remains valid even if the configured pathname is later
        renamed or replaced.  Consumers such as ``ToolRunner`` must close the
        duplicate when the individual operation completes.
        """
        return self._root_pin.duplicate()

    def assert_root_descriptor(self, descriptor: int) -> None:
        """Fail closed unless ``descriptor`` names this captured root object.

        This lets descriptor-based consumers validate a newly opened handle
        with the same stable birth generation as the durable namespace, rather
        than reimplementing device/inode-only comparisons.
        """
        try:
            # A closed identity must not grant authority through a duplicate
            # that another component retained before lifecycle shutdown.
            self._root_pin.fd
            snapshot = _snapshot_from_descriptor(
                descriptor,
                root=self.root,
                canonical_path=self.canonical_path,
            )
        except OSError as exc:
            raise ProjectIdentityChanged(
                "project root identity is unavailable; restart pp-agent"
            ) from exc
        if not self._matches_snapshot(snapshot):
            raise ProjectIdentityChanged(
                "project root identity changed; restart pp-agent"
            )

    def close(self) -> None:
        """Release the retained root descriptor before process shutdown."""
        self._root_pin.close()

    def __enter__(self) -> "ProjectIdentity":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _matches_snapshot(self, snapshot: _RootSnapshot) -> bool:
        return (
            snapshot.canonical_path == self.canonical_path
            and snapshot.device == self.device
            and snapshot.inode == self.inode
            and snapshot.generation == self.generation
        )

    def assert_current(self, root: str | Path | None = None) -> Path:
        """Fail closed unless ``root`` still names this exact project object."""
        current_capture: _RootCapture | None = None
        try:
            pinned = _snapshot_from_descriptor(
                self._root_pin.fd,
                root=self.root,
                canonical_path=self.canonical_path,
            )
            current_capture = _capture_root(self.root if root is None else root)
        except OSError as exc:
            raise ProjectIdentityChanged(
                "project root identity is unavailable; restart pp-agent"
            ) from exc
        try:
            current = current_capture.snapshot
            if not self._matches_snapshot(pinned) or not self._matches_snapshot(current):
                raise ProjectIdentityChanged(
                    "project root identity changed; restart pp-agent"
                )
            return current.root
        finally:
            if current_capture is not None:
                current_capture.close()


def _namespace_from_snapshot(snapshot: _RootSnapshot) -> str:
    """Derive a durable state namespace from a non-reusable root identity."""
    material = "\0".join(
        (
            "protoprompt-agent-state-v3",
            snapshot.canonical_path,
            str(snapshot.device),
            str(snapshot.inode),
            str(snapshot.generation),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _windows_filetime_generation(filetime) -> int:
    """Return an opaque, stable Windows object-creation generation."""
    generation = int(filetime.dwLowDateTime) | (int(filetime.dwHighDateTime) << 32)
    if generation <= 0:
        raise OSError("filesystem birth identity is unavailable for project root")
    return generation


def _windows_generation_from_descriptor(descriptor: int) -> int:
    """Read CreationTime from an already-open Windows object handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

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

    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("cannot inspect project root descriptor")
    get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    info = _ByHandleFileInformation()
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "cannot inspect project root")
    return _windows_filetime_generation(info.CreationTime)


def _linux_generation_from_descriptor(descriptor: int) -> int:
    """Return Linux ``statx`` birth time for an already-open root descriptor.

    Inode values can be reused after a checkout is deleted.  Linux does not
    expose creation time through ``os.fstat``, so use ``statx`` with
    ``AT_EMPTY_PATH`` to query the descriptor itself.  A filesystem without
    ``STATX_BTIME`` fails closed rather than silently reusing state.
    """
    import ctypes
    import errno

    class _StatxTimestamp(ctypes.Structure):
        _fields_ = [
            ("tv_sec", ctypes.c_longlong),
            ("tv_nsec", ctypes.c_uint32),
            ("__reserved", ctypes.c_int32),
        ]

    class _Statx(ctypes.Structure):
        _fields_ = [
            ("stx_mask", ctypes.c_uint32),
            ("stx_blksize", ctypes.c_uint32),
            ("stx_attributes", ctypes.c_uint64),
            ("stx_nlink", ctypes.c_uint32),
            ("stx_uid", ctypes.c_uint32),
            ("stx_gid", ctypes.c_uint32),
            ("stx_mode", ctypes.c_uint16),
            ("__spare0", ctypes.c_uint16),
            ("stx_ino", ctypes.c_uint64),
            ("stx_size", ctypes.c_uint64),
            ("stx_blocks", ctypes.c_uint64),
            ("stx_attributes_mask", ctypes.c_uint64),
            ("stx_atime", _StatxTimestamp),
            ("stx_btime", _StatxTimestamp),
            ("stx_ctime", _StatxTimestamp),
            ("stx_mtime", _StatxTimestamp),
            ("stx_rdev_major", ctypes.c_uint32),
            ("stx_rdev_minor", ctypes.c_uint32),
            ("stx_dev_major", ctypes.c_uint32),
            ("stx_dev_minor", ctypes.c_uint32),
            ("__spare2", ctypes.c_uint64 * 14),
        ]

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statx = libc.statx
    except (AttributeError, OSError) as exc:
        raise OSError(
            "safe project identity requires Linux statx birth-time support"
        ) from exc
    statx.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_Statx),
    )
    statx.restype = ctypes.c_int
    at_empty_path = 0x1000
    statx_btime = 0x00000800
    result = statx(
        descriptor,
        b"",
        at_empty_path,
        statx_btime,
        ctypes.byref(info := _Statx()),
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EOPNOTSUPP
        raise OSError(error, "cannot read filesystem birth identity for project root")
    if not info.stx_mask & statx_btime:
        raise OSError(
            errno.EOPNOTSUPP,
            "filesystem does not provide stable project-root birth identity",
        )
    nanoseconds = int(info.stx_btime.tv_nsec)
    if not 0 <= nanoseconds < 1_000_000_000:
        raise OSError("filesystem returned an invalid project-root birth identity")
    return int(info.stx_btime.tv_sec) * 1_000_000_000 + nanoseconds


def _generation_from_descriptor(descriptor: int, root_stat: os.stat_result) -> int:
    """Return a stable creation generation for a live directory descriptor."""
    if os.name == "nt":
        return _windows_generation_from_descriptor(descriptor)
    if sys.platform == "linux":
        return _linux_generation_from_descriptor(descriptor)
    generation = getattr(root_stat, "st_birthtime_ns", None)
    if isinstance(generation, int):
        return generation
    birth_time = getattr(root_stat, "st_birthtime", None)
    if isinstance(birth_time, (int, float)):
        # macOS and BSD expose st_birthtime.  It is stable across ordinary
        # directory mutations, unlike ctime; retain as much precision as the
        # Python runtime exposes.
        return int(float(birth_time) * 1_000_000_000)
    raise OSError("filesystem birth identity is unavailable for project root")


def _snapshot_from_descriptor(
    descriptor: int, *, root: Path, canonical_path: str
) -> _RootSnapshot:
    """Build a root snapshot entirely from an already-open descriptor."""
    root_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise OSError("project root is not a directory")
    device = root_stat.st_dev
    inode = root_stat.st_ino
    if not isinstance(device, int) or not isinstance(inode, int) or inode == 0:
        raise OSError("filesystem identity is unavailable for project root")
    return _RootSnapshot(
        root=root,
        canonical_path=canonical_path,
        device=device,
        inode=inode,
        generation=_generation_from_descriptor(descriptor, root_stat),
    )


def _windows_local_absolute_path(root: str | Path) -> Path:
    """Normalize a local Windows pathname without resolving filesystem links."""
    raw = os.fspath(root)
    if not isinstance(raw, str):
        raise OSError("project root must be a text path")
    if "\x00" in raw:
        raise OSError("project root contains NUL")
    if raw.startswith(("\\\\", "//")):
        raise OSError("project root must be a local Windows drive path")
    drive, tail = os.path.splitdrive(raw)
    # ``C:project`` depends on the process's per-drive working directory.
    # Do not silently turn it into a different authority with ``abspath``.
    if drive and not tail.startswith(("\\", "/")):
        raise OSError("project root must not use a drive-relative path")
    if not drive and raw.startswith(("\\", "/")):
        raise OSError("project root must be a local Windows drive path")
    normalized = os.path.normpath(os.path.abspath(raw))
    candidate = Path(normalized)
    if not candidate.is_absolute() or not candidate.drive:
        raise OSError("project root must be an absolute Windows drive path")
    if str(candidate.drive).startswith("\\\\"):
        raise OSError("project root must not be a UNC path")
    return candidate


def find_root(start: str | Path | None = None) -> Path:
    """Nearest git root; Windows discovery never resolves reparse points."""
    if os.name != "nt":
        cur = Path(start or Path.cwd()).resolve()
        if cur.is_file():
            cur = cur.parent
        for candidate in (cur, *cur.parents):
            if (candidate / ".git").exists():
                return candidate
        return cur

    selected = _windows_local_absolute_path(start or Path.cwd())
    _, _, _, is_directory = _inspect_windows_path_no_reparse(
        selected, require_directory=False
    )
    base = selected if is_directory else selected.parent
    # Each candidate and its .git marker are inspected from the drive handle.
    # A worktree's regular .git file is accepted; reparse markers are not.
    for candidate in (base, *base.parents):
        _inspect_windows_path_no_reparse(candidate, require_directory=True)
        try:
            _inspect_windows_path_no_reparse(
                candidate / ".git", require_directory=False
            )
        except FileNotFoundError:
            continue
        return candidate
    return base


def _inspect_windows_path_no_reparse(
    root: str | Path, *, require_directory: bool, _retain_descriptor: bool = False
) -> tuple[Path, int, int, bool] | tuple[Path, int, int, bool, int, int]:
    """Inspect one Windows file or directory without following reparse points.

    ``Path.resolve()`` follows a swapped root junction before its caller can
    compare file IDs.  Walk an absolute local-drive path component by component
    with ``NtCreateFile(RootDirectory=..., OBJ_DONT_REPARSE)`` instead.  The
    result is intentionally lexical: the native handles, not a canonicalized
    pathname, establish the no-reparse boundary.  Intermediate components are
    always directories; the final component may be a regular file only when
    ``require_directory`` is false (for a Git worktree's ``.git`` marker).
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    candidate = _windows_local_absolute_path(root)
    normalized = str(candidate)
    components = tuple(
        part for part in candidate.parts if part not in (candidate.anchor, "", ".")
    )
    if any(part == ".." for part in components):
        raise OSError("project root contains traversal")

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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
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

    file_list_directory = 0x00000001
    file_read_attributes = 0x00000080
    file_traverse = 0x00000020
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    obj_dont_reparse = 0x00001000
    file_open = 1
    file_directory_file = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value

    def inspect_object(
        handle: int, *, directory_required: bool
    ) -> tuple[_ByHandleFileInformation, bool]:
        info = _ByHandleFileInformation()
        if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "cannot inspect project root")
        is_directory = bool(info.FileAttributes & file_attribute_directory)
        if info.FileAttributes & file_attribute_reparse_point:
            raise OSError("project root contains an unsafe reparse component")
        if directory_required and not is_directory:
            raise OSError("project root component is not a directory")
        return info, is_directory

    root_anchor = candidate.anchor
    root_handle = create_file(
        root_anchor,
        file_list_directory | file_read_attributes | file_traverse | synchronize,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if not root_handle or int(root_handle) == invalid_handle:
        raise OSError(ctypes.get_last_error(), "cannot open project drive root")
    current_handle = int(root_handle)
    try:
        final_info, is_directory = inspect_object(
            current_handle, directory_required=True
        )
        status_object_name_not_found = -1073741772  # 0xC0000034
        status_object_path_not_found = -1073741766  # 0xC000003A
        for index, component in enumerate(components):
            is_final = index == len(components) - 1
            directory_required = not is_final or require_directory
            encoded = component.encode("utf-16-le")
            if len(encoded) + 2 > 0xFFFF:
                raise OSError("project root component is too long")
            buffer = ctypes.create_unicode_buffer(component)
            unicode_name = _UnicodeString(
                len(encoded), len(encoded) + 2, ctypes.cast(buffer, wintypes.LPWSTR)
            )
            attributes = _ObjectAttributes(
                ctypes.sizeof(_ObjectAttributes),
                wintypes.HANDLE(current_handle),
                ctypes.pointer(unicode_name),
                obj_case_insensitive | obj_dont_reparse,
                None,
                None,
            )
            io_status = _IoStatusBlock()
            child_handle = wintypes.HANDLE()
            desired_access = file_read_attributes | synchronize
            create_options = file_synchronous_io_nonalert | file_open_reparse_point
            if directory_required:
                desired_access |= file_list_directory | file_traverse
                create_options |= file_directory_file
            status = int(
                nt_create(
                    ctypes.byref(child_handle),
                    desired_access,
                    ctypes.byref(attributes),
                    ctypes.byref(io_status),
                    None,
                    0x00000080,
                    file_share_read | file_share_write | file_share_delete,
                    file_open,
                    create_options,
                    None,
                    0,
                )
            )
            if status < 0 or not child_handle.value:
                if status in (status_object_name_not_found, status_object_path_not_found):
                    raise FileNotFoundError("project root component does not exist")
                raise OSError(
                    f"cannot open safe project root component: 0x{status & 0xFFFFFFFF:08X}"
                )
            child = int(child_handle.value)
            try:
                final_info, is_directory = inspect_object(
                    child, directory_required=directory_required
                )
            except Exception:
                close_handle(wintypes.HANDLE(child))
                raise
            close_handle(wintypes.HANDLE(current_handle))
            current_handle = child

        # Convert the inspected native handle to a descriptor only after the
        # complete no-reparse walk.  This deliberately uses Python's own
        # device/inode representation so ToolRunner's fstat-based descriptor
        # binding compares like with like on Windows too.
        descriptor = msvcrt.open_osfhandle(current_handle, os.O_RDONLY | os.O_BINARY)
        current_handle = 0  # descriptor now owns the underlying HANDLE
        try:
            os.set_inheritable(descriptor, False)
            root_stat = os.fstat(descriptor)
            generation = _windows_filetime_generation(final_info.CreationTime)
            device = root_stat.st_dev
            inode = root_stat.st_ino
            if device == 0 or inode == 0:
                raise OSError("filesystem identity is unavailable for project root")
            if not is_directory and not stat.S_ISREG(root_stat.st_mode):
                raise OSError("project root marker is not a regular file")
            if _retain_descriptor:
                result = (
                    Path(normalized),
                    device,
                    inode,
                    is_directory,
                    generation,
                    descriptor,
                )
                descriptor = -1  # ownership transferred to the caller
                return result
            return Path(normalized), device, inode, is_directory
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        if current_handle:
            close_handle(wintypes.HANDLE(current_handle))


def _capture_windows_root_no_reparse(root: str | Path) -> _RootCapture:
    """Pin a Windows directory identity without following reparse points."""
    result = _inspect_windows_path_no_reparse(
        root, require_directory=True, _retain_descriptor=True
    )
    canonical_root, device, inode, is_directory, generation, descriptor = result
    if not is_directory:  # defensive: require_directory already enforces this
        os.close(descriptor)
        raise OSError("project root is not a directory")
    canonical_path = os.path.normcase(str(canonical_root))
    return _RootCapture(
        _RootSnapshot(
            root=canonical_root,
            canonical_path=canonical_path,
            device=device,
            inode=inode,
            generation=generation,
        ),
        descriptor,
    )


def _capture_posix_root(root: str | Path) -> _RootCapture:
    """Open and pin one POSIX root without a final-component link race."""
    canonical_root = Path(root).resolve()
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, flag) for flag in required):
        raise OSError("safe project identity requires no-follow directory handles")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(canonical_root, flags)
        canonical_path = os.path.normcase(str(canonical_root))
        snapshot = _snapshot_from_descriptor(
            descriptor, root=canonical_root, canonical_path=canonical_path
        )
        # The descriptor is the authority; this second no-follow check only
        # ensures that the requested pathname still names that object at the
        # capture boundary.  Keeping the descriptor open prevents its inode
        # from being recycled for a replacement while the identity lives.
        named_root = os.stat(canonical_root, follow_symlinks=False)
        if (
            named_root.st_dev != snapshot.device
            or named_root.st_ino != snapshot.inode
            or not stat.S_ISDIR(named_root.st_mode)
        ):
            raise OSError("project root changed while capturing its identity")
        capture = _RootCapture(snapshot, descriptor)
        descriptor = -1
        return capture
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _capture_root(root: str | Path) -> _RootCapture:
    """Capture a root snapshot plus a retained descriptor on this platform."""
    if os.name == "nt":
        return _capture_windows_root_no_reparse(root)
    return _capture_posix_root(root)


def safe_project_directory(path: str | Path) -> Path:
    """Return a checked project directory without accepting Windows reparses."""
    if os.name == "nt":
        capture = _capture_windows_root_no_reparse(path)
        try:
            return capture.snapshot.root
        finally:
            capture.close()
    return Path(path).resolve()


def capture_project_identity(root: str | Path) -> ProjectIdentity:
    """Capture the canonical root and its fail-closed filesystem identity.

    The root path alone is not an authority boundary: a deleted checkout can
    be replaced by another directory at the same pathname.  Retain a native
    root descriptor for this process and include stable object-creation
    generation alongside device and inode in the durable namespace.  Do not
    fall back to a path-only or ``ctime``-based key when that identity is
    unavailable.
    """
    capture = _capture_root(root)
    try:
        snapshot = capture.snapshot
        return ProjectIdentity(
            root=snapshot.root,
            canonical_path=snapshot.canonical_path,
            device=snapshot.device,
            inode=snapshot.inode,
            generation=snapshot.generation,
            namespace=_namespace_from_snapshot(snapshot),
            _root_pin=_RootPin(capture.detach_fd()),
        )
    finally:
        capture.close()


def namespace_for(root: str | Path | ProjectIdentity) -> str:
    """Return the namespace of a captured or freshly resolved project identity."""
    if isinstance(root, ProjectIdentity):
        return root.namespace
    identity = capture_project_identity(root)
    try:
        return identity.namespace
    finally:
        identity.close()


def user_state_root() -> Path:
    """Return the user-owned base directory for pp-agent state.

    ``PROTOPROMPT_AGENT_STATE_DIR`` is intentionally an explicit process-level
    override for tests, containers and managed deployments.  It is not read
    from repository configuration.
    """
    explicit = os.environ.get("PROTOPROMPT_AGENT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "ProtoPrompt" / "agent"
        return Path.home() / "AppData" / "Local" / "ProtoPrompt" / "agent"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home).expanduser() if xdg_state_home else (
        Path.home() / ".local" / "state"
    )
    return base / "protoprompt" / "agent"


def legacy_state_dir(root: str | Path) -> Path:
    """Return the former project-local path without ever loading it by default."""
    return Path(root) / LEGACY_STATE_DIR


def state_dir(root: str | Path | ProjectIdentity) -> Path:
    """Return the per-user state directory for one canonical project root."""
    return user_state_root() / namespace_for(root)


def ensure_state_dir(root: str | Path | ProjectIdentity) -> Path:
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    return directory


def cold_db_path(root: str | Path | ProjectIdentity) -> Path:
    return state_dir(root) / COLD_DB


def state_json_path(root: str | Path | ProjectIdentity) -> Path:
    return state_dir(root) / STATE_JSON


def perms_json_path(root: str | Path | ProjectIdentity) -> Path:
    return state_dir(root) / PERMS_JSON


def load_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_state(mem, root: str | Path | ProjectIdentity) -> None:
    ensure_state_dir(root)
    save_json(state_json_path(root), mem.export_state())


def load_state(mem, root: str | Path | ProjectIdentity) -> bool:
    """Восстановить горячий набор в ``mem``. True, если состояние было."""
    data = load_json(state_json_path(root))
    return _import_state_safely(mem, data)


# ── сессии ───────────────────────────────────────────────────────


def _sanitize_session(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return cleaned.strip("._") or DEFAULT_SESSION


def session_dir(root: str | Path | ProjectIdentity) -> Path:
    return state_dir(root) / SESSION_DIR


def session_file(root: str | Path | ProjectIdentity, name: str) -> Path:
    return session_dir(root) / f"{_sanitize_session(name)}.json"


def session_exists(root: str | Path | ProjectIdentity, name: str) -> bool:
    return session_file(root, name).is_file()


def save_session(
    mem, root: str | Path | ProjectIdentity, name: str = DEFAULT_SESSION
) -> None:
    ensure_state_dir(root)
    save_json(session_file(root, name), mem.export_state())


def load_session(
    mem, root: str | Path | ProjectIdentity, name: str = DEFAULT_SESSION
) -> bool:
    data = load_json(session_file(root, name))
    return _import_state_safely(mem, data)


def _import_state_safely(mem, data: Any) -> bool:
    """Import a persisted snapshot without corrupting the active session.

    Session files are ordinary local JSON and can be cut off during a crash
    or hand-edited.  ``WorkingMemory.import_state`` clears its current state
    before iterating entries, so keep a valid rollback snapshot if a
    structurally malformed (but valid JSON) payload raises halfway through.
    """
    if not isinstance(data, dict) or not data:
        return False
    before = mem.export_state()
    try:
        mem.import_state(data)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        mem.import_state(before)
        return False
    return True


def list_sessions(root: str | Path | ProjectIdentity) -> list[dict]:
    """Метаданные всех сессий проекта, свежие первыми."""
    directory = session_dir(root)
    entries = []
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        data = load_json(path, {}) or {}
        entries.append({
            "name": path.stem,
            "updated_at": path.stat().st_mtime,
            "items": len(data.get("items", [])),
            "goal": (data.get("goal_text") or "")[:60],
        })
    entries.sort(key=lambda e: e["updated_at"], reverse=True)
    return entries


def latest_session(root: str | Path | ProjectIdentity) -> str | None:
    """Имя самой свежей сохранённой сессии."""
    sessions = list_sessions(root)
    return sessions[0]["name"] if sessions else None
