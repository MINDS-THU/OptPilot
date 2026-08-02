"""Report-only local fsck and ledger-driven tombstone I/O hooks.

This module never decides reachability.  The RealmLedger must mark and recheck
an object before calling :class:`LocalGcBackend`; these hooks only reconcile the
corresponding atomic filesystem rename and eventual private deletion.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Protocol, Tuple, Type

from ._validation import freeze_json
from .content import (
    _MAX_MANIFEST_BYTES,
    LocalContentStore,
    LocalObjectRef,
    _open_child_directory_nofollow,
    _publication_from_bytes,
    _read_child_immutable_regular,
    _remove_private_tree_at,
    _require_recoverable_object_directory,
    _require_name_identity,
    _verify_blob_fd,
    _verify_tree_fd,
    publication_digest,
)
from .errors import ContentCorrupt, RealmIntegrityError
from .refs import BlobRef, SnapshotRef


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PREFIX_RE = re.compile(r"^[0-9a-f]{2}$")
_DELETION_TOKEN_RE = re.compile(r"^delete-[0-9a-f]{32}$")
_STAGING_ID_RE = re.compile(r"^stage-[0-9a-f]{32}$")
_CLEANUP_TOKEN_RE = re.compile(r"^cleanup-[0-9a-f]{32}$")


@dataclass(frozen=True)
class FsckIssue:
    severity: str
    code: str
    message: str
    relative_path: str
    content_ref: Optional[str] = None


@dataclass(frozen=True)
class FsckReport:
    store_id: str
    checked_blobs: int
    checked_trees: int
    staging_ids: Tuple[str, ...]
    tombstone_tokens: Tuple[str, ...]
    issues: Tuple[FsckIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass(frozen=True)
class TombstoneReceipt:
    content_ref: LocalObjectRef
    deletion_token: str
    state: str
    moved: bool


@dataclass(frozen=True)
class AbandonedStagingCleanupDecision:
    """Exact token-fenced authority returned while the store lock is held."""

    store_id: str
    backend_kind: str
    root_marker: str
    staging_id: str
    cleanup_token: str
    content_ref: Optional[LocalObjectRef]
    publication_digest: Optional[str]
    remove_live_orphan: bool

    def __post_init__(self) -> None:
        if not isinstance(self.store_id, str) or not self.store_id:
            raise ValueError("cleanup store_id is required.")
        if not isinstance(self.backend_kind, str) or not self.backend_kind:
            raise ValueError("cleanup backend_kind is required.")
        if not isinstance(self.root_marker, str) or not self.root_marker:
            raise ValueError("cleanup root_marker is required.")
        _validate_staging_id(self.staging_id)
        _validate_cleanup_token(self.cleanup_token)
        if (self.content_ref is None) != (self.publication_digest is None):
            raise ValueError("cleanup publication facts must be present together.")
        if self.content_ref is not None:
            if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
                raise ValueError("cleanup content_ref must identify local content.")
            if (
                not isinstance(self.publication_digest, str)
                or not _DIGEST_RE.fullmatch(self.publication_digest)
            ):
                raise ValueError("cleanup publication_digest is malformed.")
        if not isinstance(self.remove_live_orphan, bool):
            raise TypeError("remove_live_orphan must be boolean.")
        if self.remove_live_orphan and self.content_ref is None:
            raise ValueError("live-orphan removal requires exact prepared publication facts.")


@dataclass(frozen=True)
class AbandonedStagingCleanupReceipt:
    store_id: str
    staging_id: str
    cleanup_token: str
    state: str
    stage_removed: bool
    live_orphan_rehomed: bool

    def __post_init__(self) -> None:
        if self.state != "cleaned":
            raise ValueError("abandoned staging receipt state must be cleaned.")
        _validate_staging_id(self.staging_id)
        _validate_cleanup_token(self.cleanup_token)
        if not isinstance(self.stage_removed, bool) or not isinstance(
            self.live_orphan_rehomed, bool
        ):
            raise TypeError("cleanup receipt flags must be boolean.")


@dataclass(frozen=True)
class RegisteredContentEdge:
    parent_ref: SnapshotRef
    child_ref: BlobRef
    canonical_path: str
    edge_role: str = "tree-file"


@dataclass(frozen=True)
class RegisteredContentFact:
    content_ref: LocalObjectRef
    kind: str
    logical_bytes: int
    physical_bytes: int
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("registered local content must be a blob or tree ref.")
        expected_kind = "blob" if isinstance(self.content_ref, BlobRef) else "tree"
        if self.kind != expected_kind:
            raise ValueError("registered content kind differs from its reference.")
        for name in ("logical_bytes", "physical_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer.")
        object.__setattr__(self, "metadata", freeze_json(self.metadata, label="content facts"))


@dataclass(frozen=True)
class RegisteredStagingAllocation:
    """Ledger staging state needed to distinguish work from recovery debt."""

    staging_id: str
    state: str
    content_ref: Optional[LocalObjectRef] = None
    publication_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"stage-[0-9a-f]{32}", self.staging_id):
            raise ValueError("registered staging_id is malformed.")
        if self.state not in {"allocated", "prepared", "published"}:
            raise ValueError(
                "registered staging state must be allocated, prepared, or published."
            )
        if self.state == "allocated":
            if self.content_ref is not None or self.publication_digest is not None:
                raise ValueError("allocated staging must not claim prepared publication facts.")
            return
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("prepared staging requires a local content reference.")
        if (
            not isinstance(self.publication_digest, str)
            or not _DIGEST_RE.fullmatch(self.publication_digest)
        ):
            raise ValueError("prepared staging requires a canonical publication digest.")


@dataclass(frozen=True)
class RegisteredTombstone:
    """Ledger tombstone state relevant to physical trash reconciliation."""

    content_ref: LocalObjectRef
    state: str
    deletion_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("registered tombstone must reference local content.")
        if self.state not in {"deleting", "cancelled"}:
            raise ValueError("registered tombstone state must be deleting or cancelled.")
        _validate_deletion_token(self.deletion_token)


@dataclass(frozen=True)
class RegisteredStagingCleanup:
    """Terminal staging debt relevant to report-only reconciliation."""

    staging_id: str
    state: str
    content_ref: Optional[LocalObjectRef]
    publication_digest: Optional[str]

    def __post_init__(self) -> None:
        _validate_staging_id(self.staging_id)
        if self.state not in {"abandoned", "cleaning"}:
            raise ValueError("inventory staging cleanup must be abandoned or cleaning.")
        if self.content_ref is not None and not isinstance(
            self.content_ref, (BlobRef, SnapshotRef)
        ):
            raise ValueError("inventory staging cleanup must reference local content.")
        if (self.content_ref is None) != (self.publication_digest is None):
            raise ValueError("inventory cleanup publication facts must be present together.")
        if self.publication_digest is not None and not _DIGEST_RE.fullmatch(
            self.publication_digest
        ):
            raise ValueError("inventory cleanup publication digest is malformed.")


class ContentInventoryAdapter(Protocol):
    """One transactionally consistent ledger inventory used by composed fsck."""

    def expected_live_refs(self, *, store_id: str) -> Iterable[LocalObjectRef]: ...

    def registered_edges(self, *, store_id: str) -> Iterable[RegisteredContentEdge]: ...

    def registered_content(self, *, store_id: str) -> Iterable[RegisteredContentFact]: ...

    def expected_staging_allocations(
        self, *, store_id: str
    ) -> Iterable[RegisteredStagingAllocation]: ...

    def expected_tombstones(self, *, store_id: str) -> Iterable[RegisteredTombstone]: ...

    def expected_staging_cleanups(
        self, *, store_id: str
    ) -> Iterable[RegisteredStagingCleanup]: ...

    def transitional_gc_refs(self, *, store_id: str) -> Iterable[LocalObjectRef]: ...


def new_deletion_token() -> str:
    return f"delete-{uuid.uuid4().hex}"


def fsck_local_store(
    store: LocalContentStore,
    *,
    inventory_factory: Optional[Callable[[], ContentInventoryAdapter]] = None,
) -> FsckReport:
    """Inspect a publication/GC-quiescent local store without repairing it.

    A caller that needs an inventory captured in the same quiescent window can
    pass ``inventory_factory``.  The callback runs after acquiring the store
    lock and before any physical scan.  In-progress ``allocated`` staging is
    handled explicitly because source capture occurs outside this lock.
    Omitting authority performs a physical-only check; there is intentionally
    no racy pre-captured inventory form.
    """

    with store.exclusive_lock():
        stable_inventory = inventory_factory() if inventory_factory is not None else None
        return _fsck_local_store_locked(store, inventory=stable_inventory)


def _fsck_local_store_locked(
    store: LocalContentStore,
    *,
    inventory: Optional[ContentInventoryAdapter],
) -> FsckReport:
    """Implementation for a caller holding the store's interprocess lock."""

    issues: List[FsckIssue] = []
    blobs = _discover_objects(store, BlobRef, "blobs", issues)
    trees = _discover_objects(store, SnapshotRef, "trees", issues)
    checked_blobs = 0
    blob_manifests = {}
    for blob_ref in blobs:
        _check_object_permissions(store, blob_ref, issues)
        try:
            blob_manifests[blob_ref] = store.verify_blob(blob_ref)
            checked_blobs += 1
        except (ContentCorrupt, OSError) as error:
            issues.append(
                FsckIssue(
                    "error",
                    "corrupt_blob",
                    str(error),
                    _relative(store, store._object_directory(blob_ref)),
                    str(blob_ref),
                )
            )

    checked_trees = 0
    tree_manifests = {}
    for tree_ref in trees:
        _check_object_permissions(store, tree_ref, issues)
        try:
            tree_manifests[tree_ref] = store.verify_tree(tree_ref, verify_children=False)
            checked_trees += 1
        except (ContentCorrupt, OSError) as error:
            issues.append(
                FsckIssue(
                    "error",
                    "corrupt_tree",
                    str(error),
                    _relative(store, store._object_directory(tree_ref)),
                    str(tree_ref),
                )
            )
            continue

    staging_ids, staging_issues = _discover_generated_directories(
        store,
        "staging",
        expected_prefix="stage-",
        expected_length=38,
        malformed_code="malformed_staging_path",
    )
    issues.extend(staging_issues)
    tombstones, tombstone_issues = _discover_generated_directories(
        store,
        "trash",
        expected_prefix="delete-",
        expected_length=39,
        malformed_code="malformed_tombstone_path",
    )
    issues.extend(tombstone_issues)
    if inventory is None:
        _check_tree_children(store, tree_manifests, blob_manifests, issues)
    if inventory is not None:
        physical_refs = set(blobs) | set(trees)
        expected_refs = set(inventory.expected_live_refs(store_id=store.store_id))
        registered_staging_cleanups = _registered_staging_cleanups(
            inventory,
            store.store_id,
        )
        staging_cleanup_by_id = {
            item.staging_id: item for item in registered_staging_cleanups
        }
        if len(staging_cleanup_by_id) != len(registered_staging_cleanups):
            issues.append(
                FsckIssue(
                    "error",
                    "duplicate_registered_staging_cleanup",
                    "Ledger inventory repeats one terminal staging cleanup.",
                    "ledger/staging_allocations",
                )
            )
        registered_tombstones = _registered_tombstones(inventory, store.store_id)
        tombstones_by_ref = {item.content_ref: item for item in registered_tombstones}
        if len(tombstones_by_ref) != len(registered_tombstones):
            issues.append(
                FsckIssue(
                    "error",
                    "duplicate_registered_tombstone",
                    "Ledger inventory repeats one content tombstone.",
                    "ledger/gc_tombstones",
                )
            )
        cleanup_refs = set(inventory.transitional_gc_refs(store_id=store.store_id))
        tombstone_deleting_refs = {
            item.content_ref for item in registered_tombstones if item.state == "deleting"
        }
        if not tombstone_deleting_refs.issubset(cleanup_refs):
            issues.append(
                FsckIssue(
                    "error",
                    "inconsistent_gc_inventory",
                    "Ledger deleting tombstones are absent from its cleanup ref set.",
                    "ledger/gc_tombstones",
                )
            )
        physical_tombstones = set(tombstones)
        expected_by_token = {
            item.deletion_token: item for item in registered_tombstones
        }
        if len(expected_by_token) != len(registered_tombstones):
            issues.append(
                FsckIssue(
                    "error",
                    "duplicate_registered_tombstone_token",
                    "Ledger inventory assigns one deletion token to multiple refs.",
                    "ledger/gc_tombstones",
                )
            )
        for deletion_token in sorted(physical_tombstones - set(expected_by_token)):
            issues.append(
                FsckIssue(
                    "error",
                    "orphan_tombstone",
                    "Physical trash directory has no current ledger tombstone token.",
                    f"trash/{deletion_token}",
                )
            )
        tombstone_blob_manifests = {}
        tombstone_tree_manifests = {}
        verified_tombstone_refs = set()
        matched_tokens = physical_tombstones & set(expected_by_token)
        if matched_tokens:
            trash_fd = store._open_namespace_fd("trash")
            try:
                for deletion_token in sorted(matched_tokens):
                    registered = expected_by_token[deletion_token]
                    try:
                        manifest = _verify_at_fd(
                            registered.content_ref,
                            trash_fd,
                            deletion_token,
                        )
                    except (ContentCorrupt, RealmIntegrityError, OSError) as error:
                        issues.append(
                            FsckIssue(
                                "error",
                                "corrupt_tombstone",
                                str(error),
                                f"trash/{deletion_token}",
                                str(registered.content_ref),
                            )
                        )
                    else:
                        verified_tombstone_refs.add(registered.content_ref)
                        target = (
                            tombstone_blob_manifests
                            if isinstance(registered.content_ref, BlobRef)
                            else tombstone_tree_manifests
                        )
                        target[registered.content_ref] = manifest
                    if (
                        registered.state == "deleting"
                        and registered.content_ref in physical_refs
                    ):
                        issues.append(
                            FsckIssue(
                                "error",
                                "duplicate_deleting_bytes",
                                "Deleting content exists in both live and trash namespaces.",
                                f"trash/{deletion_token}",
                                str(registered.content_ref),
                            )
                        )
            finally:
                os.close(trash_fd)
        for deletion_token in sorted(set(expected_by_token) - physical_tombstones):
            registered = expected_by_token[deletion_token]
            issues.append(
                FsckIssue(
                    "warning",
                    "expected_tombstone_missing",
                    (
                        "Ledger tombstone has no physical trash directory; this is valid "
                        "between claim and move or between deletion and ledger completion."
                    ),
                    f"trash/{deletion_token}",
                    str(registered.content_ref),
                )
            )
        managed_refs = expected_refs | cleanup_refs
        physical_staging = set(staging_ids)
        authorized_cleanup_live_refs = set()
        cleanup_live_authorities = tuple(
            item
            for item in registered_staging_cleanups
            if item.content_ref in physical_refs and item.content_ref not in managed_refs
        )
        if cleanup_live_authorities:
            staging_parent_fd = store._open_namespace_fd("staging")
            try:
                for cleanup in cleanup_live_authorities:
                    assert cleanup.content_ref is not None
                    if cleanup.staging_id not in physical_staging:
                        issues.append(
                            FsckIssue(
                                "error",
                                "cleanup_stage_missing_for_live_orphan",
                                (
                                    "Unregistered live content cannot be safely removed because "
                                    "its exact cleanup staging directory is absent."
                                ),
                                f"staging/{cleanup.staging_id}",
                                str(cleanup.content_ref),
                            )
                        )
                        continue
                    try:
                        stage_fd = _open_child_directory_nofollow(
                            staging_parent_fd,
                            cleanup.staging_id,
                            label=f"staging allocation {cleanup.staging_id}",
                        )
                        try:
                            publication = _load_prepared_publication_at(
                                stage_fd,
                                store_id=store.store_id,
                                staging_id=cleanup.staging_id,
                            )
                        finally:
                            os.close(stage_fd)
                        if (
                            publication.content_ref != cleanup.content_ref
                            or publication_digest(publication)
                            != cleanup.publication_digest
                        ):
                            raise RealmIntegrityError(
                                "Cleanup marker differs from its exact ledger publication facts."
                            )
                    except (ContentCorrupt, RealmIntegrityError, OSError, ValueError) as error:
                        issues.append(
                            FsckIssue(
                                "error",
                                "invalid_cleanup_stage_authority",
                                str(error),
                                f"staging/{cleanup.staging_id}",
                                str(cleanup.content_ref),
                            )
                        )
                    else:
                        authorized_cleanup_live_refs.add(cleanup.content_ref)
            finally:
                os.close(staging_parent_fd)
        staging_cleanup_only_refs = authorized_cleanup_live_refs
        all_blob_manifests = {**blob_manifests, **tombstone_blob_manifests}
        all_tree_manifests = {**tree_manifests, **tombstone_tree_manifests}
        _check_tree_children(
            store,
            all_tree_manifests,
            all_blob_manifests,
            issues,
            allow_missing_for=cleanup_refs | authorized_cleanup_live_refs,
        )
        registered_facts = tuple(inventory.registered_content(store_id=store.store_id))
        facts_by_ref = {item.content_ref: item for item in registered_facts}
        if len(facts_by_ref) != len(registered_facts):
            issues.append(
                FsckIssue(
                    "error",
                    "duplicate_registered_fact",
                    "Ledger inventory repeats one immutable content fact.",
                    "ledger/content_objects",
                )
            )
        for content_ref in sorted(managed_refs, key=str):
            fact = facts_by_ref.get(content_ref)
            if fact is None:
                issues.append(
                    FsckIssue(
                        "error",
                        "registered_fact_missing",
                        "Expected content has no immutable ledger facts.",
                        "ledger/content_objects",
                        str(content_ref),
                    )
                )
                continue
            mismatch = _immutable_fact_mismatch(
                content_ref,
                fact,
                blob_manifests=all_blob_manifests,
                tree_manifests=all_tree_manifests,
            )
            if mismatch is not None:
                issues.append(
                    FsckIssue(
                        "error",
                        "registered_fact_mismatch",
                        mismatch,
                        _relative(store, store._object_directory(content_ref)),
                        str(content_ref),
                    )
                )
        for content_ref in sorted(set(facts_by_ref) - managed_refs, key=str):
            issues.append(
                FsckIssue(
                    "error",
                    "unexpected_registered_fact",
                    "Ledger immutable facts are outside its managed content set.",
                    "ledger/content_objects",
                    str(content_ref),
                )
            )
        for content_ref in sorted(
            physical_refs - managed_refs - authorized_cleanup_live_refs, key=str
        ):
            issues.append(
                FsckIssue(
                    "error",
                    "physical_orphan",
                    "Managed bytes have no registered live content row.",
                    _relative(store, store._object_directory(content_ref)),
                    str(content_ref),
                )
            )
        physically_present_refs = physical_refs | verified_tombstone_refs
        for content_ref in sorted(managed_refs - physically_present_refs, key=str):
            issues.append(
                FsckIssue(
                    "error",
                    "registered_bytes_missing",
                    "Registered content has no verified live or tombstoned bytes.",
                    _relative(store, store._object_directory(content_ref)),
                    str(content_ref),
                )
            )
        registered_edges = set(inventory.registered_edges(store_id=store.store_id))
        for edge in registered_edges:
            if edge.parent_ref not in managed_refs or edge.child_ref not in managed_refs:
                issues.append(
                    FsckIssue(
                        "error",
                        "dangling_registered_closure",
                        (
                            f"Registered edge {edge.parent_ref} -> {edge.child_ref} "
                            "does not resolve inside the registered managed set."
                        ),
                        edge.canonical_path,
                        str(edge.parent_ref),
                    )
                )
        physical_edges = {
            RegisteredContentEdge(
                parent_ref=tree_ref,
                child_ref=entry.blob_ref,
                canonical_path=entry.path,
                edge_role="tree-file",
            )
            for tree_ref, manifest in all_tree_manifests.items()
            for entry in manifest.entries
            if (
                entry.kind == "file"
                and entry.blob_ref is not None
                and not (
                    tree_ref in cleanup_refs
                    and entry.blob_ref not in managed_refs
                )
                and tree_ref not in staging_cleanup_only_refs
            )
        }
        for edge in sorted(
            physical_edges - registered_edges,
            key=lambda item: (str(item.parent_ref), item.canonical_path, str(item.child_ref)),
        ):
            issues.append(
                FsckIssue(
                    "error",
                    "registered_edge_missing",
                    "Canonical tree edge is missing from the ledger registry.",
                    edge.canonical_path,
                    str(edge.parent_ref),
                )
            )
        for edge in sorted(
            registered_edges - physical_edges,
            key=lambda item: (str(item.parent_ref), item.canonical_path, str(item.child_ref)),
        ):
            issues.append(
                FsckIssue(
                    "error",
                    "registered_edge_mismatch",
                    "Ledger edge does not occur in the canonical tree manifest.",
                    edge.canonical_path,
                    str(edge.parent_ref),
                )
            )
        staging_allocations = _registered_staging_allocations(inventory, store.store_id)
        allocation_by_id = {item.staging_id: item for item in staging_allocations}
        if len(allocation_by_id) != len(staging_allocations):
            issues.append(
                FsckIssue(
                    "error",
                    "duplicate_registered_staging",
                    "Ledger inventory repeats one active staging allocation.",
                    "ledger/staging_allocations",
                )
            )
        expected_staging = set(allocation_by_id)
        cleanup_staging = set(staging_cleanup_by_id)
        for staging_id in sorted(expected_staging & cleanup_staging):
            issues.append(
                FsckIssue(
                    "error",
                    "conflicting_staging_inventory",
                    "Staging id is both active and terminal cleanup debt.",
                    f"staging/{staging_id}",
                )
            )
        for staging_id in sorted(
            physical_staging - expected_staging - cleanup_staging
        ):
            issues.append(
                FsckIssue(
                    "error",
                    "stale_staging",
                    "Physical staging directory has no active ledger allocation.",
                    f"staging/{staging_id}",
                )
            )
        for staging_id in sorted(cleanup_staging):
            cleanup = staging_cleanup_by_id[staging_id]
            presence = "present" if staging_id in physical_staging else "already absent"
            issues.append(
                FsckIssue(
                    "warning",
                    "staging_cleanup_debt",
                    (
                        f"Terminal staging cleanup is {cleanup.state}; its physical "
                        f"directory is {presence}."
                    ),
                    f"staging/{staging_id}",
                    str(cleanup.content_ref) if cleanup.content_ref is not None else None,
                )
            )
        for staging_id in sorted(expected_staging - physical_staging):
            allocation = allocation_by_id[staging_id]
            if allocation.state in {"allocated", "published"}:
                issues.append(
                    FsckIssue(
                        "warning",
                        f"{allocation.state}_staging_missing",
                        (
                            f"{allocation.state.title()} staging has no physical directory; "
                            "this is valid at its filesystem/ledger transition boundary."
                        ),
                        f"staging/{staging_id}",
                    )
                )
                continue
            issues.append(
                FsckIssue(
                    "error",
                    "registered_staging_missing",
                    "Active ledger allocation has no physical staging directory.",
                    f"staging/{staging_id}",
                )
            )
        for staging_id in sorted(expected_staging & physical_staging):
            if allocation_by_id[staging_id].state not in {"prepared", "published"}:
                continue
            try:
                publication = store.load_prepared_publication(staging_id)
            except (ContentCorrupt, RealmIntegrityError, OSError, ValueError) as error:
                issues.append(
                    FsckIssue(
                        "error",
                        "invalid_prepared_staging",
                        f"Prepared staging allocation lacks a valid publication marker: {error}",
                        f"staging/{staging_id}",
                    )
                )
                continue
            allocation = allocation_by_id[staging_id]
            if (
                publication.content_ref != allocation.content_ref
                or publication_digest(publication) != allocation.publication_digest
            ):
                issues.append(
                    FsckIssue(
                        "error",
                        "prepared_staging_mismatch",
                        "Prepared marker differs from the ledger's exact publication facts.",
                        f"staging/{staging_id}",
                        str(allocation.content_ref),
                    )
                )
    return FsckReport(
        store_id=store.store_id,
        checked_blobs=checked_blobs,
        checked_trees=checked_trees,
        staging_ids=tuple(staging_ids),
        tombstone_tokens=tuple(tombstones),
        issues=tuple(issues),
    )


