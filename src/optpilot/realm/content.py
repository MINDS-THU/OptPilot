"""Verified local blob/tree sealing for the realm content plane.

The store is deliberately a low-level filesystem service.  It publishes bytes
only through generated private names and reports each durable publication to a
retention adapter; authorization, owner commit, and reachability remain ledger
responsibilities.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Protocol, Sequence, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - secure v1 capture is POSIX-only
    fcntl = None  # type: ignore[assignment]

from ._validation import freeze_json, thaw_json
from .config import prepare_private_directory
from .errors import ContentCorrupt, ContentRejected, RealmIntegrityError, SourceChanged
from .manifests import (
    BlobManifest,
    SealLimits,
    TreeEntry,
    TreeManifest,
    validate_portable_path,
    validate_portable_paths,
)
from .refs import (
    BlobRef,
    SnapshotRef,
    canonical_json_bytes,
    parse_content_ref,
    request_digest,
)


LocalObjectRef = Union[BlobRef, SnapshotRef]
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
# Opening an attacker-controlled FIFO for reading must not wait for a writer
# before we can reject it by type.  O_NONBLOCK is inert for regular files.
_FILE_FLAGS = (
    _READ_FLAGS
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_STORE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
# macOS 26 and some macOS/file-provider volumes attach these OS-maintained
# provenance, recency, and transparent-compression markers to ordinary files.
# They do not add semantics beyond the bytes returned by the open descriptor,
# so they are deliberately neither hashed nor serialized.  In particular,
# ``com.apple.decmpfs`` may contain the on-disk compressed representation, but
# descriptor reads already return the canonical uncompressed file bytes that
# OptPilot seals.  Docker Desktop's ownership transport marker is handled
# separately and accepted only when its encoded permission mode is identical
# to the descriptor mode.  Every other xattr, including quarantine, resource
# forks, ACL/security metadata, and arbitrary user values, remains rejected.
DARWIN_IGNORED_NONCONTENT_XATTRS = frozenset(
    {
        "com.apple.decmpfs",
        "com.apple.provenance",
        "com.apple.lastuseddate#PS",
    }
)
_DARWIN_DOCKER_OWNERSHIP_XATTR = "com.docker.grpcfuse.ownership"
_MAX_DARWIN_DOCKER_OWNERSHIP_XATTR_BYTES = 1024
_MAX_MANIFEST_BYTES = 128 * 1024**2
_MAX_MARKER_BYTES = 64 * 1024


@dataclass(frozen=True)
class AllowedTreeSource:
    """A relative selection beneath a caller-established trust root.

    ``excluded_directory_names`` is deliberately narrower than a glob or
    ignore-file language.  It lets a trusted caller omit well-known generated
    directory trees (for example ``node_modules``) while the content service
    still performs one descriptor-rooted, race-detecting capture of every
    selected byte.
    """

    allowed_root: Path
    relative_selection: str = "."
    expected_generation: Optional[int] = None
    excluded_directory_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_root, Path):
            object.__setattr__(self, "allowed_root", Path(self.allowed_root))
        if not isinstance(self.relative_selection, str):
            raise TypeError("relative_selection must be a string.")
        if self.expected_generation is not None and (
            isinstance(self.expected_generation, bool)
            or not isinstance(self.expected_generation, int)
            or self.expected_generation < 0
        ):
            raise ValueError("expected_generation must be a nonnegative integer.")
        if not isinstance(self.excluded_directory_names, tuple):
            raise TypeError("excluded_directory_names must be a tuple of names.")
        names = self.excluded_directory_names
        if any(not isinstance(name, str) for name in names):
            raise TypeError("excluded_directory_names must contain only strings.")
        if any("/" in name or "\\" in name for name in names):
            raise ValueError(
                "excluded_directory_names entries must be single directory names."
            )
        if len(set(names)) != len(names) or names != tuple(
            sorted(names, key=lambda name: name.encode("utf-8"))
        ):
            raise ValueError(
                "excluded_directory_names must be unique and sorted canonically."
            )
        try:
            canonical = validate_portable_paths(names)
        except ContentRejected as error:
            raise ValueError(
                "excluded_directory_names contains an invalid directory name."
            ) from error
        if names != canonical:
            raise ValueError(
                "excluded_directory_names must be unique and sorted canonically."
            )


@dataclass(frozen=True)
class AllowedFileSource:
    """One relative regular file beneath a caller-established trust root."""

    allowed_root: Path
    relative_file: str
    expected_generation: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_root, Path):
            object.__setattr__(self, "allowed_root", Path(self.allowed_root))
        if not isinstance(self.relative_file, str) or not self.relative_file:
            raise ValueError("relative_file must be a non-empty string.")
        if self.expected_generation is not None and (
            isinstance(self.expected_generation, bool)
            or not isinstance(self.expected_generation, int)
            or self.expected_generation < 0
        ):
            raise ValueError("expected_generation must be a nonnegative integer.")


@dataclass(frozen=True)
class ContentEdge:
    parent_ref: SnapshotRef
    child_ref: BlobRef
    role: str
    canonical_path: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, str)
            or not self.role
            or len(self.role.encode("utf-8")) > 128
        ):
            raise ValueError("content edge role must be a non-empty bounded string")
        validate_portable_path(self.canonical_path)


@dataclass(frozen=True)
class PublishedObject:
    """Ledger-ready facts about one durably published local object."""

    staging_id: str
    store_id: str
    content_ref: LocalObjectRef
    kind: str
    logical_bytes: int
    physical_bytes: int
    metadata: Mapping[str, Any]
    edges: Tuple[ContentEdge, ...] = ()

    def __post_init__(self) -> None:
        _validate_staging_id(self.staging_id)
        _validate_store_id(self.store_id)
        if self.kind not in {"blob", "tree"}:
            raise ValueError("local publication kind must be 'blob' or 'tree'")
        if self.kind == "blob" and not isinstance(self.content_ref, BlobRef):
            raise ValueError("blob publication requires a BlobRef")
        if self.kind == "tree" and not isinstance(self.content_ref, SnapshotRef):
            raise ValueError("tree publication requires a SnapshotRef")
        for field_name in ("logical_bytes", "physical_bytes"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if self.kind == "blob" and self.edges:
            raise ValueError("blob publications cannot have content edges")
        if any(edge.parent_ref != self.content_ref for edge in self.edges):
            raise ValueError("publication edge parent differs from the tree ref")
        object.__setattr__(self, "metadata", freeze_json(self.metadata, label="publication metadata"))


@dataclass(frozen=True)
class TreeSealReceipt:
    snapshot_ref: SnapshotRef
    manifest: TreeManifest
    publications: Tuple[PublishedObject, ...]


@dataclass(frozen=True)
class CompletedTreeCapture:
    """Compact ledger reconstruction of one operation-keyed tree capture."""

    snapshot_ref: SnapshotRef
    publications: Tuple[PublishedObject, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("completed tree capture requires a SnapshotRef")
        if not self.publications:
            raise ValueError("completed tree capture must contain publications")
        if len({item.staging_id for item in self.publications}) != len(
            self.publications
        ):
            raise ValueError("completed tree capture staging ids must be unique")
        stores = {item.store_id for item in self.publications}
        if len(stores) != 1:
            raise ValueError("completed tree capture publications must share one store")
        if any(item.kind != "blob" for item in self.publications[:-1]):
            raise ValueError("completed tree capture may contain only blobs before its root")
        root = self.publications[-1]
        if root.kind != "tree" or root.content_ref != self.snapshot_ref:
            raise ValueError("completed tree capture must end with its tree root")


@dataclass(frozen=True)
class BlobSealReceipt:
    blob_ref: BlobRef
    publication: PublishedObject


class ContentRetentionAdapter(Protocol):
    """Small boundary expected from the concurrent RealmLedger work.

    Implementations must make all methods idempotent by ``staging_id``.  A
    failure after physical publication intentionally leaves an allocation for
    recovery rather than guessing whether the ledger observed the publication.
    """

    def validate_capture_binding(
        self,
        *,
        change_id: str,
        store_id: str,
        backend_kind: str,
        root_marker: str,
    ) -> None: ...

    def reserve_staging(
        self,
        *,
        change_id: str,
        staging_id: str,
        store_id: str,
        object_kind: str,
    ) -> None: ...

    def record_publication(
        self,
        *,
        change_id: str,
        publication: PublishedObject,
    ) -> None: ...

    def complete_staging_publication(
        self,
        *,
        change_id: str,
        staging_id: str,
    ) -> None: ...

    def prepare_publication(
        self,
        *,
        change_id: str,
        publication: PublishedObject,
    ) -> None: ...

    def abandon_staging(
        self,
        *,
        change_id: str,
        staging_id: str,
    ) -> None: ...

    def rollback_capture(
        self,
        *,
        change_id: str,
        staging_ids: Sequence[str],
    ) -> None: ...

    def record_completed_tree_capture(
        self,
        *,
        change_id: str,
        operation_id: str,
        completion: CompletedTreeCapture,
    ) -> None: ...

    def recover_completed_tree_capture(
        self,
        *,
        change_id: str,
        operation_id: str,
    ) -> Optional[CompletedTreeCapture]: ...

    def validate_composition_binding(
        self,
        *,
        change_id: str,
        composition_request_digest: str,
    ) -> None: ...


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
class _InventoryNode:
    raw_path: str
    kind: str
    identity: _Identity


@dataclass(frozen=True)
class _Inventory:
    root_identity: _Identity
    nodes: Tuple[_InventoryNode, ...]
    logical_bytes: int


@dataclass(frozen=True)
class LocalContentCapture:
    """One immutable, change-scoped mutation view over a local store.

    The physical store can be opened and registered without owning mutation
    authority.  Every seal or recovery operation instead goes through this
    bound view, whose authority adapter is expected to enforce the same change
    and store in the RealmLedger transaction.
    """

    store: "LocalContentStore"
    change_id: str
    authority: ContentRetentionAdapter

    def __post_init__(self) -> None:
        if not isinstance(self.change_id, str) or not self.change_id:
            raise ValueError("change_id is required.")
        self.authority.validate_capture_binding(
            change_id=self.change_id,
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )

    def seal_tree(
        self,
        *,
        source: AllowedTreeSource,
        limits: Optional[SealLimits] = None,
        operation_id: Optional[str] = None,
    ) -> TreeSealReceipt:
        """Seal a tree, optionally with durable post-completion replay.

        A keyed seal is complete only after its compact ledger completion
        record commits.  Retrying the same key then verifies and reconstructs
        the exact receipt without reading the source tree.  Allocations left by
        a crash before that completion boundary remain ordinary partial
        capture work; this API deliberately does not guess that they comprise
        a complete tree.
        """

        if operation_id is not None:
            completed = self.authority.recover_completed_tree_capture(
                change_id=self.change_id,
                operation_id=operation_id,
            )
            if completed is not None:
                return self.store._verified_tree_seal_receipt(completed)
        receipt = self.store._seal_tree(
            change_id=self.change_id,
            authority=self.authority,
            source=source,
            limits=limits,
        )
        if operation_id is not None:
            self.authority.record_completed_tree_capture(
                change_id=self.change_id,
                operation_id=operation_id,
                completion=CompletedTreeCapture(
                    snapshot_ref=receipt.snapshot_ref,
                    publications=receipt.publications,
                ),
            )
        return receipt

    def seal_blob(
        self,
        *,
        source: AllowedFileSource,
        limits: Optional[SealLimits] = None,
    ) -> BlobSealReceipt:
        return self.store._seal_blob(
            change_id=self.change_id,
            authority=self.authority,
            source=source,
            limits=limits,
        )

    def recover_completed_tree_manifest(
        self,
        *,
        operation_id: str,
    ) -> TreeSealReceipt | None:
        """Recover one exact completed tree publication without new mutation."""

        completed = self.authority.recover_completed_tree_capture(
            change_id=self.change_id,
            operation_id=operation_id,
        )
        if completed is None:
            return None
        return self.store._verified_tree_seal_receipt(completed)

    def publish_composed_tree_manifest(
        self,
        *,
        manifest: TreeManifest,
        composition_request_digest: str,
        operation_id: str | None = None,
    ) -> TreeSealReceipt:
        """Publish one verified tree manifest without recapturing blob bytes.

        A higher-level composition service must prove that every referenced
        file belongs to authorized retained source trees before calling this
        narrow primitive.
        """

        if not isinstance(manifest, TreeManifest):
            raise TypeError("manifest must be a TreeManifest.")
        if (
            not isinstance(composition_request_digest, str)
            or len(composition_request_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in composition_request_digest
            )
        ):
            raise ValueError(
                "composition_request_digest must be a lowercase SHA-256 digest."
            )
        if operation_id is not None:
            receipt = self.recover_completed_tree_manifest(operation_id=operation_id)
            if receipt is not None:
                if receipt.manifest != manifest:
                    raise RealmIntegrityError(
                        "Completed composed tree differs from the requested manifest."
                    )
                return receipt
        self.authority.validate_composition_binding(
            change_id=self.change_id,
            composition_request_digest=composition_request_digest,
        )
        self.store._verify_composed_tree_children(manifest)
        publication = self.store._publish_tree_manifest(
            change_id=self.change_id,
            authority=self.authority,
            manifest=manifest,
        )
        receipt = TreeSealReceipt(
            snapshot_ref=manifest.snapshot_ref,
            manifest=manifest,
            publications=(publication,),
        )
        if operation_id is not None:
            self.authority.record_completed_tree_capture(
                change_id=self.change_id,
                operation_id=operation_id,
                completion=CompletedTreeCapture(
                    snapshot_ref=receipt.snapshot_ref,
                    publications=receipt.publications,
                ),
            )
        return receipt

    def recover_prepared_publication(self, *, staging_id: str) -> PublishedObject:
        return self.store._recover_prepared_publication(
            change_id=self.change_id,
            authority=self.authority,
            staging_id=staging_id,
        )


class LocalContentStore:
    """Private local CAS using verified blob copies and tree manifests."""

    STORE_FORMAT = "optpilot.local-content-store.v1"
    BACKEND_KIND = "local-cas"

    def __init__(
        self,
        root: Path,
        *,
        store_id: str,
    ) -> None:
        _validate_store_id(store_id)
        self.store_id = store_id
        self.root = prepare_private_directory(root)
        self.objects = self.root / "objects"
        self.blobs = self.objects / "blobs"
        self.trees = self.objects / "trees"
        self.staging = self.root / "staging"
        self.trash = self.root / "trash"
        self._namespace_fds: Dict[str, int] = {}
        self._namespace_identities: Dict[str, Tuple[int, int]] = {}
        self._object_kind_fds: Dict[str, int] = {}
        self._object_kind_identities: Dict[str, Tuple[int, int]] = {}
        self._lock_fd: Optional[int] = None
        root_fd = os.open(self.root, _DIRECTORY_FLAGS)
        try:
            root_info = os.fstat(root_fd)
            self._root_device_inode = (root_info.st_dev, root_info.st_ino)
            objects_fd = _prepare_private_child_directory(root_fd, "objects")
            self._register_namespace_fd("objects", objects_fd)
            blobs_fd = _prepare_private_child_directory(objects_fd, "blobs")
            self._register_object_kind_fd("blobs", blobs_fd)
            trees_fd = _prepare_private_child_directory(objects_fd, "trees")
            self._register_object_kind_fd("trees", trees_fd)
            staging_fd = _prepare_private_child_directory(root_fd, "staging")
            self._register_namespace_fd("staging", staging_fd)
            trash_fd = _prepare_private_child_directory(root_fd, "trash")
            self._register_namespace_fd("trash", trash_fd)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(root_fd)
        self.ignored_source_xattrs = (
            DARWIN_IGNORED_NONCONTENT_XATTRS if sys.platform == "darwin" else frozenset()
        )
        self.lock_path = self.root / ".store.lock"
        self._prepare_lock_file()
        self.root_marker = self._load_or_create_marker()

    def close(self) -> None:
        lock_fd = getattr(self, "_lock_fd", None)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        for descriptor in tuple(getattr(self, "_namespace_fds", {}).values()):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if hasattr(self, "_namespace_fds"):
            self._namespace_fds.clear()
        for descriptor in tuple(getattr(self, "_object_kind_fds", {}).values()):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if hasattr(self, "_object_kind_fds"):
            self._object_kind_fds.clear()

    def __enter__(self) -> "LocalContentStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def capture(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
    ) -> LocalContentCapture:
        return LocalContentCapture(self, change_id, authority)

    def _seal_tree(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        source: AllowedTreeSource,
        limits: Optional[SealLimits] = None,
    ) -> TreeSealReceipt:
        """Capture one stable directory selection into blob CAS plus a tree manifest."""

        if not isinstance(change_id, str) or not change_id:
            raise ValueError("change_id is required.")
        self._assert_root_identity()
        if source.expected_generation is not None:
            raise ContentRejected(
                "expected_generation requires the future workspace revision coordinator."
            )
        limits = limits or SealLimits()
        root_fd = _open_selected_directory(source.allowed_root, source.relative_selection)
        publications = []
        try:
            before = _inventory(
                root_fd,
                limits=limits,
                ignored_xattrs=self.ignored_source_xattrs,
                excluded_directory_names=source.excluded_directory_names,
            )
            entries = []
            for node in before.nodes:
                if node.kind == "directory":
                    entries.append(TreeEntry.directory(node.raw_path))
                    continue
                file_fd = _open_relative_file(root_fd, node.raw_path)
                try:
                    _require_identity(os.fstat(file_fd), node.identity, node.raw_path)
                    publication = self._publish_blob_from_fd(
                        change_id=change_id,
                        authority=authority,
                        source_fd=file_fd,
                        expected=node.identity,
                    )
                    # Record the durable hold before any subsequent source
                    # stability check can fail, so rollback has the exact set.
                    publications.append(publication)
                    _require_identity(os.fstat(file_fd), node.identity, node.raw_path)
                finally:
                    os.close(file_fd)
                entries.append(
                    TreeEntry.file(
                        node.raw_path,
                        blob_ref=_require_blob_ref(publication.content_ref),
                        size=node.identity.size,
                        executable=bool(node.identity.mode & 0o111),
                    )
                )

            after = _inventory(
                root_fd,
                limits=limits,
                ignored_xattrs=self.ignored_source_xattrs,
                excluded_directory_names=source.excluded_directory_names,
            )
            if after != before:
                raise SourceChanged("Source tree changed during capture.")
            manifest = TreeManifest.build(entries, limits=limits)
            tree_publication = self._publish_tree_manifest(
                change_id=change_id,
                authority=authority,
                manifest=manifest,
            )
            publications.append(tree_publication)
            return TreeSealReceipt(
                snapshot_ref=manifest.snapshot_ref,
                manifest=manifest,
                publications=tuple(publications),
            )
        except BaseException:
            if publications:
                self._rollback_capture(change_id, publications, authority=authority)
            raise
        finally:
            os.close(root_fd)

    def _seal_blob(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        source: AllowedFileSource,
        limits: Optional[SealLimits] = None,
    ) -> BlobSealReceipt:
        """Seal one descriptor-rooted regular file without inventing a one-file tree."""

        if not isinstance(change_id, str) or not change_id:
            raise ValueError("change_id is required.")
        self._assert_root_identity()
        if source.expected_generation is not None:
            raise ContentRejected(
                "expected_generation requires the future workspace revision coordinator."
            )
        limits = limits or SealLimits()
        file_fd = _open_selected_file(source.allowed_root, source.relative_file)
        publications = []
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ContentRejected("Selected blob source is not a regular file.")
            _reject_sparse(before, source.relative_file)
            _reject_xattrs(
                file_fd,
                source.relative_file,
                ignored=self.ignored_source_xattrs,
            )
            if before.st_size > min(limits.max_file_bytes, limits.max_total_bytes):
                raise ContentRejected("Selected blob exceeds the configured seal limits.")
            publication = self._publish_blob_from_fd(
                change_id=change_id,
                authority=authority,
                source_fd=file_fd,
                expected=_identity(before),
            )
            publications.append(publication)
            _require_identity(os.fstat(file_fd), _identity(before), source.relative_file)
            return BlobSealReceipt(
                blob_ref=_require_blob_ref(publication.content_ref),
                publication=publication,
            )
        except BaseException:
            if publications:
                self._rollback_capture(change_id, publications, authority=authority)
            raise
        finally:
            os.close(file_fd)

    def verify_blob(self, blob_ref: BlobRef) -> BlobManifest:
        self._assert_root_identity()
        directory_fd = self._open_live_object_fd(blob_ref)
        try:
            _require_immutable_object_directory(directory_fd, blob_ref)
            return _verify_blob_fd(directory_fd, blob_ref)
        finally:
            os.close(directory_fd)

    def verify_tree(
        self,
        snapshot_ref: SnapshotRef,
        *,
        verify_children: bool = True,
    ) -> TreeManifest:
        self._assert_root_identity()
        directory_fd = self._open_live_object_fd(snapshot_ref)
        try:
            _require_immutable_object_directory(directory_fd, snapshot_ref)
            manifest = _verify_tree_fd(directory_fd, snapshot_ref)
        finally:
            os.close(directory_fd)
        if verify_children:
            self._verify_composed_tree_children(manifest)
        return manifest

    def _verify_composed_tree_children(self, manifest: TreeManifest) -> None:
        """Verify every physical child before a manifest-only tree is published.

        Ledger composition leases prove authority to reference retained blobs;
        they do not prove that the caller supplied truthful sizes.  Checking the
        live CAS bytes here keeps malformed manifests out of both the physical
        store and the ledger, including for callers of the narrow store capture
        primitive.
        """

        if not isinstance(manifest, TreeManifest):
            raise TypeError("manifest must be a TreeManifest.")
        verified_sizes: Dict[BlobRef, int] = {}
        for entry in manifest.entries:
            if entry.kind != "file":
                continue
            assert entry.blob_ref is not None
            child_size = verified_sizes.get(entry.blob_ref)
            if child_size is None:
                child_size = self.verify_blob(entry.blob_ref).size
                verified_sizes[entry.blob_ref] = child_size
            if child_size != entry.size:
                raise ContentCorrupt(
                    f"Tree entry size differs from blob manifest: {entry.path!r}."
                )

    def _verified_tree_seal_receipt(
        self, completion: CompletedTreeCapture
    ) -> TreeSealReceipt:
        """Verify exact live bytes before returning a recovered capture."""

        if not isinstance(completion, CompletedTreeCapture):
            raise TypeError("completion must be a CompletedTreeCapture")
        manifest = self.verify_tree(completion.snapshot_ref, verify_children=True)
        verified_facts: Dict[LocalObjectRef, tuple[Any, ...]] = {}
        for publication in completion.publications:
            facts = (
                publication.kind,
                publication.logical_bytes,
                publication.physical_bytes,
                thaw_json(publication.metadata),
                publication.edges,
            )
            previous = verified_facts.get(publication.content_ref)
            if previous is not None:
                if previous != facts:
                    raise RealmIntegrityError(
                        "Completed tree capture duplicates disagree on publication facts."
                    )
                continue
            self._verify_publication(publication)
            verified_facts[publication.content_ref] = facts
        expected_refs = tuple(
            entry.blob_ref
            for entry in manifest.entries
            if entry.kind == "file" and entry.blob_ref is not None
        ) + (completion.snapshot_ref,)
        actual_refs = tuple(item.content_ref for item in completion.publications)
        manifest_only = actual_refs == (completion.snapshot_ref,)
        if not manifest_only and actual_refs != expected_refs:
            raise RealmIntegrityError(
                "Completed tree capture publications differ from its verified manifest."
            )
        if not manifest_only:
            for entry, publication in zip(
                (item for item in manifest.entries if item.kind == "file"),
                completion.publications[:-1],
            ):
                if publication.logical_bytes != entry.size:
                    raise RealmIntegrityError(
                        "Completed tree capture blob facts differ from its verified manifest."
                    )
        root = completion.publications[-1]
        expected_edges = tuple(
            ContentEdge(
                parent_ref=completion.snapshot_ref,
                child_ref=entry.blob_ref,
                role="tree-file",
                canonical_path=entry.path,
            )
            for entry in manifest.entries
            if entry.kind == "file" and entry.blob_ref is not None
        )
        if (
            root.logical_bytes != manifest.logical_bytes
            or root.edges != expected_edges
        ):
            raise RealmIntegrityError(
                "Completed tree capture root facts differ from its verified manifest."
            )
        return TreeSealReceipt(
            snapshot_ref=completion.snapshot_ref,
            manifest=manifest,
            publications=completion.publications,
        )

    def load_prepared_publication(self, staging_id: str) -> PublishedObject:
        """Read the durable staging-id-to-ref association used by recovery."""

        self._assert_root_identity()
        stage_fd = self._open_stage_fd(staging_id)
        try:
            raw = _read_child_immutable_regular(
                stage_fd,
                "publication.json",
                max_bytes=_MAX_MANIFEST_BYTES,
                label=f"prepared publication {staging_id}",
            )
        finally:
            os.close(stage_fd)
        publication = _publication_from_bytes(raw)
        if publication.staging_id != staging_id or publication.store_id != self.store_id:
            raise RealmIntegrityError("Prepared publication marker belongs to another allocation.")
        return publication

    def _recover_prepared_publication(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        staging_id: str,
    ) -> PublishedObject:
        """Finish an idempotent prepared publication after process recovery."""

        publication = self.load_prepared_publication(staging_id)
        stage_fd = self._open_stage_fd(staging_id)

        def verify_existing():
            return self._verify_publication(publication)

        def verify_source(object_fd: int):
            return self._verify_publication_fd(publication, object_fd)

        try:
            # The marker is intentionally durable before the ledger prepare.
            # Replaying prepare closes that crash window before bytes become
            # visible.  Keep the staging descriptor covered by this finally
            # even when the ledger rejects the replay.
            authority.prepare_publication(
                change_id=change_id,
                publication=publication,
            )
            with self.exclusive_lock():
                self._publish_object_directory(
                    stage_fd,
                    "object",
                    publication.content_ref,
                    verify_source=verify_source,
                    verify_existing=verify_existing,
                    on_visible=lambda: None,
                )
                authority.record_publication(
                    change_id=change_id,
                    publication=publication,
                )
                self._remove_staging(staging_id)
                authority.complete_staging_publication(
                    change_id=change_id,
                    staging_id=staging_id,
                )
        finally:
            os.close(stage_fd)
        return publication

    def _verify_publication(self, publication: PublishedObject):
        object_fd = self._open_live_object_fd(publication.content_ref)
        try:
            _require_immutable_object_directory(object_fd, publication.content_ref)
            return self._verify_publication_fd(publication, object_fd)
        finally:
            os.close(object_fd)

    @staticmethod
    def _verify_publication_fd(publication: PublishedObject, object_fd: int):
        if isinstance(publication.content_ref, BlobRef):
            manifest = _verify_blob_fd(object_fd, publication.content_ref)
            expected_physical = manifest.size + len(manifest.to_bytes())
            if (
                publication.kind != "blob"
                or publication.logical_bytes != manifest.size
                or publication.physical_bytes != expected_physical
                or thaw_json(publication.metadata)
                != {"manifest_format": "optpilot.blob.v1"}
                or publication.edges
            ):
                raise ContentCorrupt("Prepared blob facts differ from its staged bytes.")
            return manifest
        manifest = _verify_tree_fd(object_fd, publication.content_ref)
        expected_edges = tuple(
            ContentEdge(
                publication.content_ref,
                entry.blob_ref,
                "tree-file",
                entry.path,
            )
            for entry in manifest.entries
            if entry.kind == "file" and entry.blob_ref is not None
        )
        if (
            publication.kind != "tree"
            or publication.logical_bytes != manifest.logical_bytes
            or publication.physical_bytes != len(manifest.to_bytes())
            or thaw_json(publication.metadata)
            != {
                "entry_count": len(manifest.entries),
                "manifest_format": "optpilot.tree.v1",
            }
            or publication.edges != expected_edges
        ):
            raise ContentCorrupt("Prepared tree facts differ from its staged bytes.")
        return manifest

    def has_object(self, content_ref: LocalObjectRef) -> bool:
        self._assert_root_identity()
        kind_name = "blobs" if isinstance(content_ref, BlobRef) else "trees"
        current_fd = self._open_object_kind_fd(kind_name)
        try:
            for component in (content_ref.digest[:2],):
                try:
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    return False
                except OSError as error:
                    raise RealmIntegrityError(
                        f"Managed object namespace is unsafe for {content_ref}."
                    ) from error
                os.close(current_fd)
                current_fd = next_fd
            try:
                object_fd = _open_child_directory_nofollow(
                    current_fd,
                    content_ref.digest,
                    label=f"object {content_ref}",
                )
            except FileNotFoundError:
                return False
            except RealmIntegrityError as error:
                # Preserve the useful absent/not-absent distinction while
                # treating every present unsafe node as store corruption.
                try:
                    os.stat(content_ref.digest, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                raise error
            else:
                os.close(object_fd)
            return True
        finally:
            os.close(current_fd)

    def iter_live_refs(self) -> Iterator[LocalObjectRef]:
        """Yield syntactically valid live object paths; this is not an authorization API."""

        self._assert_root_identity()
        for kind_name, reference_type in (("blobs", BlobRef), ("trees", SnapshotRef)):
            kind_fd = self._open_object_kind_fd(kind_name)
            try:
                for prefix_name in sorted(os.listdir(kind_fd)):
                    try:
                        prefix_fd = _open_child_directory_nofollow(
                            kind_fd, prefix_name, label=f"{kind_name} prefix"
                        )
                    except RealmIntegrityError:
                        continue
                    try:
                        for object_name in sorted(os.listdir(prefix_fd)):
                            try:
                                reference = reference_type(object_name)
                            except ValueError:
                                continue
                            if reference.digest[:2] != prefix_name:
                                continue
                            try:
                                object_fd = _open_child_directory_nofollow(
                                    prefix_fd,
                                    object_name,
                                    label=f"object {reference}",
                                )
                            except RealmIntegrityError:
                                continue
                            else:
                                os.close(object_fd)
                                yield reference
                    finally:
                        os.close(prefix_fd)
            finally:
                os.close(kind_fd)

    def _publish_blob_from_fd(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        source_fd: int,
        expected: _Identity,
    ) -> PublishedObject:
        staging_id, stage_fd = self._begin_staging(
            change_id=change_id,
            authority=authority,
            kind="blob",
        )
        physically_published = False
        try:
            os.mkdir("object", 0o700, dir_fd=stage_fd)
            object_fd = _open_child_directory_nofollow(stage_fd, "object", label="staged object")
            try:
                data_fd = os.open(
                    "data",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=object_fd,
                )
                digest = hashlib.sha256(b"optpilot/blob/v1\0")
                copied = 0
                try:
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        copied += len(chunk)
                        if copied > expected.size:
                            raise SourceChanged("Source file grew while it was copied.")
                        _write_all(data_fd, chunk)
                    if os.name != "nt":
                        os.fchmod(data_fd, 0o400)
                    os.fsync(data_fd)
                finally:
                    os.close(data_fd)
                if copied != expected.size:
                    raise SourceChanged("Source file size changed while it was copied.")
                blob_ref = BlobRef(digest.hexdigest())

                verification_fd = os.open("data", _FILE_FLAGS, dir_fd=object_fd)
                try:
                    verified_digest, verified_size = _hash_blob_fd(verification_fd)
                finally:
                    os.close(verification_fd)
                if verified_digest != blob_ref.digest or verified_size != copied:
                    raise ContentCorrupt("Private staged blob failed read-back verification.")

                manifest = BlobManifest(blob_ref=blob_ref, size=copied)
                manifest_bytes = manifest.to_bytes()
                _write_file_exclusive_at(
                    object_fd, "manifest.json", manifest_bytes, mode=0o400
                )
                os.fsync(object_fd)
                os.fsync(stage_fd)
            finally:
                os.close(object_fd)

            publication = PublishedObject(
                staging_id=staging_id,
                store_id=self.store_id,
                content_ref=blob_ref,
                kind="blob",
                logical_bytes=copied,
                physical_bytes=copied + len(manifest_bytes),
                metadata={"manifest_format": "optpilot.blob.v1"},
            )
            self._prepare_publication(
                change_id=change_id,
                authority=authority,
                stage_fd=stage_fd,
                publication=publication,
            )
            def mark_physically_published() -> None:
                nonlocal physically_published
                physically_published = True

            with self.exclusive_lock():
                self._publish_object_directory(
                    stage_fd,
                    "object",
                    blob_ref,
                    verify_source=lambda object_fd: self._verify_publication_fd(
                        publication, object_fd
                    ),
                    verify_existing=lambda: self.verify_blob(blob_ref),
                    on_visible=mark_physically_published,
                )
                authority.record_publication(
                    change_id=change_id,
                    publication=publication,
                )
                self._remove_staging(staging_id)
                authority.complete_staging_publication(
                    change_id=change_id,
                    staging_id=staging_id,
                )
            return publication
        except BaseException:
            if not physically_published:
                self._abandon_staging(
                    change_id=change_id,
                    authority=authority,
                    staging_id=staging_id,
                )
            raise
        finally:
            os.close(stage_fd)
            if not physically_published:
                with self.exclusive_lock():
                    self._remove_staging(staging_id)

    def _publish_tree_manifest(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        manifest: TreeManifest,
    ) -> PublishedObject:
        staging_id, stage_fd = self._begin_staging(
            change_id=change_id,
            authority=authority,
            kind="tree",
        )
        physically_published = False
        try:
            os.mkdir("object", 0o700, dir_fd=stage_fd)
            object_fd = _open_child_directory_nofollow(stage_fd, "object", label="staged object")
            manifest_bytes = manifest.to_bytes()
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                os.close(object_fd)
                raise ContentRejected("Canonical tree manifest exceeds the v1 manifest limit.")
            try:
                _write_file_exclusive_at(
                    object_fd, "manifest.json", manifest_bytes, mode=0o400
                )
                os.fsync(object_fd)
                os.fsync(stage_fd)
            finally:
                os.close(object_fd)

            snapshot_ref = manifest.snapshot_ref
            edges = tuple(
                ContentEdge(
                    parent_ref=snapshot_ref,
                    child_ref=entry.blob_ref,
                    role="tree-file",
                    canonical_path=entry.path,
                )
                for entry in manifest.entries
                if entry.kind == "file" and entry.blob_ref is not None
            )
            publication = PublishedObject(
                staging_id=staging_id,
                store_id=self.store_id,
                content_ref=snapshot_ref,
                kind="tree",
                logical_bytes=manifest.logical_bytes,
                physical_bytes=len(manifest_bytes),
                metadata={
                    "entry_count": len(manifest.entries),
                    "manifest_format": "optpilot.tree.v1",
                },
                edges=edges,
            )
            self._prepare_publication(
                change_id=change_id,
                authority=authority,
                stage_fd=stage_fd,
                publication=publication,
            )
            def mark_physically_published() -> None:
                nonlocal physically_published
                physically_published = True

            with self.exclusive_lock():
                self._publish_object_directory(
                    stage_fd,
                    "object",
                    snapshot_ref,
                    verify_source=lambda object_fd: self._verify_publication_fd(
                        publication, object_fd
                    ),
                    verify_existing=lambda: self.verify_tree(
                        snapshot_ref,
                        verify_children=False,
                    ),
                    on_visible=mark_physically_published,
                )
                authority.record_publication(
                    change_id=change_id,
                    publication=publication,
                )
                self._remove_staging(staging_id)
                authority.complete_staging_publication(
                    change_id=change_id,
                    staging_id=staging_id,
                )
            return publication
        except BaseException:
            if not physically_published:
                self._abandon_staging(
                    change_id=change_id,
                    authority=authority,
                    staging_id=staging_id,
                )
            raise
        finally:
            os.close(stage_fd)
            if not physically_published:
                with self.exclusive_lock():
                    self._remove_staging(staging_id)

    def _begin_staging(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        kind: str,
    ) -> Tuple[str, int]:
        staging_id = f"stage-{uuid.uuid4().hex}"
        authority.reserve_staging(
            change_id=change_id,
            staging_id=staging_id,
            store_id=self.store_id,
            object_kind=kind,
        )
        staging_fd = self._open_namespace_fd("staging")
        try:
            os.mkdir(staging_id, 0o700, dir_fd=staging_fd)
            os.fsync(staging_fd)
            stage_fd = _open_child_directory_nofollow(
                staging_fd,
                staging_id,
                label=f"staging allocation {staging_id}",
            )
        except BaseException:
            self._abandon_staging(
                change_id=change_id,
                authority=authority,
                staging_id=staging_id,
            )
            raise
        finally:
            os.close(staging_fd)
        return staging_id, stage_fd

    def _abandon_staging(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        staging_id: str,
    ) -> None:
        try:
            authority.abandon_staging(change_id=change_id, staging_id=staging_id)
        except BaseException:
            # The original filesystem/capture error is authoritative.  The
            # durable allocation remains discoverable for reaper reconciliation.
            pass

    def _prepare_publication(
        self,
        *,
        change_id: str,
        authority: ContentRetentionAdapter,
        stage_fd: int,
        publication: PublishedObject,
    ) -> None:
        marker = _publication_bytes(publication)
        if len(marker) > _MAX_MANIFEST_BYTES:
            raise ContentRejected("Prepared publication metadata exceeds the v1 limit.")
        _write_file_exclusive_at(stage_fd, "publication.json", marker, mode=0o400)
        os.fsync(stage_fd)
        authority.prepare_publication(
            change_id=change_id,
            publication=publication,
        )

    def _rollback_capture(
        self,
        change_id: str,
        publications: Sequence[PublishedObject],
        *,
        authority: ContentRetentionAdapter,
    ) -> None:
        """Release known successful holds after a multi-object capture fails."""

        staging_ids = tuple(publication.staging_id for publication in publications)
        try:
            authority.rollback_capture(
                change_id=change_id,
                staging_ids=staging_ids,
            )
        except BaseException as error:
            raise RealmIntegrityError(
                "Capture failed and its published retention holds could not be rolled back."
            ) from error

    def _prepare_lock_file(self) -> None:
        root_fd = self._open_root_fd()
        fd: Optional[int] = None
        try:
            fd = os.open(
                ".store.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_dev != os.fstat(root_fd).st_dev
            ):
                raise RealmIntegrityError("Local content-store lock is not a private regular file.")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(root_fd)
            self._lock_fd = fd
            self._lock_identity = (info.st_dev, info.st_ino)
            fd = None
        finally:
            if fd is not None:
                os.close(fd)
            os.close(root_fd)

    def _register_namespace_fd(self, name: str, descriptor: int) -> None:
        info = os.fstat(descriptor)
        self._namespace_fds[name] = descriptor
        self._namespace_identities[name] = (info.st_dev, info.st_ino)

    def _register_object_kind_fd(self, name: str, descriptor: int) -> None:
        info = os.fstat(descriptor)
        self._object_kind_fds[name] = descriptor
        self._object_kind_identities[name] = (info.st_dev, info.st_ino)

    def _open_object_kind_fd(self, name: str) -> int:
        pinned_fd = self._object_kind_fds.get(name)
        expected = self._object_kind_identities.get(name)
        if pinned_fd is None or expected is None:
            raise RealmIntegrityError("Managed object-kind namespace is closed.")
        objects_fd = self._open_namespace_fd("objects")
        opened_fd: Optional[int] = None
        try:
            path_info = os.stat(name, dir_fd=objects_fd, follow_symlinks=False)
            opened_fd = _open_child_directory_nofollow(
                objects_fd,
                name,
                label=f"objects/{name}",
            )
        except OSError as error:
            raise RealmIntegrityError(
                f"Managed object-kind namespace was removed or replaced: {name}."
            ) from error
        finally:
            os.close(objects_fd)
        assert opened_fd is not None
        try:
            pinned = os.fstat(pinned_fd)
            opened = os.fstat(opened_fd)
        except BaseException:
            os.close(opened_fd)
            raise
        identities = {
            (pinned.st_dev, pinned.st_ino),
            (path_info.st_dev, path_info.st_ino),
            (opened.st_dev, opened.st_ino),
        }
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or identities != {expected}
        ):
            os.close(opened_fd)
            raise RealmIntegrityError(
                f"Managed object-kind namespace identity changed: {name}."
            )
        return opened_fd

    def _open_namespace_fd(self, name: str) -> int:
        """Open a pinned root child and reject path replacement or parent links."""

        self._assert_root_identity()
        pinned_fd = self._namespace_fds.get(name)
        expected = self._namespace_identities.get(name)
        if pinned_fd is None or expected is None:
            raise RealmIntegrityError("Local content-store namespace is closed.")
        try:
            pinned = os.fstat(pinned_fd)
        except OSError as error:
            raise RealmIntegrityError("Local content-store namespace is unavailable.") from error
        root_fd = self._open_root_fd()
        try:
            try:
                path_info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                opened_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
            except OSError as error:
                raise RealmIntegrityError(
                    f"Managed store namespace was removed or replaced: {name}."
                ) from error
            opened = os.fstat(opened_fd)
            identities = {
                (pinned.st_dev, pinned.st_ino),
                (path_info.st_dev, path_info.st_ino),
                (opened.st_dev, opened.st_ino),
            }
            if (
                not stat.S_ISDIR(path_info.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or identities != {expected}
            ):
                os.close(opened_fd)
                raise RealmIntegrityError(
                    f"Managed store namespace identity changed: {name}."
                )
            return opened_fd
        finally:
            os.close(root_fd)

    def _open_stage_fd(self, staging_id: str) -> int:
        _validate_staging_id(staging_id)
        staging_fd = self._open_namespace_fd("staging")
        try:
            return _open_child_directory_nofollow(
                staging_fd,
                staging_id,
                label=f"staging allocation {staging_id}",
            )
        finally:
            os.close(staging_fd)

    def _remove_staging(self, staging_id: str) -> None:
        _validate_staging_id(staging_id)
        staging_fd = self._open_namespace_fd("staging")
        try:
            _remove_private_tree_at(staging_fd, staging_id)
        finally:
            os.close(staging_fd)

    def _open_root_fd(self) -> int:
        try:
            fd = os.open(self.root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise RealmIntegrityError("Local content-store root is no longer safely reachable.") from error
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != self._root_device_inode:
            os.close(fd)
            raise RealmIntegrityError("Local content-store root identity changed.")
        return fd

    def _assert_root_identity(self) -> None:
        fd = self._open_root_fd()
        os.close(fd)

    @contextmanager
    def exclusive_lock(self):
        """Serialize publication/adoption and ledger-rechecked GC per store."""

        if fcntl is None:
            raise RealmIntegrityError("This platform lacks the v1 interprocess store lock.")
        pinned_fd = self._lock_fd
        if pinned_fd is None:
            raise RealmIntegrityError("Local content-store lock is closed.")
        root_fd = self._open_root_fd()
        try:
            pinned = os.fstat(pinned_fd)
            path_info = os.stat(".store.lock", dir_fd=root_fd, follow_symlinks=False)
            fd = os.open(
                ".store.lock",
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            try:
                opened = os.fstat(fd)
                identities = {
                    (pinned.st_dev, pinned.st_ino),
                    (path_info.st_dev, path_info.st_ino),
                    (opened.st_dev, opened.st_ino),
                }
                if (
                    not stat.S_ISREG(path_info.st_mode)
                    or not stat.S_ISREG(opened.st_mode)
                    or path_info.st_nlink != 1
                    or opened.st_nlink != 1
                    or identities != {self._lock_identity}
                ):
                    raise RealmIntegrityError("Local content-store lock identity changed.")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            os.close(root_fd)

    def _publish_object_directory(
        self,
        source_parent_fd: int,
        source_name: str,
        content_ref: LocalObjectRef,
        *,
        verify_source,
        verify_existing,
        on_visible,
    ) -> None:
        prefix_fd = self._open_object_prefix_fd(content_ref)

        def adopt_visible_object() -> None:
            """Verify a 0700 crash remnant before making it immutable.

            Freshly renamed staged directories are 0700 until finalization.  A
            recovery may safely adopt that one transitional mode only after
            exact byte/fact verification; arbitrary content is never chmodded
            first.
            """

            object_fd = _open_child_directory_nofollow(
                prefix_fd,
                content_ref.digest,
                label=f"visible object {content_ref}",
            )
            try:
                info = _require_recoverable_object_directory(object_fd, content_ref)
                verify_source(object_fd)
                _require_name_identity(prefix_fd, content_ref.digest, info)
            finally:
                os.close(object_fd)
            _finalize_visible_object_fd(prefix_fd, content_ref.digest)
            verify_existing()

        try:
            existing = None
            try:
                existing = os.stat(
                    content_ref.digest,
                    dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            if existing is not None:
                if not stat.S_ISDIR(existing.st_mode):
                    raise RealmIntegrityError(
                        f"Managed object destination is not a real directory: {content_ref}."
                    )
                adopt_visible_object()
                on_visible()
                _remove_private_tree_at(source_parent_fd, source_name)
                return
            source_fd = _open_child_directory_nofollow(
                source_parent_fd,
                source_name,
                label=f"staged object {content_ref}",
            )
            try:
                verify_source(source_fd)
                _require_name_identity(
                    source_parent_fd,
                    source_name,
                    os.fstat(source_fd),
                )
            finally:
                os.close(source_fd)
            try:
                os.rename(
                    source_name,
                    content_ref.digest,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=prefix_fd,
                )
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                collided = os.stat(
                    content_ref.digest,
                    dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(collided.st_mode):
                    raise RealmIntegrityError(
                        f"Managed object destination became unsafe: {content_ref}."
                    ) from error
                adopt_visible_object()
                on_visible()
                _remove_private_tree_at(source_parent_fd, source_name)
                return
            # Record in-process visibility immediately so every later failure
            # preserves the prepared allocation for recovery.  A cross-
            # directory rename is durable only after both entries are synced.
            on_visible()
            os.fsync(source_parent_fd)
            os.fsync(prefix_fd)
            adopt_visible_object()
            os.fsync(prefix_fd)
        finally:
            os.close(prefix_fd)

    def _object_directory(self, content_ref: LocalObjectRef) -> Path:
        if isinstance(content_ref, BlobRef):
            root = self.blobs
        elif isinstance(content_ref, SnapshotRef):
            root = self.trees
        else:  # pragma: no cover - guarded by type-facing callers
            raise TypeError(f"Unsupported local object ref: {type(content_ref).__name__}.")
        return root / content_ref.digest[:2] / content_ref.digest

    def _open_live_object_fd(self, content_ref: LocalObjectRef) -> int:
        kind_name = "blobs" if isinstance(content_ref, BlobRef) else "trees"
        current_fd = self._open_object_kind_fd(kind_name)
        try:
            for component in (content_ref.digest[:2], content_ref.digest):
                next_fd = _open_child_directory_nofollow(
                    current_fd,
                    component,
                    label=f"object path for {content_ref}",
                )
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except (OSError, RealmIntegrityError) as error:
            os.close(current_fd)
            raise ContentCorrupt(
                f"Managed object path is missing or contains a symlink: {content_ref}."
            ) from error

    def _open_object_prefix_fd(self, content_ref: LocalObjectRef) -> int:
        kind_name = "blobs" if isinstance(content_ref, BlobRef) else "trees"
        current_fd = self._open_object_kind_fd(kind_name)
        try:
            try:
                os.mkdir(content_ref.digest[:2], 0o700, dir_fd=current_fd)
                # Persist creation in the blobs/trees namespace before any
                # object row can be recorded by the ledger.
                os.fsync(current_fd)
            except FileExistsError:
                pass
            prefix_fd = _open_child_directory_nofollow(
                current_fd,
                content_ref.digest[:2],
                label=f"object prefix for {content_ref}",
            )
            os.close(current_fd)
            current_fd = prefix_fd
            return current_fd
        except OSError as error:
            os.close(current_fd)
            raise RealmIntegrityError(
                f"Could not safely open managed object namespace for {content_ref}."
            ) from error

    def _open_existing_object_prefix_fd(self, content_ref: LocalObjectRef) -> Optional[int]:
        """Open an existing object prefix without creating GC-visible state."""

        kind_name = "blobs" if isinstance(content_ref, BlobRef) else "trees"
        current_fd = self._open_object_kind_fd(kind_name)
        try:
            for component in (content_ref.digest[:2],):
                try:
                    next_fd = _open_child_directory_nofollow(
                        current_fd,
                        component,
                        label=f"object prefix for {content_ref}",
                    )
                except FileNotFoundError:
                    os.close(current_fd)
                    return None
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except (OSError, RealmIntegrityError) as error:
            os.close(current_fd)
            raise RealmIntegrityError(
                f"Could not safely open managed object namespace for {content_ref}."
            ) from error

    def _load_or_create_marker(self) -> str:
        root_fd = self._open_root_fd()
        try:
            try:
                marker_info = os.stat("store.json", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                marker_info = None
            if marker_info is None:
                marker = uuid.uuid4().hex
                payload = canonical_json_bytes(
                    {
                        "format": self.STORE_FORMAT,
                        "root_marker": marker,
                        "store_id": self.store_id,
                    }
                )
                temporary = f".store-{uuid.uuid4().hex}.tmp"
                try:
                    _write_file_exclusive_at(root_fd, temporary, payload, mode=0o400)
                    try:
                        os.link(
                            temporary,
                            "store.json",
                            src_dir_fd=root_fd,
                            dst_dir_fd=root_fd,
                            follow_symlinks=False,
                        )
                        os.fsync(root_fd)
                    except FileExistsError:
                        pass
                finally:
                    try:
                        os.unlink(temporary, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass
                    os.fsync(root_fd)
            raw = _read_child_immutable_regular(
                root_fd,
                "store.json",
                max_bytes=_MAX_MARKER_BYTES,
                label="content-store marker",
            )
        except (OSError, ContentCorrupt) as error:
            raise RealmIntegrityError(f"Local content-store marker is unreadable: {error}") from error
        finally:
            os.close(root_fd)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RealmIntegrityError(f"Local content-store marker is unreadable: {error}") from error
        expected_keys = {"format", "root_marker", "store_id"}
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise RealmIntegrityError("Local content-store marker has an invalid shape.")
        if canonical_json_bytes(value) != raw:
            raise RealmIntegrityError("Local content-store marker is not canonical.")
        if value["format"] != self.STORE_FORMAT or value["store_id"] != self.store_id:
            raise RealmIntegrityError("Local content-store marker belongs to another store.")
        marker = value["root_marker"]
        if not isinstance(marker, str) or len(marker) != 32 or any(
            character not in "0123456789abcdef" for character in marker
        ):
            raise RealmIntegrityError("Local content-store marker id is invalid.")
        return marker


def _validate_store_id(store_id: str) -> None:
    if (
        not isinstance(store_id, str)
        or not store_id
        or len(store_id) > 128
        or any(character not in _STORE_ID_CHARACTERS for character in store_id)
    ):
        raise ValueError("store_id must use 1-128 ASCII letters, digits, dot, underscore, or dash.")


def _validate_staging_id(staging_id: str) -> None:
    prefix = "stage-"
    suffix = (
        staging_id[len(prefix) :]
        if isinstance(staging_id, str) and staging_id.startswith(prefix)
        else ""
    )
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("staging_id must be a generated stage identifier")


def _prepare_private_child_directory(parent_fd: int, name: str) -> int:
    """Create/open one store-owned child without mutating a mount boundary."""

    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    descriptor = _open_child_directory_nofollow(
        parent_fd,
        name,
        label=f"private namespace {name}",
    )
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _selection_components(selection: str) -> Tuple[str, ...]:
    if selection in {"", "."}:
        return ()
    if selection.startswith("/") or selection.endswith("/") or "\\" in selection:
        raise ContentRejected(f"Tree selection must be relative: {selection!r}.")
    components = selection.split("/")
    if any(component in {"", ".", ".."} or "\x00" in component for component in components):
        raise ContentRejected(f"Tree selection contains traversal: {selection!r}.")
    return tuple(components)


def _open_selected_directory(allowed_root: Path, selection: str) -> int:
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise ContentRejected("This platform lacks secure descriptor-rooted capture support.")
    root_path = Path(allowed_root).expanduser().absolute()
    try:
        root_fd = os.open(root_path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ContentRejected(f"Allowed root is not a real readable directory: {root_path}.") from error
    selected_fd = os.dup(root_fd)
    try:
        for component in _selection_components(selection):
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=selected_fd)
            os.close(selected_fd)
            selected_fd = next_fd
        return selected_fd
    except OSError as error:
        os.close(selected_fd)
        raise ContentRejected(f"Tree selection is not a contained real directory: {selection!r}.") from error
    finally:
        os.close(root_fd)


def _open_selected_file(allowed_root: Path, selection: str) -> int:
    components = _selection_components(selection)
    if not components:
        raise ContentRejected("Blob selection must name one relative file.")
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise ContentRejected("This platform lacks secure descriptor-rooted capture support.")
    root_path = Path(allowed_root).expanduser().absolute()
    try:
        root_fd = os.open(root_path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ContentRejected(f"Allowed root is not a real readable directory: {root_path}.") from error
    directory_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(components[-1], _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise ContentRejected(f"Blob selection is not a contained regular file: {selection!r}.") from error
    finally:
        os.close(directory_fd)
        os.close(root_fd)


def _open_relative_file(root_fd: int, path: str) -> int:
    components = path.split("/")
    directory_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(components[-1], _FILE_FLAGS, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


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


def _require_identity(actual: os.stat_result, expected: _Identity, path: str) -> None:
    if _identity(actual) != expected:
        raise SourceChanged(f"Source node changed during capture: {path!r}.")


def _inventory(
    root_fd: int,
    *,
    limits: SealLimits,
    ignored_xattrs: frozenset[str],
    excluded_directory_names: tuple[str, ...] = (),
) -> _Inventory:
    root_info = os.fstat(root_fd)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ContentRejected("Selected capture root is not a directory.")
    _reject_xattrs(root_fd, ".", ignored=ignored_xattrs)
    nodes = []
    logical_bytes = 0

    def visit(directory_fd: int, prefix: str) -> None:
        nonlocal logical_bytes
        for name in os.listdir(directory_fd):
            raw_path = f"{prefix}/{name}" if prefix else name
            # Validate before any destination name is derived from source text.
            validate_portable_paths([raw_path], limits=limits)
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ContentRejected(f"Symlinks are unsupported in v1 trees: {raw_path!r}.")
            if stat.S_ISDIR(before.st_mode):
                if name in excluded_directory_names:
                    continue
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    _require_identity(os.fstat(child_fd), _identity(before), raw_path)
                    _reject_xattrs(child_fd, raw_path, ignored=ignored_xattrs)
                    nodes.append(_InventoryNode(raw_path, "directory", _identity(before)))
                    _check_entry_count(nodes, limits)
                    visit(child_fd, raw_path)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ContentRejected(f"Special filesystem node is unsupported: {raw_path!r}.")
            _reject_sparse(before, raw_path)
            if before.st_size > limits.max_file_bytes:
                raise ContentRejected(
                    f"File {raw_path!r} exceeds maximum size {limits.max_file_bytes} bytes."
                )
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
            try:
                _require_identity(os.fstat(file_fd), _identity(before), raw_path)
                _reject_xattrs(file_fd, raw_path, ignored=ignored_xattrs)
            finally:
                os.close(file_fd)
            nodes.append(_InventoryNode(raw_path, "file", _identity(before)))
            _check_entry_count(nodes, limits)
            logical_bytes += before.st_size
            if logical_bytes > limits.max_total_bytes:
                raise ContentRejected(
                    f"Tree exceeds maximum logical size {limits.max_total_bytes} bytes."
                )

    visit(root_fd, "")
    validate_portable_paths((node.raw_path for node in nodes), limits=limits)
    nodes.sort(key=lambda node: node.raw_path.encode("utf-8"))
    return _Inventory(_identity(root_info), tuple(nodes), logical_bytes)


def _check_entry_count(nodes: Sequence[_InventoryNode], limits: SealLimits) -> None:
    if len(nodes) > limits.max_entries:
        raise ContentRejected(f"Tree exceeds maximum entry count {limits.max_entries}.")


def _reject_xattrs(fd: int, path: str, *, ignored: frozenset[str]) -> None:
    attributes = _unsupported_xattrs(fd, path, ignored=ignored)
    if not attributes:
        return
    named = ", ".join(sorted(attributes))
    remedy = ""
    if sys.platform == "darwin" and "com.apple.quarantine" in attributes:
        # By far the most likely way an ordinary person meets this: macOS marks
        # anything arrived from a browser as quarantined, so a package
        # downloaded rather than cloned cannot be captured. Naming the command
        # turns a dead end into a one-line fix. The marker stays rejected
        # rather than ignored: it is security metadata, and silently dropping
        # it would strip a warning the operating system meant to keep.
        remedy = (
            " This usually means the files were downloaded. Clear the marker "
            "with: xattr -dr com.apple.quarantine <folder>"
        )
    raise ContentRejected(
        f"Extended attributes are unsupported in v1 trees: {path!r} "
        f"carries {named}.{remedy}"
    )


def _unsupported_xattrs(
    fd: int,
    path: str,
    *,
    ignored: frozenset[str],
) -> set[str]:
    attributes = set(_fd_xattrs(fd, path))
    attributes.difference_update(ignored)
    if (
        sys.platform == "darwin"
        and _DARWIN_DOCKER_OWNERSHIP_XATTR in attributes
        and _darwin_docker_ownership_xattr_is_redundant(fd, path)
    ):
        attributes.remove(_DARWIN_DOCKER_OWNERSHIP_XATTR)
    return attributes


def _darwin_docker_ownership_xattr_is_redundant(fd: int, path: str) -> bool:
    """Accept Docker Desktop metadata only when it adds no mode semantics."""

    payload = _fd_xattr_value(
        fd,
        _DARWIN_DOCKER_OWNERSHIP_XATTR,
        path,
        max_bytes=_MAX_DARWIN_DOCKER_OWNERSHIP_XATTR_BYTES,
    )

    def unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate Docker ownership attribute field")
            value[key] = item
        return value

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError, TypeError):
        return False
    if not isinstance(value, dict) or set(value) != {"UID", "GID", "mode"}:
        return False
    if any(type(value[field]) is not int for field in ("UID", "GID", "mode")):
        return False
    if value["UID"] < -1 or value["GID"] < -1:
        return False
    encoded_mode = str(value["mode"])
    if not encoded_mode or len(encoded_mode) > 4 or any(
        character not in "01234567" for character in encoded_mode
    ):
        return False
    docker_mode = int(encoded_mode, 8)
    return docker_mode == stat.S_IMODE(os.fstat(fd).st_mode)


def _fd_xattrs(fd: int, path: str) -> Tuple[str, ...]:
    if hasattr(os, "listxattr"):
        try:
            return tuple(os.listxattr(fd))  # type: ignore[attr-defined]
        except OSError as error:
            if error.errno in {
                getattr(errno, "ENOTSUP", -1),
                getattr(errno, "EOPNOTSUPP", -1),
            }:
                return ()
            raise ContentRejected(
                f"Cannot inspect extended attributes for {path!r}: {error}"
            ) from error
        except TypeError:
            pass

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "flistxattr", None)
    if function is None:
        raise ContentRejected(f"Platform cannot inspect descriptor xattrs for {path!r}.")
    if sys.platform == "darwin":
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]

        def call(buffer, size):
            return function(fd, buffer, size, 0)

    else:
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]

        def call(buffer, size):
            return function(fd, buffer, size)

    function.restype = ctypes.c_ssize_t
    size = call(None, 0)
    if size < 0:
        error_number = ctypes.get_errno()
        if error_number in {
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return ()
        raise ContentRejected(
            f"Cannot inspect extended attributes for {path!r}: "
            f"[{error_number}] {os.strerror(error_number)}"
        )
    if size == 0:
        return ()
    buffer = ctypes.create_string_buffer(size)
    written = call(buffer, size)
    if written < 0:
        error_number = ctypes.get_errno()
        raise ContentRejected(
            f"Cannot inspect extended attributes for {path!r}: "
            f"[{error_number}] {os.strerror(error_number)}"
        )
    return tuple(
        sorted(
            item.decode("utf-8", errors="surrogateescape")
            for item in bytes(buffer.raw[:written]).split(b"\x00")
            if item
        )
    )


def _fd_xattr_value(
    fd: int,
    name: str,
    path: str,
    *,
    max_bytes: int,
) -> bytes:
    if hasattr(os, "getxattr"):
        try:
            value = os.getxattr(fd, name)  # type: ignore[attr-defined]
            if len(value) > max_bytes:
                raise ContentRejected(
                    f"Extended attribute {name!r} is too large for {path!r}."
                )
            return bytes(value)
        except OSError as error:
            raise ContentRejected(
                f"Cannot inspect extended attribute {name!r} for {path!r}: {error}"
            ) from error
        except TypeError:
            pass

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "fgetxattr", None)
    if function is None:
        raise ContentRejected(
            f"Platform cannot inspect descriptor extended attribute {name!r} "
            f"for {path!r}."
        )
    encoded_name = name.encode("utf-8", errors="surrogateescape")
    if sys.platform == "darwin":
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]

        def call(buffer, size):
            return function(fd, encoded_name, buffer, size, 0, 0)

    else:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]

        def call(buffer, size):
            return function(fd, encoded_name, buffer, size)

    function.restype = ctypes.c_ssize_t
    size = call(None, 0)
    if size < 0:
        error_number = ctypes.get_errno()
        raise ContentRejected(
            f"Cannot inspect extended attribute {name!r} for {path!r}: "
            f"[{error_number}] {os.strerror(error_number)}"
        )
    if size > max_bytes:
        raise ContentRejected(f"Extended attribute {name!r} is too large for {path!r}.")
    if size == 0:
        return b""
    buffer = ctypes.create_string_buffer(size)
    written = call(buffer, size)
    if written < 0:
        error_number = ctypes.get_errno()
        raise ContentRejected(
            f"Cannot inspect extended attribute {name!r} for {path!r}: "
            f"[{error_number}] {os.strerror(error_number)}"
        )
    if written > max_bytes:
        raise ContentRejected(f"Extended attribute {name!r} is too large for {path!r}.")
    return bytes(buffer.raw[:written])


def _reject_sparse(info: os.stat_result, path: str) -> None:
    if info.st_size == 0:
        return
    blocks = getattr(info, "st_blocks", None)
    if blocks is None or blocks < 0:
        raise ContentRejected(f"Platform cannot inspect sparse-file semantics for {path!r}.")
    if blocks * 512 < info.st_size:
        raise ContentRejected(f"Sparse files are unsupported in v1 trees: {path!r}.")


def _hash_blob_fd(fd: int) -> Tuple[str, int]:
    digest = hashlib.sha256(b"optpilot/blob/v1\0")
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _verify_blob_directory(object_directory: Path, blob_ref: BlobRef) -> BlobManifest:
    directory_fd = _open_directory_nofollow(object_directory, label=f"blob {blob_ref}")
    try:
        return _verify_blob_fd(directory_fd, blob_ref)
    finally:
        os.close(directory_fd)


def _verify_blob_fd(directory_fd: int, blob_ref: BlobRef) -> BlobManifest:
    try:
        if set(os.listdir(directory_fd)) != {"data", "manifest.json"}:
            raise ContentCorrupt(f"Blob object has unexpected entries: {blob_ref}.")
        manifest_fd = _open_immutable_regular_child(
            directory_fd, "manifest.json", label=f"blob manifest {blob_ref}"
        )
        try:
            manifest_before = os.fstat(manifest_fd)
            manifest_bytes = _read_regular_fd(manifest_fd, max_bytes=_MAX_MANIFEST_BYTES)
            _require_stable_managed_file(
                directory_fd,
                "manifest.json",
                manifest_fd,
                manifest_before,
            )
        finally:
            os.close(manifest_fd)
        manifest = BlobManifest.from_bytes(manifest_bytes)
        if manifest.blob_ref != blob_ref:
            raise ContentCorrupt(f"Blob manifest identity differs from {blob_ref}.")
        data_fd = _open_immutable_regular_child(
            directory_fd, "data", label=f"blob payload {blob_ref}"
        )
        try:
            data_before = os.fstat(data_fd)
            digest, size = _hash_blob_fd(data_fd)
            _require_stable_managed_file(
                directory_fd,
                "data",
                data_fd,
                data_before,
            )
        finally:
            os.close(data_fd)
    except OSError as error:
        raise ContentCorrupt(f"Cannot read blob {blob_ref}: {error}") from error
    if digest != blob_ref.digest or size != manifest.size:
        raise ContentCorrupt(f"Blob payload does not match manifest: {blob_ref}.")
    return manifest


def _verify_tree_directory(object_directory: Path, snapshot_ref: SnapshotRef) -> TreeManifest:
    directory_fd = _open_directory_nofollow(object_directory, label=f"tree {snapshot_ref}")
    try:
        return _verify_tree_fd(directory_fd, snapshot_ref)
    finally:
        os.close(directory_fd)


def _verify_tree_fd(directory_fd: int, snapshot_ref: SnapshotRef) -> TreeManifest:
    try:
        if set(os.listdir(directory_fd)) != {"manifest.json"}:
            raise ContentCorrupt(f"Tree object has unexpected entries: {snapshot_ref}.")
        manifest_fd = _open_immutable_regular_child(
            directory_fd, "manifest.json", label=f"tree manifest {snapshot_ref}"
        )
        try:
            manifest_before = os.fstat(manifest_fd)
            manifest_bytes = _read_regular_fd(manifest_fd, max_bytes=_MAX_MANIFEST_BYTES)
            _require_stable_managed_file(
                directory_fd,
                "manifest.json",
                manifest_fd,
                manifest_before,
            )
        finally:
            os.close(manifest_fd)
        manifest = TreeManifest.from_bytes(manifest_bytes)
    except OSError as error:
        raise ContentCorrupt(f"Cannot read tree {snapshot_ref}: {error}") from error
    if manifest.snapshot_ref != snapshot_ref:
        raise ContentCorrupt(f"Tree manifest identity differs from {snapshot_ref}.")
    return manifest


def _require_immutable_object_directory(
    directory_fd: int, content_ref: LocalObjectRef
) -> None:
    info = _require_recoverable_object_directory(directory_fd, content_ref)
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o500:
        raise ContentCorrupt(f"Managed object directory is writable or unsafe: {content_ref}.")


def _require_recoverable_object_directory(
    directory_fd: int, content_ref: LocalObjectRef
) -> os.stat_result:
    """Accept only immutable objects or the single 0700 rename crash state."""

    info = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or (os.name != "nt" and stat.S_IMODE(info.st_mode) not in {0o500, 0o700})
    ):
        raise ContentCorrupt(
            f"Managed object directory has an unsafe recovery mode: {content_ref}."
        )
    _require_managed_xattrs(directory_fd, f"object directory {content_ref}")
    return info


def _open_immutable_regular_child(directory_fd: int, name: str, *, label: str) -> int:
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise ContentCorrupt(f"Managed {label} is not safely readable: {error}") from error
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_dev != os.fstat(directory_fd).st_dev
        or (os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o400)
    ):
        os.close(descriptor)
        raise ContentCorrupt(
            f"Managed {label} must be an immutable single-link regular file."
        )
    _require_managed_xattrs(descriptor, label)
    _require_name_identity(directory_fd, name, info)
    return descriptor


def _require_managed_xattrs(fd: int, label: str) -> None:
    ignored = DARWIN_IGNORED_NONCONTENT_XATTRS if sys.platform == "darwin" else frozenset()
    try:
        attributes = _unsupported_xattrs(fd, label, ignored=ignored)
    except ContentRejected as error:
        raise ContentCorrupt(f"Cannot validate managed {label} attributes: {error}") from error
    if attributes:
        raise ContentCorrupt(f"Managed {label} has unsupported extended attributes.")


def _require_stable_managed_file(
    parent_fd: int,
    name: str,
    descriptor: int,
    before: os.stat_result,
) -> None:
    after = os.fstat(descriptor)
    if _identity(after) != _identity(before):
        raise ContentCorrupt("Managed immutable file changed while it was verified.")
    _require_name_identity(parent_fd, name, after)


def _open_directory_nofollow(path: Path, *, label: str) -> int:
    try:
        fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ContentCorrupt(f"Managed {label} is not a real directory: {error}") from error
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ContentCorrupt(f"Managed {label} is not a directory.")
    return fd


def _read_child_immutable_regular(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    fd = _open_immutable_regular_child(directory_fd, name, label=label)
    try:
        before = os.fstat(fd)
        payload = _read_regular_fd(fd, max_bytes=max_bytes)
        _require_stable_managed_file(directory_fd, name, fd, before)
        return payload
    finally:
        os.close(fd)


def _read_regular_fd(fd: int, *, max_bytes: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise RealmIntegrityError("Managed metadata is not a regular file.")
    if info.st_size < 0 or info.st_size > max_bytes:
        raise RealmIntegrityError("Managed metadata exceeds its bounded size.")
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise RealmIntegrityError("Managed metadata exceeds its bounded size.")
    return b"".join(chunks)


def _finalize_visible_object_fd(prefix_fd: int, digest: str) -> None:
    object_fd = _open_child_directory_nofollow(
        prefix_fd,
        digest,
        label="newly visible object",
    )
    try:
        if os.name != "nt":
            os.fchmod(object_fd, 0o500)
        os.fsync(object_fd)
    finally:
        os.close(object_fd)


def publication_payload(publication: PublishedObject) -> Dict[str, Any]:
    """Return the single canonical authority payload for prepare and recovery."""

    if not isinstance(publication, PublishedObject):
        raise TypeError("publication must be a PublishedObject")
    children = [
        {
            "canonical_path": edge.canonical_path,
            "child_ref": str(edge.child_ref),
            "edge_role": edge.role,
        }
        for edge in publication.edges
    ]
    children.sort(
        key=lambda item: (
            item["canonical_path"].encode("utf-8"),
            item["child_ref"],
            item["edge_role"],
        )
    )
    if len(
        {
            (item["canonical_path"], item["child_ref"], item["edge_role"])
            for item in children
        }
    ) != len(children):
        raise ValueError("publication edges must not contain duplicates")
    return {
        "content_ref": str(publication.content_ref),
        "children": children,
        "kind": publication.kind,
        "logical_bytes": publication.logical_bytes,
        "metadata": thaw_json(publication.metadata),
        "physical_bytes": publication.physical_bytes,
        "staging_id": publication.staging_id,
        "store_id": publication.store_id,
    }


def publication_digest(publication: PublishedObject) -> str:
    return request_digest(publication_payload(publication))


def _publication_bytes(publication: PublishedObject) -> bytes:
    return canonical_json_bytes(
        {
            "format": "optpilot.prepared-publication.v1",
            "publication": publication_payload(publication),
        }
    )


def _publication_from_bytes(payload: bytes) -> PublishedObject:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(f"Prepared publication marker is invalid JSON: {error}") from error
    expected = {
        "format",
        "publication",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise RealmIntegrityError("Prepared publication marker has an invalid shape.")
    if value["format"] != "optpilot.prepared-publication.v1":
        raise RealmIntegrityError("Prepared publication marker has an unsupported format.")
    value = value["publication"]
    publication_expected = {
        "content_ref",
        "children",
        "kind",
        "logical_bytes",
        "metadata",
        "physical_bytes",
        "staging_id",
        "store_id",
    }
    if not isinstance(value, dict) or set(value) != publication_expected:
        raise RealmIntegrityError("Prepared publication marker has an invalid shape.")
    try:
        content_ref = parse_content_ref(value["content_ref"])
        if not isinstance(content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("candidate refs are not physical local objects")
        if not isinstance(value["children"], list) or not isinstance(value["metadata"], dict):
            raise ValueError("children and metadata must have canonical container types")
        edges = tuple(
            ContentEdge(
                parent_ref=SnapshotRef.parse(str(content_ref)),
                child_ref=BlobRef.parse(edge["child_ref"]),
                role=edge["edge_role"],
                canonical_path=edge["canonical_path"],
            )
            for edge in value["children"]
        )
        publication = PublishedObject(
            staging_id=value["staging_id"],
            store_id=value["store_id"],
            content_ref=content_ref,
            kind=value["kind"],
            logical_bytes=value["logical_bytes"],
            physical_bytes=value["physical_bytes"],
            metadata=dict(value["metadata"]),
            edges=edges,
        )
    except (ContentRejected, KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError(f"Prepared publication marker is invalid: {error}") from error
    if _publication_bytes(publication) != payload:
        raise RealmIntegrityError("Prepared publication marker is not canonical.")
    return publication


def _require_blob_ref(content_ref: LocalObjectRef) -> BlobRef:
    if not isinstance(content_ref, BlobRef):
        raise ContentCorrupt("Blob publication returned a non-blob ref.")
    return content_ref


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]


def _write_file_exclusive_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_child_directory_nofollow(parent_fd: int, name: str, *, label: str) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise RealmIntegrityError(f"Managed {label} is not a real directory.")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RealmIntegrityError(f"Managed {label} is not safely reachable.") from error
    opened = os.fstat(descriptor)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or opened.st_dev != parent.st_dev
    ):
        os.close(descriptor)
        raise RealmIntegrityError(
            f"Managed {label} identity changed or crosses a filesystem boundary."
        )
    return descriptor


def _require_name_identity(parent_fd: int, name: str, opened: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise RealmIntegrityError("Managed private-tree entry was removed or replaced.") from error
    if (
        stat.S_IFMT(current.st_mode) != stat.S_IFMT(opened.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RealmIntegrityError("Managed private-tree entry identity changed.")


def _remove_private_tree_at(parent_fd: int, name: str) -> None:
    """Remove one generated private tree without following any path component."""

    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(before.st_mode):
        raise RealmIntegrityError("Managed private tree is not a real directory.")
    directory_fd = _open_child_directory_nofollow(parent_fd, name, label="private tree")
    opened = os.fstat(directory_fd)
    try:
        _require_name_identity(parent_fd, name, opened)
        if os.name != "nt":
            os.fchmod(directory_fd, 0o700)
        for child_name in os.listdir(directory_fd):
            child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(child.st_mode):
                _remove_private_tree_at(directory_fd, child_name)
                continue
            if not stat.S_ISREG(child.st_mode):
                raise RealmIntegrityError(
                    "Managed private tree contains a symlink or special filesystem node."
                )
            child_fd = os.open(child_name, _FILE_FLAGS, dir_fd=directory_fd)
            try:
                opened_child = os.fstat(child_fd)
                if (
                    not stat.S_ISREG(opened_child.st_mode)
                    or opened_child.st_nlink != 1
                    or opened_child.st_dev != opened.st_dev
                    or (opened_child.st_dev, opened_child.st_ino)
                    != (child.st_dev, child.st_ino)
                ):
                    raise RealmIntegrityError("Managed private file identity changed.")
                if os.name != "nt":
                    os.fchmod(child_fd, 0o600)
                _require_name_identity(directory_fd, child_name, opened_child)
            finally:
                os.close(child_fd)
            os.unlink(child_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        _require_name_identity(parent_fd, name, opened)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


__all__ = [
    "AllowedFileSource",
    "AllowedTreeSource",
    "BlobSealReceipt",
    "CompletedTreeCapture",
    "ContentEdge",
    "ContentRetentionAdapter",
    "DARWIN_IGNORED_NONCONTENT_XATTRS",
    "LocalContentCapture",
    "LocalContentStore",
    "PublishedObject",
    "TreeSealReceipt",
    "publication_digest",
    "publication_payload",
]
