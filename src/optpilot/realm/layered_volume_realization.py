"""Descriptor-safe local fallback for immutable-lower + writable-upper scopes.

Portable runtime records describe a layered writable scope.  This module is
only the local provider's physical fallback: it compiles exact files from an
already verified read-only projection, rejects non-identical collisions, and
realizes the effective tree into a pinned fresh writable volume.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, Tuple

from ..runtime_binding import ProjectedInputLayer

from .errors import RealmIntegrityError
from .filesystem_quota import FilesystemQuota
from .manifests import validate_portable_path
from .projection import _remove_tree_contents
from .refs import request_digest


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class _EffectiveNode:
    path: str
    kind: str
    digest: str | None = None
    size: int | None = None
    executable: bool | None = None
    source_path: str | None = None

    def semantic(self) -> tuple[object, ...]:
        return (self.kind, self.digest, self.size, self.executable)

    def portable(self) -> dict[str, object]:
        if self.kind == "directory":
            return {"path": self.path, "type": "directory"}
        return {
            "digest": self.digest,
            "executable": self.executable,
            "path": self.path,
            "size": self.size,
            "type": "file",
        }


@dataclass(frozen=True)
class LocalLayeredVolumePlan:
    nodes: Tuple[_EffectiveNode, ...]
    logical_bytes: int
    digest: str


def compile_local_layered_volume_plan(
    source_root: Path,
    lower_layers: Sequence[ProjectedInputLayer],
    quota: FilesystemQuota,
    *,
    progress: Callable[[], None] | None = None,
) -> LocalLayeredVolumePlan:
    """Compile exact effective lower content from one verified projection."""

    source_root = Path(source_root)
    if not source_root.is_absolute():
        raise ValueError("layered volume source_root must be absolute.")
    if not isinstance(quota, FilesystemQuota):
        raise TypeError("quota must be FilesystemQuota.")
    layers = tuple(sorted(tuple(lower_layers), key=lambda item: item.precedence))
    if not layers or any(
        not isinstance(item, ProjectedInputLayer) for item in layers
    ):
        raise TypeError("lower_layers must contain ProjectedInputLayer values.")
    if tuple(item.precedence for item in layers) != tuple(range(len(layers))):
        raise ValueError("lower_layers precedence is not contiguous.")

    nodes: dict[str, _EffectiveNode] = {}
    spellings: dict[str, str] = {}

    def add(node: _EffectiveNode, *, collision_policy: str) -> None:
        path = validate_portable_path(node.path)
        folded = "/".join(part.casefold() for part in path.split("/"))
        spelling = spellings.get(folded)
        if spelling is not None and spelling != path:
            raise RealmIntegrityError(
                "Layered volume paths collide on a case-insensitive target."
            )
        previous = nodes.get(path)
        if previous is not None:
            if previous.semantic() == node.semantic():
                if collision_policy == "replace" and node.kind == "file":
                    nodes[path] = node
                return
            if collision_policy == "identical":
                raise RealmIntegrityError(
                    f"Layered volume lower entries conflict at {path!r}."
                )
            if collision_policy != "replace":  # defensive typed-layer check
                raise RealmIntegrityError(
                    "Layered volume collision policy is unsupported."
                )
            if node.kind == "file":
                prefix = path + "/"
                for child_path in tuple(nodes):
                    if child_path.startswith(prefix):
                        del nodes[child_path]
                        spellings.pop(
                            "/".join(
                                part.casefold() for part in child_path.split("/")
                            ),
                            None,
                        )
            nodes[path] = node
            return
        nodes[path] = node
        spellings[folded] = path

    def add_parents(path: str, *, collision_policy: str) -> None:
        parts = path.split("/")
        for index in range(1, len(parts)):
            add(
                _EffectiveNode("/".join(parts[:index]), "directory"),
                collision_policy=collision_policy,
            )

    root_fd = os.open(source_root, _DIRECTORY_FLAGS)
    try:
        for layer in layers:
            _progress(progress)
            projected_source = _join_portable(
                layer.projection_subpath, layer.source_subpath
            )
            source_info = _source_info(root_fd, projected_source)
            if stat.S_ISREG(source_info.st_mode):
                if layer.destination_subpath == ".":
                    raise RealmIntegrityError(
                        "A file lower layer requires an explicit destination path."
                    )
                digest, size, executable = _hash_source_file(
                    root_fd, projected_source, progress=progress
                )
                add_parents(
                    layer.destination_subpath,
                    collision_policy=layer.collision_policy,
                )
                add(
                    _EffectiveNode(
                        layer.destination_subpath,
                        "file",
                        digest,
                        size,
                        executable,
                        projected_source,
                    ),
                    collision_policy=layer.collision_policy,
                )
            elif stat.S_ISDIR(source_info.st_mode):
                if layer.destination_subpath != ".":
                    add_parents(
                        layer.destination_subpath,
                        collision_policy=layer.collision_policy,
                    )
                    add(
                        _EffectiveNode(layer.destination_subpath, "directory"),
                        collision_policy=layer.collision_policy,
                    )
                source_fd = _open_source_directory(
                    root_fd, projected_source
                )
                try:
                    _collect_source_directory(
                        source_fd,
                        source_prefix=(
                            "" if projected_source == "." else projected_source
                        ),
                        destination=layer.destination_subpath,
                        add=add,
                        add_parents=add_parents,
                        collision_policy=layer.collision_policy,
                        progress=progress,
                    )
                finally:
                    os.close(source_fd)
            else:
                raise RealmIntegrityError(
                    "Layered volume source is not a regular file or directory."
                )
    finally:
        os.close(root_fd)

    ordered = tuple(sorted(nodes.values(), key=lambda item: item.path.encode("utf-8")))
    sizes = [int(item.size) for item in ordered if item.kind == "file"]
    if (
        len(ordered) > quota.max_entries
        or any(size > quota.max_file_bytes for size in sizes)
        or sum(sizes) > quota.max_total_bytes
    ):
        raise RealmIntegrityError(
            "Layered volume lower content exceeds its writable volume quota."
        )
    payload = {
        "entries": [item.portable() for item in ordered],
        "format": "optpilot.local-layered-volume-plan.v1",
        "logical_bytes": sum(sizes),
    }
    return LocalLayeredVolumePlan(
        ordered, sum(sizes), request_digest(payload)
    )


def realize_local_layered_volume_plan(
    source_root: Path,
    destination_fd: int,
    plan: LocalLayeredVolumePlan,
    *,
    progress: Callable[[], None] | None = None,
) -> None:
    """Reset and realize one exact effective tree into a pinned volume."""

    if not isinstance(plan, LocalLayeredVolumePlan):
        raise TypeError("plan must be a LocalLayeredVolumePlan.")
    _remove_tree_contents(destination_fd)
    _progress(progress)
    directories = sorted(
        (item for item in plan.nodes if item.kind == "directory"),
        key=lambda item: (item.path.count("/"), item.path.encode("utf-8")),
    )
    for item in directories:
        _progress(progress)
        parent_fd, name = _open_destination_parent(destination_fd, item.path)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    source_fd = os.open(Path(source_root), _DIRECTORY_FLAGS)
    try:
        for item in plan.nodes:
            if item.kind != "file":
                continue
            _progress(progress)
            assert item.source_path is not None
            assert item.digest is not None
            assert item.size is not None
            assert item.executable is not None
            input_fd = _open_source_file(source_fd, item.source_path)
            parent_fd, name = _open_destination_parent(destination_fd, item.path)
            output_fd: int | None = None
            try:
                before = os.fstat(input_fd)
                digest, size = _hash_file_descriptor(
                    input_fd, progress=progress
                )
                if (
                    digest != item.digest
                    or size != item.size
                    or bool(before.st_mode & 0o111) != item.executable
                ):
                    raise RealmIntegrityError(
                        "Layered volume source changed before realization."
                    )
                os.lseek(input_fd, 0, os.SEEK_SET)
                output_fd = os.open(
                    name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd
                )
                remaining = item.size
                while remaining:
                    chunk = os.read(input_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise RealmIntegrityError(
                            "Layered volume source shrank during realization."
                        )
                    _progress(progress)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(output_fd, view)
                        if written <= 0:  # pragma: no cover - OS contract
                            raise OSError(
                                "Layered volume realization made no progress."
                                )
                        view = view[written:]
                    remaining -= len(chunk)
                if os.read(input_fd, 1):
                    raise RealmIntegrityError(
                        "Layered volume source grew during realization."
                    )
                os.fchmod(output_fd, 0o700 if item.executable else 0o600)
                os.fsync(output_fd)
                after = os.fstat(input_fd)
                if _identity(before) != _identity(after):
                    raise RealmIntegrityError(
                        "Layered volume source changed during realization."
                    )
                output = os.fstat(output_fd)
                if (
                    not stat.S_ISREG(output.st_mode)
                    or output.st_nlink != 1
                    or output.st_size != item.size
                ):
                    raise RealmIntegrityError(
                        "Layered volume destination file is unsafe."
                    )
                os.fsync(parent_fd)
            finally:
                if output_fd is not None:
                    os.close(output_fd)
                os.close(parent_fd)
                os.close(input_fd)
    finally:
        os.close(source_fd)
    os.fsync(destination_fd)
    validate_local_layered_volume_plan(
        destination_fd, plan, progress=progress
    )


def validate_local_layered_volume_plan(
    destination_fd: int,
    plan: LocalLayeredVolumePlan,
    *,
    progress: Callable[[], None] | None = None,
) -> None:
    actual: dict[str, tuple[object, ...]] = {}
    _collect_actual_tree(
        destination_fd, prefix="", output=actual, progress=progress
    )
    expected = {item.path: item.semantic() for item in plan.nodes}
    if actual != expected:
        raise RealmIntegrityError(
            "Layered writable volume differs from its immutable lower plan."
        )


def _collect_source_directory(
    directory_fd: int,
    *,
    source_prefix: str,
    destination: str,
    add,
    add_parents,
    collision_policy: str,
    progress: Callable[[], None] | None,
) -> None:
    names = tuple(sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8")))
    for name in names:
        _progress(progress)
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        source_path = name if not source_prefix else f"{source_prefix}/{name}"
        target = name if destination == "." else f"{destination}/{name}"
        add_parents(target, collision_policy=collision_policy)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if _identity(before) != _identity(os.fstat(child_fd)):
                    raise RealmIntegrityError(
                        "Layered volume source directory changed."
                    )
                add(
                    _EffectiveNode(target, "directory"),
                    collision_policy=collision_policy,
                )
                _collect_source_directory(
                    child_fd,
                    source_prefix=source_path,
                    destination=target,
                    add=add,
                    add_parents=add_parents,
                    collision_policy=collision_policy,
                    progress=progress,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            try:
                if _identity(before) != _identity(os.fstat(file_fd)):
                    raise RealmIntegrityError(
                        "Layered volume source file changed."
                    )
                digest, size = _hash_file_descriptor(
                    file_fd, progress=progress
                )
                if _identity(before) != _identity(os.fstat(file_fd)):
                    raise RealmIntegrityError(
                        "Layered volume source file changed."
                    )
            finally:
                os.close(file_fd)
            add(
                _EffectiveNode(
                    target,
                    "file",
                    digest,
                    size,
                    bool(before.st_mode & 0o111),
                    source_path,
                ),
                collision_policy=collision_policy,
            )
        else:
            raise RealmIntegrityError(
                "Layered volume source contains an unsupported entry."
            )
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(before) != _identity(after):
            raise RealmIntegrityError(
                "Layered volume source changed during planning."
            )
    if tuple(sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8"))) != names:
        raise RealmIntegrityError("Layered volume source changed during planning.")


def _collect_actual_tree(
    directory_fd: int,
    *,
    prefix: str,
    output: dict[str, tuple[object, ...]],
    progress: Callable[[], None] | None,
) -> None:
    names = tuple(sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8")))
    for name in names:
        _progress(progress)
        path = name if not prefix else f"{prefix}/{name}"
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if _identity(before) != _identity(os.fstat(child_fd)):
                    raise RealmIntegrityError(
                        "Layered volume destination directory changed."
                    )
                output[path] = ("directory", None, None, None)
                _collect_actual_tree(
                    child_fd,
                    prefix=path,
                    output=output,
                    progress=progress,
                )
                after = os.fstat(child_fd)
                linked = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if (
                    _identity(before) != _identity(after)
                    or _identity(before) != _identity(linked)
                ):
                    raise RealmIntegrityError(
                        "Layered volume destination directory changed."
                    )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            try:
                if _identity(before) != _identity(os.fstat(file_fd)):
                    raise RealmIntegrityError(
                        "Layered volume destination file changed."
                    )
                digest, size = _hash_file_descriptor(
                    file_fd, progress=progress
                )
                after = os.fstat(file_fd)
                linked = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if (
                    _identity(before) != _identity(after)
                    or _identity(before) != _identity(linked)
                    or after.st_nlink != 1
                    or linked.st_nlink != 1
                ):
                    raise RealmIntegrityError(
                        "Layered volume destination file changed."
                    )
            finally:
                os.close(file_fd)
            output[path] = (
                "file",
                digest,
                size,
                bool(before.st_mode & 0o111),
            )
        else:
            raise RealmIntegrityError(
                "Layered volume destination contains an unsupported entry."
            )
    if tuple(
        sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8"))
    ) != names:
        raise RealmIntegrityError(
            "Layered volume destination changed during validation."
        )


def _source_info(root_fd: int, path: str) -> os.stat_result:
    if path == ".":
        return os.fstat(root_fd)
    parent_fd, name = _open_source_parent(root_fd, path)
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)


def _open_source_directory(root_fd: int, path: str) -> int:
    if path == ".":
        return os.dup(root_fd)
    parent_fd, name = _open_source_parent(root_fd, path)
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _open_source_file(root_fd: int, path: str) -> int:
    parent_fd, name = _open_source_parent(root_fd, path)
    try:
        return os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _open_source_parent(root_fd: int, path: str) -> tuple[int, str]:
    path = validate_portable_path(path)
    components = path.split("/")
    descriptor = os.dup(root_fd)
    try:
        for component in components[:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, components[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _open_destination_parent(root_fd: int, path: str) -> tuple[int, str]:
    path = validate_portable_path(path)
    components = path.split("/")
    descriptor = os.dup(root_fd)
    try:
        for component in components[:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, components[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _hash_source_file(
    root_fd: int,
    path: str,
    *,
    progress: Callable[[], None] | None,
) -> tuple[str, int, bool]:
    descriptor = _open_source_file(root_fd, path)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RealmIntegrityError("Layered volume source file is unsafe.")
        digest, size = _hash_file_descriptor(
            descriptor, progress=progress
        )
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            raise RealmIntegrityError("Layered volume source file changed.")
        return digest, size, bool(before.st_mode & 0o111)
    finally:
        os.close(descriptor)


def _hash_file_descriptor(
    descriptor: int,
    *,
    progress: Callable[[], None] | None = None,
) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256(b"optpilot/blob/v1\0")
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        _progress(progress)
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _progress(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _join_portable(root: str, relative: str) -> str:
    if root == ".":
        return relative
    if relative == ".":
        return root
    return f"{root}/{relative}"


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


__all__ = [
    "LocalLayeredVolumePlan",
    "compile_local_layered_volume_plan",
    "realize_local_layered_volume_plan",
    "validate_local_layered_volume_plan",
]
