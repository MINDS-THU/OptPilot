"""Single internal facade for bound local capture and lifecycle reconciliation.

The retained runtime and Studio compose this boundary for package, workspace,
attempt, and interface content.  Domain code should continue to use it instead
of raw ledger adapters or physical store paths, preserving one canonical
metadata writer and one token-fenced mutation protocol.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .content import (
    LocalContentCapture,
    LocalContentStore,
    PublishedObject,
    TreeSealReceipt,
)
from .errors import RealmConflict, RealmExpired, RealmNotFound
from .gc import AbandonedStagingCleanupReceipt, LocalAbandonedStagingBackend
from .ledger import AbandonedStagingCleanupRecord, RealmLedger
from .leases import LeaseRecord
from .manifests import TreeManifest
from .owners import OwnerMembership, OwnerPermission, OwnerState
from .refs import SnapshotRef, request_digest
from .selections import SelectionRef


@dataclass(frozen=True)
class TreeCompositionSource:
    """One exact retained tree authorized for manifest-only composition."""

    owner_id: str
    owner_revision: int
    membership: OwnerMembership

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise ValueError("composition source owner_id is required.")
        if (
            isinstance(self.owner_revision, bool)
            or not isinstance(self.owner_revision, int)
            or self.owner_revision < 0
        ):
            raise ValueError("composition source owner_revision must be nonnegative.")
        if not isinstance(self.membership, OwnerMembership) or not isinstance(
            self.membership.content_ref, SnapshotRef
        ):
            raise TypeError("composition source membership must retain one tree.")


@dataclass(frozen=True)
class AbandonedStagingReconcileReceipt:
    """End-to-end result after physical cleanup and ledger completion agree."""

    cleanup: AbandonedStagingCleanupRecord
    physical: Optional[AbandonedStagingCleanupReceipt]

    @property
    def already_complete(self) -> bool:
        return self.physical is None


@dataclass(frozen=True)
class AbandonedStagingReconcileOutcome:
    """One isolated batch result; a poison item never hides later work."""

    staging_id: str
    receipt: Optional[AbandonedStagingReconcileReceipt] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        succeeded = self.receipt is not None
        failed = self.error_type is not None and self.error_message is not None
        if succeeded == failed:
            raise ValueError("reconcile outcome must contain exactly one success or failure")

    @property
    def ok(self) -> bool:
        return self.receipt is not None


class RealmContentService:
    """Internal owner/ref authority facade for one realm's local stores."""

    def __init__(
        self,
        ledger: RealmLedger,
        *,
        local_stores: Mapping[str, LocalContentStore],
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger")
        stores = dict(local_stores)
        for store_id, store in stores.items():
            if not isinstance(store, LocalContentStore):
                raise TypeError("local_stores must contain LocalContentStore values")
            if store_id != store.store_id:
                raise ValueError("local store mapping key differs from its registered store_id")
        self._ledger = ledger
        self._local_stores = stores

    def capture(
        self,
        *,
        actor_principal_id: str,
        change_id: str,
        store_id: str,
    ) -> LocalContentCapture:
        """Mint one exact change/store/root-bound local capture capability."""

        store = self._store(store_id)
        authority = self._ledger.content_capture_handle(
            actor_principal_id=actor_principal_id,
            change_id=change_id,
            store_id=store_id,
        )
        return store.capture(change_id=change_id, authority=authority)

    def verify_owner_tree_manifest(
        self,
        *,
        actor_principal_id: str,
        owner_id: str,
        expected_owner_revision: int,
        membership: OwnerMembership,
    ) -> TreeManifest:
        """Authorize and verify one exact retained owner tree and all children."""

        if (
            isinstance(expected_owner_revision, bool)
            or not isinstance(expected_owner_revision, int)
            or expected_owner_revision < 0
        ):
            raise ValueError("expected_owner_revision must be a nonnegative integer")
        if not isinstance(membership, OwnerMembership):
            raise TypeError("membership must be an OwnerMembership")
        if not isinstance(membership.content_ref, SnapshotRef):
            raise ValueError("membership must retain a tree snapshot")
        owner = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if (
            owner.state is not OwnerState.ACTIVE
            or owner.revision != expected_owner_revision
        ):
            raise RealmConflict("Owner revision changed.")
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if membership not in memberships:
            raise RealmNotFound("Entity not found.")
        self._ledger.resolve_content_closure(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            store_id=membership.store_id,
            root_ref=membership.content_ref,
            permission=OwnerPermission.DERIVE,
        )
        manifest = self._store(membership.store_id).verify_tree(
            membership.content_ref,
            verify_children=True,
        )
        current = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if (
            current.state is not OwnerState.ACTIVE
            or current.revision != expected_owner_revision
        ):
            raise RealmConflict("Owner revision changed.")
        current_memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if membership not in current_memberships:
            raise RealmNotFound("Entity not found.")
        return manifest

    def verify_selection_tree_manifest(
        self,
        *,
        actor_principal_id: str,
        selection: SelectionRef,
    ) -> tuple[OwnerMembership, TreeManifest]:
        """Resolve and verify one exact stable tree selection without copying it.

        Unlike owner-head verification, this supports an immutable historical
        workspace revision whose owner has since advanced.  Authority and the
        exact selected root are resolved both before and after physical
        manifest verification, so a concurrent retirement or retention change
        cannot silently retarget the result.
        """

        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        first = self._ledger.resolve_selection_for_read_projection(
            actor_principal_id=actor_principal_id,
            selection=selection,
        )
        if not first.eligibility.eligible or first.root is None:
            raise RealmNotFound("Entity not found.")
        membership = first.root
        if not isinstance(membership.content_ref, SnapshotRef):
            raise RealmConflict("Selection does not resolve to an immutable tree.")
        self._ledger.resolve_content_closure(
            actor_principal_id=actor_principal_id,
            owner_id=selection.source_owner_id,
            store_id=membership.store_id,
            root_ref=membership.content_ref,
            permission=OwnerPermission.DERIVE,
        )
        manifest = self._store(membership.store_id).verify_tree(
            membership.content_ref,
            verify_children=True,
        )
        current = self._ledger.resolve_selection_for_read_projection(
            actor_principal_id=actor_principal_id,
            selection=selection,
        )
        if (
            not current.eligibility.eligible
            or current.root != membership
            or current.source_current_owner_revision
            != first.source_current_owner_revision
        ):
            raise RealmConflict("Selection authority or retention changed.")
        return membership, manifest

    def compose_tree(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        change_id: str,
        store_id: str,
        sources: Sequence[TreeCompositionSource],
        manifest: TreeManifest,
        hold_membership: OwnerMembership | None = None,
        change_publications: Sequence[PublishedObject] = (),
        source_lease_ttl_seconds: float = 300,
    ) -> TreeSealReceipt:
        """Publish a tree manifest over exact authorized source blobs only.

        When ``hold_membership`` is supplied, the new root is attached to the
        same provisional owner change before source leases are released.  This
        closes the publication-to-retention gap for higher-level atomic
        assembly commands without exposing a second copy or capture path.

        ``change_publications`` permits a caller to combine retained source
        trees with blobs it captured through this exact owner change.  The
        ledger revalidates that every such blob is provisionally retained by
        ``change_id`` before registering the composed tree; passing an
        unrelated or forged publication therefore fails closed.
        """

        _validate_operation_id(operation_id)
        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("composition actor principal id is required.")
        if not isinstance(change_id, str) or not change_id:
            raise ValueError("composition change_id is required.")
        if not isinstance(manifest, TreeManifest):
            raise TypeError("composition manifest must be a TreeManifest.")
        values = tuple(sources)
        if not values or any(
            not isinstance(item, TreeCompositionSource) for item in values
        ):
            raise TypeError(
                "composition sources must contain TreeCompositionSource values."
            )
        if any(item.membership.store_id != store_id for item in values):
            raise ValueError("composition sources must use the target store.")
        change_publication_values = tuple(change_publications)
        if any(
            not isinstance(item, PublishedObject)
            for item in change_publication_values
        ):
            raise TypeError(
                "change_publications must contain PublishedObject values."
            )
        if any(
            item.store_id != store_id or item.kind != "blob"
            for item in change_publication_values
        ):
            raise ValueError(
                "change publications must be blobs in the target store."
            )
        if hold_membership is not None:
            if not isinstance(hold_membership, OwnerMembership):
                raise TypeError("hold_membership must be an OwnerMembership or None.")
            if (
                hold_membership.store_id != store_id
                or hold_membership.content_ref != manifest.snapshot_ref
            ):
                raise ValueError(
                    "hold_membership must retain the exact composed tree in the target store."
                )
        composition_request = {
            "change_id": change_id,
            "manifest_ref": str(manifest.snapshot_ref),
            "schema": "optpilot.tree-composition-request.v1",
            "sources": [
                {
                    "membership": item.membership.to_dict(),
                    "owner_id": item.owner_id,
                    "owner_revision": item.owner_revision,
                }
                for item in values
            ],
            "store_id": store_id,
        }
        composition_request_digest = self._ledger.bind_content_composition_request(
            operation_id=(
                "content-composition.bind/"
                + request_digest({"operation_id": operation_id})
            ),
            actor_principal_id=actor_principal_id,
            change_id=change_id,
            store_id=store_id,
            composition_request=composition_request,
        )
        capture_operation_id = f"{operation_id}/publish"
        ordinary_capture = self.capture(
            actor_principal_id=actor_principal_id,
            change_id=change_id,
            store_id=store_id,
        )
        completed = ordinary_capture.recover_completed_tree_manifest(
            operation_id=capture_operation_id
        )
        if completed is not None:
            if completed.manifest != manifest:
                raise RealmConflict(
                    "Completed composition differs from its bound request."
                )
            if hold_membership is not None:
                self._ledger.hold_owner_content(
                    operation_id=f"{operation_id}/hold",
                    actor_principal_id=actor_principal_id,
                    change_id=change_id,
                    memberships=(hold_membership,),
                )
            return completed
        holder_id = "tree-compose-" + composition_request_digest[:40]
        leases = []
        primary_error: BaseException | None = None
        try:
            source_manifests = []
            for index, source in enumerate(values):
                lease = self._acquire_composition_source_lease(
                    actor_principal_id=actor_principal_id,
                    source=source,
                    source_index=index,
                    composition_request_digest=composition_request_digest,
                    holder_id=holder_id,
                    ttl_seconds=source_lease_ttl_seconds,
                )
                leases.append(lease)
                source_manifests.append(
                    self.verify_owner_tree_manifest(
                        actor_principal_id=actor_principal_id,
                        owner_id=source.owner_id,
                        expected_owner_revision=source.owner_revision,
                        membership=source.membership,
                    )
                )
            authorized_files = {
                (entry.blob_ref, entry.size, entry.executable)
                for source_manifest in source_manifests
                for entry in source_manifest.entries
                if entry.kind == "file"
            }
            for item in change_publication_values:
                authorized_files.add(
                    (item.content_ref, item.logical_bytes, False)
                )
                authorized_files.add(
                    (item.content_ref, item.logical_bytes, True)
                )
            if any(
                (entry.blob_ref, entry.size, entry.executable)
                not in authorized_files
                for entry in manifest.entries
                if entry.kind == "file"
            ):
                raise RealmConflict(
                    "Composed tree references file content outside its authorized sources."
                )
            authority = self._ledger.content_composition_capture_handle(
                actor_principal_id=actor_principal_id,
                change_id=change_id,
                store_id=store_id,
                composition_request_digest=composition_request_digest,
                source_leases=tuple(leases),
            )
            capture = self._store(store_id).capture(
                change_id=change_id,
                authority=authority,
            )
            completed = capture.publish_composed_tree_manifest(
                manifest=manifest,
                composition_request_digest=composition_request_digest,
                operation_id=capture_operation_id,
            )
            if hold_membership is not None:
                self._ledger.hold_owner_content(
                    operation_id=f"{operation_id}/hold",
                    actor_principal_id=actor_principal_id,
                    change_id=change_id,
                    memberships=(hold_membership,),
                )
            return completed
        except BaseException as error:
            primary_error = error
            raise
        finally:
            for index, lease in reversed(tuple(enumerate(leases))):
                try:
                    self._ledger.release_lease(
                        operation_id=(
                            "content-composition.source.release/"
                            + request_digest(
                                {
                                    "fencing_token": lease.fencing_token,
                                    "lease_id": lease.lease_id,
                                }
                            )
                        ),
                        actor_principal_id=actor_principal_id,
                        lease_id=lease.lease_id,
                        holder_id=lease.holder_id,
                        fencing_token=lease.fencing_token,
                    )
                except (RealmConflict, RealmNotFound):
                    if primary_error is None:
                        raise

    def _acquire_composition_source_lease(
        self,
        *,
        actor_principal_id: str,
        source: TreeCompositionSource,
        source_index: int,
        composition_request_digest: str,
        holder_id: str,
        ttl_seconds: float,
    ) -> LeaseRecord:
        operation_stem = (
            "content-composition.source.acquire/"
            + request_digest(
                {
                    "composition_request_digest": composition_request_digest,
                    "source_index": source_index,
                }
            )
        )
        request = {
            "actor_principal_id": actor_principal_id,
            "composition_request_digest": composition_request_digest,
            "source_index": source_index,
            "holder_id": holder_id,
            "ttl_seconds": ttl_seconds,
        }
        lease = self._ledger.acquire_content_composition_source_lease(
            operation_id=operation_stem,
            **request,
        )
        try:
            return self._ledger.validate_lease(
                actor_principal_id=actor_principal_id,
                lease_id=lease.lease_id,
                holder_id=lease.holder_id,
                fencing_token=lease.fencing_token,
            )
        except (RealmConflict, RealmExpired):
            return self._ledger.acquire_content_composition_source_lease(
                operation_id=f"{operation_stem}/retry-{uuid.uuid4().hex}",
                **request,
            )

    def reconcile_abandoned_staging(
        self,
        *,
        operation_id: str,
        store_id: str,
        staging_id: str,
    ) -> AbandonedStagingReconcileReceipt:
        """Resume one abandoned/cleaning row through physical and ledger completion."""

        _validate_operation_id(operation_id)
        store = self._store(store_id)
        record = self._find_cleanup(store_id=store_id, staging_id=staging_id)
        if record.state == "cleaned":
            return AbandonedStagingReconcileReceipt(record, None)
        if record.state == "abandoned":
            try:
                record = self._ledger.claim_abandoned_staging_cleanup(
                    operation_id=_derived_operation_id(
                        operation_id,
                        phase="claim",
                        store_id=store_id,
                        staging_id=staging_id,
                    ),
                    store_id=store_id,
                    staging_id=staging_id,
                )
            except RealmConflict:
                # A competing reaper may have claimed or completed the exact
                # row.  Refresh durable state; never invent or replace a token.
                record = self._find_cleanup(store_id=store_id, staging_id=staging_id)
                if record.state == "cleaned":
                    return AbandonedStagingReconcileReceipt(record, None)
        if record.state != "cleaning" or record.cleanup_token is None:
            raise RealmConflict("Staging cleanup is not claimable or resumable.")

        backend = LocalAbandonedStagingBackend(store)
        cleanup_token = record.cleanup_token
        try:
            physical = backend.cleanup(
                staging_id=staging_id,
                cleanup_token=cleanup_token,
                validate=lambda: self._ledger.validate_abandoned_staging_cleanup(
                    store_id=store_id,
                    staging_id=staging_id,
                    cleanup_token=cleanup_token,
                ),
            )
        except RealmConflict:
            completed = self._find_cleanup(store_id=store_id, staging_id=staging_id)
            if completed.state == "cleaned" and completed.cleanup_token == cleanup_token:
                return AbandonedStagingReconcileReceipt(completed, None)
            raise
        try:
            completed = self._ledger.complete_abandoned_staging_cleanup(
                operation_id=_derived_operation_id(
                    operation_id,
                    phase="complete",
                    store_id=store_id,
                    staging_id=staging_id,
                    cleanup_token=cleanup_token,
                ),
                store_id=store_id,
                staging_id=staging_id,
                cleanup_token=cleanup_token,
            )
        except RealmConflict:
            completed = self._find_cleanup(store_id=store_id, staging_id=staging_id)
            if completed.state != "cleaned" or completed.cleanup_token != cleanup_token:
                raise
        return AbandonedStagingReconcileReceipt(completed, physical)

    def reconcile_all_abandoned_staging(
        self,
        *,
        operation_id: str,
        store_id: str,
    ) -> Tuple[AbandonedStagingReconcileOutcome, ...]:
        """Reconcile one stable snapshot of pending work; later work waits for the next pass."""

        _validate_operation_id(operation_id)
        self._store(store_id)
        pending = self._ledger.list_abandoned_staging_cleanups(
            store_id=store_id,
            states=("abandoned", "cleaning"),
        )
        outcomes = []
        for item in pending:
            try:
                receipt = self.reconcile_abandoned_staging(
                    operation_id=operation_id,
                    store_id=store_id,
                    staging_id=item.staging_id,
                )
            except Exception as error:
                outcomes.append(
                    AbandonedStagingReconcileOutcome(
                        staging_id=item.staging_id,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                )
            else:
                outcomes.append(
                    AbandonedStagingReconcileOutcome(
                        staging_id=item.staging_id,
                        receipt=receipt,
                    )
                )
        return tuple(outcomes)

    def _find_cleanup(
        self,
        *,
        store_id: str,
        staging_id: str,
    ) -> AbandonedStagingCleanupRecord:
        matches = tuple(
            item
            for item in self._ledger.list_abandoned_staging_cleanups(
                store_id=store_id,
                states=("abandoned", "cleaning", "cleaned"),
            )
            if item.staging_id == staging_id
        )
        if len(matches) != 1:
            raise RealmNotFound("Entity not found.")
        return matches[0]

    def _store(self, store_id: str) -> LocalContentStore:
        try:
            return self._local_stores[store_id]
        except (KeyError, TypeError) as error:
            raise RealmNotFound("Entity not found.") from error


def _validate_operation_id(operation_id: str) -> None:
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id.encode("utf-8")) > 512
    ):
        raise ValueError("operation_id must be a non-empty string of at most 512 bytes")


def _derived_operation_id(
    operation_id: str,
    *,
    phase: str,
    store_id: str,
    staging_id: str,
    cleanup_token: Optional[str] = None,
) -> str:
    digest = request_digest(
        {
            "operation_id": operation_id,
            "phase": phase,
            "store_id": store_id,
            "staging_id": staging_id,
            "cleanup_token": cleanup_token,
        }
    )
    return f"realm.content.reconcile/{phase}/{digest}"


__all__ = [
    "AbandonedStagingReconcileOutcome",
    "AbandonedStagingReconcileReceipt",
    "RealmContentService",
    "TreeCompositionSource",
]