class LocalGcBackend:
    """Idempotent physical hooks for ledger-owned GC state transitions."""

    def __init__(self, store: LocalContentStore) -> None:
        self.store = store

    def tombstone(
        self,
        content_ref: LocalObjectRef,
        *,
        deletion_token: str,
        still_eligible: Callable[[], bool],
    ) -> TombstoneReceipt:
        _validate_deletion_token(deletion_token)
        with self.store.exclusive_lock():
            if not still_eligible():
                return TombstoneReceipt(content_ref, deletion_token, "cancelled", False)
            prefix_fd = self.store._open_existing_object_prefix_fd(content_ref)
            trash_fd = self.store._open_namespace_fd("trash")
            try:
                live_info = (
                    _directory_entry(prefix_fd, content_ref.digest, "live object")
                    if prefix_fd is not None
                    else None
                )
                tombstone_info = _directory_entry(trash_fd, deletion_token, "tombstone")
                if live_info is not None and tombstone_info is not None:
                    raise RealmIntegrityError(
                        f"Both live and tombstoned bytes exist for {content_ref}/{deletion_token}."
                    )
                if tombstone_info is not None:
                    _verify_at_fd(content_ref, trash_fd, deletion_token)
                    if prefix_fd is not None:
                        os.fsync(prefix_fd)
                    os.fsync(trash_fd)
                    return TombstoneReceipt(content_ref, deletion_token, "tombstoned", False)
                if live_info is None or prefix_fd is None:
                    return TombstoneReceipt(content_ref, deletion_token, "missing", False)

                live_fd = _open_child_directory_nofollow(
                    prefix_fd, content_ref.digest, label=f"live object {content_ref}"
                )
                try:
                    _require_recoverable_object_directory(live_fd, content_ref)
                    _verify_open_object_fd(content_ref, live_fd)
                    # macOS requires write permission on a directory being renamed.
                    if os.name != "nt":
                        os.fchmod(live_fd, 0o700)
                    os.fsync(live_fd)
                    _require_name_identity(prefix_fd, content_ref.digest, os.fstat(live_fd))
                finally:
                    os.close(live_fd)
                try:
                    os.rename(
                        content_ref.digest,
                        deletion_token,
                        src_dir_fd=prefix_fd,
                        dst_dir_fd=trash_fd,
                    )
                except OSError:
                    if (
                        _directory_entry(trash_fd, deletion_token, "tombstone") is not None
                        and _directory_entry(prefix_fd, content_ref.digest, "live object") is None
                    ):
                        _verify_at_fd(content_ref, trash_fd, deletion_token)
                        os.fsync(prefix_fd)
                        os.fsync(trash_fd)
                        return TombstoneReceipt(content_ref, deletion_token, "tombstoned", False)
                    remaining = _directory_entry(
                        prefix_fd,
                        content_ref.digest,
                        "live object",
                    )
                    if remaining is not None:
                        restore_fd = _open_child_directory_nofollow(
                            prefix_fd,
                            content_ref.digest,
                            label=f"live object {content_ref}",
                        )
                        try:
                            _require_recoverable_object_directory(
                                restore_fd,
                                content_ref,
                            )
                            if os.name != "nt":
                                os.fchmod(restore_fd, 0o500)
                            os.fsync(restore_fd)
                            _require_name_identity(
                                prefix_fd,
                                content_ref.digest,
                                os.fstat(restore_fd),
                            )
                        finally:
                            os.close(restore_fd)
                        os.fsync(prefix_fd)
                    raise
                os.fsync(prefix_fd)
                os.fsync(trash_fd)
                _verify_at_fd(content_ref, trash_fd, deletion_token)
                return TombstoneReceipt(content_ref, deletion_token, "tombstoned", True)
            finally:
                if prefix_fd is not None:
                    os.close(prefix_fd)
                os.close(trash_fd)

    def delete(
        self,
        content_ref: LocalObjectRef,
        *,
        deletion_token: str,
        still_deletable: Callable[[], bool],
    ) -> TombstoneReceipt:
        _validate_deletion_token(deletion_token)
        with self.store.exclusive_lock():
            if not still_deletable():
                return TombstoneReceipt(content_ref, deletion_token, "cancelled", False)
            prefix_fd = self.store._open_existing_object_prefix_fd(content_ref)
            trash_fd = self.store._open_namespace_fd("trash")
            try:
                if (
                    prefix_fd is not None
                    and _directory_entry(prefix_fd, content_ref.digest, "live object") is not None
                ):
                    raise RealmIntegrityError(
                        f"Refusing to delete tombstone while live bytes still exist: {content_ref}."
                    )
                if _directory_entry(trash_fd, deletion_token, "tombstone") is None:
                    return TombstoneReceipt(content_ref, deletion_token, "deleted", False)
                # The ledger callback above is the authority to remove this
                # token.  Do not require the private trash copy to remain a
                # complete immutable object: a previous process may have
                # crashed after unlinking only some members.  The descriptor-
                # rooted remover still rejects symlinks, special nodes, and
                # hardlinks before touching them.
                _remove_private_tree_at(trash_fd, deletion_token)
                return TombstoneReceipt(content_ref, deletion_token, "deleted", True)
            finally:
                if prefix_fd is not None:
                    os.close(prefix_fd)
                os.close(trash_fd)

    def discard_cancelled_tombstone(
        self,
        content_ref: LocalObjectRef,
        *,
        deletion_token: str,
        still_cancelled: Callable[[], bool],
    ) -> TombstoneReceipt:
        """Remove a stale trash duplicate after the same ref was republished."""

        _validate_deletion_token(deletion_token)
        with self.store.exclusive_lock():
            if not still_cancelled():
                return TombstoneReceipt(content_ref, deletion_token, "cancelled", False)
            prefix_fd = self.store._open_existing_object_prefix_fd(content_ref)
            trash_fd = self.store._open_namespace_fd("trash")
            try:
                if (
                    prefix_fd is None
                    or _directory_entry(prefix_fd, content_ref.digest, "live object") is None
                ):
                    raise RealmIntegrityError(
                        f"Republished live bytes are missing for {content_ref}."
                    )
                _verify_at_fd(content_ref, prefix_fd, content_ref.digest)
                if _directory_entry(trash_fd, deletion_token, "tombstone") is None:
                    return TombstoneReceipt(
                        content_ref, deletion_token, "cancelled-clean", False
                    )
                # As with committed deletion, cleanup must resume after a
                # crash partway through descriptor-rooted private-tree removal.
                # The republished live object was fully verified above.
                _remove_private_tree_at(trash_fd, deletion_token)
                return TombstoneReceipt(
                    content_ref, deletion_token, "cancelled-clean", True
                )
            finally:
                if prefix_fd is not None:
                    os.close(prefix_fd)
                os.close(trash_fd)

    def reconcile(
        self,
        content_ref: LocalObjectRef,
        *,
        deletion_token: str,
        desired_state: str,
        recheck: Callable[[], bool],
    ) -> TombstoneReceipt:
        if desired_state == "tombstoned":
            return self.tombstone(
                content_ref,
                deletion_token=deletion_token,
                still_eligible=recheck,
            )
        if desired_state == "deleted":
            # Try the final transition first.  This lets a restarted worker
            # finish a partially removed private trash tree without routing it
            # back through the complete-object verification required before
            # the initial live-to-trash rename.
            try:
                return self.delete(
                    content_ref,
                    deletion_token=deletion_token,
                    still_deletable=recheck,
                )
            except RealmIntegrityError:
                self.tombstone(
                    content_ref,
                    deletion_token=deletion_token,
                    still_eligible=recheck,
                )
                return self.delete(
                    content_ref,
                    deletion_token=deletion_token,
                    still_deletable=recheck,
                )
        raise ValueError("desired_state must be 'tombstoned' or 'deleted'.")


