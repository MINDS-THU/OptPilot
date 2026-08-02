"""Provider-neutral immutable tree plans and safe local projections.

Portable projection identity stops at logical paths and content references.  A
``ProjectionContentCapability`` is the only data-plane input accepted by the
compiler/provider: it is expected to be minted from an authorized, fenced
Realm lease and deliberately exposes neither CAS paths nor provider details.

The verified-copy provider is the universal local fallback.  It creates a
fresh private checkout beneath a caller-established projection root, copies
only regular files described by the compiled plan, verifies both sides of each
copy, and returns an explicitly cleaned runtime lease.  It does not implement
process/container bindings or persistent writable workspaces.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ContextManager, Protocol, Tuple

from .errors import (
    ContentCorrupt,
    ContentRejected,
    RealmAuthorizationError,
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
)
from .filesystem_quota import FilesystemQuota
from .manifests import TreeEntry, TreeManifest, validate_portable_path
from .refs import BlobRef, SnapshotRef, request_digest


_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = (
    _READ_FLAGS
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_CREATE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_MAX_TEXT_BYTES = 4096


def _required_text(value: object, label: str, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8.") from error
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _logical_root_or_path(value: object, label: str) -> str:
    if value == ".":
        return "."
    if not isinstance(value, str):
        raise ContentRejected(f"{label} must be '.' or a portable relative path.")
    return validate_portable_path(value)


@dataclass(frozen=True)
class ProjectionQuota(FilesystemQuota):
    """Filesystem-quota defaults for one effective immutable projection."""

    max_entries: int = 100_000
    max_total_bytes: int = 100 * 1024**3
    max_file_bytes: int = 100 * 1024**3

@dataclass(frozen=True)
class TreeMapping:
    """Map one immutable snapshot (or directory subtree) to a logical root."""

    snapshot_ref: SnapshotRef
    destination: str = "."
    source_subpath: str = "."

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("snapshot_ref must be a SnapshotRef.")
        object.__setattr__(
            self, "destination", _logical_root_or_path(self.destination, "mapping destination")
        )
        object.__setattr__(
            self, "source_subpath", _logical_root_or_path(self.source_subpath, "source subpath")
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "destination": self.destination,
            "snapshot_ref": str(self.snapshot_ref),
            "source_subpath": self.source_subpath,
        }


@dataclass(frozen=True)
class ProjectionSpec:
    """Portable, provider-independent projection request.

    In particular, this value cannot contain a host checkout path, CAS path,
    mount path, or backend identifier.  Such operational facts belong to the
    returned ``ProjectionLease`` only.
    """

    owner_id: str
    mappings: Tuple[TreeMapping, ...] = ()
    quota: ProjectionQuota = ProjectionQuota()

    def __post_init__(self) -> None:
        _required_text(self.owner_id, "owner_id")
        mappings = tuple(self.mappings)
        if any(not isinstance(item, TreeMapping) for item in mappings):
            raise TypeError("Projection mappings must be TreeMapping values.")
        if not isinstance(self.quota, ProjectionQuota):
            raise TypeError("quota must be a ProjectionQuota.")
        if len(mappings) > self.quota.max_entries:
            raise ContentRejected("Projection exceeds its mapping-count quota.")
        object.__setattr__(self, "mappings", mappings)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "optpilot.projection-spec.v1",
            "mappings": [mapping.to_dict() for mapping in self.mappings],
            "owner_id": self.owner_id,
            "quota": self.quota.to_dict(),
        }

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())


@dataclass(frozen=True, order=True)
class TreePlanEntry:
    """One effective node after all mapping collisions are resolved."""

    path: str
    kind: str
    blob_ref: BlobRef | None = None
    size: int | None = None
    executable: bool | None = None

    @classmethod
    def directory(cls, path: str) -> "TreePlanEntry":
        return cls(path=path, kind="directory")

    @classmethod
    def file(
        cls, path: str, *, blob_ref: BlobRef, size: int, executable: bool
    ) -> "TreePlanEntry":
        return cls(path, "file", blob_ref, size, executable)

    def __post_init__(self) -> None:
        validate_portable_path(self.path)
        if self.kind == "directory":
            if self.blob_ref is not None or self.size is not None or self.executable is not None:
                raise ValueError("A directory plan entry cannot carry file metadata.")
            return
        if self.kind != "file":
            raise ValueError("A tree plan entry kind must be 'directory' or 'file'.")
        if not isinstance(self.blob_ref, BlobRef):
            raise TypeError("A file plan entry requires a BlobRef.")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("A file plan entry size must be a nonnegative integer.")
        if not isinstance(self.executable, bool):
            raise TypeError("A file plan entry requires an executable boolean.")

    def to_dict(self) -> dict[str, object]:
        if self.kind == "directory":
            return {"path": self.path, "type": "directory"}
        return {
            "blob_ref": str(self.blob_ref),
            "executable": self.executable,
            "path": self.path,
            "size": self.size,
            "type": "file",
        }


@dataclass(frozen=True)
class TreePlan:
    """Deterministic, collision-free effective tree for a projection spec."""

    spec_digest: str
    source_snapshots: Tuple[SnapshotRef, ...]
    entries: Tuple[TreePlanEntry, ...]
    logical_bytes: int

    def __post_init__(self) -> None:
        _required_text(self.spec_digest, "spec_digest", max_bytes=128)
        if len(self.spec_digest) != 64 or any(ch not in "0123456789abcdef" for ch in self.spec_digest):
            raise ValueError("spec_digest must be a lowercase SHA-256 digest.")
        snapshots = tuple(self.source_snapshots)
        entries = tuple(self.entries)
        if any(not isinstance(item, SnapshotRef) for item in snapshots):
            raise TypeError("source_snapshots must contain SnapshotRef values.")
        if any(not isinstance(item, TreePlanEntry) for item in entries):
            raise TypeError("entries must contain TreePlanEntry values.")
        expected = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
        if entries != expected or len({item.path for item in entries}) != len(entries):
            raise ValueError("TreePlan entries must have unique paths in canonical order.")
        if isinstance(self.logical_bytes, bool) or not isinstance(self.logical_bytes, int):
            raise ValueError("logical_bytes must be a nonnegative integer.")
        if self.logical_bytes < 0 or self.logical_bytes != sum(
            item.size or 0 for item in entries if item.kind == "file"
        ):
            raise ValueError("logical_bytes differs from the planned files.")
        object.__setattr__(self, "source_snapshots", snapshots)
        object.__setattr__(self, "entries", entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "format": "optpilot.tree-plan.v1",
            "logical_bytes": self.logical_bytes,
            "source_snapshots": [str(item) for item in self.source_snapshots],
            "spec_digest": self.spec_digest,
        }

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())


class ProjectionContentCapability(Protocol):
    """Fenced, authorized content resolution without physical store paths."""

    @property
    def owner_id(self) -> str: ...

    @property
    def lease_id(self) -> str: ...

    @property
    def fencing_token(self) -> int: ...

    def assert_current(self) -> None: ...

    def load_tree(self, snapshot_ref: SnapshotRef) -> TreeManifest: ...

    def open_blob(self, blob_ref: BlobRef) -> ContextManager[BinaryIO]: ...


def compile_tree_plan(
    spec: ProjectionSpec,
    source: ProjectionContentCapability,
) -> TreePlan:
    """Resolve a portable spec into one canonical collision-free logical tree."""

    if not isinstance(spec, ProjectionSpec):
        raise TypeError("spec must be a ProjectionSpec.")
    if source.owner_id != spec.owner_id:
        raise RealmAuthorizationError("Projection owner is unavailable.")
    source.assert_current()
    entries: dict[str, TreePlanEntry] = {}
    portable_paths: dict[str, str] = {}
    planned_bytes = 0

    def add(entry: TreePlanEntry) -> None:
        nonlocal planned_bytes
        folded = "/".join(component.casefold() for component in entry.path.split("/"))
        previous_spelling = portable_paths.get(folded)
        if previous_spelling is not None and previous_spelling != entry.path:
            raise ContentRejected(
                "Projection paths collide on a case-insensitive target: "
                f"{previous_spelling!r} and {entry.path!r}."
            )
        previous = entries.get(entry.path)
        if previous is not None:
            if previous != entry:
                raise ContentRejected(f"Projection mappings conflict at {entry.path!r}.")
            return
        if len(entries) >= spec.quota.max_entries:
            raise ContentRejected("Projection exceeds its entry quota.")
        if entry.kind == "file":
            assert entry.size is not None
            if entry.size > spec.quota.max_file_bytes:
                raise ContentRejected(f"Projection file {entry.path!r} exceeds its quota.")
            if planned_bytes + entry.size > spec.quota.max_total_bytes:
                raise ContentRejected("Projection exceeds its logical-byte quota.")
            planned_bytes += entry.size
        entries[entry.path] = entry
        portable_paths[folded] = entry.path

    for mapping in spec.mappings:
        manifest = source.load_tree(mapping.snapshot_ref)
        if manifest.snapshot_ref != mapping.snapshot_ref:
            raise ContentCorrupt(
                f"Projection capability returned the wrong manifest for {mapping.snapshot_ref}."
            )
        selected = _select_subtree(manifest, mapping.source_subpath)
        if mapping.destination != ".":
            components = mapping.destination.split("/")
            for index in range(1, len(components) + 1):
                add(TreePlanEntry.directory("/".join(components[:index])))
        for item in selected:
            relative = item.path
            destination = (
                relative
                if mapping.destination == "."
                else f"{mapping.destination}/{relative}"
            )
            if item.kind == "directory":
                add(TreePlanEntry.directory(destination))
            else:
                assert item.blob_ref is not None
                assert item.size is not None
                assert item.executable is not None
                add(
                    TreePlanEntry.file(
                        destination,
                        blob_ref=item.blob_ref,
                        size=item.size,
                        executable=item.executable,
                    )
                )

    for path, entry in entries.items():
        components = path.split("/")
        for index in range(1, len(components)):
            parent = entries.get("/".join(components[:index]))
            if parent is None:
                raise ContentRejected(f"Projection path {path!r} has a missing directory parent.")
            if parent.kind != "directory":
                raise ContentRejected(f"Projection path {path!r} descends through a file.")

    ordered = tuple(sorted(entries.values(), key=lambda item: item.path.encode("utf-8")))
    logical_bytes = sum(item.size or 0 for item in ordered if item.kind == "file")
    if len(ordered) > spec.quota.max_entries:
        raise ContentRejected("Projection exceeds its entry quota.")
    if logical_bytes > spec.quota.max_total_bytes:
        raise ContentRejected("Projection exceeds its logical-byte quota.")
    source.assert_current()
    return TreePlan(
        spec_digest=spec.digest,
        source_snapshots=tuple(mapping.snapshot_ref for mapping in spec.mappings),
        entries=ordered,
        logical_bytes=logical_bytes,
    )


def _select_subtree(manifest: TreeManifest, source_subpath: str) -> Tuple[TreeEntry, ...]:
    if source_subpath == ".":
        return manifest.entries
    root = next((entry for entry in manifest.entries if entry.path == source_subpath), None)
    if root is None:
        raise ContentRejected(f"Projection source subtree does not exist: {source_subpath!r}.")
    if root.kind != "directory":
        raise ContentRejected(f"Projection source subtree is not a directory: {source_subpath!r}.")
    prefix = source_subpath + "/"
    selected = []
    for entry in manifest.entries:
        if not entry.path.startswith(prefix):
            continue
        relative = entry.path[len(prefix) :]
        if entry.kind == "directory":
            selected.append(TreeEntry.directory(relative))
        else:
            assert entry.blob_ref is not None
            assert entry.size is not None
            assert entry.executable is not None
            selected.append(
                TreeEntry.file(
                    relative,
                    blob_ref=entry.blob_ref,
                    size=entry.size,
                    executable=entry.executable,
                )
            )
    return tuple(selected)


@dataclass(frozen=True)
class ProjectionTarget:
    """One fresh child name beneath a caller-established managed root."""

    allowed_root: Path
    directory_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_root, Path):
            object.__setattr__(self, "allowed_root", Path(self.allowed_root))
        if not self.allowed_root.is_absolute():
            raise ValueError("Projection allowed_root must be absolute.")
        value = validate_portable_path(self.directory_name)
        if "/" in value:
            raise ContentRejected("Projection directory_name must be one path component.")
        object.__setattr__(self, "directory_name", value)


class ProjectionLease:
    """Non-portable runtime handle for exactly one fresh checkout.

    The root path is intentionally absent from ``portable_record``.  Callers
    must validate the fence immediately before binding a runtime and must call
    ``cleanup`` explicitly when the projection is no longer in use.
    """

    def __init__(
        self,
        *,
        projection_id: str,
        plan: TreePlan,
        source: ProjectionContentCapability,
        target: ProjectionTarget,
        parent_fd: int,
        root_fd: int,
        provider: str,
        copied_payload_bytes: int,
    ) -> None:
        self.projection_id = _required_text(projection_id, "projection_id")
        self.plan = plan
        self.owner_id = source.owner_id
        self.lease_id = source.lease_id
        self.fencing_token = source.fencing_token
        self.provider = _required_text(provider, "projection provider", max_bytes=128)
        if (
            isinstance(copied_payload_bytes, bool)
            or not isinstance(copied_payload_bytes, int)
            or copied_payload_bytes < 0
        ):
            raise ValueError("copied_payload_bytes must be a nonnegative integer.")
        _required_text(source.owner_id, "projection source owner_id")
        _required_text(source.lease_id, "projection source lease_id")
        _positive_int(source.fencing_token, "projection source fencing_token")
        self.copied_payload_bytes = copied_payload_bytes
        self.created_at = time.time()
        self._source = source
        self._target = target
        self._parent_fd: int | None = parent_fd
        self._root_fd: int | None = root_fd
        parent_info = os.fstat(parent_fd)
        self._parent_identity = (parent_info.st_dev, parent_info.st_ino)
        info = os.fstat(root_fd)
        self._root_identity = (info.st_dev, info.st_ino)
        self._namespace_removed = False
        self._cleaned = False
        self._detached = False
        self._lock = threading.RLock()

    @property
    def cleaned(self) -> bool:
        with self._lock:
            return self._cleaned

    @property
    def detached(self) -> bool:
        """Whether builder descriptors were released after durable publication."""

        with self._lock:
            return self._detached

    @property
    def root_path(self) -> Path:
        self.validate()
        return self._target.allowed_root / self._target.directory_name

    def validate(self) -> None:
        with self._lock:
            if (
                self._cleaned
                or self._detached
                or self._parent_fd is None
                or self._root_fd is None
            ):
                raise RealmExpired("Projection lease is already cleaned.")
            self._source.assert_current()
            _require_directory_path_identity(
                self._target.allowed_root,
                self._parent_fd,
                self._parent_identity,
            )
            _require_link_identity(
                self._parent_fd,
                self._target.directory_name,
                self._root_fd,
                self._root_identity,
            )

    def portable_record(self) -> dict[str, object]:
        """Return evidence facts without any physical checkout/store path."""

        return {
            "fencing_token": self.fencing_token,
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "copied_payload_bytes": self.copied_payload_bytes,
            "plan_digest": self.plan.digest,
            "projection_id": self.projection_id,
            "provider": self.provider,
            "source_snapshots": [str(item) for item in self.plan.source_snapshots],
        }

    def cleanup(self) -> None:
        with self._lock:
            if self._cleaned:
                return
            if self._detached:
                raise RealmIntegrityError(
                    "A durably published projection must be cleaned by its reconciler."
                )
            if self._parent_fd is None or self._root_fd is None:
                raise RealmIntegrityError("Projection cleanup descriptors are unavailable.")
            if not self._namespace_removed:
                _require_link_identity(
                    self._parent_fd,
                    self._target.directory_name,
                    self._root_fd,
                    self._root_identity,
                )
                _remove_tree_contents(
                    self._root_fd, expected_device=self._root_identity[0]
                )
                os.fsync(self._root_fd)
                _require_link_identity(
                    self._parent_fd,
                    self._target.directory_name,
                    self._root_fd,
                    self._root_identity,
                )
                os.rmdir(self._target.directory_name, dir_fd=self._parent_fd)
                # Namespace removal is a distinct retryable phase.  If the
                # parent fsync below fails, a later cleanup call must not look
                # for a link that was already removed successfully.
                self._namespace_removed = True
            os.fsync(self._parent_fd)
            os.close(self._root_fd)
            os.close(self._parent_fd)
            self._root_fd = None
            self._parent_fd = None
            self._cleaned = True

    def detach_after_publish(self) -> None:
        """Release builder descriptors without deleting a durably published tree.

        The caller must first commit the ready realization and its exact
        wrapper/tree identities.  Subsequent consumers reopen independent
        descriptor views; lifecycle cleanup is owned by the durable reconciler.
        """

        with self._lock:
            if self._cleaned:
                raise RealmExpired("Projection lease is already cleaned.")
            if self._detached:
                return
            # Durable publication has already transferred validation and
            # cleanup authority to the ledger-backed namespace.  Revalidating
            # the now-released builder/source lease here can fail after the
            # commit and strand a READY row with live builder descriptors.
            assert self._root_fd is not None and self._parent_fd is not None
            os.close(self._root_fd)
            os.close(self._parent_fd)
            self._root_fd = None
            self._parent_fd = None
            self._detached = True

    def close(self) -> None:
        self.cleanup()

    def __enter__(self) -> "ProjectionLease":
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.cleanup()


class VerifiedCopyProjectionProvider:
    """Descriptor-rooted, hash-verifying local projection fallback."""

    PROVIDER_KIND = "verified-copy.v1"

    def project(
        self,
        *,
        spec: ProjectionSpec,
        source: ProjectionContentCapability,
        target: ProjectionTarget,
    ) -> ProjectionLease:
        if os.name == "nt":  # pragma: no cover - the secure v1 provider is POSIX-only
            raise NotImplementedError("The verified-copy v1 provider requires POSIX descriptors.")
        plan = compile_tree_plan(spec, source)
        parent_fd = _open_projection_parent(target.allowed_root)
        root_fd: int | None = None
        created = False
        created_identity: tuple[int, int] | None = None
        try:
            try:
                os.mkdir(target.directory_name, mode=0o700, dir_fd=parent_fd)
                created = True
                created_info = os.stat(
                    target.directory_name, dir_fd=parent_fd, follow_symlinks=False
                )
                created_identity = (created_info.st_dev, created_info.st_ino)
            except FileExistsError as error:
                raise RealmConflict("Projection destination must be fresh.") from error
            root_fd = os.open(target.directory_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            root_info = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or (root_info.st_dev, root_info.st_ino) != created_identity
                or (os.name != "nt" and root_info.st_nlink < 2)
            ):
                raise RealmIntegrityError("Fresh projection root is not a private directory.")
            os.fsync(parent_fd)

            directories = [entry for entry in plan.entries if entry.kind == "directory"]
            for entry in sorted(
                directories, key=lambda item: (item.path.count("/"), item.path.encode("utf-8"))
            ):
                _create_directory(root_fd, entry.path)

            copied_payload_bytes = 0
            for entry in plan.entries:
                if entry.kind != "file":
                    continue
                assert entry.blob_ref is not None
                assert entry.size is not None
                assert entry.executable is not None
                source.assert_current()
                with source.open_blob(entry.blob_ref) as stream:
                    _copy_verified_file(root_fd, entry, stream)
                source.assert_current()
                copied_payload_bytes += entry.size

            source.assert_current()
            for entry in sorted(
                directories, key=lambda item: (-item.path.count("/"), item.path.encode("utf-8"))
            ):
                directory_fd = _open_relative_directory(root_fd, entry.path)
                try:
                    os.fchmod(directory_fd, 0o500)
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            os.fchmod(root_fd, 0o500)
            os.fsync(root_fd)
            _verify_projection_tree(root_fd, plan)
            _require_link_identity(
                parent_fd,
                target.directory_name,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            source.assert_current()
            return ProjectionLease(
                projection_id=f"projection-{uuid.uuid4().hex}",
                plan=plan,
                source=source,
                target=target,
                parent_fd=parent_fd,
                root_fd=root_fd,
                provider=self.PROVIDER_KIND,
                copied_payload_bytes=copied_payload_bytes,
            )
        except BaseException:
            try:
                if root_fd is not None:
                    try:
                        _remove_tree_contents(
                            root_fd, expected_device=os.fstat(root_fd).st_dev
                        )
                    finally:
                        os.close(root_fd)
                    root_fd = None
                if created:
                    try:
                        os.rmdir(target.directory_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(parent_fd)
            raise


def _open_projection_parent(path: Path) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RealmIntegrityError("Projection target root is unavailable or unsafe.") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise RealmIntegrityError("Projection target root is not a directory.")
        if os.name != "nt":
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise RealmIntegrityError("Projection target root is owned by another user.")
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise RealmIntegrityError("Projection target root is group/world writable.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_relative_directory(root_fd: int, path: str) -> int:
    current = os.dup(root_fd)
    try:
        for component in path.split("/"):
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _open_parent_directory(root_fd: int, path: str) -> tuple[int, str]:
    components = path.split("/")
    current = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, components[-1]
    except BaseException:
        os.close(current)
        raise


def _create_directory(root_fd: int, path: str) -> None:
    parent_fd, name = _open_parent_directory(root_fd, path)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            info = os.fstat(child_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise RealmIntegrityError(f"Projection directory is unsafe: {path!r}.")
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise RealmIntegrityError(f"Fresh projection unexpectedly contains {path!r}.") from error
    finally:
        os.close(parent_fd)


def _copy_verified_file(root_fd: int, entry: TreePlanEntry, source: BinaryIO) -> None:
    assert entry.blob_ref is not None
    assert entry.size is not None
    assert entry.executable is not None
    parent_fd, name = _open_parent_directory(root_fd, entry.path)
    destination_fd: int | None = None
    try:
        destination_fd = os.open(name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
        before = os.fstat(destination_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RealmIntegrityError(f"Projection file is unsafe: {entry.path!r}.")
        source_hash = hashlib.sha256(b"optpilot/blob/v1\0")
        copied = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ContentCorrupt("Projection blob source did not return bytes.")
            source_hash.update(chunk)
            if copied + len(chunk) > entry.size:
                raise ContentCorrupt(f"Projection source is larger than {entry.blob_ref}.")
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("Projection copy made no write progress.")
                view = view[written:]
        if copied != entry.size or source_hash.hexdigest() != entry.blob_ref.digest:
            raise ContentCorrupt(f"Projection source bytes differ from {entry.blob_ref}.")
        os.fsync(destination_fd)
        os.lseek(destination_fd, 0, os.SEEK_SET)
        destination_hash = hashlib.sha256(b"optpilot/blob/v1\0")
        verified = 0
        while True:
            chunk = os.read(destination_fd, 1024 * 1024)
            if not chunk:
                break
            destination_hash.update(chunk)
            verified += len(chunk)
        after = os.fstat(destination_fd)
        if (
            verified != entry.size
            or destination_hash.hexdigest() != entry.blob_ref.digest
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
        ):
            raise ContentCorrupt(f"Projection destination verification failed: {entry.path!r}.")
        os.fchmod(destination_fd, 0o500 if entry.executable else 0o400)
        os.fsync(destination_fd)
        os.fsync(parent_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(parent_fd)


def _require_directory_path_identity(
    path: Path,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    try:
        linked = os.stat(path, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as error:
        raise RealmIntegrityError("Projection target root path was removed or replaced.") from error
    if (
        not stat.S_ISDIR(linked.st_mode)
        or (linked.st_dev, linked.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError("Projection target root path was removed or replaced.")


def _verify_projection_tree(root_fd: int, plan: TreePlan) -> None:
    """Rewalk the final visible checkout and match the compiled plan exactly."""

    root_info = os.fstat(root_fd)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o500:
        raise ContentCorrupt("Projection root mode changed before publication.")
    expected_device = root_info.st_dev
    actual: dict[str, tuple[str, str | None, int | None, int]] = {}

    def walk(directory_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory_fd), key=lambda value: value.encode("utf-8")):
            path = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if info.st_dev != expected_device:
                raise RealmIntegrityError("Projection verification crossed a filesystem boundary.")
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise RealmIntegrityError(
                            "Projection directory changed during final verification."
                        )
                    mode = stat.S_IMODE(opened.st_mode)
                    actual[path] = ("directory", None, None, mode)
                    walk(child_fd, path)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContentCorrupt(
                    f"Projection contains an unsupported final entry: {path!r}."
                )
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise RealmIntegrityError(
                        "Projection file changed during final verification."
                    )
                digest, size = _hash_projection_blob_fd(file_fd)
                actual[path] = (
                    "file",
                    digest,
                    size,
                    stat.S_IMODE(opened.st_mode),
                )
            finally:
                os.close(file_fd)

    walk(root_fd, "")
    expected_paths = {entry.path for entry in plan.entries}
    if set(actual) != expected_paths:
        raise ContentCorrupt("Projection final tree differs from its compiled path set.")
    for entry in plan.entries:
        kind, digest, size, mode = actual[entry.path]
        if entry.kind == "directory":
            if kind != "directory" or mode != 0o500:
                raise ContentCorrupt(
                    f"Projection directory differs from its plan: {entry.path!r}."
                )
            continue
        assert entry.blob_ref is not None
        assert entry.size is not None
        assert entry.executable is not None
        if (
            kind != "file"
            or digest != entry.blob_ref.digest
            or size != entry.size
            or mode != (0o500 if entry.executable else 0o400)
        ):
            raise ContentCorrupt(
                f"Projection file differs from its plan: {entry.path!r}."
            )


def _hash_projection_blob_fd(descriptor: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256(b"optpilot/blob/v1\0")
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _require_link_identity(
    parent_fd: int,
    name: str,
    root_fd: int,
    expected: tuple[int, int],
) -> None:
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise RealmIntegrityError("Projection checkout disappeared before cleanup.") from error
    opened = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(linked.st_mode)
        or (linked.st_dev, linked.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise RealmIntegrityError("Projection checkout path was replaced.")


def _projection_mount_identity(descriptor: int) -> tuple[object, ...]:
    """Return a mount-aware identity, failing closed when none is available."""

    current = os.fstat(descriptor)
    if sys.platform.startswith("linux"):
        try:
            with open(
                f"/proc/self/fdinfo/{descriptor}", "r", encoding="ascii"
            ) as stream:
                for line in stream:
                    if line.startswith("mnt_id:"):
                        return ("linux-mnt-id", int(line.split(":", 1)[1]))
        except (OSError, ValueError) as error:
            raise RealmIntegrityError(
                "Projection cleanup cannot determine its Linux mount identity."
            ) from error
        raise RealmIntegrityError(
            "Projection cleanup cannot determine its Linux mount identity."
        )
    try:
        filesystem_id = os.fstatvfs(descriptor).f_fsid
    except (AttributeError, OSError) as error:
        raise RealmIntegrityError(
            "Projection cleanup cannot determine its filesystem mount identity."
        ) from error
    return ("statvfs-fsid", current.st_dev, filesystem_id)


def _remove_tree_contents(
    directory_fd: int,
    *,
    expected_device: int | None = None,
    expected_mount_identity: tuple[object, ...] | None = None,
) -> None:
    current = os.fstat(directory_fd)
    if expected_device is None:
        expected_device = current.st_dev
    if expected_mount_identity is None:
        expected_mount_identity = _projection_mount_identity(directory_fd)
    if current.st_dev != expected_device:
        raise RealmIntegrityError("Projection cleanup crossed a filesystem boundary.")
    if _projection_mount_identity(directory_fd) != expected_mount_identity:
        raise RealmIntegrityError("Projection cleanup crossed a mount boundary.")
    os.fchmod(directory_fd, 0o700)
    for name in sorted(os.listdir(directory_fd)):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if info.st_dev != expected_device:
            raise RealmIntegrityError("Projection cleanup encountered a mounted external tree.")
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise RealmIntegrityError("Projection directory changed during cleanup.")
                if _projection_mount_identity(child_fd) != expected_mount_identity:
                    raise RealmIntegrityError(
                        "Projection cleanup encountered a nested mount."
                    )
                _remove_tree_contents(
                    child_fd,
                    expected_device=expected_device,
                    expected_mount_identity=expected_mount_identity,
                )
                os.fsync(child_fd)
                _require_link_identity(
                    directory_fd,
                    name,
                    child_fd,
                    (opened.st_dev, opened.st_ino),
                )
                os.rmdir(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(child_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


__all__ = [
    "ProjectionContentCapability",
    "ProjectionLease",
    "ProjectionQuota",
    "ProjectionSpec",
    "ProjectionTarget",
    "TreeMapping",
    "TreePlan",
    "TreePlanEntry",
    "VerifiedCopyProjectionProvider",
    "compile_tree_plan",
    "_projection_mount_identity",
]
