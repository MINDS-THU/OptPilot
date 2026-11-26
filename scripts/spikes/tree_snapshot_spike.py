"""Disposable immutable-tree snapshot architecture spike.

This module tests the risky filesystem invariants needed by the future content
plane.  It is deliberately isolated from :mod:`optpilot` and is not a
production API.  In particular, it proves that a source tree can be captured
through directory descriptors, privately materialized, deterministically
identified, and atomically published with provisional retention already in
place.

The verified-copy provider is portable and mandatory.  ``APFSCloneProvider``
is a strict optimization: it calls ``fclonefileat(2)`` and raises instead of
falling back when the source and store are not on one APFS device.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple


JsonDict = Dict[str, Any]
FaultHook = Callable[[str], None]
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = _READ_FLAGS | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = _READ_FLAGS | os.O_NOFOLLOW
_DARWIN_O_SYMLINK = 0x00200000
_SYSTEM_NONCONTENT_XATTRS = frozenset({"com.apple.provenance"})


class SnapshotSpikeError(RuntimeError):
    """Base error for the disposable tree-snapshot spike."""


class CaptureRejected(SnapshotSpikeError):
    """The source cannot be proven safe and deterministic."""


class SourceChanged(CaptureRejected):
    """The source changed while it was being captured."""


class UnsupportedProvider(SnapshotSpikeError):
    """A requested physical provider is not supported for these paths."""


class CorruptObject(SnapshotSpikeError):
    """A published object no longer matches its immutable identity."""


@dataclass(frozen=True)
class CaptureReceipt:
    tree_ref: str
    retention_token: str
    provider: str


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    links: int
    blocks: int


@dataclass(frozen=True)
class _InventoryEntry:
    raw_path: str
    canonical_path: str
    kind: str
    identity: _Identity
    symlink_target: Optional[str] = None


@dataclass(frozen=True)
class _Inventory:
    root_identity: _Identity
    entries: Tuple[_InventoryEntry, ...]


def canonical_relative_path(path: str, *, allow_empty: bool = False) -> str:
    """Return the NFC spelling of a safe portable relative path.

    Backslashes are rejected rather than interpreted because the manifest uses
    POSIX separators on every host.  ``.`` and ``..`` components are also
    rejected even where a particular use could prove them lexically contained;
    the spike intentionally tests the smallest unambiguous contract.
    """

    if not isinstance(path, str):
        raise CaptureRejected("manifest paths must be strings")
    if not path:
        if allow_empty:
            return ""
        raise CaptureRejected("empty relative path")
    if "\\" in path:
        raise CaptureRejected(f"backslash is not portable in path: {path!r}")
    if path.startswith("/") or PurePosixPath(path).is_absolute():
        raise CaptureRejected(f"absolute path is forbidden: {path!r}")
    components = path.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise CaptureRejected(f"empty or traversal component in path: {path!r}")
    if any("\x00" in component for component in components):
        raise CaptureRejected("NUL is forbidden in paths")
    canonical = "/".join(unicodedata.normalize("NFC", component) for component in components)
    try:
        canonical.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CaptureRejected(f"path is not valid UTF-8: {path!r}") from error
    return canonical


def validate_canonical_paths(paths: Sequence[str]) -> List[str]:
    """Canonicalize paths and reject NFC or Unicode-casefold collisions."""

    normalized: List[str] = []
    nfc_seen: Dict[str, str] = {}
    case_seen: Dict[str, str] = {}
    for raw_path in paths:
        canonical = canonical_relative_path(raw_path)
        previous = nfc_seen.get(canonical)
        if previous is not None and previous != raw_path:
            raise CaptureRejected(
                f"NFC path collision: {previous!r} and {raw_path!r}"
            )
        folded = canonical.casefold()
        previous = case_seen.get(folded)
        if previous is not None and previous != raw_path:
            raise CaptureRejected(
                f"case-insensitive path collision: {previous!r} and {raw_path!r}"
            )
        nfc_seen[canonical] = raw_path
        case_seen[folded] = raw_path
        normalized.append(canonical)
    return normalized


def canonical_symlink_target(target: str) -> str:
    """Validate and NFC-normalize a relative symlink target."""

    try:
        return canonical_relative_path(target)
    except CaptureRejected as error:
        raise CaptureRejected(f"unsafe symlink target {target!r}: {error}") from error


def _canonical_selection(selection: str) -> Tuple[str, ...]:
    if selection in ("", "."):
        return ()
    canonical = canonical_relative_path(selection)
    if canonical != selection:
        raise CaptureRejected(
            f"tree selection must already use canonical NFC spelling: {selection!r}"
        )
    return tuple(canonical.split("/"))


def _open_selected_directory(allowed_root: Path, selection: str) -> int:
    """Walk a canonical selection from a trusted root without following links."""

    allowed_root = Path(allowed_root).absolute()
    root_lstat = os.lstat(allowed_root)
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise CaptureRejected("allowed root must be a real directory, not a symlink")
    root_fd = os.open(allowed_root, _DIRECTORY_FLAGS)
    try:
        _require_same_identity(os.fstat(root_fd), _identity(root_lstat), "allowed root")
        selected_fd = os.dup(root_fd)
        try:
            for component in _canonical_selection(selection):
                try:
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=selected_fd)
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                        raise CaptureRejected(
                            f"selection component is missing, not a directory, or a symlink: {component!r}"
                        ) from error
                    raise
                os.close(selected_fd)
                selected_fd = next_fd
            return selected_fd
        except BaseException:
            os.close(selected_fd)
            raise
    finally:
        os.close(root_fd)


def _identity(info: os.stat_result) -> _Identity:
    return _Identity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=info.st_mode,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        links=info.st_nlink,
        blocks=getattr(info, "st_blocks", -1),
    )


def _is_sparse(info: os.stat_result) -> bool:
    blocks = getattr(info, "st_blocks", None)
    return bool(info.st_size and blocks is not None and blocks >= 0 and blocks * 512 < info.st_size)


def _open_symlink(parent_fd: int, name: str) -> Optional[int]:
    if sys.platform == "darwin":
        return os.open(
            name,
            _READ_FLAGS | _DARWIN_O_SYMLINK,
            dir_fd=parent_fd,
        )
    if hasattr(os, "O_PATH"):
        return os.open(
            name,
            os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    return None


def _fd_xattrs(fd: int) -> Tuple[str, ...]:
    """List extended attributes through an already-open descriptor."""

    if hasattr(os, "listxattr"):
        try:
            return tuple(sorted(os.listxattr(fd)))  # type: ignore[attr-defined]
        except (OSError, TypeError):
            pass

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "flistxattr", None)
    if function is None:
        raise CaptureRejected("this platform cannot inspect descriptor xattrs")
    if sys.platform == "darwin":
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        call = lambda buffer, size: function(fd, buffer, size, 0)
    else:
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        call = lambda buffer, size: function(fd, buffer, size)
    function.restype = ctypes.c_ssize_t
    size = call(None, 0)
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if size == 0:
        return ()
    buffer = ctypes.create_string_buffer(size)
    written = call(buffer, size)
    if written < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return tuple(
        sorted(
            item.decode("utf-8", errors="surrogateescape")
            for item in bytes(buffer.raw[:written]).split(b"\x00")
            if item
        )
    )


def _reject_xattrs(fd: int, display_path: str) -> None:
    try:
        attributes = set(_fd_xattrs(fd))
    except OSError as error:
        raise CaptureRejected(f"cannot inspect xattrs for {display_path!r}: {error}") from error
    # macOS 26 attaches an immutable provenance marker to newly created files.
    # It has no payload semantics and cannot be removed in this environment; all
    # other attributes remain rejected.  Production must make this portability
    # exception an explicit platform policy rather than baking it into identity.
    if sys.platform == "darwin":
        attributes.difference_update(_SYSTEM_NONCONTENT_XATTRS)
    if attributes:
        raise CaptureRejected(
            f"extended attributes are unsupported for {display_path!r}: {sorted(attributes)!r}"
        )


def _reject_symlink_xattrs(parent_fd: int, name: str, display_path: str) -> None:
    symlink_fd = _open_symlink(parent_fd, name)
    if symlink_fd is not None:
        try:
            try:
                _reject_xattrs(symlink_fd, display_path)
                return
            except CaptureRejected as error:
                if not sys.platform.startswith("linux") or "cannot inspect xattrs" not in str(error):
                    raise
        finally:
            os.close(symlink_fd)

    # Linux O_PATH descriptors cannot be passed to flistxattr.  llistxattr on
    # this procfs path is still anchored by the already-open parent descriptor,
    # and it does not follow the final symlink.
    if sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        proc_path = f"/proc/self/fd/{parent_fd}/{name}"
        try:
            if hasattr(os, "listxattr"):
                attributes = set(os.listxattr(proc_path, follow_symlinks=False))  # type: ignore[attr-defined]
            else:
                libc = ctypes.CDLL(None, use_errno=True)
                function = libc.llistxattr
                function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
                function.restype = ctypes.c_ssize_t
                encoded = os.fsencode(proc_path)
                size = function(encoded, None, 0)
                if size < 0:
                    error_number = ctypes.get_errno()
                    raise OSError(error_number, os.strerror(error_number))
                buffer = ctypes.create_string_buffer(size) if size else None
                written = function(encoded, buffer, size) if size else 0
                if written < 0:
                    error_number = ctypes.get_errno()
                    raise OSError(error_number, os.strerror(error_number))
                attributes = {
                    item.decode("utf-8", errors="surrogateescape")
                    for item in bytes(buffer.raw[:written]).split(b"\x00")
                    if item
                }
        except OSError as error:
            raise CaptureRejected(
                f"cannot inspect xattrs for symlink {display_path!r}: {error}"
            ) from error
        if attributes:
            raise CaptureRejected(
                f"extended attributes are unsupported for {display_path!r}: {sorted(attributes)!r}"
            )
        return
    raise CaptureRejected(f"this platform cannot inspect symlink xattrs: {display_path!r}")


def _stat_kind(info: os.stat_result, display_path: str) -> str:
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        if _is_sparse(info):
            raise CaptureRejected(f"sparse file semantics are unsupported: {display_path!r}")
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise CaptureRejected(f"special filesystem node is unsupported: {display_path!r}")


def _inventory(root_fd: int) -> _Inventory:
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise CaptureRejected("capture root is not a directory")
    _reject_xattrs(root_fd, ".")
    entries: List[_InventoryEntry] = []

    def visit(directory_fd: int, raw_prefix: str) -> None:
        names = os.listdir(directory_fd)
        for name in names:
            raw_path = f"{raw_prefix}/{name}" if raw_prefix else name
            canonical_path = canonical_relative_path(raw_path)
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            kind = _stat_kind(before, raw_path)
            target: Optional[str] = None
            if kind == "directory":
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    if _identity(os.fstat(child_fd)) != _identity(before):
                        raise SourceChanged(f"directory changed while opening: {raw_path!r}")
                    _reject_xattrs(child_fd, raw_path)
                    entries.append(
                        _InventoryEntry(raw_path, canonical_path, kind, _identity(before))
                    )
                    visit(child_fd, raw_path)
                finally:
                    os.close(child_fd)
            elif kind == "file":
                file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                try:
                    if _identity(os.fstat(file_fd)) != _identity(before):
                        raise SourceChanged(f"file changed while opening: {raw_path!r}")
                    _reject_xattrs(file_fd, raw_path)
                finally:
                    os.close(file_fd)
                entries.append(
                    _InventoryEntry(raw_path, canonical_path, kind, _identity(before))
                )
            else:
                target = os.readlink(name, dir_fd=directory_fd)
                canonical_symlink_target(target)
                symlink_fd = _open_symlink(directory_fd, name)
                if symlink_fd is not None:
                    try:
                        if _identity(os.fstat(symlink_fd)) != _identity(before):
                            raise SourceChanged(f"symlink changed while opening: {raw_path!r}")
                    finally:
                        os.close(symlink_fd)
                _reject_symlink_xattrs(directory_fd, name, raw_path)
                entries.append(
                    _InventoryEntry(
                        raw_path,
                        canonical_path,
                        kind,
                        _identity(before),
                        symlink_target=target,
                    )
                )

    visit(root_fd, "")
    validate_canonical_paths([entry.raw_path for entry in entries])
    entries.sort(key=lambda entry: entry.canonical_path.encode("utf-8"))
    return _Inventory(_identity(root_stat), tuple(entries))


def _require_same_identity(
    actual: os.stat_result,
    expected: _Identity,
    display_path: str,
) -> None:
    if _identity(actual) != expected:
        raise SourceChanged(f"source identity changed during capture: {display_path!r}")


def _hash_fd(fd: int) -> Tuple[str, int]:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


class FileProvider:
    """Private-file materialization strategy used by the spike."""

    name = "abstract"

    def validate(self, source_root_fd: int, staging_root: Path) -> None:
        raise NotImplementedError

    def copy_file(
        self,
        source_fd: int,
        destination_directory_fd: int,
        destination_name: str,
    ) -> Tuple[str, int]:
        raise NotImplementedError


class VerifiedCopyProvider(FileProvider):
    """Always-available provider that writes and re-reads private bytes."""

    name = "verified-copy"

    def validate(self, source_root_fd: int, staging_root: Path) -> None:
        del source_root_fd, staging_root

    def copy_file(
        self,
        source_fd: int,
        destination_directory_fd: int,
        destination_name: str,
    ) -> Tuple[str, int]:
        destination_fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=destination_directory_fd,
        )
        source_digest = hashlib.sha256()
        source_size = 0
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                source_digest.update(chunk)
                source_size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)

        verification_fd = os.open(
            destination_name,
            _FILE_FLAGS,
            dir_fd=destination_directory_fd,
        )
        try:
            copied_digest, copied_size = _hash_fd(verification_fd)
        finally:
            os.close(verification_fd)
        if copied_digest != source_digest.hexdigest() or copied_size != source_size:
            raise CorruptObject(f"verified copy mismatch for {destination_name!r}")
        return copied_digest, copied_size


def _filesystem_type(path: Path) -> Optional[str]:
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["/sbin/mount"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout
            device = os.stat(path).st_dev
            for line in output.splitlines():
                if " on " not in line or " (" not in line:
                    continue
                remainder = line.split(" on ", 1)[1]
                mountpoint, options = remainder.rsplit(" (", 1)
                try:
                    if os.stat(mountpoint).st_dev == device:
                        return options.split(",", 1)[0].rstrip(")").lower()
                except OSError:
                    continue
        except (OSError, subprocess.SubprocessError):
            return None
        return None
    try:
        return subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return None


def apfs_clone_supported(source_root: Path, store_root: Path) -> bool:
    """Return whether strict same-device APFS clone capture can be attempted."""

    source_root = source_root.resolve()
    store_root = store_root.resolve()
    return (
        sys.platform == "darwin"
        and os.stat(source_root).st_dev == os.stat(store_root).st_dev
        and _filesystem_type(source_root) == "apfs"
        and _filesystem_type(store_root) == "apfs"
        and hasattr(ctypes.CDLL(None), "fclonefileat")
    )


def _apfs_clone_supported_fd(source_root_fd: int, store_root: Path) -> bool:
    store_root = store_root.resolve()
    return (
        sys.platform == "darwin"
        and os.fstat(source_root_fd).st_dev == os.stat(store_root).st_dev
        and _filesystem_type(store_root) == "apfs"
        and hasattr(ctypes.CDLL(None), "fclonefileat")
    )


class APFSCloneProvider(FileProvider):
    """Strict APFS CoW clone provider; this class never copies as fallback."""

    name = "apfs-fclonefileat"

    def validate(self, source_root_fd: int, staging_root: Path) -> None:
        if not _apfs_clone_supported_fd(source_root_fd, staging_root):
            raise UnsupportedProvider(
                "APFS fclonefileat requires source and store on the same APFS device"
            )

    def copy_file(
        self,
        source_fd: int,
        destination_directory_fd: int,
        destination_name: str,
    ) -> Tuple[str, int]:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "fclonefileat", None)
        if function is None:
            raise UnsupportedProvider("fclonefileat is unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        function.restype = ctypes.c_int
        encoded_name = os.fsencode(destination_name)
        if function(source_fd, destination_directory_fd, encoded_name, 0) != 0:
            error = ctypes.get_errno()
            raise UnsupportedProvider(
                f"fclonefileat failed without fallback: [{error}] {os.strerror(error)}"
            )
        destination_fd = os.open(
            destination_name,
            _FILE_FLAGS,
            dir_fd=destination_directory_fd,
        )
        try:
            digest, size = _hash_fd(destination_fd)
        finally:
            os.close(destination_fd)
        return digest, size


def _entry_map(inventory: _Inventory) -> Mapping[str, _InventoryEntry]:
    return {entry.raw_path: entry for entry in inventory.entries}


def _copy_tree(
    root_fd: int,
    destination_root: Path,
    inventory: _Inventory,
    provider: FileProvider,
) -> JsonDict:
    expected_entries = _entry_map(inventory)
    manifest_entries: List[JsonDict] = []

    def visit(source_directory_fd: int, destination_directory: Path, raw_prefix: str) -> None:
        destination_fd = os.open(destination_directory, _DIRECTORY_FLAGS)
        try:
            for name in os.listdir(source_directory_fd):
                raw_path = f"{raw_prefix}/{name}" if raw_prefix else name
                expected = expected_entries.get(raw_path)
                if expected is None:
                    raise SourceChanged(f"new source entry appeared: {raw_path!r}")
                canonical_name = unicodedata.normalize("NFC", name)
                actual = os.stat(name, dir_fd=source_directory_fd, follow_symlinks=False)
                _require_same_identity(actual, expected.identity, raw_path)
                if expected.kind == "directory":
                    os.mkdir(canonical_name, 0o700, dir_fd=destination_fd)
                    child_source_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_directory_fd)
                    try:
                        _require_same_identity(os.fstat(child_source_fd), expected.identity, raw_path)
                        _reject_xattrs(child_source_fd, raw_path)
                        child_destination = destination_directory / canonical_name
                        manifest_entries.append(
                            {"path": expected.canonical_path, "type": "directory"}
                        )
                        visit(child_source_fd, child_destination, raw_path)
                    finally:
                        os.close(child_source_fd)
                    os.chmod(child_destination, 0o555, follow_symlinks=False)
                    os.utime(child_destination, ns=(0, 0), follow_symlinks=False)
                    _fsync_directory(child_destination)
                elif expected.kind == "file":
                    source_fd = os.open(name, _FILE_FLAGS, dir_fd=source_directory_fd)
                    try:
                        _require_same_identity(os.fstat(source_fd), expected.identity, raw_path)
                        _reject_xattrs(source_fd, raw_path)
                        digest, size = provider.copy_file(
                            source_fd,
                            destination_fd,
                            canonical_name,
                        )
                        _require_same_identity(os.fstat(source_fd), expected.identity, raw_path)
                    finally:
                        os.close(source_fd)
                    if size != expected.identity.size:
                        raise SourceChanged(f"source size changed during capture: {raw_path!r}")
                    destination_file = destination_directory / canonical_name
                    executable = bool(expected.identity.mode & 0o111)
                    os.chmod(destination_file, 0o555 if executable else 0o444)
                    os.utime(destination_file, ns=(0, 0), follow_symlinks=False)
                    _fsync_file(destination_file)
                    manifest_entries.append(
                        {
                            "digest": f"sha256:{digest}",
                            "executable": executable,
                            "path": expected.canonical_path,
                            "size": size,
                            "type": "file",
                        }
                    )
                else:
                    target = os.readlink(name, dir_fd=source_directory_fd)
                    if target != expected.symlink_target:
                        raise SourceChanged(f"symlink changed during capture: {raw_path!r}")
                    canonical_target = canonical_symlink_target(target)
                    os.symlink(canonical_target, canonical_name, dir_fd=destination_fd)
                    os.utime(
                        destination_directory / canonical_name,
                        ns=(0, 0),
                        follow_symlinks=False,
                    )
                    manifest_entries.append(
                        {
                            "path": expected.canonical_path,
                            "target": canonical_target,
                            "type": "symlink",
                        }
                    )
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)

    visit(root_fd, destination_root, "")
    if len(manifest_entries) != len(inventory.entries):
        raise SourceChanged("source entry set changed during capture")
    manifest_entries.sort(key=lambda item: item["path"].encode("utf-8"))
    os.chmod(destination_root, 0o555)
    os.utime(destination_root, ns=(0, 0), follow_symlinks=False)
    _fsync_directory(destination_root)
    return {"entries": manifest_entries, "format": "optpilot-tree-spike-v1"}


def _canonical_manifest_bytes(manifest: JsonDict) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _tree_ref(manifest_bytes: bytes) -> str:
    return f"tree-sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"


def _manifest_from_tree(tree_path: Path) -> JsonDict:
    root_fd = os.open(tree_path, _DIRECTORY_FLAGS)
    try:
        inventory = _inventory(root_fd)
        entries: List[JsonDict] = []
        for entry in inventory.entries:
            if entry.kind == "directory":
                entries.append({"path": entry.canonical_path, "type": "directory"})
                continue
            parent_parts = entry.raw_path.split("/")
            directory_fd = os.dup(root_fd)
            try:
                for component in parent_parts[:-1]:
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd
                name = parent_parts[-1]
                if entry.kind == "file":
                    file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                    try:
                        _require_same_identity(os.fstat(file_fd), entry.identity, entry.raw_path)
                        digest, size = _hash_fd(file_fd)
                        _require_same_identity(os.fstat(file_fd), entry.identity, entry.raw_path)
                    finally:
                        os.close(file_fd)
                    entries.append(
                        {
                            "digest": f"sha256:{digest}",
                            "executable": bool(entry.identity.mode & 0o111),
                            "path": entry.canonical_path,
                            "size": size,
                            "type": "file",
                        }
                    )
                else:
                    target = os.readlink(name, dir_fd=directory_fd)
                    entries.append(
                        {
                            "path": entry.canonical_path,
                            "target": canonical_symlink_target(target),
                            "type": "symlink",
                        }
                    )
            finally:
                os.close(directory_fd)
        entries.sort(key=lambda item: item["path"].encode("utf-8"))
        return {"entries": entries, "format": "optpilot-tree-spike-v1"}
    finally:
        os.close(root_fd)


def _safe_identifier(value: str, label: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise SnapshotSpikeError(f"unsafe {label}: {value!r}")
    return value


def _write_bytes_exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_exclusive(path: Path, payload: JsonDict) -> None:
    _write_bytes_exclusive(path, _canonical_manifest_bytes(payload))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, _DIRECTORY_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, _FILE_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_tree_removable(path: Path) -> None:
    for directory, child_directories, _files in os.walk(path, topdown=False, followlinks=False):
        for child in child_directories:
            child_path = Path(directory) / child
            if not child_path.is_symlink():
                os.chmod(child_path, 0o700)
        os.chmod(directory, 0o700)


class TreeObjectStoreSpike:
    """Tiny object store proving atomic seal plus provisional protection."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.objects = self.root / "objects"
        self.staging = self.root / "staging"
        self.trash = self.root / "trash"
        self.fault_hook: Optional[FaultHook] = None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.objects.mkdir(exist_ok=True, mode=0o700)
        self.staging.mkdir(exist_ok=True, mode=0o700)
        self.trash.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.objects, 0o700)
        os.chmod(self.staging, 0o700)
        os.chmod(self.trash, 0o700)
        self.lock_path = self.root / ".store.lock"
        self.lock_path.touch(mode=0o600, exist_ok=True)

    def _fault(self, step: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(step)

    @contextlib.contextmanager
    def _lock(self, *, blocked_step: Optional[str] = None) -> Iterator[None]:
        fd = os.open(self.lock_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        try:
            if blocked_step is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._fault(blocked_step)
                    fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _object_path(self, tree_ref: str) -> Path:
        prefix = "tree-sha256:"
        if not tree_ref.startswith(prefix):
            raise SnapshotSpikeError(f"invalid tree ref: {tree_ref!r}")
        digest = tree_ref[len(prefix) :]
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise SnapshotSpikeError(f"invalid tree ref: {tree_ref!r}")
        return self.objects / digest

    def capture(
        self,
        allowed_root: Path,
        selection: str,
        *,
        provider: Optional[FileProvider] = None,
    ) -> CaptureReceipt:
        """Capture a canonical relative selection beneath a trusted policy root.

        ``allowed_root`` is the caller-established trust anchor.  Every
        component of ``selection`` is opened relative to its directory
        descriptor with ``O_DIRECTORY | O_NOFOLLOW``; the composed absolute
        path is never opened as the source.
        """

        provider = provider or VerifiedCopyProvider()
        root_fd = _open_selected_directory(allowed_root, selection)

        capture_id = uuid.uuid4().hex
        token = f"ret-{uuid.uuid4().hex}"
        capture_root = self.staging / capture_id
        object_stage = capture_root / "object"
        tree_stage = object_stage / "tree"
        provisional_stage = object_stage / "meta" / "provisional"
        owners_stage = object_stage / "meta" / "owners"
        try:
            capture_root.mkdir(mode=0o700)
            object_stage.mkdir(mode=0o700)
            tree_stage.mkdir(mode=0o700)
            provisional_stage.mkdir(parents=True, mode=0o700)
            owners_stage.mkdir(mode=0o700)

            provider.validate(root_fd, capture_root)
            root_identity = _identity(os.fstat(root_fd))
            try:
                before = _inventory(root_fd)
                manifest = _copy_tree(root_fd, tree_stage, before, provider)
                after = _inventory(root_fd)
                if after != before:
                    raise SourceChanged("whole-tree inventory changed during capture")
                _require_same_identity(os.fstat(root_fd), root_identity, ".")
            except OSError as error:
                raise SourceChanged(f"selected source became unreadable: {error}") from error

            verified_manifest = _manifest_from_tree(tree_stage)
            if verified_manifest != manifest:
                raise CorruptObject("private staged tree does not match capture manifest")
            manifest_bytes = _canonical_manifest_bytes(manifest)
            tree_ref = _tree_ref(manifest_bytes)
            manifest_path = object_stage / "manifest.json"
            _write_bytes_exclusive(manifest_path, manifest_bytes, mode=0o400)
            os.utime(manifest_path, ns=(0, 0), follow_symlinks=False)
            _fsync_file(manifest_path)
            _write_json_exclusive(
                provisional_stage / f"{token}.json",
                {"retention_token": token, "tree_ref": tree_ref},
            )
            _fsync_directory(provisional_stage)
            _fsync_directory(owners_stage)
            _fsync_directory(object_stage / "meta")
            _fsync_directory(tree_stage)
            _fsync_directory(object_stage)
            _fsync_directory(capture_root)
            _fsync_directory(self.staging)
            self._fault("after_staging_durable_before_publish")

            destination = self._object_path(tree_ref)
            with self._lock():
                self._fault("capture_publish_lock_acquired")
                if destination.exists():
                    self.verify_object(tree_ref)
                    destination_manifest = (destination / "manifest.json").read_bytes()
                    if destination_manifest != manifest_bytes:
                        raise CorruptObject("tree-ref collision with a different manifest")
                    token_path = destination / "meta" / "provisional" / f"{token}.json"
                    _write_json_exclusive(
                        token_path,
                        {"retention_token": token, "tree_ref": tree_ref},
                    )
                    _fsync_directory(token_path.parent)
                else:
                    os.rename(object_stage, destination)
                    _fsync_directory(self.objects)
            return CaptureReceipt(tree_ref, token, provider.name)
        finally:
            os.close(root_fd)
            shutil.rmtree(capture_root, ignore_errors=True)

    def manifest_bytes(self, tree_ref: str) -> bytes:
        return (self._object_path(tree_ref) / "manifest.json").read_bytes()

    def manifest(self, tree_ref: str) -> JsonDict:
        return json.loads(self.manifest_bytes(tree_ref))

    def tree_path(self, tree_ref: str) -> Path:
        path = self._object_path(tree_ref) / "tree"
        if not path.is_dir():
            raise CorruptObject(f"missing tree for {tree_ref}")
        return path

    def verify_object(self, tree_ref: str) -> None:
        object_path = self._object_path(tree_ref)
        try:
            manifest_bytes = (object_path / "manifest.json").read_bytes()
            recorded = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise CorruptObject(f"cannot read manifest for {tree_ref}: {error}") from error
        if _canonical_manifest_bytes(recorded) != manifest_bytes:
            raise CorruptObject(f"manifest is not canonical for {tree_ref}")
        if _tree_ref(manifest_bytes) != tree_ref:
            raise CorruptObject(f"manifest digest mismatch for {tree_ref}")
        actual = _manifest_from_tree(object_path / "tree")
        if actual != recorded:
            raise CorruptObject(f"sealed tree bytes differ from manifest for {tree_ref}")

    def sealed_refs(self) -> List[str]:
        refs = []
        for path in self.objects.iterdir():
            if path.is_dir() and len(path.name) == 64:
                refs.append(f"tree-sha256:{path.name}")
        return sorted(refs)

    def _protection_state(self, tree_ref: str) -> Tuple[List[Path], List[Path]]:
        metadata = self._object_path(tree_ref) / "meta"
        provisional = list((metadata / "provisional").glob("*.json"))
        owners = list((metadata / "owners").glob("*.json"))
        return provisional, owners

    def is_protected(self, tree_ref: str) -> bool:
        provisional, owners = self._protection_state(tree_ref)
        return bool(provisional or owners)

    def gc_eligible_refs(self) -> List[str]:
        """Return an advisory snapshot; callers must use :meth:`collect`."""

        with self._lock():
            return [tree_ref for tree_ref in self.sealed_refs() if not self.is_protected(tree_ref)]

    def collect(self, tree_ref: str) -> bool:
        """Atomically tombstone and delete an object if still unprotected.

        Eligibility is rechecked while holding the same store lock used by
        capture and adoption.  The rename removes the object from the live
        namespace atomically; tombstone cleanup remains under that lock too.
        """

        object_path = self._object_path(tree_ref)
        with self._lock(blocked_step="collect_lock_blocked"):
            self._fault("collect_lock_acquired")
            if not object_path.exists():
                return False
            if self.is_protected(tree_ref):
                return False
            tombstone = self.trash / f"{object_path.name}-{uuid.uuid4().hex}"
            os.rename(object_path, tombstone)
            _fsync_directory(self.objects)
            _fsync_directory(self.trash)
            self._fault("collect_tombstone_durable")
            _make_tree_removable(tombstone)
            shutil.rmtree(tombstone)
            _fsync_directory(self.trash)
            return True

    def release(self, tree_ref: str, retention_token: str) -> None:
        token = _safe_identifier(retention_token, "retention token")
        token_path = self._object_path(tree_ref) / "meta" / "provisional" / f"{token}.json"
        with self._lock():
            try:
                token_path.unlink()
            except FileNotFoundError as error:
                raise SnapshotSpikeError("unknown provisional retention token") from error
            _fsync_directory(token_path.parent)

    def adopt(self, tree_ref: str, retention_token: str, *, owner_id: str) -> None:
        """Protect by owner before removing the matching provisional token."""

        token = _safe_identifier(retention_token, "retention token")
        owner = _safe_identifier(owner_id, "owner id")
        object_path = self._object_path(tree_ref)
        token_path = object_path / "meta" / "provisional" / f"{token}.json"
        owner_path = object_path / "meta" / "owners" / f"{owner}.json"
        with self._lock():
            if not token_path.is_file():
                raise SnapshotSpikeError("unknown provisional retention token")
            if not owner_path.exists():
                _write_json_exclusive(
                    owner_path,
                    {"owner_id": owner_id, "tree_ref": tree_ref},
                )
                _fsync_directory(owner_path.parent)
            self._fault("adopt_owner_durable")
            token_path.unlink()
            _fsync_directory(token_path.parent)
            self._fault("adopt_transition_complete")


__all__ = [
    "APFSCloneProvider",
    "CaptureReceipt",
    "CaptureRejected",
    "CorruptObject",
    "SnapshotSpikeError",
    "SourceChanged",
    "TreeObjectStoreSpike",
    "UnsupportedProvider",
    "VerifiedCopyProvider",
    "apfs_clone_supported",
    "canonical_relative_path",
    "canonical_symlink_target",
    "validate_canonical_paths",
]