class LocalAbandonedStagingBackend:
    """Token-fenced cleanup for terminal local staging allocations.

    Live CAS objects are never recursively removed in place.  When the ledger
    proves that a prepared publication is an unregistered orphan, this backend
    first verifies and atomically rehomes it beneath the exact private staging
    allocation.  Only then does the descriptor-rooted private-tree remover run.
    """

    def __init__(self, store: LocalContentStore) -> None:
        self.store = store

    def cleanup(
        self,
        *,
        staging_id: str,
        cleanup_token: str,
        validate: Callable[[], AbandonedStagingCleanupDecision],
    ) -> AbandonedStagingCleanupReceipt:
        _validate_staging_id(staging_id)
        _validate_cleanup_token(cleanup_token)
        if not callable(validate):
            raise TypeError("validate must be callable.")

        with self.store.exclusive_lock():
            decision = validate()
            self._require_bound_decision(
                decision,
                staging_id=staging_id,
                cleanup_token=cleanup_token,
            )
            staging_parent_fd = self.store._open_namespace_fd("staging")
            prefix_fd = None
            try:
                stage_info = _directory_entry(
                    staging_parent_fd,
                    staging_id,
                    "staging allocation",
                )
                if decision.content_ref is None or not decision.remove_live_orphan:
                    if stage_info is not None:
                        _remove_private_tree_at(staging_parent_fd, staging_id)
                    return AbandonedStagingCleanupReceipt(
                        store_id=self.store.store_id,
                        staging_id=staging_id,
                        cleanup_token=cleanup_token,
                        state="cleaned",
                        stage_removed=stage_info is not None,
                        live_orphan_rehomed=False,
                    )

                content_ref = decision.content_ref
                assert content_ref is not None
                prefix_fd = self.store._open_existing_object_prefix_fd(content_ref)
                live_info = (
                    _directory_entry(prefix_fd, content_ref.digest, "live object")
                    if prefix_fd is not None
                    else None
                )
                if live_info is None:
                    # Recovery after the live-to-private rename (or its full
                    # deletion) must not depend on a still-complete marker.
                    if prefix_fd is not None:
                        os.fsync(prefix_fd)
                    if stage_info is not None:
                        stage_fd = _open_child_directory_nofollow(
                            staging_parent_fd,
                            staging_id,
                            label=f"staging allocation {staging_id}",
                        )
                        try:
                            os.fsync(stage_fd)
                        finally:
                            os.close(stage_fd)
                        _remove_private_tree_at(staging_parent_fd, staging_id)
                    return AbandonedStagingCleanupReceipt(
                        store_id=self.store.store_id,
                        staging_id=staging_id,
                        cleanup_token=cleanup_token,
                        state="cleaned",
                        stage_removed=stage_info is not None,
                        live_orphan_rehomed=False,
                    )

                if stage_info is None:
                    raise RealmIntegrityError(
                        "Cannot remove a live orphan without its exact private staging allocation."
                    )
                assert prefix_fd is not None
                stage_fd = _open_child_directory_nofollow(
                    staging_parent_fd,
                    staging_id,
                    label=f"staging allocation {staging_id}",
                )
                stage_opened = os.fstat(stage_fd)
                try:
                    publication = _load_prepared_publication_at(
                        stage_fd,
                        store_id=self.store.store_id,
                        staging_id=staging_id,
                    )
                    if (
                        publication.content_ref != content_ref
                        or publication_digest(publication) != decision.publication_digest
                    ):
                        raise RealmIntegrityError(
                            "Prepared staging marker differs from the cleanup authority."
                        )

                    live_fd = _open_child_directory_nofollow(
                        prefix_fd,
                        content_ref.digest,
                        label=f"live object {content_ref}",
                    )
                    try:
                        _require_recoverable_object_directory(live_fd, content_ref)
                        _verify_open_object_fd(content_ref, live_fd)
                        live_opened = os.fstat(live_fd)
                        _require_name_identity(
                            prefix_fd,
                            content_ref.digest,
                            live_opened,
                        )
                        if os.name != "nt":
                            os.fchmod(live_fd, 0o700)
                        os.fsync(live_fd)
                        live_opened = os.fstat(live_fd)
                        _require_name_identity(
                            prefix_fd,
                            content_ref.digest,
                            live_opened,
                        )
                    finally:
                        os.close(live_fd)

                    collision = _directory_entry(stage_fd, "object", "staged object")
                    if collision is not None:
                        _remove_private_tree_at(stage_fd, "object")
                    _require_name_identity(staging_parent_fd, staging_id, stage_opened)
                    _require_name_identity(prefix_fd, content_ref.digest, live_opened)
                    try:
                        os.rename(
                            content_ref.digest,
                            "object",
                            src_dir_fd=prefix_fd,
                            dst_dir_fd=stage_fd,
                        )
                    except OSError:
                        if (
                            _directory_entry(
                                prefix_fd,
                                content_ref.digest,
                                "live object",
                            )
                            is not None
                        ):
                            restore_fd = _open_child_directory_nofollow(
                                prefix_fd,
                                content_ref.digest,
                                label=f"live object {content_ref}",
                            )
                            try:
                                _require_recoverable_object_directory(
                                    restore_fd,
                                    content_ref,
                                )
                                if os.name != "nt":
                                    os.fchmod(restore_fd, 0o500)
                                os.fsync(restore_fd)
                                _require_name_identity(
                                    prefix_fd,
                                    content_ref.digest,
                                    os.fstat(restore_fd),
                                )
                            finally:
                                os.close(restore_fd)
                            os.fsync(prefix_fd)
                        raise
                    os.fsync(prefix_fd)
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
                _remove_private_tree_at(staging_parent_fd, staging_id)
                return AbandonedStagingCleanupReceipt(
                    store_id=self.store.store_id,
                    staging_id=staging_id,
                    cleanup_token=cleanup_token,
                    state="cleaned",
                    stage_removed=True,
                    live_orphan_rehomed=True,
                )
            finally:
                if prefix_fd is not None:
                    os.close(prefix_fd)
                os.close(staging_parent_fd)

    def _require_bound_decision(
        self,
        decision: AbandonedStagingCleanupDecision,
        *,
        staging_id: str,
        cleanup_token: str,
    ) -> None:
        if not isinstance(decision, AbandonedStagingCleanupDecision):
            raise TypeError("cleanup validator must return AbandonedStagingCleanupDecision.")
        if (
            decision.store_id != self.store.store_id
            or decision.backend_kind != self.store.BACKEND_KIND
            or decision.root_marker != self.store.root_marker
            or decision.staging_id != staging_id
            or decision.cleanup_token != cleanup_token
        ):
            raise RealmIntegrityError("Abandoned staging cleanup authority is bound elsewhere.")


