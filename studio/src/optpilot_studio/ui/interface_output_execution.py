"""Isolated execution of immutable interface-output trees.

This module is intentionally independent of Studio's HTTP routes and of any
domain-specific interface.  The caller resolves an interface output to an
immutable, supervisor-owned projection and supplies the originating launch's
prepared-runtime lease.  The executor starts one short-lived sibling
container from the exact prepared-runtime image, never a process inside Studio
or inside the live interface container.

Commands are trusted profile data.  Generated output records and execution
requests may select an authored action and provide bounded arguments, but they
cannot supply a command, environment, image, mount, or network policy.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from optpilot.prepared_runtime_cache import PreparedRuntimeLease
from optpilot.realm.errors import ContentRejected
from optpilot.realm.manifests import SealLimits, TreeEntry, TreeManifest, validate_portable_path
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_closure import InterfaceOutputActionSpec


OUTPUT_EXECUTION_REQUEST_SCHEMA = "optpilot.interface-output-execution-request.v1"
OUTPUT_EXECUTION_RESULT_SCHEMA = "optpilot.interface-output-execution-result.v1"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_IDENTIFIER_BYTES = 128
_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_BYTES = 4096
_MAX_ARGUMENT_VECTOR_BYTES = 32 * 1024
_MAX_CAPTURE_BYTES = 64 * 1024
_CONTAINER_OUTPUT_ROOT = PurePosixPath("/optpilot/output")
_CONTAINER_RESULT_ROOT = PurePosixPath("/optpilot/results")


class InterfaceOutputExecutionError(RuntimeError):
    """Base class for launch-owned output execution errors."""


class InterfaceOutputExecutionUnavailable(InterfaceOutputExecutionError):
    """The isolated runtime cannot be established or proved cleanly retired."""


class InterfaceOutputExecutionRejected(InterfaceOutputExecutionError):
    """Untrusted request or source data did not satisfy the bounded contract."""


@dataclass(frozen=True)
class InterfaceOutputSnapshotLimits:
    """Hard bounds for an execution-local copy of one live interface output."""

    max_entries: int = 10_000
    max_depth: int = 48
    max_total_bytes: int = 512 * 1024**2
    max_file_bytes: int = 256 * 1024**2
    max_path_bytes: int = 4096
    max_component_bytes: int = 255

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_depth",
            "max_total_bytes",
            "max_file_bytes",
            "max_path_bytes",
            "max_component_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")

    def as_seal_limits(self) -> SealLimits:
        return SealLimits(
            max_entries=self.max_entries,
            max_depth=self.max_depth,
            max_total_bytes=self.max_total_bytes,
            max_file_bytes=self.max_file_bytes,
            max_path_bytes=self.max_path_bytes,
            max_component_bytes=self.max_component_bytes,
        )


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
        or _IDENTIFIER_RE.fullmatch(value) is None
    ):
        raise InterfaceOutputExecutionRejected(
            f"{label} must be a bounded identifier."
        )
    return value


def _portable_path(value: Any, label: str, *, allow_root: bool) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise InterfaceOutputExecutionRejected(
            f"{label} must be a canonical relative path."
        )
    if "\\" in value or "\x00" in value:
        raise InterfaceOutputExecutionRejected(
            f"{label} must be a canonical relative path."
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise InterfaceOutputExecutionRejected(
            f"{label} must be a canonical relative path."
        )
    normalized = path.as_posix()
    if normalized == "." and not allow_root:
        raise InterfaceOutputExecutionRejected(f"{label} must not select the root.")
    if normalized != value:
        raise InterfaceOutputExecutionRejected(
            f"{label} must be a canonical relative path."
        )
    if len(value.encode("utf-8")) > 4096:
        raise InterfaceOutputExecutionRejected(f"{label} is too long.")
    return normalized


def _bounded_tokens(
    values: Any,
    label: str,
    *,
    max_items: int,
    max_token_bytes: int,
    max_total_bytes: int | None = None,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise InterfaceOutputExecutionRejected(f"{label} must be a sequence.")
    if not values or len(values) > max_items:
        raise InterfaceOutputExecutionRejected(
            f"{label} must contain between 1 and {max_items} values."
        )
    result: list[str] = []
    total = 0
    for index, raw in enumerate(values):
        if not isinstance(raw, str) or "\x00" in raw:
            raise InterfaceOutputExecutionRejected(
                f"{label}[{index}] must be text without NUL bytes."
            )
        encoded = raw.encode("utf-8")
        if len(encoded) > max_token_bytes:
            raise InterfaceOutputExecutionRejected(
                f"{label}[{index}] exceeds its size limit."
            )
        total += len(encoded)
        result.append(raw)
    if max_total_bytes is not None and total > max_total_bytes:
        raise InterfaceOutputExecutionRejected(
            f"{label} exceeds its total encoded size limit."
        )
    return tuple(result)


@dataclass(frozen=True)
class InterfaceOutputExecutionRequest:
    """Untrusted selection of one pre-authorized action and immutable output."""

    request_id: str
    action_id: str
    output_path: str
    arguments: tuple[str, ...] = ()
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _identifier(self.request_id, "execution request id")
        )
        object.__setattr__(
            self, "action_id", _identifier(self.action_id, "execution action id")
        )
        object.__setattr__(
            self,
            "output_path",
            _portable_path(
                self.output_path,
                "execution output path",
                allow_root=True,
            ),
        )
        if not isinstance(self.arguments, (list, tuple)):
            raise InterfaceOutputExecutionRejected(
                "execution arguments must be a sequence."
            )
        arguments = tuple(self.arguments)
        if arguments:
            arguments = _bounded_tokens(
                arguments,
                "execution arguments",
                max_items=_MAX_ARGUMENTS,
                max_token_bytes=_MAX_ARGUMENT_BYTES,
                max_total_bytes=_MAX_ARGUMENT_VECTOR_BYTES,
            )
        object.__setattr__(self, "arguments", arguments)
        timeout_seconds = self.timeout_seconds
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise InterfaceOutputExecutionRejected(
                "execution timeout_seconds must be a positive integer or null."
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterfaceOutputExecutionRequest":
        expected = {
            "schema_version",
            "request_id",
            "action_id",
            "output_path",
            "arguments",
            "timeout_seconds",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InterfaceOutputExecutionRejected(
                "execution request fields are not canonical."
            )
        if value["schema_version"] != OUTPUT_EXECUTION_REQUEST_SCHEMA:
            raise InterfaceOutputExecutionRejected(
                "execution request schema is unsupported."
            )
        arguments = value["arguments"]
        if not isinstance(arguments, (list, tuple)):
            raise InterfaceOutputExecutionRejected(
                "execution arguments must be a sequence."
            )
        return cls(
            request_id=value["request_id"],
            action_id=value["action_id"],
            output_path=value["output_path"],
            arguments=tuple(arguments),
            timeout_seconds=value["timeout_seconds"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OUTPUT_EXECUTION_REQUEST_SCHEMA,
            "request_id": self.request_id,
            "action_id": self.action_id,
            "output_path": self.output_path,
            "arguments": list(self.arguments),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class ImmutableInterfaceOutputTree:
    """Supervisor-attested projection of one exact retained output tree."""

    snapshot_ref: SnapshotRef
    root_path: Path
    owned_root: Path | None = field(default=None, repr=False, compare=False)
    device: int = field(init=False, repr=False)
    inode: int = field(init=False, repr=False)
    owned_device: int | None = field(init=False, repr=False, compare=False)
    owned_inode: int | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("snapshot_ref must be a SnapshotRef.")
        root = Path(os.path.abspath(Path(self.root_path).expanduser()))
        try:
            linked = root.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Immutable output projection is unavailable."
            ) from error
        if root.is_symlink() or not stat.S_ISDIR(linked.st_mode):
            raise InterfaceOutputExecutionUnavailable(
                "Immutable output projection must be a real directory."
            )
        object.__setattr__(self, "root_path", root.resolve())
        object.__setattr__(self, "device", int(linked.st_dev))
        object.__setattr__(self, "inode", int(linked.st_ino))
        owned = self.owned_root
        if owned is None:
            object.__setattr__(self, "owned_device", None)
            object.__setattr__(self, "owned_inode", None)
            return
        owned_path = Path(os.path.abspath(Path(owned).expanduser()))
        try:
            owned_info = owned_path.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Owned immutable output projection is unavailable."
            ) from error
        if (
            owned_path.is_symlink()
            or not stat.S_ISDIR(owned_info.st_mode)
            or root.resolve() != owned_path.resolve()
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Owned immutable output projection is not self-contained."
            )
        object.__setattr__(self, "owned_root", owned_path.resolve())
        object.__setattr__(self, "owned_device", int(owned_info.st_dev))
        object.__setattr__(self, "owned_inode", int(owned_info.st_ino))

    def assert_attached(self) -> None:
        try:
            linked = self.root_path.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Immutable output projection is no longer available."
            ) from error
        if (
            self.root_path.is_symlink()
            or not stat.S_ISDIR(linked.st_mode)
            or linked.st_dev != self.device
            or linked.st_ino != self.inode
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Immutable output projection identity changed."
            )

    def cleanup(self) -> None:
        """Retire an execution-owned projection; borrowed projections are untouched."""

        owned = self.owned_root
        if owned is None:
            return
        try:
            linked = owned.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Owned immutable output projection could not be inspected."
            ) from error
        if (
            owned.is_symlink()
            or not stat.S_ISDIR(linked.st_mode)
            or linked.st_dev != self.owned_device
            or linked.st_ino != self.owned_inode
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Owned immutable output projection identity changed."
            )
        _remove_private_tree(owned)

    def __enter__(self) -> "ImmutableInterfaceOutputTree":
        self.assert_attached()
        return self

    def __exit__(self, *_unused: object) -> None:
        self.cleanup()


@dataclass(frozen=True)
class _SourceNode:
    relative_path: str
    kind: str
    identity: tuple[int, int, int, int, int, int]


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _directory_flags() -> int:
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise InterfaceOutputExecutionUnavailable(
            "This platform lacks secure descriptor-rooted snapshot support."
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    if not getattr(os, "O_NOFOLLOW", 0):
        raise InterfaceOutputExecutionUnavailable(
            "This platform lacks secure no-follow file support."
        )
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_absolute_directory_nofollow(path: Path, *, create: bool) -> int:
    """Walk one canonical absolute directory without following components."""

    if not path.is_absolute():
        raise InterfaceOutputExecutionUnavailable(
            "Private publication directory must be absolute."
        )
    current_fd = os.open(Path(path.anchor), _directory_flags())
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _bind_real_directory(path: Path, *, create: bool) -> tuple[int, Path, tuple[int, int]]:
    """Bind a real directory path to one descriptor and stable outer identity."""

    original = Path(os.path.abspath(Path(path).expanduser()))
    try:
        original_link = original.lstat()
    except FileNotFoundError:
        if not create:
            raise InterfaceOutputExecutionUnavailable(
                "Private publication directory is unavailable."
            )
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private publication directory could not be inspected."
        ) from error
    else:
        if original.is_symlink() or not stat.S_ISDIR(original_link.st_mode):
            raise InterfaceOutputExecutionUnavailable(
                "Private publication directory must be a real directory."
            )
    canonical = original.resolve(strict=False)
    try:
        descriptor = _open_absolute_directory_nofollow(canonical, create=create)
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private publication directory could not be opened safely."
        ) from error
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise InterfaceOutputExecutionUnavailable(
            "Private publication directory is not a directory."
        )
    return descriptor, canonical, (int(info.st_dev), int(info.st_ino))


def _require_directory_binding(
    canonical_path: Path,
    identity: tuple[int, int],
) -> None:
    try:
        descriptor = _open_absolute_directory_nofollow(
            canonical_path,
            create=False,
        )
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private publication directory identity is no longer reachable."
        ) from error
    try:
        info = os.fstat(descriptor)
        if (int(info.st_dev), int(info.st_ino)) != identity:
            raise InterfaceOutputExecutionUnavailable(
                "Private publication directory identity changed."
            )
    finally:
        os.close(descriptor)


def _publication_leaf(value: str) -> str:
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > 255
    ):
        raise InterfaceOutputExecutionRejected(
            "Publication target must have one bounded file name."
        )
    return value


def _require_directory_fd(
    parent_fd: int,
    expected_identity: tuple[int, int] | None,
) -> tuple[int, int]:
    if isinstance(parent_fd, bool) or not isinstance(parent_fd, int) or parent_fd < 0:
        raise TypeError("parent_fd must be an open directory descriptor.")
    try:
        info = os.fstat(parent_fd)
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Publication directory descriptor is unavailable."
        ) from error
    if not stat.S_ISDIR(info.st_mode):
        raise InterfaceOutputExecutionUnavailable(
            "Publication descriptor does not name a directory."
        )
    identity = (int(info.st_dev), int(info.st_ino))
    if expected_identity is not None:
        if (
            not isinstance(expected_identity, tuple)
            or len(expected_identity) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in expected_identity
            )
        ):
            raise TypeError("expected_parent_identity must be a (device, inode) tuple.")
        if identity != expected_identity:
            raise InterfaceOutputExecutionUnavailable(
                "Publication directory descriptor identity changed."
            )
    return identity


def _scan_source_tree(
    root_fd: int,
    *,
    limits: InterfaceOutputSnapshotLimits,
) -> tuple[_SourceNode, ...]:
    nodes: list[_SourceNode] = []
    total_bytes = 0
    seal_limits = limits.as_seal_limits()

    def scan(directory_fd: int, parts: tuple[str, ...]) -> None:
        nonlocal total_bytes
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise InterfaceOutputExecutionRejected(
                "Interface output changed from a directory during snapshot."
            )
        try:
            names_before = tuple(
                sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8"))
            )
        except (OSError, UnicodeError) as error:
            raise InterfaceOutputExecutionRejected(
                "Interface output contains an unreadable directory."
            ) from error
        for name in names_before:
            relative = "/".join((*parts, name))
            try:
                validate_portable_path(relative, limits=seal_limits)
                linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except (ContentRejected, OSError, UnicodeError) as error:
                raise InterfaceOutputExecutionRejected(
                    "Interface output contains an unsafe or non-portable path."
                ) from error
            identity = _file_identity(linked)
            if stat.S_ISLNK(linked.st_mode):
                raise InterfaceOutputExecutionRejected(
                    "Interface output snapshots do not accept symbolic links."
                )
            if stat.S_ISDIR(linked.st_mode):
                nodes.append(_SourceNode(relative, "directory", identity))
                if len(nodes) > limits.max_entries:
                    raise InterfaceOutputExecutionRejected(
                        "Interface output exceeds the snapshot entry limit."
                    )
                try:
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                except OSError as error:
                    raise InterfaceOutputExecutionRejected(
                        "Interface output directory changed during snapshot."
                    ) from error
                try:
                    if _file_identity(os.fstat(child_fd)) != identity:
                        raise InterfaceOutputExecutionRejected(
                            "Interface output directory changed during snapshot."
                        )
                    scan(child_fd, (*parts, name))
                    if _file_identity(os.fstat(child_fd)) != identity:
                        raise InterfaceOutputExecutionRejected(
                            "Interface output directory changed during snapshot."
                        )
                finally:
                    os.close(child_fd)
                try:
                    after_child = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError as error:
                    raise InterfaceOutputExecutionRejected(
                        "Interface output directory changed during snapshot."
                    ) from error
                if _file_identity(after_child) != identity:
                    raise InterfaceOutputExecutionRejected(
                        "Interface output directory changed during snapshot."
                    )
                continue
            if not stat.S_ISREG(linked.st_mode):
                raise InterfaceOutputExecutionRejected(
                    "Interface output snapshots accept only directories and regular files."
                )
            size = int(linked.st_size)
            if size > limits.max_file_bytes:
                raise InterfaceOutputExecutionRejected(
                    "Interface output file exceeds the snapshot file-size limit."
                )
            total_bytes += size
            if total_bytes > limits.max_total_bytes:
                raise InterfaceOutputExecutionRejected(
                    "Interface output exceeds the snapshot byte limit."
                )
            nodes.append(_SourceNode(relative, "file", identity))
            if len(nodes) > limits.max_entries:
                raise InterfaceOutputExecutionRejected(
                    "Interface output exceeds the snapshot entry limit."
                )
        try:
            names_after = tuple(
                sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8"))
            )
        except (OSError, UnicodeError) as error:
            raise InterfaceOutputExecutionRejected(
                "Interface output directory changed during snapshot."
            ) from error
        if (
            names_after != names_before
            or _file_identity(os.fstat(directory_fd)) != _file_identity(before)
        ):
            raise InterfaceOutputExecutionRejected(
                "Interface output changed during snapshot."
            )

    scan(root_fd, ())
    return tuple(nodes)


def _open_relative_file(root_fd: int, relative_path: str) -> int:
    parts = relative_path.split("/")
    directory_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, _directory_flags(), dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(parts[-1], _file_flags(), dir_fd=directory_fd)
    except OSError as error:
        raise InterfaceOutputExecutionRejected(
            "Interface output file changed during snapshot."
        ) from error
    finally:
        os.close(directory_fd)


def _open_selected_directory(
    allowed_root: Path,
    relative_path: str,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> int:
    root = Path(os.path.abspath(Path(allowed_root).expanduser()))
    try:
        root_link = root.lstat()
        root_fd = os.open(root, _directory_flags())
    except OSError as error:
        raise InterfaceOutputExecutionRejected(
            "Launch-owned interface output root is unavailable."
        ) from error
    try:
        if (
            root.is_symlink()
            or not stat.S_ISDIR(root_link.st_mode)
            or _file_identity(os.fstat(root_fd)) != _file_identity(root_link)
            or (
                expected_root_identity is not None
                and (int(root_link.st_dev), int(root_link.st_ino))
                != expected_root_identity
            )
        ):
            raise InterfaceOutputExecutionRejected(
                "Launch-owned interface output root must be a real directory."
            )
        selected_fd = os.dup(root_fd)
        try:
            if relative_path != ".":
                for component in relative_path.split("/"):
                    next_fd = os.open(
                        component,
                        _directory_flags(),
                        dir_fd=selected_fd,
                    )
                    os.close(selected_fd)
                    selected_fd = next_fd
            return selected_fd
        except OSError as error:
            os.close(selected_fd)
            raise InterfaceOutputExecutionRejected(
                "Selected interface output is not a contained real directory."
            ) from error
    finally:
        os.close(root_fd)


def _copy_snapshot_file(
    *,
    root_fd: int,
    node: _SourceNode,
    destination: Path,
) -> TreeEntry:
    source_fd = _open_relative_file(root_fd, node.relative_path)
    digest = hashlib.sha256(b"optpilot/blob/v1\0")
    copied = 0
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or _file_identity(before) != node.identity
        ):
            raise InterfaceOutputExecutionRejected(
                "Interface output file changed during snapshot."
            )
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise InterfaceOutputExecutionUnavailable(
                        "Immutable output snapshot could not be written."
                    )
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            copied != node.identity[3]
            or _file_identity(after) != node.identity
        ):
            raise InterfaceOutputExecutionRejected(
                "Interface output file changed during snapshot."
            )
        executable = bool(node.identity[2] & 0o111)
        os.fchmod(destination_fd, 0o500 if executable else 0o400)
        return TreeEntry.file(
            node.relative_path,
            blob_ref=BlobRef(digest.hexdigest()),
            size=copied,
            executable=executable,
        )
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _remove_private_tree(root: Path) -> None:
    """Remove one provider-owned tree whose outer identity was already checked."""

    for directory, directory_names, _file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(directory)
        try:
            linked = current.lstat()
            if current.is_symlink() or not stat.S_ISDIR(linked.st_mode):
                continue
            os.chmod(current, 0o700, follow_symlinks=False)
        except OSError:
            pass
        # Never ask os.walk to descend through a replacement symlink.
        directory_names[:] = [
            name
            for name in directory_names
            if not (current / name).is_symlink()
        ]
    try:
        shutil.rmtree(root)
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private execution tree could not be retired."
        ) from error


def _remove_private_tree_at(parent_fd: int, name: str) -> None:
    """Retire a private child tree through an already-bound parent descriptor."""

    leaf = _publication_leaf(name)
    try:
        root_fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private execution tree could not be opened for retirement."
        ) from error

    def clear(directory_fd: int) -> None:
        try:
            os.fchmod(directory_fd, 0o700)
            names = tuple(os.listdir(directory_fd))
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Private execution tree could not be inspected for retirement."
            ) from error
        for child_name in names:
            try:
                linked = os.stat(
                    child_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise InterfaceOutputExecutionUnavailable(
                    "Private execution child could not be inspected for retirement."
                ) from error
            if stat.S_ISDIR(linked.st_mode):
                try:
                    child_fd = os.open(
                        child_name,
                        _directory_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise InterfaceOutputExecutionUnavailable(
                        "Private execution directory changed during retirement."
                    ) from error
                try:
                    clear(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(child_name, dir_fd=directory_fd)
            else:
                os.unlink(child_name, dir_fd=directory_fd)

    try:
        clear(root_fd)
    finally:
        os.close(root_fd)
    try:
        os.rmdir(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Private execution tree could not be retired."
        ) from error


def snapshot_interface_output_tree(
    allowed_root: Path,
    snapshot_parent: Path,
    request_id: str,
    *,
    relative_path: str = ".",
    limits: InterfaceOutputSnapshotLimits = InterfaceOutputSnapshotLimits(),
    expected_allowed_root_identity: tuple[int, int] | None = None,
) -> ImmutableInterfaceOutputTree:
    """Copy one live output into a bounded, immutable, execution-owned tree.

    ``relative_path`` is walked from ``allowed_root`` with descriptor-relative,
    no-follow opens.  The caller therefore passes the launch-owned output root
    and the unmodified canonical request path, never a pre-resolved child.
    This function accepts no command, image, environment, or mount data from
    the output tree.
    """

    request = _identifier(request_id, "execution request id")
    selection = _portable_path(
        relative_path,
        "execution output path",
        allow_root=True,
    )
    source_root = Path(os.path.abspath(Path(allowed_root).expanduser()))
    parent = Path(os.path.abspath(Path(snapshot_parent).expanduser()))
    try:
        source_link = source_root.lstat()
    except OSError as error:
        raise InterfaceOutputExecutionRejected(
            "Interface output is unavailable."
        ) from error
    if source_root.is_symlink() or not stat.S_ISDIR(source_link.st_mode):
        raise InterfaceOutputExecutionRejected(
            "Interface output root must be a real directory."
        )
    if expected_allowed_root_identity is not None and (
        not isinstance(expected_allowed_root_identity, tuple)
        or len(expected_allowed_root_identity) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in expected_allowed_root_identity
        )
    ):
        raise TypeError(
            "expected_allowed_root_identity must be a (device, inode) tuple."
        )
    if expected_allowed_root_identity is not None and (
        int(source_link.st_dev),
        int(source_link.st_ino),
    ) != expected_allowed_root_identity:
        raise InterfaceOutputExecutionRejected(
            "Interface output root identity changed."
        )
    source_real = source_root.resolve()
    proposed_parent = parent.resolve(strict=False)
    try:
        proposed_parent.relative_to(source_real)
    except ValueError:
        pass
    else:
        raise InterfaceOutputExecutionRejected(
            "Snapshot namespace must not be inside the live interface output."
        )
    try:
        parent_fd, parent_real, parent_identity = _bind_real_directory(
            parent,
            create=True,
        )
    except InterfaceOutputExecutionUnavailable as error:
        raise InterfaceOutputExecutionRejected(
            "Snapshot namespace is unavailable."
        ) from error

    staging_name = f".snapshot-{request}-{uuid.uuid4().hex}.staging"
    staging = parent_real / staging_name
    cleanup_name: str | None = staging_name
    final: Path | None = None
    root_fd = -1
    try:
        _require_directory_binding(parent_real, parent_identity)
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        root_fd = _open_selected_directory(
            source_real,
            selection,
            expected_root_identity=expected_allowed_root_identity,
        )
        root_identity = _file_identity(os.fstat(root_fd))
        before = _scan_source_tree(root_fd, limits=limits)
        entries: list[TreeEntry] = []
        for node in before:
            destination = staging / PurePosixPath(node.relative_path)
            if node.kind == "directory":
                destination.mkdir(mode=0o700)
                entries.append(TreeEntry.directory(node.relative_path))
        for node in before:
            if node.kind != "file":
                continue
            destination = staging / PurePosixPath(node.relative_path)
            entries.append(
                _copy_snapshot_file(
                    root_fd=root_fd,
                    node=node,
                    destination=destination,
                )
            )
        after = _scan_source_tree(root_fd, limits=limits)
        try:
            reselected_fd = _open_selected_directory(
                source_real,
                selection,
                expected_root_identity=expected_allowed_root_identity,
            )
            try:
                reselected_identity = _file_identity(os.fstat(reselected_fd))
            finally:
                os.close(reselected_fd)
        except InterfaceOutputExecutionRejected:
            raise
        if (
            before != after
            or _file_identity(os.fstat(root_fd)) != root_identity
            or reselected_identity != root_identity
        ):
            raise InterfaceOutputExecutionRejected(
                "Interface output changed during snapshot."
            )
        manifest = TreeManifest.build(entries, limits=limits.as_seal_limits())
        # Files are already read-only.  Make nested directories and then the
        # root read/execute-only after every write and stability check.
        directories = sorted(
            (path for path in staging.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            os.chmod(directory, 0o500, follow_symlinks=False)
        os.chmod(staging, 0o500, follow_symlinks=False)
        final_name = (
            f"snapshot-{request}-{manifest.snapshot_ref.digest[:16]}-"
            f"{uuid.uuid4().hex[:12]}"
        )
        final = parent_real / final_name
        _require_directory_binding(parent_real, parent_identity)
        os.rename(
            staging_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        cleanup_name = final_name
        os.fsync(parent_fd)
        _require_directory_binding(parent_real, parent_identity)
        result = ImmutableInterfaceOutputTree(
            snapshot_ref=manifest.snapshot_ref,
            root_path=final,
            owned_root=final,
        )
        cleanup_name = None
        return result
    except InterfaceOutputExecutionError:
        raise
    except (ContentRejected, OSError, ValueError) as error:
        raise InterfaceOutputExecutionRejected(
            "Interface output could not be safely snapshotted."
        ) from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        try:
            if cleanup_name is not None:
                _remove_private_tree_at(parent_fd, cleanup_name)
        finally:
            os.close(parent_fd)


@dataclass(frozen=True)
class OriginatingPreparedRuntime:
    """Exact prepared runtime inherited from the live originating launch."""

    launch_id: str
    cache_key: str
    image_digest: str
    entry_root: Path
    payload_root: Path
    entry_device: int = field(init=False, repr=False)
    entry_inode: int = field(init=False, repr=False)
    payload_device: int = field(init=False, repr=False)
    payload_inode: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "launch_id",
            _identifier(self.launch_id, "originating interface launch id"),
        )
        object.__setattr__(
            self,
            "cache_key",
            _identifier(self.cache_key, "prepared runtime cache key"),
        )
        if _IMAGE_DIGEST_RE.fullmatch(self.image_digest) is None:
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime has no immutable container image identity."
            )
        entry = Path(os.path.abspath(Path(self.entry_root).expanduser()))
        payload = Path(os.path.abspath(Path(self.payload_root).expanduser()))
        try:
            entry_info = entry.lstat()
            payload_info = payload.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime is unavailable."
            ) from error
        if (
            entry.is_symlink()
            or payload.is_symlink()
            or not stat.S_ISDIR(entry_info.st_mode)
            or not stat.S_ISDIR(payload_info.st_mode)
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime must be a real immutable directory."
            )
        entry_real = entry.resolve()
        payload_real = payload.resolve()
        try:
            payload_real.relative_to(entry_real)
        except ValueError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime payload escapes its leased entry."
            ) from error
        object.__setattr__(self, "entry_root", entry_real)
        object.__setattr__(self, "payload_root", payload_real)
        object.__setattr__(self, "entry_device", int(entry_info.st_dev))
        object.__setattr__(self, "entry_inode", int(entry_info.st_ino))
        object.__setattr__(self, "payload_device", int(payload_info.st_dev))
        object.__setattr__(self, "payload_inode", int(payload_info.st_ino))

    @classmethod
    def from_lease(
        cls, lease: PreparedRuntimeLease
    ) -> "OriginatingPreparedRuntime":
        if not isinstance(lease, PreparedRuntimeLease):
            raise TypeError("lease must be a PreparedRuntimeLease.")
        manifest = lease.manifest
        key_payload = (
            manifest.get("key_payload") if isinstance(manifest, Mapping) else None
        )
        provider = (
            key_payload.get("provider") if isinstance(key_payload, Mapping) else None
        )
        image_digest = (
            str(provider.get("imageDigest") or "")
            if isinstance(provider, Mapping)
            else ""
        )
        if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime has no immutable container image identity."
            )
        return cls(
            launch_id=lease.launch_id,
            cache_key=lease.cache_key,
            image_digest=image_digest,
            entry_root=lease.entry_root,
            payload_root=lease.payload_root,
        )

    def assert_attached(self) -> None:
        try:
            entry_info = self.entry_root.lstat()
            payload_info = self.payload_root.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime is no longer available."
            ) from error
        if (
            self.entry_root.is_symlink()
            or self.payload_root.is_symlink()
            or not stat.S_ISDIR(entry_info.st_mode)
            or not stat.S_ISDIR(payload_info.st_mode)
            or entry_info.st_dev != self.entry_device
            or entry_info.st_ino != self.entry_inode
            or payload_info.st_dev != self.payload_device
            or payload_info.st_ino != self.payload_inode
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Prepared runtime identity changed."
            )


@dataclass(frozen=True)
class InterfaceOutputResultFile:
    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _portable_path(
                self.relative_path,
                "execution result path",
                allow_root=False,
            ),
        )
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("execution result size must be nonnegative.")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("execution result digest must be lowercase SHA-256.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class LocalExecutionResultTree:
    """Private local result projection retained until broker publication."""

    root_path: Path
    owned_root: Path
    device: int = field(init=False, repr=False)
    inode: int = field(init=False, repr=False)
    owned_device: int = field(init=False, repr=False)
    owned_inode: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = Path(os.path.abspath(Path(self.root_path).expanduser()))
        owned = Path(os.path.abspath(Path(self.owned_root).expanduser()))
        try:
            root_info = root.lstat()
            owned_info = owned.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree is unavailable."
            ) from error
        if (
            root.is_symlink()
            or owned.is_symlink()
            or not stat.S_ISDIR(root_info.st_mode)
            or not stat.S_ISDIR(owned_info.st_mode)
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree must use real directories."
            )
        root_real = root.resolve()
        owned_real = owned.resolve()
        try:
            root_real.relative_to(owned_real)
        except ValueError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Local execution results escape their private execution root."
            ) from error
        object.__setattr__(self, "root_path", root_real)
        object.__setattr__(self, "owned_root", owned_real)
        object.__setattr__(self, "device", int(root_info.st_dev))
        object.__setattr__(self, "inode", int(root_info.st_ino))
        object.__setattr__(self, "owned_device", int(owned_info.st_dev))
        object.__setattr__(self, "owned_inode", int(owned_info.st_ino))

    def assert_attached(self) -> None:
        try:
            root_info = self.root_path.lstat()
            owned_info = self.owned_root.lstat()
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree is no longer available."
            ) from error
        if (
            self.root_path.is_symlink()
            or self.owned_root.is_symlink()
            or not stat.S_ISDIR(root_info.st_mode)
            or not stat.S_ISDIR(owned_info.st_mode)
            or root_info.st_dev != self.device
            or root_info.st_ino != self.inode
            or owned_info.st_dev != self.owned_device
            or owned_info.st_ino != self.owned_inode
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree identity changed."
            )

    def cleanup(self) -> None:
        try:
            linked = self.owned_root.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree could not be inspected."
            ) from error
        if (
            self.owned_root.is_symlink()
            or not stat.S_ISDIR(linked.st_mode)
            or linked.st_dev != self.owned_device
            or linked.st_ino != self.owned_inode
        ):
            raise InterfaceOutputExecutionUnavailable(
                "Local execution result tree identity changed."
            )
        _remove_private_tree(self.owned_root)


@dataclass(frozen=True)
class InterfaceOutputExecutionResult:
    request_id: str
    action_id: str
    snapshot_ref: SnapshotRef | None
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    result_files: tuple[InterfaceOutputResultFile, ...] = ()
    failure_code: str | None = None
    local_results: LocalExecutionResultTree | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _identifier(self.request_id, "execution request id")
        _identifier(self.action_id, "execution action id")
        if self.snapshot_ref is not None and not isinstance(
            self.snapshot_ref, SnapshotRef
        ):
            raise TypeError("snapshot_ref must be a SnapshotRef or None.")
        if self.status not in {
            "succeeded",
            "failed",
            "timed_out",
            "cancelled",
            "rejected",
            "infrastructure_failed",
        }:
            raise ValueError("execution result status is unsupported.")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("execution duration must be finite and nonnegative.")
        if self.failure_code is not None:
            _identifier(self.failure_code, "execution failure code")
        if self.local_results is not None and not isinstance(
            self.local_results, LocalExecutionResultTree
        ):
            raise TypeError("local_results must be a LocalExecutionResultTree.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OUTPUT_EXECUTION_RESULT_SCHEMA,
            "request_id": self.request_id,
            "action_id": self.action_id,
            "snapshot_ref": (
                str(self.snapshot_ref) if self.snapshot_ref is not None else None
            ),
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "result_files": [item.to_dict() for item in self.result_files],
            "failure_code": self.failure_code,
        }

    def cleanup(self) -> None:
        """Retire private result bytes after broker publication."""

        if self.local_results is not None:
            self.local_results.cleanup()


@dataclass(frozen=True)
class LocalContainerExecutionLimits:
    cpu_count: float = 1.0
    memory_mib: int = 512
    pids: int = 64
    tmp_mib: int = 64
    result_mib: int = 64
    result_entries: int = 10_000
    result_depth: int = 48
    result_path_bytes: int = 4096
    result_component_bytes: int = 255
    capture_bytes: int = _MAX_CAPTURE_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.cpu_count, bool)
            or not isinstance(self.cpu_count, (int, float))
            or not 0.1 <= float(self.cpu_count) <= 8
        ):
            raise ValueError("execution CPU limit is invalid.")
        for value, label, minimum, maximum in (
            (self.memory_mib, "memory", 64, 8192),
            (self.pids, "PID", 8, 1024),
            (self.tmp_mib, "temporary storage", 8, 1024),
            (self.result_mib, "result storage", 1, 1024),
            (self.result_entries, "result entries", 1, 1_000_000),
            (self.result_depth, "result depth", 1, 256),
            (self.result_path_bytes, "result path", 64, 16_384),
            (self.result_component_bytes, "result component", 16, 1024),
            (self.capture_bytes, "capture", 1024, 1024 * 1024),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"execution {label} limit is invalid.")

    def result_tree_limits(self) -> InterfaceOutputSnapshotLimits:
        maximum_bytes = self.result_mib * 1024 * 1024
        return InterfaceOutputSnapshotLimits(
            max_entries=self.result_entries,
            max_depth=self.result_depth,
            max_total_bytes=maximum_bytes,
            max_file_bytes=maximum_bytes,
            max_path_bytes=self.result_path_bytes,
            max_component_bytes=self.result_component_bytes,
        )

    def result_archive_bytes(self) -> int:
        """Bound payload plus portable tar headers without buffering the archive."""

        payload_bytes = self.result_mib * 1024 * 1024
        per_entry_overhead = self.result_path_bytes + 4096
        return (
            payload_bytes
            + (self.result_entries + 1) * per_entry_overhead
            + 1024 * 1024
        )


def _inspect_result_tree(
    root: Path,
    *,
    limits: InterfaceOutputSnapshotLimits,
) -> tuple[InterfaceOutputResultFile, ...]:
    root_fd = -1
    files: list[InterfaceOutputResultFile] = []
    try:
        root_fd = _open_selected_directory(Path(root), ".")
        root_identity = _file_identity(os.fstat(root_fd))
        before = _scan_source_tree(root_fd, limits=limits)
        for node in before:
            if node.kind != "file":
                continue
            source_fd = _open_relative_file(root_fd, node.relative_path)
            digest = hashlib.sha256()
            size = 0
            try:
                if _file_identity(os.fstat(source_fd)) != node.identity:
                    raise InterfaceOutputExecutionRejected(
                        "Execution result file changed during inspection."
                    )
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                if (
                    size != node.identity[3]
                    or _file_identity(os.fstat(source_fd)) != node.identity
                ):
                    raise InterfaceOutputExecutionRejected(
                        "Execution result file changed during inspection."
                    )
            finally:
                os.close(source_fd)
            files.append(
                InterfaceOutputResultFile(
                    relative_path=node.relative_path,
                    size=size,
                    sha256=digest.hexdigest(),
                )
            )
        after = _scan_source_tree(root_fd, limits=limits)
        reselected_fd = _open_selected_directory(Path(root), ".")
        try:
            reselected_identity = _file_identity(os.fstat(reselected_fd))
        finally:
            os.close(reselected_fd)
        if (
            before != after
            or _file_identity(os.fstat(root_fd)) != root_identity
            or reselected_identity != root_identity
        ):
            raise InterfaceOutputExecutionRejected(
                "Execution result tree changed during inspection."
            )
        return tuple(
            sorted(files, key=lambda item: item.relative_path.encode("utf-8"))
        )
    except (InterfaceOutputExecutionRejected, OSError) as error:
        raise InterfaceOutputExecutionUnavailable(
            "Output execution produced an unsafe or over-limit result tree."
        ) from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self.limit - self._size
            if remaining > 0:
                kept = chunk[:remaining]
                self._chunks.append(kept)
                self._size += len(kept)
            if len(chunk) > max(remaining, 0):
                self.truncated = True

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", errors="replace")


class _BoundedArchiveReader:
    """A streaming ceiling in front of an untrusted container archive."""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.consumed
        if remaining < 0:
            raise InterfaceOutputExecutionUnavailable(
                "Output execution result archive exceeded its byte limit."
            )
        requested = 64 * 1024 if size is None or size < 0 else size
        chunk = self.stream.read(min(requested, remaining + 1))
        if not isinstance(chunk, bytes):
            raise InterfaceOutputExecutionUnavailable(
                "Output execution result archive was not a byte stream."
            )
        self.consumed += len(chunk)
        if self.consumed > self.limit:
            raise InterfaceOutputExecutionUnavailable(
                "Output execution result archive exceeded its byte limit."
            )
        return chunk

    def drain(self) -> None:
        while self.read(64 * 1024):
            pass


def _archive_relative_path(
    member: tarfile.TarInfo,
    *,
    limits: InterfaceOutputSnapshotLimits,
) -> str | None:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        if not member.isdir():
            raise InterfaceOutputExecutionUnavailable(
                "Output execution archive has an invalid root entry."
            )
        return None
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    try:
        return validate_portable_path(name, limits=limits.as_seal_limits())
    except (ContentRejected, UnicodeError) as error:
        raise InterfaceOutputExecutionUnavailable(
            "Output execution archive contains an unsafe path."
        ) from error


def _open_archive_parent(root_fd: int, relative_path: str) -> tuple[int, str]:
    parts = relative_path.split("/")
    parent_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, _directory_flags(), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd, parts[-1]
    except OSError as error:
        os.close(parent_fd)
        raise InterfaceOutputExecutionUnavailable(
            "Output execution archive has an undeclared parent directory."
        ) from error


def _extract_result_archive(
    stream: Any,
    destination: Path,
    *,
    limits: InterfaceOutputSnapshotLimits,
) -> None:
    """Validate and descriptor-safely extract one bounded streaming tar."""

    root_fd = -1
    try:
        root_fd, canonical, identity = _bind_real_directory(
            destination,
            create=False,
        )
        if os.listdir(root_fd):
            raise InterfaceOutputExecutionUnavailable(
                "Private result extraction directory must be empty."
            )
        seen: dict[str, str] = {}
        member_count = 0
        root_seen = False
        total_bytes = 0
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                member_count += 1
                if member_count > limits.max_entries + 1:
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive exceeds its entry limit."
                    )
                relative_path = _archive_relative_path(member, limits=limits)
                if relative_path is None:
                    if root_seen:
                        raise InterfaceOutputExecutionUnavailable(
                            "Output execution archive contains a duplicate root."
                        )
                    root_seen = True
                    continue
                if relative_path in seen:
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive contains a duplicate path."
                    )
                parent_path = PurePosixPath(relative_path).parent.as_posix()
                if parent_path != "." and seen.get(parent_path) != "directory":
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive has an undeclared parent directory."
                    )
                if len(seen) >= limits.max_entries:
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive exceeds its entry limit."
                    )
                if member.isdir():
                    kind = "directory"
                elif member.isfile() and not getattr(member, "sparse", None):
                    kind = "file"
                else:
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive contains an unsupported entry."
                    )
                parent_fd, leaf = _open_archive_parent(root_fd, relative_path)
                try:
                    if kind == "directory":
                        os.mkdir(leaf, 0o700, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                        child_fd = os.open(
                            leaf,
                            _directory_flags(),
                            dir_fd=parent_fd,
                        )
                        try:
                            os.fsync(child_fd)
                        finally:
                            os.close(child_fd)
                    else:
                        size = int(member.size)
                        if size < 0 or size > limits.max_file_bytes:
                            raise InterfaceOutputExecutionUnavailable(
                                "Output execution archive file exceeds its size limit."
                            )
                        total_bytes += size
                        if total_bytes > limits.max_total_bytes:
                            raise InterfaceOutputExecutionUnavailable(
                                "Output execution archive exceeds its byte limit."
                            )
                        source = archive.extractfile(member)
                        if source is None:
                            raise InterfaceOutputExecutionUnavailable(
                                "Output execution archive file has no payload."
                            )
                        target_fd = os.open(
                            leaf,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=parent_fd,
                        )
                        try:
                            copied = 0
                            while copied < size:
                                chunk = source.read(min(1024 * 1024, size - copied))
                                if not chunk:
                                    raise InterfaceOutputExecutionUnavailable(
                                        "Output execution archive file was truncated."
                                    )
                                copied += len(chunk)
                                view = memoryview(chunk)
                                while view:
                                    written = os.write(target_fd, view)
                                    if written <= 0:
                                        raise InterfaceOutputExecutionUnavailable(
                                            "Output execution result could not be retained."
                                        )
                                    view = view[written:]
                            if source.read(1):
                                raise InterfaceOutputExecutionUnavailable(
                                    "Output execution archive file exceeded its declared size."
                                )
                            os.fsync(target_fd)
                        finally:
                            os.close(target_fd)
                        os.fsync(parent_fd)
                except FileExistsError as error:
                    raise InterfaceOutputExecutionUnavailable(
                        "Output execution archive path collided during extraction."
                    ) from error
                finally:
                    os.close(parent_fd)
                seen[relative_path] = kind
        _require_directory_binding(canonical, identity)
        if (int(os.fstat(root_fd).st_dev), int(os.fstat(root_fd).st_ino)) != identity:
            raise InterfaceOutputExecutionUnavailable(
                "Private result extraction directory identity changed."
            )
        os.fsync(root_fd)
    except (tarfile.TarError, OSError, UnicodeError) as error:
        raise InterfaceOutputExecutionUnavailable(
            "Output execution result archive was unsafe or malformed."
        ) from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _redact_execution_text(
    value: str,
    replacements: Mapping[str, str],
) -> str:
    """Remove provider coordinates from evidence emitted by untrusted code."""

    redacted = value
    ordered = sorted(
        (
            (raw, placeholder)
            for raw, placeholder in replacements.items()
            if isinstance(raw, str) and len(raw) > 1
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for raw, placeholder in ordered:
        redacted = redacted.replace(raw, placeholder)
    return redacted


class LocalContainerInterfaceOutputExecutor:
    """Run one immutable output in a short-lived, networkless sibling container."""

    def __init__(
        self,
        *,
        executable: str,
        runtime_root: Path,
        limits: LocalContainerExecutionLimits = LocalContainerExecutionLimits(),
    ) -> None:
        selected = shutil.which(executable) if os.sep not in executable else executable
        if not selected:
            raise InterfaceOutputExecutionUnavailable(
                "Docker or Podman execution is unavailable."
            )
        self.executable = str(Path(selected).resolve())
        root = Path(os.path.abspath(Path(runtime_root).expanduser()))
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        linked = root.lstat()
        if root.is_symlink() or not stat.S_ISDIR(linked.st_mode):
            raise InterfaceOutputExecutionUnavailable(
                "Output execution runtime root must be a real directory."
            )
        self.runtime_root = root.resolve()
        self.limits = limits

    def execute(
        self,
        *,
        source: ImmutableInterfaceOutputTree,
        runtime: OriginatingPreparedRuntime,
        action: InterfaceOutputActionSpec,
        request: InterfaceOutputExecutionRequest,
        should_cancel: Callable[[], bool] | None = None,
    ) -> InterfaceOutputExecutionResult:
        if request.action_id != action.action_id:
            raise InterfaceOutputExecutionRejected(
                "execution request selected another action."
            )
        if request.arguments and not action.accepts_arguments:
            raise InterfaceOutputExecutionRejected(
                "execution action does not accept arguments."
            )
        if (
            request.timeout_seconds is not None
            and request.timeout_seconds > action.timeout_seconds
        ):
            raise InterfaceOutputExecutionRejected(
                "execution timeout_seconds may not exceed the authored action maximum."
            )
        source.assert_attached()
        runtime.assert_attached()
        execution_root = self.runtime_root / (
            f"execution-{request.request_id}-{uuid.uuid4().hex[:12]}"
        )
        execution_root.mkdir(mode=0o700)
        extracted_results = execution_root / "results"
        extracted_results.mkdir(mode=0o700)
        container_name = (
            "optpilot-output-"
            + hashlib.sha256(
                f"{runtime.launch_id}:{request.request_id}".encode("utf-8")
            ).hexdigest()[:16]
            + "-"
            + uuid.uuid4().hex[:8]
        )
        command = self._container_command(
            container_name=container_name,
            source=source,
            runtime=runtime,
        )
        action_command = self._action_command(
            container_name=container_name,
            runtime=runtime,
            action=action,
            request=request,
        )
        stdout_capture = _BoundedCapture(self.limits.capture_bytes)
        stderr_capture = _BoundedCapture(self.limits.capture_bytes)
        started = time.monotonic()
        status = "infrastructure_failed"
        failure_code: str | None = "container_start_failed"
        exit_code: int | None = None
        result_files: tuple[InterfaceOutputResultFile, ...] = ()
        results_verified = False
        container_started = False
        process: subprocess.Popen[bytes] | None = None
        drain_threads: list[threading.Thread] = []
        try:
            source.assert_attached()
            runtime.assert_attached()
            self._start_container(command)
            container_started = True
            process = subprocess.Popen(
                action_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            drain_threads = [
                threading.Thread(
                    target=self._drain,
                    args=(process.stdout, stdout_capture),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._drain,
                    args=(process.stderr, stderr_capture),
                    daemon=True,
                ),
            ]
            for thread in drain_threads:
                thread.start()
            timeout_seconds = (
                action.timeout_seconds
                if request.timeout_seconds is None
                else request.timeout_seconds
            )
            deadline = started + timeout_seconds
            while process.poll() is None:
                if should_cancel is not None and should_cancel():
                    status = "cancelled"
                    failure_code = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    failure_code = "timeout"
                    break
                time.sleep(0.05)
            if process.poll() is None:
                self._force_remove(container_name)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            exit_code = process.returncode
            for thread in drain_threads:
                thread.join(timeout=5)
            if status not in {"cancelled", "timed_out"}:
                if exit_code == 0:
                    status = "succeeded"
                    failure_code = None
                elif exit_code == 125:
                    status = "infrastructure_failed"
                    failure_code = "container_start_failed"
                else:
                    status = "failed"
                    failure_code = "nonzero_exit"
                if status in {"succeeded", "failed"}:
                    self._copy_results(container_name, extracted_results)
                    result_files = self._inspect_results(extracted_results)
                    results_verified = True
        except InterfaceOutputExecutionUnavailable as error:
            status = "infrastructure_failed"
            failure_code = (
                "result_retention_failed"
                if container_started
                else "container_start_failed"
            )
            stderr_capture.add(str(error).encode("utf-8", errors="replace"))
            result_files = ()
        except (
            InterfaceOutputExecutionRejected,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            status = "infrastructure_failed"
            failure_code = "container_provider_failed"
            stderr_capture.add(str(error).encode("utf-8", errors="replace"))
            result_files = ()
        finally:
            cleanup_error: BaseException | None = None
            try:
                self._force_remove(container_name)
                self._require_container_absent(container_name)
            except BaseException as error:  # cleanup proof takes precedence
                cleanup_error = error
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for thread in drain_threads:
                thread.join(timeout=1)
            if cleanup_error is not None:
                status = "infrastructure_failed"
                failure_code = "container_cleanup_unconfirmed"
                stderr_capture.add(
                    str(cleanup_error).encode("utf-8", errors="replace")
                )
        local_results: LocalExecutionResultTree | None = None
        if results_verified and status in {"succeeded", "failed"}:
            try:
                local_results = LocalExecutionResultTree(
                    root_path=extracted_results,
                    owned_root=execution_root,
                )
            except InterfaceOutputExecutionUnavailable as error:
                status = "infrastructure_failed"
                failure_code = "result_retention_failed"
                stderr_capture.add(str(error).encode("utf-8", errors="replace"))
                result_files = ()
        if local_results is None and execution_root.exists():
            try:
                _remove_private_tree(execution_root)
            except InterfaceOutputExecutionUnavailable as error:
                status = "infrastructure_failed"
                failure_code = "result_cleanup_unconfirmed"
                stderr_capture.add(str(error).encode("utf-8", errors="replace"))
        redactions = {
            str(execution_root): "<execution-work>",
            str(source.root_path): "<interface-output>",
            str(runtime.payload_root): "<prepared-runtime-payload>",
            str(runtime.entry_root): "<prepared-runtime>",
            str(self.runtime_root): "<execution-runtime>",
            container_name: "<execution-container>",
        }
        stdout = _redact_execution_text(stdout_capture.text(), redactions)
        stderr = _redact_execution_text(stderr_capture.text(), redactions)
        return InterfaceOutputExecutionResult(
            request_id=request.request_id,
            action_id=request.action_id,
            snapshot_ref=source.snapshot_ref,
            status=status,
            exit_code=exit_code,
            duration_seconds=max(0.0, time.monotonic() - started),
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            result_files=tuple(result_files),
            failure_code=failure_code,
            local_results=local_results,
        )

    def _container_command(
        self,
        *,
        container_name: str,
        source: ImmutableInterfaceOutputTree,
        runtime: OriginatingPreparedRuntime,
    ) -> list[str]:
        command = [
            self.executable,
            "run",
            "--detach",
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(float(self.limits.cpu_count)),
            "--memory",
            f"{self.limits.memory_mib}m",
            "--pids-limit",
            str(self.limits.pids),
            "--tmpfs",
            (
                "/tmp:rw,nosuid,nodev,noexec,"
                f"size={self.limits.tmp_mib}m,"
                f"nr_inodes={self.limits.result_entries + 1},mode=1777"
            ),
            "--tmpfs",
            (
                "/optpilot/results:rw,nosuid,nodev,noexec,"
                f"size={self.limits.result_mib * 1024 * 1024},"
                f"nr_inodes={self.limits.result_entries + 1},mode=1777"
            ),
            "--mount",
            f"type=bind,source={source.root_path},target={_CONTAINER_OUTPUT_ROOT},readonly",
            "--mount",
            (
                f"type=bind,source={runtime.entry_root},"
                f"target={runtime.entry_root},readonly"
            ),
            "--workdir",
            str(_CONTAINER_OUTPUT_ROOT),
        ]
        for value in self._fixed_environment(runtime):
            command.extend(["--env", value])
        user = self._container_user()
        if user:
            command.extend(["--user", user])
        command.extend(
            [
                "--entrypoint",
                "python3",
                runtime.image_digest,
                "-I",
                "-S",
                "-B",
                "-c",
                "import time\nwhile True:\n time.sleep(3600)",
            ]
        )
        return command

    def _action_command(
        self,
        *,
        container_name: str,
        runtime: OriginatingPreparedRuntime,
        action: InterfaceOutputActionSpec,
        request: InterfaceOutputExecutionRequest,
    ) -> list[str]:
        cwd = (
            _CONTAINER_OUTPUT_ROOT
            if action.cwd == "."
            else _CONTAINER_OUTPUT_ROOT / PurePosixPath(action.cwd)
        )
        command = [
            self.executable,
            "exec",
            "--workdir",
            str(cwd),
        ]
        user = self._container_user()
        if user:
            command.extend(["--user", user])
        for value in self._fixed_environment(runtime):
            command.extend(["--env", value])
        command.extend(
            [
                container_name,
                *action.command,
                *request.arguments,
            ]
        )
        return command

    @staticmethod
    def _container_user() -> str:
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            return f"{os.getuid()}:{os.getgid()}"
        return ""

    @staticmethod
    def _fixed_environment(runtime: OriginatingPreparedRuntime) -> tuple[str, ...]:
        return (
            "HOME=/tmp",
            "TMPDIR=/tmp",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "PYTHONUNBUFFERED=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONNOUSERSITE=1",
            f"OPTPILOT_PREPARED_RUNTIME_ROOT={runtime.payload_root}",
            "OPTPILOT_PREPARED_RUNTIME_ACCESS=read-only",
            f"OPTPILOT_OUTPUT_EXECUTION_RESULTS_ROOT={_CONTAINER_RESULT_ROOT}",
        )

    def _start_container(self, command: list[str]) -> None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise InterfaceOutputExecutionUnavailable(
                "Output execution container could not be started."
            )
        self._require_container_running(command[command.index("--name") + 1])

    def _require_container_running(self, container_name: str) -> None:
        completed = subprocess.run(
            [
                self.executable,
                "inspect",
                "--format",
                "{{.State.Running}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip().lower() != "true":
            raise InterfaceOutputExecutionUnavailable(
                "Output execution keeper container is not running."
            )

    @staticmethod
    def _drain(stream: Any, capture: _BoundedCapture) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            capture.add(chunk)

    def _force_remove(self, container_name: str) -> None:
        completed = subprocess.run(
            [self.executable, "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        detail = ((completed.stderr or "") + (completed.stdout or "")).lower()
        absent = (
            "no such" in detail
            or "no container with name or id" in detail
            or "does not exist" in detail
        )
        if completed.returncode != 0 and not absent:
            raise InterfaceOutputExecutionUnavailable(
                "Output execution container could not be retired."
            )

    def _require_container_absent(self, container_name: str) -> None:
        completed = subprocess.run(
            [self.executable, "inspect", container_name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            raise InterfaceOutputExecutionUnavailable(
                "Output execution container retirement is unconfirmed."
            )

    def _copy_results(self, container_name: str, destination: Path) -> None:
        archive_code = (
            "import os,sys,tarfile\n"
            "os.chdir('/optpilot/results')\n"
            "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',"
            "dereference=False) as archive:\n"
            " archive.add('.',arcname='.',recursive=True)\n"
        )
        command = [
            self.executable,
            "exec",
            "--workdir",
            str(_CONTAINER_RESULT_ROOT),
        ]
        user = self._container_user()
        if user:
            command.extend(["--user", user])
        command.extend(
            [
                container_name,
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                archive_code,
            ]
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_capture = _BoundedCapture(self.limits.capture_bytes)
        stderr_thread = threading.Thread(
            target=self._drain,
            args=(process.stderr, stderr_capture),
            daemon=True,
        )
        stderr_thread.start()
        reader = _BoundedArchiveReader(
            process.stdout,
            self.limits.result_archive_bytes(),
        )
        retention_timed_out = threading.Event()

        def stop_stalled_retention() -> None:
            if process.poll() is None:
                retention_timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass

        timer = threading.Timer(30, stop_stalled_retention)
        timer.daemon = True
        timer.start()
        try:
            _extract_result_archive(
                reader,
                destination,
                limits=self.limits.result_tree_limits(),
            )
            reader.drain()
            if retention_timed_out.is_set():
                raise InterfaceOutputExecutionUnavailable(
                    "Output execution result retention timed out."
                )
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=5)
                raise InterfaceOutputExecutionUnavailable(
                    "Output execution result retention timed out."
                ) from error
            if return_code != 0:
                raise InterfaceOutputExecutionUnavailable(
                    "Output execution results could not be retained."
                )
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise
        finally:
            timer.cancel()
            stderr_thread.join(timeout=5)

    def _inspect_results(
        self, root: Path
    ) -> tuple[InterfaceOutputResultFile, ...]:
        return _inspect_result_tree(
            root,
            limits=self.limits.result_tree_limits(),
        )


def failed_execution_result(
    request: InterfaceOutputExecutionRequest,
    *,
    failure_code: str,
    stderr: str = "",
    snapshot_ref: SnapshotRef | None = None,
    status: str = "rejected",
) -> InterfaceOutputExecutionResult:
    """Create a truthful terminal response when isolated execution cannot start."""

    if not isinstance(request, InterfaceOutputExecutionRequest):
        raise TypeError("request must be an InterfaceOutputExecutionRequest.")
    if status not in {"rejected", "infrastructure_failed", "failed"}:
        raise ValueError("pre-execution failure status is unsupported.")
    _identifier(failure_code, "execution failure code")
    if not isinstance(stderr, str):
        raise TypeError("stderr must be text.")
    encoded = stderr.encode("utf-8", errors="replace")
    truncated = len(encoded) > _MAX_CAPTURE_BYTES
    bounded = encoded[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    return InterfaceOutputExecutionResult(
        request_id=request.request_id,
        action_id=request.action_id,
        snapshot_ref=snapshot_ref,
        status=status,
        exit_code=None,
        duration_seconds=0.0,
        stdout="",
        stderr=bounded,
        stdout_truncated=False,
        stderr_truncated=truncated,
        failure_code=failure_code,
    )


def export_execution_result_tree_at(
    result: InterfaceOutputExecutionResult,
    parent_fd: int,
    parent_path: Path,
    directory_name: str,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    limits: InterfaceOutputSnapshotLimits = InterfaceOutputSnapshotLimits(
        max_entries=10_000,
        max_depth=48,
        max_total_bytes=1024**3,
        max_file_bytes=1024**3,
    ),
) -> tuple[InterfaceOutputResultFile, ...]:
    """Publish verified results through a caller-bound broker directory."""

    if not isinstance(result, InterfaceOutputExecutionResult):
        raise TypeError("result must be an InterfaceOutputExecutionResult.")
    retained = result.local_results
    if retained is None:
        raise InterfaceOutputExecutionUnavailable(
            "This execution has no retained result tree to export."
        )
    retained.assert_attached()
    target_name = _publication_leaf(directory_name)
    parent = Path(os.path.abspath(Path(parent_path).expanduser())).resolve(
        strict=False
    )
    parent_identity = _require_directory_fd(
        parent_fd,
        expected_parent_identity,
    )
    _require_directory_binding(parent, parent_identity)
    actual = _inspect_result_tree(
        retained.root_path,
        limits=limits,
    )
    if actual != result.result_files:
        raise InterfaceOutputExecutionUnavailable(
            "Retained result bytes no longer match execution evidence."
        )
    try:
        os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise InterfaceOutputExecutionUnavailable(
            "Result publication target could not be inspected."
        ) from error
    else:
        raise InterfaceOutputExecutionRejected(
            "Result publication target already exists."
        )

    export_id = "export-" + hashlib.sha256(
        f"{result.request_id}:{result.action_id}".encode("utf-8")
    ).hexdigest()[:24]
    projection = snapshot_interface_output_tree(
        retained.root_path,
        parent,
        export_id,
        limits=limits,
    )
    try:
        _require_directory_fd(parent_fd, parent_identity)
        _require_directory_binding(parent, parent_identity)
        try:
            os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise InterfaceOutputExecutionRejected(
                "Result publication target already exists."
            )
        try:
            os.rename(
                projection.root_path.name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            _require_directory_fd(parent_fd, parent_identity)
            _require_directory_binding(parent, parent_identity)
            return actual
        except OSError as error:
            raise InterfaceOutputExecutionUnavailable(
                "Execution results could not be published."
            ) from error
    finally:
        cleanup_error: InterfaceOutputExecutionUnavailable | None = None
        try:
            _remove_private_tree_at(parent_fd, projection.root_path.name)
        except InterfaceOutputExecutionUnavailable as error:
            cleanup_error = error
        try:
            projection.cleanup()
        except InterfaceOutputExecutionUnavailable as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error


def export_execution_result_tree(
    result: InterfaceOutputExecutionResult,
    destination: Path,
    *,
    limits: InterfaceOutputSnapshotLimits = InterfaceOutputSnapshotLimits(
        max_entries=10_000,
        max_depth=48,
        max_total_bytes=1024**3,
        max_file_bytes=1024**3,
    ),
) -> tuple[InterfaceOutputResultFile, ...]:
    """Bind a path once, then publish verified results through that descriptor."""

    target = Path(os.path.abspath(Path(destination).expanduser()))
    target_name = _publication_leaf(target.name)
    parent_fd, parent, parent_identity = _bind_real_directory(
        target.parent,
        create=True,
    )
    try:
        return export_execution_result_tree_at(
            result,
            parent_fd,
            parent,
            target_name,
            expected_parent_identity=parent_identity,
            limits=limits,
        )
    finally:
        os.close(parent_fd)


def write_execution_result_at(
    parent_fd: int,
    file_name: str,
    result: InterfaceOutputExecutionResult,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Publish a path-free response through a caller-bound directory."""

    if not isinstance(result, InterfaceOutputExecutionResult):
        raise TypeError("result must be an InterfaceOutputExecutionResult.")
    target_name = _publication_leaf(file_name)
    parent_identity = _require_directory_fd(
        parent_fd,
        expected_parent_identity,
    )
    temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _require_directory_fd(parent_fd, parent_identity)
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        _require_directory_fd(parent_fd, parent_identity)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def write_execution_result(path: Path, result: InterfaceOutputExecutionResult) -> None:
    """Bind a path once, then publish one path-free response atomically."""

    target = Path(os.path.abspath(Path(path).expanduser()))
    target_name = _publication_leaf(target.name)
    parent_fd, parent, parent_identity = _bind_real_directory(
        target.parent,
        create=True,
    )
    try:
        _require_directory_binding(parent, parent_identity)
        write_execution_result_at(
            parent_fd,
            target_name,
            result,
            expected_parent_identity=parent_identity,
        )
        _require_directory_binding(parent, parent_identity)
    finally:
        os.close(parent_fd)


__all__ = [
    "ImmutableInterfaceOutputTree",
    "InterfaceOutputSnapshotLimits",
    "InterfaceOutputExecutionError",
    "InterfaceOutputExecutionRejected",
    "InterfaceOutputExecutionRequest",
    "InterfaceOutputExecutionResult",
    "InterfaceOutputExecutionUnavailable",
    "InterfaceOutputActionSpec",
    "InterfaceOutputResultFile",
    "LocalExecutionResultTree",
    "LocalContainerExecutionLimits",
    "LocalContainerInterfaceOutputExecutor",
    "OriginatingPreparedRuntime",
    "OUTPUT_EXECUTION_REQUEST_SCHEMA",
    "OUTPUT_EXECUTION_RESULT_SCHEMA",
    "export_execution_result_tree",
    "export_execution_result_tree_at",
    "failed_execution_result",
    "snapshot_interface_output_tree",
    "write_execution_result",
    "write_execution_result_at",
]