def _registered_staging_allocations(
    inventory: ContentInventoryAdapter,
    store_id: str,
) -> Tuple[RegisteredStagingAllocation, ...]:
    values = inventory.expected_staging_allocations(store_id=store_id)
    return tuple(
        value
        if isinstance(value, RegisteredStagingAllocation)
        else RegisteredStagingAllocation(
            staging_id=value.staging_id,
            state=value.state,
            content_ref=value.content_ref,
            publication_digest=value.publication_digest,
        )
        for value in values
    )


def _load_prepared_publication_at(
    stage_fd: int,
    *,
    store_id: str,
    staging_id: str,
):
    raw = _read_child_immutable_regular(
        stage_fd,
        "publication.json",
        max_bytes=_MAX_MANIFEST_BYTES,
        label=f"prepared publication {staging_id}",
    )
    publication = _publication_from_bytes(raw)
    if publication.staging_id != staging_id or publication.store_id != store_id:
        raise RealmIntegrityError("Prepared publication marker belongs to another allocation.")
    return publication


def _registered_staging_cleanups(
    inventory: ContentInventoryAdapter,
    store_id: str,
) -> Tuple[RegisteredStagingCleanup, ...]:
    return tuple(
        value
        if isinstance(value, RegisteredStagingCleanup)
        else RegisteredStagingCleanup(
            staging_id=value.staging_id,
            state=value.state,
            content_ref=value.content_ref,
            publication_digest=value.publication_digest,
        )
        for value in inventory.expected_staging_cleanups(store_id=store_id)
    )


def _registered_tombstones(
    inventory: ContentInventoryAdapter,
    store_id: str,
) -> Tuple[RegisteredTombstone, ...]:
    values = inventory.expected_tombstones(store_id=store_id)
    result = []
    for value in values:
        state = value.state
        if state not in {"deleting", "cancelled"}:
            continue
        result.append(
            value
            if isinstance(value, RegisteredTombstone)
            else RegisteredTombstone(
                content_ref=value.content_ref,
                state=state,
                deletion_token=value.deletion_token,
            )
        )
    return tuple(result)


def _check_tree_children(
    store: LocalContentStore,
    tree_manifests,
    blob_manifests,
    issues: List[FsckIssue],
    *,
    allow_missing_for=frozenset(),
) -> None:
    """Check closure and sizes across both live and expected trash objects."""

    for tree_ref, manifest in tree_manifests.items():
        for entry in manifest.entries:
            if entry.kind != "file" or entry.blob_ref is None:
                continue
            blob_manifest = blob_manifests.get(entry.blob_ref)
            if blob_manifest is None:
                if tree_ref in allow_missing_for:
                    # GC may have already completed an unreferenced child while
                    # its equally unreachable parent is still being removed.
                    continue
                message = f"Tree entry {entry.path!r} references missing {entry.blob_ref}."
                issues.append(
                    FsckIssue(
                        "error",
                        "missing_tree_blob",
                        message,
                        _relative(store, store._object_directory(tree_ref)),
                        str(tree_ref),
                    )
                )
                issues.append(
                    FsckIssue(
                        "error",
                        "corrupt_tree",
                        message,
                        _relative(store, store._object_directory(tree_ref)),
                        str(tree_ref),
                    )
                )
            elif blob_manifest.size != entry.size:
                issues.append(
                    FsckIssue(
                        "error",
                        "corrupt_tree",
                        f"Tree entry size differs from blob manifest: {entry.path!r}.",
                        _relative(store, store._object_directory(tree_ref)),
                        str(tree_ref),
                    )
                )


def _immutable_fact_mismatch(
    content_ref: LocalObjectRef,
    fact: RegisteredContentFact,
    *,
    blob_manifests,
    tree_manifests,
) -> Optional[str]:
    if isinstance(content_ref, BlobRef):
        manifest = blob_manifests.get(content_ref)
        if manifest is None:
            return None
        expected_kind = "blob"
        expected_logical = manifest.size
        expected_physical = manifest.size + len(manifest.to_bytes())
        expected_metadata = {"manifest_format": "optpilot.blob.v1"}
    else:
        manifest = tree_manifests.get(content_ref)
        if manifest is None:
            return None
        expected_kind = "tree"
        expected_logical = manifest.logical_bytes
        expected_physical = len(manifest.to_bytes())
        expected_metadata = {
            "entry_count": len(manifest.entries),
            "manifest_format": "optpilot.tree.v1",
        }
    if (
        fact.kind != expected_kind
        or fact.logical_bytes != expected_logical
        or fact.physical_bytes != expected_physical
        or dict(fact.metadata) != expected_metadata
    ):
        return "Ledger immutable facts differ from the canonical managed object."
    return None


def _discover_objects(
    store: LocalContentStore,
    reference_type: Type[LocalObjectRef],
    kind_name: str,
    issues: List[FsckIssue],
) -> List[LocalObjectRef]:
    references = []
    try:
        kind_fd = store._open_object_kind_fd(kind_name)
    except (RealmIntegrityError, OSError) as error:
        issues.append(
            FsckIssue(
                "error",
                "unsafe_object_namespace",
                str(error),
                f"objects/{kind_name}",
            )
        )
        return references
    try:
        prefix_names = sorted(os.listdir(kind_fd))
        for prefix_name in prefix_names:
            try:
                prefix_info = os.stat(prefix_name, dir_fd=kind_fd, follow_symlinks=False)
            except OSError as error:
                issues.append(
                    FsckIssue(
                        "error",
                        "malformed_object_path",
                        str(error),
                        f"objects/{kind_name}/{prefix_name}",
                    )
                )
                continue
            if not stat.S_ISDIR(prefix_info.st_mode) or not _PREFIX_RE.fullmatch(prefix_name):
                issues.append(
                    FsckIssue(
                        "error",
                        "malformed_object_path",
                        f"Unexpected entry below {kind_name}: {prefix_name!r}.",
                        f"objects/{kind_name}/{prefix_name}",
                    )
                )
                continue
            try:
                prefix_fd = _open_child_directory_nofollow(
                    kind_fd,
                    prefix_name,
                    label=f"{kind_name} prefix {prefix_name}",
                )
            except RealmIntegrityError as error:
                issues.append(
                    FsckIssue(
                        "error",
                        "malformed_object_path",
                        str(error),
                        f"objects/{kind_name}/{prefix_name}",
                    )
                )
                continue
            try:
                object_names = sorted(os.listdir(prefix_fd))
                for object_name in object_names:
                    try:
                        object_info = os.stat(
                            object_name,
                            dir_fd=prefix_fd,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        issues.append(
                            FsckIssue(
                                "error",
                                "malformed_object_path",
                                str(error),
                                f"objects/{kind_name}/{prefix_name}/{object_name}",
                            )
                        )
                        continue
                    if (
                        not stat.S_ISDIR(object_info.st_mode)
                        or not _DIGEST_RE.fullmatch(object_name)
                        or object_name[:2] != prefix_name
                    ):
                        issues.append(
                            FsckIssue(
                                "error",
                                "malformed_object_path",
                                f"Unexpected managed-object path: {object_name!r}.",
                                f"objects/{kind_name}/{prefix_name}/{object_name}",
                            )
                        )
                        continue
                    try:
                        references.append(reference_type(object_name))
                    except ValueError as error:
                        issues.append(
                            FsckIssue(
                                "error",
                                "malformed_object_ref",
                                str(error),
                                f"objects/{kind_name}/{prefix_name}/{object_name}",
                            )
                        )
            finally:
                os.close(prefix_fd)
    finally:
        os.close(kind_fd)
    return references


def _discover_generated_directories(
    store: LocalContentStore,
    namespace_name: str,
    *,
    expected_prefix: str,
    expected_length: int,
    malformed_code: str,
) -> Tuple[List[str], List[FsckIssue]]:
    valid = []
    issues = []
    try:
        namespace_fd = store._open_namespace_fd(namespace_name)
    except (RealmIntegrityError, OSError) as error:
        issues.append(
            FsckIssue(
                "error",
                "unsafe_generated_namespace",
                str(error),
                namespace_name,
            )
        )
        return valid, issues
    try:
        names = sorted(os.listdir(namespace_fd))
        for name in names:
            suffix = name[len(expected_prefix) :] if name.startswith(expected_prefix) else ""
            try:
                info = os.stat(name, dir_fd=namespace_fd, follow_symlinks=False)
            except OSError as error:
                issues.append(
                    FsckIssue(
                        "error",
                        malformed_code,
                        str(error),
                        f"{namespace_name}/{name}",
                    )
                )
                continue
            if (
                not stat.S_ISDIR(info.st_mode)
                or len(name) != expected_length
                or len(suffix) != 32
                or any(character not in "0123456789abcdef" for character in suffix)
            ):
                issues.append(
                    FsckIssue(
                        "error",
                        malformed_code,
                        f"Unexpected generated directory: {name!r}.",
                        f"{namespace_name}/{name}",
                    )
                )
                continue
            valid.append(name)
    finally:
        os.close(namespace_fd)
    return valid, issues


def _check_object_permissions(
    store: LocalContentStore,
    content_ref: LocalObjectRef,
    issues: List[FsckIssue],
) -> None:
    object_path = store._object_directory(content_ref)
    try:
        object_fd = store._open_live_object_fd(content_ref)
    except (RealmIntegrityError, OSError) as error:
        issues.append(
            FsckIssue(
                "error",
                "unreadable_object_permissions",
                str(error),
                _relative(store, object_path),
                str(content_ref),
            )
        )
        return
    try:
        mode = os.fstat(object_fd).st_mode
        if mode & 0o222:
            issues.append(
                FsckIssue(
                    "error",
                    "writable_object_directory",
                    "Managed immutable object directory is writable.",
                    _relative(store, object_path),
                    str(content_ref),
                )
            )
        expected_files = ["manifest.json"]
        if isinstance(content_ref, BlobRef):
            expected_files.append("data")
        for child_name in expected_files:
            try:
                file_info = os.stat(child_name, dir_fd=object_fd, follow_symlinks=False)
            except OSError as error:
                issues.append(
                    FsckIssue(
                        "error",
                        "unreadable_managed_file_mode",
                        str(error),
                        _relative(store, object_path / child_name),
                        str(content_ref),
                    )
                )
                continue
            if not stat.S_ISREG(file_info.st_mode):
                issues.append(
                    FsckIssue(
                        "error",
                        "unsafe_managed_file",
                        "Managed object member is not a real regular file.",
                        _relative(store, object_path / child_name),
                        str(content_ref),
                    )
                )
            elif file_info.st_mode & 0o222:
                issues.append(
                    FsckIssue(
                        "error",
                        "writable_managed_file",
                        "Managed immutable object member is writable.",
                        _relative(store, object_path / child_name),
                        str(content_ref),
                    )
                )
    finally:
        os.close(object_fd)


def _directory_entry(parent_fd: int, name: str, label: str) -> Optional[os.stat_result]:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise RealmIntegrityError(
            f"Managed {label} is a symlink, special node, or non-directory."
        )
    return info


def _verify_open_object_fd(content_ref: LocalObjectRef, object_fd: int):
    if isinstance(content_ref, BlobRef):
        return _verify_blob_fd(object_fd, content_ref)
    return _verify_tree_fd(object_fd, content_ref)


def _verify_at_fd(content_ref: LocalObjectRef, parent_fd: int, name: str):
    object_fd = _open_child_directory_nofollow(parent_fd, name, label=f"tombstone {content_ref}")
    try:
        return _verify_open_object_fd(content_ref, object_fd)
    finally:
        os.close(object_fd)


def _validate_deletion_token(token: str) -> None:
    if not isinstance(token, str) or not _DELETION_TOKEN_RE.fullmatch(token):
        raise ValueError("deletion_token must be generated by new_deletion_token().")


def _validate_staging_id(staging_id: str) -> None:
    if not isinstance(staging_id, str) or not _STAGING_ID_RE.fullmatch(staging_id):
        raise ValueError("staging_id must be a generated stage identifier.")


def _validate_cleanup_token(cleanup_token: str) -> None:
    if not isinstance(cleanup_token, str) or not _CLEANUP_TOKEN_RE.fullmatch(cleanup_token):
        raise ValueError("cleanup_token must be a generated cleanup identifier.")


def _relative(store: LocalContentStore, path: Path) -> str:
    try:
        return path.relative_to(store.root).as_posix()
    except ValueError:
        return str(path)


__all__ = [
    "AbandonedStagingCleanupDecision",
    "AbandonedStagingCleanupReceipt",
    "FsckIssue",
    "FsckReport",
    "ContentInventoryAdapter",
    "LocalGcBackend",
    "LocalAbandonedStagingBackend",
    "RegisteredContentEdge",
    "RegisteredContentFact",
    "RegisteredStagingAllocation",
    "RegisteredStagingCleanup",
    "RegisteredTombstone",
    "TombstoneReceipt",
    "fsck_local_store",
    "new_deletion_token",
]
