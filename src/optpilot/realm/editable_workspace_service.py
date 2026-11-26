"""Persistent provider-owned checkouts for Realm-managed editable workspaces.

The public abstraction in this module is an editable workspace checkout.  A
checkout is a durable local realization owned by the Realm provider; callers
do not choose a destination, copy content, or receive CAS/projection facts.
The first local provider uses a verified physical copy internally because it
is universally available, but that transport is deliberately absent from the
public receipts.

Checkout identity is kept outside the exposed editable tree in a private
wrapper marker.  The service reopens the exact wrapper and tree by no-follow
descriptor plus device/inode identity, and refuses to capture or delete a
replacement path.  Full commits capture the stable editable tree; focused
file commits capture only their selected file and reuse the exact retained
base manifest.  Both advance the workspace through the same recoverable
``RealmLedger.commit_workspace_revision`` protocol.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import prepare_private_directory
from .content import AllowedFileSource, AllowedTreeSource
from .errors import RealmConflict, RealmIntegrityError, RealmNotFound
from .ledger import RealmLedger
from .manifests import TreeEntry, TreeManifest, validate_portable_path
from .owners import OwnerMembership, OwnerPermission, OwnerState
from .projection import _remove_tree_contents
from .projection_service import RealmProjectionService
from .refs import BlobRef, SnapshotRef, canonical_json_bytes, request_digest
from .service import RealmContentService, TreeCompositionSource
from .workspace_assembly import (
    WorkspaceAssemblyLineage,
    WorkspaceAssemblyRequest,
    WorkspaceSelectionSeed,
    WorkspaceSeed,
    WorkspaceSeedSource,
    WorkspaceSourceAnchor,
    compile_workspace_assembly,
)
from .workspaces import (
    WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE,
    WORKSPACE_REVISION_ROLE,
    WorkspaceLineage,
    WorkspaceRecord,
    WorkspaceRetirementReceipt,
    WorkspaceState,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - the secure v1 provider is POSIX-only
    fcntl = None  # type: ignore[assignment]


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
_ROOT_MARKER_NAME = ".optpilot-editable-workspace-root"
_ROOT_LOCK_NAME = ".optpilot-editable-workspace.lock"
_CHECKOUT_MARKER_NAME = "claim.json"
_CHECKOUT_TREE_NAME = "root"
_ROOT_SCHEMA = "optpilot.editable-workspace-root.v1"
_CHECKOUT_SCHEMA = "optpilot.editable-workspace-checkout.v1"
_OPEN_RECEIPT_SCHEMA = "optpilot.editable-workspace-open.v1"
_COMMIT_RECEIPT_SCHEMA = "optpilot.editable-workspace-commit.v1"
_DELETE_RECEIPT_SCHEMA = "optpilot.editable-workspace-delete.v1"
_SUMMARY_SCHEMA = "optpilot.editable-workspace-summary.v1"
_RETIRE_RECEIPT_SCHEMA = "optpilot.editable-workspace-retire.v1"
_CREATE_RECEIPT_SCHEMA = "optpilot.editable-workspace-create.v1"
_MAX_MARKER_BYTES = 64 * 1024
_WORKSPACE_ASSEMBLY_FOLLOWER_WAIT_SECONDS = 30.0
_CHECKOUT_OBSERVATION_FIELDS = frozenset(
    {"wrapper_device_id", "wrapper_inode", "tree_device_id", "tree_inode"}
)


class EditableWorkspaceCommitStatus(str, Enum):
    """A commit either advanced the managed revision or changed nothing."""

    COMMITTED = "committed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class EditableWorkspaceOpenReceipt:
    """Public semantic receipt; physical transfer details are intentionally absent."""

    workspace_id: str
    workspace_revision: int
    checkout_id: str
    recovered: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _OPEN_RECEIPT_SCHEMA,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "checkout_id": self.checkout_id,
            "ownership": "realm-managed",
            "persistence": "reopenable",
            "recovered": self.recovered,
        }


@dataclass(frozen=True)
class EditableWorkspaceCreateReceipt:
    """Path-free result of creating one workspace from exact selections."""

    workspace_id: str
    workspace_revision: int
    outcome: str
    source_count: int
    assembly_digest: str
    recovered: bool

    def __post_init__(self) -> None:
        _required_text(self.workspace_id, "workspace id")
        _positive_int(self.workspace_revision, "workspace revision")
        if self.outcome not in {"adopt", "union"}:
            raise ValueError("workspace creation outcome is unsupported.")
        if (
            isinstance(self.source_count, bool)
            or not isinstance(self.source_count, int)
            or self.source_count <= 0
        ):
            raise ValueError("workspace creation source_count must be positive.")
        _lower_hex(self.assembly_digest, "workspace assembly digest", length=64)
        if not isinstance(self.recovered, bool):
            raise TypeError("workspace creation recovered flag must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _CREATE_RECEIPT_SCHEMA,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "outcome": self.outcome,
            "source_count": self.source_count,
            "assembly_digest": self.assembly_digest,
            "recovered": self.recovered,
            "ownership": "realm-managed",
            "persistence": "reopenable",
            "content_transport": "manifest-only",
        }


@dataclass(frozen=True)
class EditableWorkspaceCommitReceipt:
    """Public result of freezing one checkout and advancing its workspace."""

    workspace_id: str
    checkout_id: str
    status: EditableWorkspaceCommitStatus
    previous_revision: int
    current_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, EditableWorkspaceCommitStatus):
            raise TypeError("status must be an EditableWorkspaceCommitStatus.")
        if self.previous_revision <= 0 or self.current_revision <= 0:
            raise ValueError("workspace revisions must be positive.")
        expected = (
            self.previous_revision + 1
            if self.status is EditableWorkspaceCommitStatus.COMMITTED
            else self.previous_revision
        )
        if self.current_revision != expected:
            raise ValueError("commit status differs from its revision transition.")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _COMMIT_RECEIPT_SCHEMA,
            "workspace_id": self.workspace_id,
            "checkout_id": self.checkout_id,
            "status": self.status.value,
            "previous_revision": self.previous_revision,
            "current_revision": self.current_revision,
            "ownership": "realm-managed",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EditableWorkspaceCommitReceipt":
        expected = {
            "format",
            "workspace_id",
            "checkout_id",
            "status",
            "previous_revision",
            "current_revision",
            "ownership",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise RealmIntegrityError("Editable workspace commit receipt is malformed.")
        if (
            value["format"] != _COMMIT_RECEIPT_SCHEMA
            or value["ownership"] != "realm-managed"
        ):
            raise RealmIntegrityError(
                "Editable workspace commit receipt is unsupported."
            )
        try:
            return cls(
                workspace_id=_required_text(value["workspace_id"], "workspace id"),
                checkout_id=_required_text(value["checkout_id"], "checkout id"),
                status=EditableWorkspaceCommitStatus(value["status"]),
                previous_revision=_positive_int(
                    value["previous_revision"], "previous workspace revision"
                ),
                current_revision=_positive_int(
                    value["current_revision"], "current workspace revision"
                ),
            )
        except (TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Editable workspace commit receipt is malformed."
            ) from error


@dataclass(frozen=True)
class EditableWorkspaceDeleteReceipt:
    """Checkout cleanup result; the managed workspace remains durable."""

    workspace_id: str
    checkout_id: str
    checkout_removed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _DELETE_RECEIPT_SCHEMA,
            "workspace_id": self.workspace_id,
            "checkout_id": self.checkout_id,
            "checkout_removed": self.checkout_removed,
            "durable_workspace_retained": True,
        }


@dataclass(frozen=True)
class EditableWorkspaceSummary:
    """Path-free durable workspace metadata suitable for discovery."""

    workspace_id: str
    title: str
    workspace_revision: int
    metadata_revision: int
    state: WorkspaceState
    created_at: float
    updated_at: float

    @classmethod
    def from_record(cls, record: WorkspaceRecord) -> "EditableWorkspaceSummary":
        if not isinstance(record, WorkspaceRecord):
            raise TypeError("record must be a WorkspaceRecord.")
        return cls(
            workspace_id=record.workspace_id,
            title=record.title,
            workspace_revision=record.current_revision,
            metadata_revision=record.metadata_revision,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _SUMMARY_SCHEMA,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "workspace_revision": self.workspace_revision,
            "metadata_revision": self.metadata_revision,
            "state": self.state.value,
            "ownership": "realm-managed",
            "persistence": "reopenable",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class EditableWorkspaceRetireReceipt:
    """Semantic retirement result without source or provider path details."""

    workspace_id: str
    workspace_revision: int
    checkout_id: str
    released_memberships: int

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _RETIRE_RECEIPT_SCHEMA,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "checkout_id": self.checkout_id,
            "released_memberships": self.released_memberships,
            "ownership": "realm-managed",
            "workspace_retired": True,
            "checkout_absent": True,
            "source_content_unchanged": True,
        }


@dataclass(frozen=True)
class _EditableWorkspaceRootBinding:
    path: Path
    realm_id: str
    checkout_root_id: str
    claim_nonce: str
    device_id: int
    inode: int

    @property
    def marker(self) -> dict[str, object]:
        return {
            "format": _ROOT_SCHEMA,
            "realm_id": self.realm_id,
            "checkout_root_id": self.checkout_root_id,
            "claim_nonce": self.claim_nonce,
        }


@dataclass(frozen=True)
class _CheckoutIdentity:
    directory_name: str
    wrapper_device_id: int
    wrapper_inode: int
    tree_device_id: int
    tree_inode: int


@dataclass(frozen=True)
class EditableWorkspaceCheckout:
    """Persistent editable checkout handle with validation-on-use."""

    workspace_id: str
    workspace_revision: int
    checkout_id: str
    recovered: bool
    _service: "RealmEditableWorkspaceService" = field(repr=False, compare=False)
    _identity: _CheckoutIdentity = field(repr=False, compare=False)

    @property
    def root_path(self) -> Path:
        self.validate()
        return (
            self._service.checkout_root
            / self._identity.directory_name
            / _CHECKOUT_TREE_NAME
        )

    def validate(self) -> None:
        self._service._validate_checkout_handle(self)

    def portable_record(self) -> dict[str, object]:
        self.validate()
        return EditableWorkspaceOpenReceipt(
            workspace_id=self.workspace_id,
            workspace_revision=self.workspace_revision,
            checkout_id=self.checkout_id,
            recovered=self.recovered,
        ).to_dict()


class RealmEditableWorkspaceService:
    """Realize, reopen, commit, and clean persistent managed-workspace checkouts."""

    def __init__(
        self,
        ledger: RealmLedger,
        content_service: RealmContentService,
        projection_service: RealmProjectionService,
        *,
        actor_principal_id: str,
        checkout_root: Path,
    ) -> None:
        if os.name == "nt" or fcntl is None:  # pragma: no cover - POSIX-only v1
            raise NotImplementedError(
                "Managed editable workspace checkouts require POSIX descriptors."
            )
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if projection_service.ledger is not ledger:
            raise ValueError("projection_service belongs to another Realm ledger.")
        self._ledger = ledger
        self._content = content_service
        self._projection = projection_service
        self._actor_principal_id = _required_text(
            actor_principal_id, "editable workspace actor principal id"
        )
        self._root = _prepare_checkout_root(checkout_root, realm_id=ledger.realm_id)
        self._checkout_attachments: dict[str, tuple[str, int, int, int, int]] = {}
        self._checkout_attachment_lock = threading.RLock()

    @property
    def checkout_root(self) -> Path:
        """Provider-owned root; callers receive only child checkout paths."""

        return self._root.path

    def list_workspaces(
        self, *, include_retired: bool = False, limit: int = 200
    ) -> tuple[EditableWorkspaceSummary, ...]:
        """List authorized durable identities without realizing checkouts."""

        return tuple(
            EditableWorkspaceSummary.from_record(workspace)
            for workspace, _revision in self._ledger.list_workspaces(
                actor_principal_id=self._actor_principal_id,
                include_deleted=include_retired,
                limit=limit,
            )
        )

    def read_workspace(
        self, *, workspace_id: str, include_retired: bool = False
    ) -> EditableWorkspaceSummary:
        """Read one authorized durable workspace without opening its checkout."""

        workspace_id = _required_text(workspace_id, "workspace id")
        workspace, _revision = self._ledger.read_workspace(
            actor_principal_id=self._actor_principal_id,
            workspace_id=workspace_id,
            permission=OwnerPermission.METADATA_READ,
        )
        if workspace.state is not WorkspaceState.ACTIVE and not include_retired:
            raise RealmConflict("Workspace is not active.")
        return EditableWorkspaceSummary.from_record(workspace)

    def rename_workspace(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        expected_metadata_revision: int,
        title: str,
    ) -> EditableWorkspaceSummary:
        """Durably rename one managed workspace without touching its files."""

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id", max_bytes=512)
        expected_metadata_revision = _positive_int(
            expected_metadata_revision, "expected workspace metadata revision"
        )
        title = _required_text(title, "workspace title", max_bytes=512)
        if title != title.strip():
            raise ValueError("workspace title must not have surrounding whitespace.")
        self._ledger.rename_workspace(
            operation_id=operation_id,
            actor_principal_id=self._actor_principal_id,
            workspace_id=workspace_id,
            expected_metadata_revision=expected_metadata_revision,
            title=title,
        )
        # Re-read after replay so an older idempotent response cannot visually
        # overwrite a newer metadata revision in a concurrent client.
        return self.read_workspace(workspace_id=workspace_id)

    def create_workspace(
        self,
        *,
        operation_id: str,
        title: str,
        seed: WorkspaceSelectionSeed,
        ttl_seconds: float = 300,
    ) -> EditableWorkspaceCreateReceipt:
        """Create one durable workspace from one or more exact tree selections.

        This is the general creation operation behind both single-source Keep
        and multi-source authoring flows.  One distinct immutable source root
        is adopted directly.  Multiple roots are unioned by publishing only a
        new tree manifest over already authorized blobs; source bytes are never
        copied into an intermediate workspace.

        The semantic request is persisted before resolving source authority.
        Reusing ``operation_id`` therefore either recovers the exact completed
        workspace or rejects changed intent.
        """

        operation_id = _operation_id(operation_id)
        title = _required_text(title, "workspace title")
        if title != title.strip() or len(title.encode("utf-8")) > 512:
            raise ValueError("workspace title must be trimmed and at most 512 bytes.")
        if not isinstance(seed, WorkspaceSelectionSeed):
            raise TypeError("seed must be a WorkspaceSelectionSeed.")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive.")

        target_digest = request_digest(
            {
                "actor_principal_id": self._actor_principal_id,
                "operation_id": operation_id,
                "schema": "optpilot.workspace-assembly-target.v1",
            }
        )
        request = WorkspaceAssemblyRequest(
            operation_id=operation_id,
            actor_principal_id=self._actor_principal_id,
            workspace_id=f"workspace-assembly-{target_digest}",
            owner_id=f"workspace-assembly-owner-{target_digest}",
            title=title,
            seed=seed,
        )
        completed = self._ledger.bind_workspace_assembly_request(request=request)
        if completed is not None:
            return _workspace_create_receipt(
                request=request,
                lineage=completed.revision.lineage,
                workspace_revision=completed.workspace.current_revision,
                recovered=True,
            )

        resolved_sources: list[WorkspaceSeedSource] = []
        composition_sources: list[TreeCompositionSource] = []
        for requested_source in request.seed.sources:
            membership, manifest = self._content.verify_selection_tree_manifest(
                actor_principal_id=self._actor_principal_id,
                selection=requested_source.selection,
            )
            resolution = self._ledger.resolve_selection_for_read_projection(
                actor_principal_id=self._actor_principal_id,
                selection=requested_source.selection,
            )
            if (
                not resolution.eligibility.eligible
                or resolution.root is None
                or resolution.root != membership
            ):
                raise RealmConflict(
                    "Workspace source authority changed during assembly."
                )
            resolved_sources.append(
                WorkspaceSeedSource(
                    anchor=WorkspaceSourceAnchor.build(
                        selection=requested_source.selection,
                        store_id=membership.store_id,
                        focuses=requested_source.focuses,
                    ),
                    tree_manifest=manifest,
                )
            )
            composition_sources.append(
                TreeCompositionSource(
                    owner_id=requested_source.selection.source_owner_id,
                    owner_revision=resolution.source_current_owner_revision,
                    membership=membership,
                )
            )

        result = compile_workspace_assembly(
            request,
            WorkspaceSeed.build(resolved_sources),
        )
        completed = self._ledger.recover_workspace_assembly(request=request)
        if completed is not None:
            return _workspace_create_receipt(
                request=request,
                lineage=completed.revision.lineage,
                workspace_revision=completed.workspace.current_revision,
                recovered=True,
            )
        if result.outcome == "adopt":
            completed = self._ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=result,
            )
            return _workspace_create_receipt(
                request=request,
                lineage=completed.revision.lineage,
                workspace_revision=completed.workspace.current_revision,
                recovered=False,
            )

        sources = _deduplicate_composition_sources(composition_sources)
        while True:
            completed = self._ledger.recover_workspace_assembly(request=request)
            if completed is not None:
                return _workspace_create_receipt(
                    request=request,
                    lineage=completed.revision.lineage,
                    workspace_revision=completed.workspace.current_revision,
                    recovered=True,
                )
            nonce = uuid.uuid4().hex
            claim = self._ledger.begin_workspace_assembly_attempt(
                operation_id=f"workspace-assembly.attempt.begin/{nonce}",
                request=request,
                attempt_id=f"workspace-assembly-attempt-{nonce}",
                owner_id=f"workspace-assembly-attempt-owner-{nonce}",
                change_id=f"workspace-assembly-attempt-change-{nonce}",
                store_id=result.store_id,
                ttl_seconds=float(ttl_seconds),
            )
            if not claim.composer:
                delay_seconds = 0.01
                wait_deadline = (
                    time.monotonic() + _WORKSPACE_ASSEMBLY_FOLLOWER_WAIT_SECONDS
                )
                while True:
                    completed = self._ledger.recover_workspace_assembly(request=request)
                    if completed is not None:
                        return _workspace_create_receipt(
                            request=request,
                            lineage=completed.revision.lineage,
                            workspace_revision=completed.workspace.current_revision,
                            recovered=True,
                        )
                    state = self._ledger.read_workspace_assembly_attempt_state(
                        request=request,
                        attempt_id=claim.attempt_id,
                    )
                    if state != "active":
                        break
                    remaining_seconds = wait_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise RealmConflict(
                            "Workspace assembly is still in progress; retry the "
                            "same operation."
                        )
                    time.sleep(min(delay_seconds, remaining_seconds))
                    delay_seconds = min(delay_seconds * 2, 0.25)
                continue

            composition_request = {
                "change_id": claim.change_id,
                "manifest_ref": str(result.root_ref),
                "schema": "optpilot.tree-composition-request.v1",
                "sources": [
                    {
                        "membership": source.membership.to_dict(),
                        "owner_id": source.owner_id,
                        "owner_revision": source.owner_revision,
                    }
                    for source in sources
                ],
                "store_id": result.store_id,
            }
            composition_request_digest = request_digest(composition_request)
            try:
                sealed = self._content.compose_tree(
                    operation_id=(
                        "workspace-assembly.attempt.compose/" + claim.attempt_id
                    ),
                    actor_principal_id=self._actor_principal_id,
                    change_id=claim.change_id,
                    store_id=result.store_id,
                    sources=sources,
                    manifest=result.tree_manifest,
                    hold_membership=OwnerMembership(
                        result.store_id,
                        result.root_ref,
                        WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE,
                    ),
                    source_lease_ttl_seconds=float(ttl_seconds),
                )
                if (
                    sealed.snapshot_ref != result.root_ref
                    or sealed.manifest != result.tree_manifest
                ):
                    raise RealmIntegrityError(
                        "Workspace composition published an unexpected tree."
                    )
                completed = self._ledger.finalize_workspace_assembly(
                    operation_id=operation_id,
                    request=request,
                    result=result,
                    attempt_id=claim.attempt_id,
                    composition_request_digest=composition_request_digest,
                )
            except BaseException:
                try:
                    self._ledger.abort_workspace_assembly_attempt(
                        operation_id=(
                            "workspace-assembly.attempt.abort/" + uuid.uuid4().hex
                        ),
                        request=request,
                        attempt_id=claim.attempt_id,
                    )
                except BaseException:
                    # Preserve the creation failure.  A retry or bounded reaper
                    # owns any surviving leased attempt.
                    pass
                raise
            return _workspace_create_receipt(
                request=request,
                lineage=completed.revision.lineage,
                workspace_revision=completed.workspace.current_revision,
                recovered=False,
            )

    def reap_abandoned_workspace_creations(
        self,
        *,
        operation_id: str,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Retire a bounded batch of this actor's expired union attempts."""

        return self._ledger.reap_workspace_assembly_attempts(
            operation_id=_operation_id(operation_id),
            actor_principal_id=self._actor_principal_id,
            limit=limit,
        )

    def open_workspace(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        expected_workspace_revision: int | None = None,
    ) -> EditableWorkspaceCheckout:
        """Open or recover the exact persistent checkout for a workspace head."""

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id")
        if expected_workspace_revision is not None:
            expected_workspace_revision = _positive_int(
                expected_workspace_revision, "expected workspace revision"
            )
        with _locked_root(self._root) as root_fd:
            workspace, revision = self._read_active_workspace(workspace_id)
            if (
                expected_workspace_revision is not None
                and workspace.current_revision != expected_workspace_revision
            ):
                raise RealmConflict("Workspace revision changed.")

            marker = _load_optional_checkout_marker(
                root_fd,
                binding=self._root,
                workspace_id=workspace_id,
            )
            recovered = marker is not None
            if marker is not None and marker["state"] == "commit-pending":
                marker = self._recover_pending_commit(root_fd, marker)
                workspace, revision = self._read_active_workspace(workspace_id)
            if marker is not None:
                if marker["state"] == "materializing":
                    _clear_checkout_tree(root_fd, marker)
                elif marker["state"] != "ready":  # pragma: no cover - validated loader
                    raise RealmIntegrityError("Editable checkout state is unsupported.")
                if (
                    marker["workspace_revision"] != workspace.current_revision
                    or marker["owner_revision"] != revision.owner_revision
                    or marker["root_store_id"] != revision.root_store_id
                    or marker["root_ref"] != str(revision.root_ref)
                ):
                    raise RealmConflict(
                        "Persistent checkout is based on another workspace revision."
                    )
            else:
                marker = _create_checkout_namespace(
                    root_fd,
                    binding=self._root,
                    workspace_id=workspace_id,
                    workspace_revision=workspace.current_revision,
                    owner_revision=revision.owner_revision,
                    root_store_id=revision.root_store_id,
                    root_ref=revision.root_ref,
                )

            if marker["state"] == "materializing":
                marker = self._materialize_checkout(
                    root_fd,
                    marker,
                    operation_id=operation_id,
                    workspace_id=workspace_id,
                )
            return self._handle_from_marker(marker, recovered=recovered)

    def commit_workspace(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        expected_workspace_revision: int,
        ttl_seconds: float = 300,
    ) -> EditableWorkspaceCommitReceipt:
        """Freeze the exact checkout and optimistically advance its workspace."""

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id")
        expected_workspace_revision = _positive_int(
            expected_workspace_revision, "expected workspace revision"
        )
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive.")

        with _locked_root(self._root) as root_fd:
            marker = _require_checkout_marker(
                root_fd, binding=self._root, workspace_id=workspace_id
            )
            if marker["state"] == "commit-pending":
                marker = self._recover_pending_commit(root_fd, marker)
            self._remember_checkout_attachment(marker)
            if marker["state"] != "ready":
                raise RealmConflict("Editable workspace checkout is not ready.")
            replay = _marker_commit_replay(marker, operation_id)
            if replay is not None:
                return replay
            if marker["workspace_revision"] != expected_workspace_revision:
                raise RealmConflict("Checkout workspace revision changed.")

            workspace, revision = self._read_active_workspace(workspace_id)
            if (
                workspace.current_revision != expected_workspace_revision
                or marker["owner_revision"] != revision.owner_revision
                or marker["root_store_id"] != revision.root_store_id
                or marker["root_ref"] != str(revision.root_ref)
            ):
                raise RealmConflict("Workspace revision changed.")
            owner = self._ledger.read_owner(
                actor_principal_id=self._actor_principal_id,
                owner_id=workspace.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            if owner.state is not OwnerState.ACTIVE:
                raise RealmConflict("Workspace owner is not active.")

            phase = _phase_coordinate(
                operation_id,
                checkout_root_id=self._root.checkout_root_id,
                workspace_id=workspace_id,
            )
            change = self._ledger.begin_owner_change(
                operation_id=f"editable-workspace.commit/begin/{phase}",
                actor_principal_id=self._actor_principal_id,
                owner_id=workspace.owner_id,
                expected_owner_revision=owner.revision,
                ttl_seconds=float(ttl_seconds),
            )
            try:
                capture = self._content.capture(
                    actor_principal_id=self._actor_principal_id,
                    change_id=change.change_id,
                    store_id=revision.root_store_id,
                )
                _validate_checkout_namespace(root_fd, marker)
                sealed = capture.seal_tree(
                    source=AllowedTreeSource(
                        self._checkout_path(marker),
                    ),
                    operation_id=f"editable-workspace.commit/capture/{phase}",
                )
                _validate_checkout_namespace(root_fd, marker)
                if sealed.snapshot_ref == revision.root_ref:
                    self._ledger.abort_owner_change(
                        operation_id=f"editable-workspace.commit/no-op/{phase}",
                        actor_principal_id=self._actor_principal_id,
                        change_id=change.change_id,
                    )
                    result = EditableWorkspaceCommitReceipt(
                        workspace_id=workspace_id,
                        checkout_id=str(marker["checkout_id"]),
                        status=EditableWorkspaceCommitStatus.UNCHANGED,
                        previous_revision=expected_workspace_revision,
                        current_revision=expected_workspace_revision,
                    )
                    marker = _record_completed_commit(marker, operation_id, result)
                    _write_checkout_marker(root_fd, marker)
                    return result

                new_root = OwnerMembership(
                    revision.root_store_id,
                    sealed.snapshot_ref,
                    WORKSPACE_REVISION_ROLE,
                )
                previous_root = OwnerMembership(
                    revision.root_store_id,
                    revision.root_ref,
                    WORKSPACE_REVISION_ROLE,
                )
                self._ledger.hold_owner_content(
                    operation_id=f"editable-workspace.commit/hold/{phase}",
                    actor_principal_id=self._actor_principal_id,
                    change_id=change.change_id,
                    memberships=(new_root,),
                )
                lineage = WorkspaceLineage(
                    source_kind="owner-revision",
                    source_owner_id=workspace.owner_id,
                    source_id=workspace.owner_id,
                    source_revision=owner.revision,
                    source_store_id=revision.root_store_id,
                    source_ref=revision.root_ref,
                )
                finalize_operation_id = f"editable-workspace.commit/finalize/{phase}"
                marker = _record_pending_commit(
                    marker,
                    operation_id=operation_id,
                    finalize_operation_id=finalize_operation_id,
                    change_id=change.change_id,
                    expected_workspace_revision=expected_workspace_revision,
                    expected_owner_revision=owner.revision,
                    new_root=new_root,
                    previous_root=previous_root,
                    lineage=lineage,
                )
                _write_checkout_marker(root_fd, marker)
                return self._finish_pending_commit(root_fd, marker)
            except BaseException:
                # Once a pending marker is durable, recovery owns the exact
                # transaction.  Before that point, abort best-effort so a
                # rejected capture does not retain provisional publications.
                current = _load_optional_checkout_marker(
                    root_fd,
                    binding=self._root,
                    workspace_id=workspace_id,
                )
                if current is None or current["state"] != "commit-pending":
                    try:
                        self._ledger.abort_owner_change(
                            operation_id=f"editable-workspace.commit/abort/{phase}",
                            actor_principal_id=self._actor_principal_id,
                            change_id=change.change_id,
                        )
                    except BaseException:
                        pass
                raise

    def commit_workspace_file(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        expected_workspace_revision: int,
        relative_path: str,
        executable: bool = False,
        ttl_seconds: float = 300,
    ) -> EditableWorkspaceCommitReceipt:
        """Commit one checkout file without recapturing the unchanged tree.

        The selected file is captured through the same descriptor-rooted
        content boundary as a full commit.  Its blob is then combined with the
        exact retained workspace head by manifest-only composition, and the
        checkout marker plus workspace revision use the ordinary recoverable
        commit protocol.  Unselected checkout edits remain local and dirty.
        """

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id")
        expected_workspace_revision = _positive_int(
            expected_workspace_revision, "expected workspace revision"
        )
        relative_path = validate_portable_path(relative_path)
        if not isinstance(executable, bool):
            raise TypeError("executable must be a boolean.")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive.")

        with _locked_root(self._root) as root_fd:
            marker = _require_checkout_marker(
                root_fd, binding=self._root, workspace_id=workspace_id
            )
            if marker["state"] == "commit-pending":
                marker = self._recover_pending_commit(root_fd, marker)
            self._remember_checkout_attachment(marker)
            if marker["state"] != "ready":
                raise RealmConflict("Editable workspace checkout is not ready.")
            replay = _marker_commit_replay(marker, operation_id)
            if replay is not None:
                return replay
            if marker["workspace_revision"] != expected_workspace_revision:
                raise RealmConflict("Checkout workspace revision changed.")

            workspace, revision = self._read_active_workspace(workspace_id)
            if (
                workspace.current_revision != expected_workspace_revision
                or marker["owner_revision"] != revision.owner_revision
                or marker["root_store_id"] != revision.root_store_id
                or marker["root_ref"] != str(revision.root_ref)
            ):
                raise RealmConflict("Workspace revision changed.")
            owner = self._ledger.read_owner(
                actor_principal_id=self._actor_principal_id,
                owner_id=workspace.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            if owner.state is not OwnerState.ACTIVE:
                raise RealmConflict("Workspace owner is not active.")
            previous_root = OwnerMembership(
                revision.root_store_id,
                revision.root_ref,
                WORKSPACE_REVISION_ROLE,
            )
            previous_manifest = self._content.verify_owner_tree_manifest(
                actor_principal_id=self._actor_principal_id,
                owner_id=workspace.owner_id,
                expected_owner_revision=owner.revision,
                membership=previous_root,
            )

            phase = _phase_coordinate(
                operation_id,
                checkout_root_id=self._root.checkout_root_id,
                workspace_id=workspace_id,
            )
            change = self._ledger.begin_owner_change(
                operation_id=f"editable-workspace.file-commit/begin/{phase}",
                actor_principal_id=self._actor_principal_id,
                owner_id=workspace.owner_id,
                expected_owner_revision=owner.revision,
                ttl_seconds=float(ttl_seconds),
            )
            try:
                capture = self._content.capture(
                    actor_principal_id=self._actor_principal_id,
                    change_id=change.change_id,
                    store_id=revision.root_store_id,
                )
                _validate_checkout_namespace(root_fd, marker)
                blob = capture.seal_blob(
                    source=AllowedFileSource(
                        self._checkout_path(marker),
                        relative_path,
                    )
                )
                _validate_checkout_namespace(root_fd, marker)
                next_manifest = _manifest_replacing_file(
                    previous_manifest,
                    relative_path=relative_path,
                    blob_ref=blob.blob_ref,
                    size=blob.publication.logical_bytes,
                    executable=executable,
                )
                if next_manifest.snapshot_ref == revision.root_ref:
                    self._ledger.abort_owner_change(
                        operation_id=(
                            f"editable-workspace.file-commit/no-op/{phase}"
                        ),
                        actor_principal_id=self._actor_principal_id,
                        change_id=change.change_id,
                    )
                    result = EditableWorkspaceCommitReceipt(
                        workspace_id=workspace_id,
                        checkout_id=str(marker["checkout_id"]),
                        status=EditableWorkspaceCommitStatus.UNCHANGED,
                        previous_revision=expected_workspace_revision,
                        current_revision=expected_workspace_revision,
                    )
                    marker = _record_completed_commit(marker, operation_id, result)
                    _write_checkout_marker(root_fd, marker)
                    return result

                new_root = OwnerMembership(
                    revision.root_store_id,
                    next_manifest.snapshot_ref,
                    WORKSPACE_REVISION_ROLE,
                )
                sealed = self._content.compose_tree(
                    operation_id=(
                        f"editable-workspace.file-commit/compose/{phase}"
                    ),
                    actor_principal_id=self._actor_principal_id,
                    change_id=change.change_id,
                    store_id=revision.root_store_id,
                    sources=(
                        TreeCompositionSource(
                            owner_id=workspace.owner_id,
                            owner_revision=owner.revision,
                            membership=previous_root,
                        ),
                    ),
                    manifest=next_manifest,
                    hold_membership=new_root,
                    change_publications=(blob.publication,),
                    source_lease_ttl_seconds=float(ttl_seconds),
                )
                if sealed.snapshot_ref != next_manifest.snapshot_ref:
                    raise RealmIntegrityError(
                        "Editable file commit composed an unexpected tree."
                    )
                lineage = WorkspaceLineage(
                    source_kind="owner-revision",
                    source_owner_id=workspace.owner_id,
                    source_id=workspace.owner_id,
                    source_revision=owner.revision,
                    source_store_id=revision.root_store_id,
                    source_ref=revision.root_ref,
                )
                finalize_operation_id = (
                    f"editable-workspace.file-commit/finalize/{phase}"
                )
                marker = _record_pending_commit(
                    marker,
                    operation_id=operation_id,
                    finalize_operation_id=finalize_operation_id,
                    change_id=change.change_id,
                    expected_workspace_revision=expected_workspace_revision,
                    expected_owner_revision=owner.revision,
                    new_root=new_root,
                    previous_root=previous_root,
                    lineage=lineage,
                )
                _write_checkout_marker(root_fd, marker)
                return self._finish_pending_commit(root_fd, marker)
            except BaseException:
                current = _load_optional_checkout_marker(
                    root_fd,
                    binding=self._root,
                    workspace_id=workspace_id,
                )
                if current is None or current["state"] != "commit-pending":
                    try:
                        self._ledger.abort_owner_change(
                            operation_id=(
                                f"editable-workspace.file-commit/abort/{phase}"
                            ),
                            actor_principal_id=self._actor_principal_id,
                            change_id=change.change_id,
                        )
                    except BaseException:
                        pass
                raise

    def delete_checkout(
        self,
        *,
        operation_id: str,
        workspace_id: str,
    ) -> EditableWorkspaceDeleteReceipt:
        """Remove only the provider checkout; retained workspace content is untouched."""

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id")
        checkout_id, directory_name = _checkout_coordinate(self._root, workspace_id)
        with _locked_root(self._root) as root_fd:
            marker = _load_optional_checkout_marker(
                root_fd,
                binding=self._root,
                workspace_id=workspace_id,
            )
            if marker is None:
                return EditableWorkspaceDeleteReceipt(workspace_id, checkout_id, False)
            if (
                marker["checkout_id"] != checkout_id
                or marker["directory_name"] != directory_name
            ):
                raise RealmIntegrityError("Editable checkout coordinate changed.")
            self._remember_checkout_attachment(marker)
            _validate_checkout_namespace(root_fd, marker)
            pending = marker.get("pending_commit")
            if isinstance(pending, Mapping):
                phase = _phase_coordinate(
                    operation_id,
                    checkout_root_id=self._root.checkout_root_id,
                    workspace_id=workspace_id,
                )
                try:
                    self._ledger.abort_owner_change(
                        operation_id=f"editable-workspace.delete/abort/{phase}",
                        actor_principal_id=self._actor_principal_id,
                        change_id=str(pending["change_id"]),
                    )
                except RealmNotFound:
                    pass
            _delete_checkout_namespace(root_fd, marker)
            with self._checkout_attachment_lock:
                self._checkout_attachments.pop(checkout_id, None)
            return EditableWorkspaceDeleteReceipt(workspace_id, checkout_id, True)

    def retire_workspace(
        self,
        *,
        operation_id: str,
        workspace_id: str,
        expected_workspace_revision: int,
    ) -> EditableWorkspaceRetireReceipt:
        """Retire the managed identity and only its provider-owned checkout.

        Checkout cleanup runs first and is identity checked.  A concurrent
        optimistic conflict can therefore leave an active durable workspace
        without a checkout, which is safe and reopenable; it can never delete
        or modify the lineage source.
        """

        operation_id = _operation_id(operation_id)
        workspace_id = _required_text(workspace_id, "workspace id")
        expected_workspace_revision = _positive_int(
            expected_workspace_revision, "expected workspace revision"
        )
        workspace = self.read_workspace(workspace_id=workspace_id, include_retired=True)
        if workspace.workspace_revision != expected_workspace_revision:
            raise RealmConflict("Workspace revision changed.")
        checkout = self.delete_checkout(
            operation_id=f"{operation_id}/checkout",
            workspace_id=workspace_id,
        )
        retired: WorkspaceRetirementReceipt = self._ledger.retire_workspace(
            operation_id=f"{operation_id}/owner",
            actor_principal_id=self._actor_principal_id,
            workspace_id=workspace_id,
            expected_workspace_revision=expected_workspace_revision,
        )
        return EditableWorkspaceRetireReceipt(
            workspace_id=workspace_id,
            workspace_revision=retired.workspace.current_revision,
            checkout_id=checkout.checkout_id,
            released_memberships=len(retired.released_memberships),
        )

    def _read_active_workspace(self, workspace_id: str):
        workspace, revision = self._ledger.read_workspace(
            actor_principal_id=self._actor_principal_id,
            workspace_id=workspace_id,
            permission=OwnerPermission.DERIVE,
        )
        if workspace.state is not WorkspaceState.ACTIVE:
            raise RealmConflict("Workspace is not active.")
        return workspace, revision

    def _materialize_checkout(
        self,
        root_fd: int,
        marker: dict[str, Any],
        *,
        operation_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        _validate_checkout_namespace(root_fd, marker)
        wrapper_fd, tree_fd = _open_checkout_namespace(root_fd, marker)
        projection = None
        try:
            if os.listdir(tree_fd):
                raise RealmIntegrityError(
                    "Materializing editable checkout is unexpectedly non-empty."
                )
            coordinate = request_digest(
                {
                    "format": "optpilot.editable-workspace-materialization.v1",
                    "checkout_id": marker["checkout_id"],
                    "operation_id": operation_id,
                    "attempt": uuid.uuid4().hex,
                }
            )
            projection = self._projection.project_workspace_read_only(
                operation_id=f"editable-workspace.materialize/{coordinate}",
                actor_principal_id=self._actor_principal_id,
                workspace_id=workspace_id,
                holder_id=f"editable-workspace-materializer-{coordinate[:40]}",
                consumer_kind="editable-workspace-materializer",
                consumer_metadata={"checkout_id": marker["checkout_id"]},
            )
            source_path = projection.projection.root_path
            source_fd = _open_directory(source_path)
            try:
                _copy_tree(source_fd, tree_fd)
                os.fsync(tree_fd)
            finally:
                os.close(source_fd)
            projection.projection.validate()
            _validate_checkout_namespace(root_fd, marker)
            ready = dict(marker)
            ready["state"] = "ready"
            _write_checkout_marker(root_fd, ready)
            return ready
        finally:
            os.close(tree_fd)
            os.close(wrapper_fd)
            if projection is not None:
                projection.projection.close()

    def _recover_pending_commit(
        self, root_fd: int, marker: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            self._finish_pending_commit(root_fd, marker)
        except RealmConflict:
            pending = marker["pending_commit"]
            assert isinstance(pending, Mapping)
            phase = request_digest(
                {
                    "format": "optpilot.editable-workspace-recovery-abort.v1",
                    "checkout_id": marker["checkout_id"],
                    "change_id": pending["change_id"],
                }
            )
            try:
                self._ledger.abort_owner_change(
                    operation_id=f"editable-workspace.recovery/abort/{phase}",
                    actor_principal_id=self._actor_principal_id,
                    change_id=str(pending["change_id"]),
                )
            finally:
                ready = dict(marker)
                ready["state"] = "ready"
                ready["pending_commit"] = None
                _write_checkout_marker(root_fd, ready)
            raise
        return _require_checkout_marker(
            root_fd,
            binding=self._root,
            workspace_id=str(marker["workspace_id"]),
        )

    def _finish_pending_commit(
        self, root_fd: int, marker: dict[str, Any]
    ) -> EditableWorkspaceCommitReceipt:
        pending = marker.get("pending_commit")
        if marker["state"] != "commit-pending" or not isinstance(pending, Mapping):
            raise RealmIntegrityError("Editable checkout has no recoverable commit.")
        new_root = OwnerMembership(
            str(pending["new_store_id"]),
            SnapshotRef.parse(str(pending["new_root_ref"])),
            WORKSPACE_REVISION_ROLE,
        )
        previous_root = OwnerMembership(
            str(pending["previous_store_id"]),
            SnapshotRef.parse(str(pending["previous_root_ref"])),
            WORKSPACE_REVISION_ROLE,
        )
        lineage_value = pending["lineage"]
        if not isinstance(
            lineage_value, Mapping
        ):  # pragma: no cover - loader validates
            raise RealmIntegrityError("Pending editable commit lineage is malformed.")
        lineage = WorkspaceLineage.from_dict(lineage_value)
        committed = self._ledger.commit_workspace_revision(
            operation_id=str(pending["finalize_operation_id"]),
            actor_principal_id=self._actor_principal_id,
            workspace_id=str(marker["workspace_id"]),
            expected_workspace_revision=int(pending["expected_workspace_revision"]),
            change_id=str(pending["change_id"]),
            expected_owner_revision=int(pending["expected_owner_revision"]),
            root=new_root,
            previous_root=previous_root,
            lineage=lineage,
        )
        result = EditableWorkspaceCommitReceipt(
            workspace_id=str(marker["workspace_id"]),
            checkout_id=str(marker["checkout_id"]),
            status=EditableWorkspaceCommitStatus.COMMITTED,
            previous_revision=int(pending["expected_workspace_revision"]),
            current_revision=committed.workspace.current_revision,
        )
        ready = dict(marker)
        ready.update(
            {
                "state": "ready",
                "workspace_revision": committed.workspace.current_revision,
                "owner_revision": committed.revision.owner_revision,
                "root_store_id": committed.revision.root_store_id,
                "root_ref": str(committed.revision.root_ref),
                "pending_commit": None,
            }
        )
        ready = _record_completed_commit(ready, str(pending["operation_id"]), result)
        _write_checkout_marker(root_fd, ready)
        return result

    def _checkout_path(self, marker: Mapping[str, Any]) -> Path:
        return self._root.path / str(marker["directory_name"]) / _CHECKOUT_TREE_NAME

    def _handle_from_marker(
        self, marker: Mapping[str, Any], *, recovered: bool
    ) -> EditableWorkspaceCheckout:
        self._remember_checkout_attachment(marker)
        return EditableWorkspaceCheckout(
            workspace_id=str(marker["workspace_id"]),
            workspace_revision=int(marker["workspace_revision"]),
            checkout_id=str(marker["checkout_id"]),
            recovered=recovered,
            _service=self,
            _identity=_CheckoutIdentity(
                directory_name=str(marker["directory_name"]),
                wrapper_device_id=int(marker["wrapper_device_id"]),
                wrapper_inode=int(marker["wrapper_inode"]),
                tree_device_id=int(marker["tree_device_id"]),
                tree_inode=int(marker["tree_inode"]),
            ),
        )

    def _validate_checkout_handle(self, handle: EditableWorkspaceCheckout) -> None:
        if handle._service is not self:
            raise RealmIntegrityError("Editable checkout belongs to another provider.")
        with _locked_root(self._root) as root_fd:
            marker = _require_checkout_marker(
                root_fd,
                binding=self._root,
                workspace_id=handle.workspace_id,
            )
            self._remember_checkout_attachment(marker)
            if (
                marker["checkout_id"] != handle.checkout_id
                or marker["workspace_revision"] != handle.workspace_revision
                or marker["directory_name"] != handle._identity.directory_name
                or marker["wrapper_device_id"] != handle._identity.wrapper_device_id
                or marker["wrapper_inode"] != handle._identity.wrapper_inode
                or marker["tree_device_id"] != handle._identity.tree_device_id
                or marker["tree_inode"] != handle._identity.tree_inode
                or marker["state"] not in {"ready", "commit-pending"}
            ):
                raise RealmIntegrityError("Editable checkout identity changed.")
            _validate_checkout_namespace(root_fd, marker)

    def _remember_checkout_attachment(self, marker: Mapping[str, Any]) -> None:
        checkout_id = str(marker["checkout_id"])
        durable_claim = {
            key: value
            for key, value in marker.items()
            if key
            in {
                "format",
                "checkout_root_id",
                "claim_nonce",
                "checkout_id",
                "directory_name",
                "workspace_id",
            }
        }
        attachment = (
            request_digest(durable_claim),
            int(marker["wrapper_device_id"]),
            int(marker["wrapper_inode"]),
            int(marker["tree_device_id"]),
            int(marker["tree_inode"]),
        )
        with self._checkout_attachment_lock:
            prior = self._checkout_attachments.get(checkout_id)
            if prior is not None and prior != attachment:
                raise RealmIntegrityError(
                    "Editable checkout directory was replaced while attached."
                )
            self._checkout_attachments[checkout_id] = attachment


def _deduplicate_composition_sources(
    sources: Sequence[TreeCompositionSource],
) -> tuple[TreeCompositionSource, ...]:
    """Match the ledger's stable first-occurrence composition-source order."""

    result: list[TreeCompositionSource] = []
    seen: set[bytes] = set()
    for source in sources:
        if not isinstance(source, TreeCompositionSource):
            raise TypeError(
                "workspace composition sources must be TreeCompositionSource values."
            )
        key = canonical_json_bytes(
            {
                "membership": source.membership.to_dict(),
                "owner_id": source.owner_id,
                "owner_revision": source.owner_revision,
            }
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    if not result:
        raise ValueError("workspace composition requires at least one source.")
    return tuple(result)


def _workspace_create_receipt(
    *,
    request: WorkspaceAssemblyRequest,
    lineage: object,
    workspace_revision: int,
    recovered: bool,
) -> EditableWorkspaceCreateReceipt:
    if not isinstance(lineage, WorkspaceAssemblyLineage):
        raise RealmIntegrityError(
            "Created workspace does not contain assembly lineage."
        )
    if (
        lineage.workspace_id != request.workspace_id
        or lineage.owner_id != request.owner_id
        or lineage.request_digest != request.digest
    ):
        raise RealmIntegrityError(
            "Created workspace lineage differs from its semantic request."
        )
    return EditableWorkspaceCreateReceipt(
        workspace_id=request.workspace_id,
        workspace_revision=workspace_revision,
        outcome=lineage.outcome,
        source_count=len(lineage.sources),
        assembly_digest=lineage.assembly_digest,
        recovered=recovered,
    )


def _prepare_checkout_root(
    path: Path, *, realm_id: str
) -> _EditableWorkspaceRootBinding:
    if not isinstance(path, Path):
        raise TypeError("checkout_root must be a Path.")
    if not path.is_absolute():
        raise ValueError("checkout_root must be absolute.")
    root = prepare_private_directory(path)
    root_fd = _open_directory(root)
    try:
        with _root_lock(root_fd):
            marker = _read_optional_marker(root_fd, _ROOT_MARKER_NAME)
            if marker is None:
                marker = {
                    "format": _ROOT_SCHEMA,
                    "realm_id": realm_id,
                    "checkout_root_id": f"editable-root-{uuid.uuid4().hex}",
                    "claim_nonce": uuid.uuid4().hex + uuid.uuid4().hex,
                }
                _publish_new_marker(root_fd, _ROOT_MARKER_NAME, marker)
            expected = {"format", "realm_id", "checkout_root_id", "claim_nonce"}
            if set(marker) != expected or marker["format"] != _ROOT_SCHEMA:
                raise RealmIntegrityError(
                    "Editable workspace root marker is malformed."
                )
            if marker["realm_id"] != realm_id:
                raise RealmIntegrityError(
                    "Editable workspace root belongs to another Realm."
                )
            _required_text(marker["checkout_root_id"], "editable checkout root id")
            _lower_hex(marker["claim_nonce"], "editable checkout root nonce", length=64)
        info = os.fstat(root_fd)
        return _EditableWorkspaceRootBinding(
            path=root,
            realm_id=realm_id,
            checkout_root_id=str(marker["checkout_root_id"]),
            claim_nonce=str(marker["claim_nonce"]),
            device_id=info.st_dev,
            inode=info.st_ino,
        )
    finally:
        os.close(root_fd)


@contextmanager
def _locked_root(binding: _EditableWorkspaceRootBinding) -> Iterator[int]:
    root_fd = _open_directory(binding.path)
    try:
        info = os.fstat(root_fd)
        if (info.st_dev, info.st_ino) != (binding.device_id, binding.inode):
            raise RealmIntegrityError("Editable workspace root identity changed.")
        with _root_lock(root_fd):
            marker = _read_marker(root_fd, _ROOT_MARKER_NAME)
            if marker != binding.marker:
                raise RealmIntegrityError("Editable workspace root marker changed.")
            yield root_fd
    finally:
        os.close(root_fd)


@contextmanager
def _root_lock(root_fd: int) -> Iterator[None]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        lock_fd = os.open(_ROOT_LOCK_NAME, flags, 0o600, dir_fd=root_fd)
    except OSError as error:
        raise RealmIntegrityError("Editable workspace root lock is unsafe.") from error
    try:
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise RealmIntegrityError("Editable workspace root lock is unsafe.")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        linked = os.stat(_ROOT_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
            raise RealmIntegrityError("Editable workspace root lock changed.")
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _checkout_coordinate(
    binding: _EditableWorkspaceRootBinding, workspace_id: str
) -> tuple[str, str]:
    digest = request_digest(
        {
            "format": "optpilot.editable-workspace-coordinate.v1",
            "realm_id": binding.realm_id,
            "checkout_root_id": binding.checkout_root_id,
            "workspace_id": workspace_id,
        }
    )
    checkout_id = f"editable-{digest[:40]}"
    return checkout_id, checkout_id


def _create_checkout_namespace(
    root_fd: int,
    *,
    binding: _EditableWorkspaceRootBinding,
    workspace_id: str,
    workspace_revision: int,
    owner_revision: int,
    root_store_id: str,
    root_ref: SnapshotRef,
) -> dict[str, Any]:
    checkout_id, directory_name = _checkout_coordinate(binding, workspace_id)
    wrapper_fd: int | None = None
    tree_fd: int | None = None
    created = False
    try:
        try:
            os.mkdir(directory_name, 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError as error:
            raise RealmIntegrityError(
                "Editable checkout exists without a valid private marker."
            ) from error
        wrapper_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        wrapper_info = os.fstat(wrapper_fd)
        _require_private_directory(wrapper_info, "editable checkout wrapper")
        linked_wrapper = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
        if (linked_wrapper.st_dev, linked_wrapper.st_ino) != (
            wrapper_info.st_dev,
            wrapper_info.st_ino,
        ):
            raise RealmIntegrityError("Editable checkout wrapper changed.")
        os.mkdir(_CHECKOUT_TREE_NAME, 0o700, dir_fd=wrapper_fd)
        tree_fd = os.open(_CHECKOUT_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        tree_info = os.fstat(tree_fd)
        _require_private_directory(tree_info, "editable checkout tree")
        marker: dict[str, Any] = {
            "format": _CHECKOUT_SCHEMA,
            "checkout_root_id": binding.checkout_root_id,
            "claim_nonce": binding.claim_nonce,
            "checkout_id": checkout_id,
            "directory_name": directory_name,
            "workspace_id": workspace_id,
            "wrapper_device_id": wrapper_info.st_dev,
            "wrapper_inode": wrapper_info.st_ino,
            "tree_device_id": tree_info.st_dev,
            "tree_inode": tree_info.st_ino,
            "state": "materializing",
            "workspace_revision": workspace_revision,
            "owner_revision": owner_revision,
            "root_store_id": root_store_id,
            "root_ref": str(root_ref),
            "pending_commit": None,
            "last_commit": None,
        }
        _publish_new_marker(wrapper_fd, _CHECKOUT_MARKER_NAME, marker)
        os.fsync(tree_fd)
        os.fsync(wrapper_fd)
        os.fsync(root_fd)
        return _validate_checkout_marker(
            marker, binding=binding, workspace_id=workspace_id
        )
    except BaseException:
        if created and wrapper_fd is not None:
            try:
                if tree_fd is not None:
                    _remove_tree_contents(
                        tree_fd, expected_device=os.fstat(tree_fd).st_dev
                    )
                    os.close(tree_fd)
                    tree_fd = None
                    os.rmdir(_CHECKOUT_TREE_NAME, dir_fd=wrapper_fd)
                try:
                    os.unlink(_CHECKOUT_MARKER_NAME, dir_fd=wrapper_fd)
                except FileNotFoundError:
                    pass
                os.close(wrapper_fd)
                wrapper_fd = None
                os.rmdir(directory_name, dir_fd=root_fd)
                os.fsync(root_fd)
            except BaseException:
                pass
        raise
    finally:
        if tree_fd is not None:
            os.close(tree_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)


def _load_optional_checkout_marker(
    root_fd: int,
    *,
    binding: _EditableWorkspaceRootBinding,
    workspace_id: str,
) -> dict[str, Any] | None:
    _, directory_name = _checkout_coordinate(binding, workspace_id)
    try:
        wrapper_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RealmIntegrityError("Editable checkout wrapper is unsafe.") from error
    try:
        marker = _read_marker(wrapper_fd, _CHECKOUT_MARKER_NAME)
    finally:
        os.close(wrapper_fd)
    validated = _validate_checkout_marker(
        marker, binding=binding, workspace_id=workspace_id
    )
    return _observe_checkout_namespace(
        root_fd,
        validated,
        binding=binding,
        workspace_id=workspace_id,
    )


def _observe_checkout_namespace(
    root_fd: int,
    marker: Mapping[str, Any],
    *,
    binding: _EditableWorkspaceRootBinding,
    workspace_id: str,
) -> dict[str, Any]:
    """Resolve one durable checkout claim to this attachment's descriptors."""

    wrapper_fd: int | None = None
    tree_fd: int | None = None
    try:
        wrapper_fd = os.open(
            str(marker["directory_name"]), _DIRECTORY_FLAGS, dir_fd=root_fd
        )
        wrapper_info = os.fstat(wrapper_fd)
        if wrapper_info.st_dev != os.fstat(root_fd).st_dev:
            raise RealmIntegrityError(
                "Editable checkout wrapper crossed a filesystem boundary."
            )
        _require_link(
            root_fd,
            str(marker["directory_name"]),
            wrapper_fd,
            (wrapper_info.st_dev, wrapper_info.st_ino),
            "editable checkout wrapper",
        )
        current_claim = _validate_checkout_marker(
            _read_marker(wrapper_fd, _CHECKOUT_MARKER_NAME),
            binding=binding,
            workspace_id=workspace_id,
        )
        if _checkout_marker_without_observations(
            current_claim
        ) != _checkout_marker_without_observations(marker):
            raise RealmIntegrityError("Editable checkout durable claim changed.")
        tree_fd = os.open(_CHECKOUT_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        tree_info = os.fstat(tree_fd)
        if tree_info.st_dev != wrapper_info.st_dev:
            raise RealmIntegrityError(
                "Editable checkout tree crossed a filesystem boundary."
            )
        _require_link(
            wrapper_fd,
            _CHECKOUT_TREE_NAME,
            tree_fd,
            (tree_info.st_dev, tree_info.st_ino),
            "editable checkout tree",
        )
        attached = dict(current_claim)
        attached.update(
            {
                "wrapper_device_id": wrapper_info.st_dev,
                "wrapper_inode": wrapper_info.st_ino,
                "tree_device_id": tree_info.st_dev,
                "tree_inode": tree_info.st_ino,
            }
        )
        return attached
    except OSError as error:
        raise RealmIntegrityError(
            "Editable checkout durable claim could not be observed safely."
        ) from error
    finally:
        if tree_fd is not None:
            os.close(tree_fd)
        if wrapper_fd is not None:
            os.close(wrapper_fd)


def _checkout_marker_without_observations(
    marker: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in marker.items()
        if key not in _CHECKOUT_OBSERVATION_FIELDS
    }


def _require_checkout_marker(
    root_fd: int,
    *,
    binding: _EditableWorkspaceRootBinding,
    workspace_id: str,
) -> dict[str, Any]:
    marker = _load_optional_checkout_marker(
        root_fd, binding=binding, workspace_id=workspace_id
    )
    if marker is None:
        raise RealmNotFound("Editable workspace checkout was not found.")
    return marker


def _validate_checkout_marker(
    value: Mapping[str, Any],
    *,
    binding: _EditableWorkspaceRootBinding,
    workspace_id: str,
) -> dict[str, Any]:
    fields = {
        "format",
        "checkout_root_id",
        "claim_nonce",
        "checkout_id",
        "directory_name",
        "workspace_id",
        "wrapper_device_id",
        "wrapper_inode",
        "tree_device_id",
        "tree_inode",
        "state",
        "workspace_revision",
        "owner_revision",
        "root_store_id",
        "root_ref",
        "pending_commit",
        "last_commit",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise RealmIntegrityError("Editable checkout marker is malformed.")
    checkout_id, directory_name = _checkout_coordinate(binding, workspace_id)
    try:
        if (
            value["format"] != _CHECKOUT_SCHEMA
            or value["checkout_root_id"] != binding.checkout_root_id
            or value["claim_nonce"] != binding.claim_nonce
            or value["checkout_id"] != checkout_id
            or value["directory_name"] != directory_name
            or value["workspace_id"] != workspace_id
        ):
            raise ValueError("checkout marker identity differs")
        for key in ("wrapper_device_id", "tree_device_id"):
            _nonnegative_int(value[key], key)
        for key in ("wrapper_inode", "tree_inode", "workspace_revision"):
            _positive_int(value[key], key)
        _nonnegative_int(value["owner_revision"], "workspace owner revision")
        _required_text(value["root_store_id"], "workspace root store id")
        SnapshotRef.parse(value["root_ref"])
        state = value["state"]
        if state not in {"materializing", "ready", "commit-pending"}:
            raise ValueError("checkout state is unsupported")
        pending = value["pending_commit"]
        if (state == "commit-pending") != isinstance(pending, Mapping):
            raise ValueError("checkout pending state differs from its intent")
        if pending is not None:
            _validate_pending_commit(pending, value)
        last = value["last_commit"]
        if last is not None:
            if not isinstance(last, Mapping) or set(last) != {"operation_id", "result"}:
                raise ValueError("last commit is malformed")
            _operation_id(last["operation_id"])
            result = EditableWorkspaceCommitReceipt.from_dict(last["result"])
            if result.workspace_id != workspace_id or result.checkout_id != checkout_id:
                raise ValueError("last commit belongs to another checkout")
        return dict(value)
    except RealmIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise RealmIntegrityError("Editable checkout marker is malformed.") from error


def _validate_pending_commit(
    pending: Mapping[str, Any], marker: Mapping[str, Any]
) -> None:
    fields = {
        "operation_id",
        "finalize_operation_id",
        "change_id",
        "expected_workspace_revision",
        "expected_owner_revision",
        "new_store_id",
        "new_root_ref",
        "previous_store_id",
        "previous_root_ref",
        "lineage",
    }
    if set(pending) != fields:
        raise ValueError("pending editable commit is malformed")
    _operation_id(pending["operation_id"])
    _operation_id(pending["finalize_operation_id"])
    _required_text(pending["change_id"], "pending change id")
    expected_revision = _positive_int(
        pending["expected_workspace_revision"], "pending workspace revision"
    )
    expected_owner = _nonnegative_int(
        pending["expected_owner_revision"], "pending owner revision"
    )
    if (
        expected_revision != marker["workspace_revision"]
        or expected_owner < marker["owner_revision"]
        or pending["previous_store_id"] != marker["root_store_id"]
        or pending["previous_root_ref"] != marker["root_ref"]
    ):
        raise ValueError("pending editable commit differs from its base")
    _required_text(pending["new_store_id"], "pending root store id")
    SnapshotRef.parse(pending["new_root_ref"])
    SnapshotRef.parse(pending["previous_root_ref"])
    if not isinstance(pending["lineage"], Mapping):
        raise ValueError("pending lineage must be an object")
    WorkspaceLineage.from_dict(pending["lineage"])


def _record_pending_commit(
    marker: Mapping[str, Any],
    *,
    operation_id: str,
    finalize_operation_id: str,
    change_id: str,
    expected_workspace_revision: int,
    expected_owner_revision: int,
    new_root: OwnerMembership,
    previous_root: OwnerMembership,
    lineage: WorkspaceLineage,
) -> dict[str, Any]:
    result = dict(marker)
    result["state"] = "commit-pending"
    result["pending_commit"] = {
        "operation_id": operation_id,
        "finalize_operation_id": finalize_operation_id,
        "change_id": change_id,
        "expected_workspace_revision": expected_workspace_revision,
        "expected_owner_revision": expected_owner_revision,
        "new_store_id": new_root.store_id,
        "new_root_ref": str(new_root.content_ref),
        "previous_store_id": previous_root.store_id,
        "previous_root_ref": str(previous_root.content_ref),
        "lineage": lineage.to_dict(),
    }
    return result


def _record_completed_commit(
    marker: Mapping[str, Any],
    operation_id: str,
    receipt: EditableWorkspaceCommitReceipt,
) -> dict[str, Any]:
    result = dict(marker)
    result["last_commit"] = {
        "operation_id": operation_id,
        "result": receipt.to_dict(),
    }
    return result


def _marker_commit_replay(
    marker: Mapping[str, Any], operation_id: str
) -> EditableWorkspaceCommitReceipt | None:
    last = marker.get("last_commit")
    if not isinstance(last, Mapping) or last.get("operation_id") != operation_id:
        return None
    result = last.get("result")
    if not isinstance(result, Mapping):
        raise RealmIntegrityError("Editable checkout commit replay is malformed.")
    return EditableWorkspaceCommitReceipt.from_dict(result)


def _open_checkout_namespace(
    root_fd: int, marker: Mapping[str, Any]
) -> tuple[int, int]:
    try:
        wrapper_fd = os.open(
            str(marker["directory_name"]), _DIRECTORY_FLAGS, dir_fd=root_fd
        )
    except OSError as error:
        raise RealmIntegrityError(
            "Editable checkout wrapper is unavailable."
        ) from error
    try:
        _require_link(
            root_fd,
            str(marker["directory_name"]),
            wrapper_fd,
            (int(marker["wrapper_device_id"]), int(marker["wrapper_inode"])),
            "editable checkout wrapper",
        )
        claim = _read_marker(wrapper_fd, _CHECKOUT_MARKER_NAME)
        if _checkout_marker_without_observations(
            claim
        ) != _checkout_marker_without_observations(marker):
            raise RealmIntegrityError("Editable checkout durable claim changed.")
        tree_fd = os.open(_CHECKOUT_TREE_NAME, _DIRECTORY_FLAGS, dir_fd=wrapper_fd)
        try:
            _require_link(
                wrapper_fd,
                _CHECKOUT_TREE_NAME,
                tree_fd,
                (int(marker["tree_device_id"]), int(marker["tree_inode"])),
                "editable checkout tree",
            )
            return wrapper_fd, tree_fd
        except BaseException:
            os.close(tree_fd)
            raise
    except BaseException:
        os.close(wrapper_fd)
        raise


def _validate_checkout_namespace(root_fd: int, marker: Mapping[str, Any]) -> None:
    wrapper_fd, tree_fd = _open_checkout_namespace(root_fd, marker)
    os.close(tree_fd)
    os.close(wrapper_fd)


def _clear_checkout_tree(root_fd: int, marker: Mapping[str, Any]) -> None:
    wrapper_fd, tree_fd = _open_checkout_namespace(root_fd, marker)
    try:
        _remove_tree_contents(tree_fd, expected_device=int(marker["tree_device_id"]))
        os.fsync(tree_fd)
        os.fsync(wrapper_fd)
    finally:
        os.close(tree_fd)
        os.close(wrapper_fd)


def _delete_checkout_namespace(root_fd: int, marker: Mapping[str, Any]) -> None:
    wrapper_fd, tree_fd = _open_checkout_namespace(root_fd, marker)
    try:
        _remove_tree_contents(tree_fd, expected_device=int(marker["tree_device_id"]))
        os.fsync(tree_fd)
        _require_link(
            wrapper_fd,
            _CHECKOUT_TREE_NAME,
            tree_fd,
            (int(marker["tree_device_id"]), int(marker["tree_inode"])),
            "editable checkout tree",
        )
        os.rmdir(_CHECKOUT_TREE_NAME, dir_fd=wrapper_fd)
        os.unlink(_CHECKOUT_MARKER_NAME, dir_fd=wrapper_fd)
        os.fsync(wrapper_fd)
        _require_link(
            root_fd,
            str(marker["directory_name"]),
            wrapper_fd,
            (int(marker["wrapper_device_id"]), int(marker["wrapper_inode"])),
            "editable checkout wrapper",
        )
        os.rmdir(str(marker["directory_name"]), dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(tree_fd)
        os.close(wrapper_fd)


def _copy_tree(source_fd: int, destination_fd: int) -> None:
    """Copy a provider projection into one fresh descriptor-rooted tree."""

    for name in sorted(os.listdir(source_fd), key=lambda item: item.encode("utf-8")):
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            os.mkdir(name, 0o700, dir_fd=destination_fd)
            source_child = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            destination_child = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination_fd)
            try:
                _require_same_identity(
                    before,
                    os.fstat(source_child),
                    "editable workspace source directory",
                )
                _copy_tree(source_child, destination_child)
                os.fsync(destination_child)
            finally:
                os.close(destination_child)
                os.close(source_child)
            after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            _require_same_identity(before, after, "editable workspace source directory")
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RealmIntegrityError(
                "Editable workspace source contains a non-regular entry."
            )
        source_file = os.open(name, _FILE_READ_FLAGS, dir_fd=source_fd)
        destination_file: int | None = None
        try:
            _require_same_identity(
                before, os.fstat(source_file), "editable workspace source file"
            )
            destination_file = os.open(
                name, _FILE_CREATE_FLAGS, 0o600, dir_fd=destination_fd
            )
            while True:
                chunk = os.read(source_file, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file, view)
                    if written <= 0:  # pragma: no cover - OS write contract
                        raise OSError("Editable workspace copy made no progress.")
                    view = view[written:]
            mode = 0o700 if before.st_mode & 0o111 else 0o600
            os.fchmod(destination_file, mode)
            os.fsync(destination_file)
            destination_info = os.fstat(destination_file)
            if (
                not stat.S_ISREG(destination_info.st_mode)
                or destination_info.st_nlink != 1
                or destination_info.st_size != before.st_size
            ):
                raise RealmIntegrityError(
                    "Editable workspace destination file is unsafe."
                )
            _require_same_identity(
                before, os.fstat(source_file), "editable workspace source file"
            )
        finally:
            if destination_file is not None:
                os.close(destination_file)
            os.close(source_file)
        after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        _require_same_identity(before, after, "editable workspace source file")


def _write_checkout_marker(root_fd: int, marker: Mapping[str, Any]) -> None:
    directory_name = str(marker["directory_name"])
    wrapper_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        _require_link(
            root_fd,
            directory_name,
            wrapper_fd,
            (int(marker["wrapper_device_id"]), int(marker["wrapper_inode"])),
            "editable checkout wrapper",
        )
        _replace_marker(wrapper_fd, _CHECKOUT_MARKER_NAME, marker)
        os.fsync(wrapper_fd)
    finally:
        os.close(wrapper_fd)


def _publish_new_marker(directory_fd: int, name: str, value: Mapping[str, Any]) -> None:
    temporary = f".{name}.new-{uuid.uuid4().hex}"
    _write_marker_file(directory_fd, temporary, value)
    try:
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise RealmConflict(
                "Private editable workspace marker already exists."
            ) from error
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass


def _replace_marker(directory_fd: int, name: str, value: Mapping[str, Any]) -> None:
    temporary = f".{name}.replace-{uuid.uuid4().hex}"
    _write_marker_file(directory_fd, temporary, value)
    try:
        os.rename(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_marker_file(directory_fd: int, name: str, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(dict(value))
    if len(payload) > _MAX_MARKER_BYTES:
        raise RealmIntegrityError("Editable workspace marker exceeds its size limit.")
    fd = os.open(name, _FILE_CREATE_FLAGS, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - OS write contract
                raise OSError("Editable workspace marker write made no progress.")
            view = view[written:]
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_optional_marker(directory_fd: int, name: str) -> dict[str, Any] | None:
    try:
        return _read_marker(directory_fd, name)
    except FileNotFoundError:
        return None


def _read_marker(directory_fd: int, name: str) -> dict[str, Any]:
    fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size <= 0
            or info.st_size > _MAX_MARKER_BYTES
        ):
            raise RealmIntegrityError("Editable workspace marker is unsafe.")
        payload = b""
        while len(payload) <= _MAX_MARKER_BYTES:
            chunk = os.read(fd, min(64 * 1024, _MAX_MARKER_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > _MAX_MARKER_BYTES:
            raise RealmIntegrityError("Editable workspace marker is too large.")
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError("Editable workspace marker is malformed.") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise RealmIntegrityError("Editable workspace marker is not canonical.")
    return value


def _open_directory(path: Path) -> int:
    try:
        fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RealmIntegrityError("Editable workspace directory is unsafe.") from error
    try:
        _require_private_directory(os.fstat(fd), "editable workspace directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _require_private_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise RealmIntegrityError(f"{label.capitalize()} is not a directory.")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise RealmIntegrityError(f"{label.capitalize()} is owned by another user.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RealmIntegrityError(f"{label.capitalize()} is not private.")


def _require_link(
    parent_fd: int,
    name: str,
    child_fd: int,
    expected: tuple[int, int],
    label: str,
) -> None:
    child = os.fstat(child_fd)
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _require_private_directory(child, label)
    if (
        not stat.S_ISDIR(linked.st_mode)
        or (child.st_dev, child.st_ino) != expected
        or (linked.st_dev, linked.st_ino) != expected
    ):
        raise RealmIntegrityError(f"{label.capitalize()} identity changed.")


def _require_same_identity(
    before: os.stat_result, after: os.stat_result, label: str
) -> None:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RealmIntegrityError(f"{label.capitalize()} changed during realization.")


def _phase_coordinate(
    operation_id: str, *, checkout_root_id: str, workspace_id: str
) -> str:
    return request_digest(
        {
            "format": "optpilot.editable-workspace-operation.v1",
            "operation_id": operation_id,
            "checkout_root_id": checkout_root_id,
            "workspace_id": workspace_id,
        }
    )


def _manifest_replacing_file(
    manifest: TreeManifest,
    *,
    relative_path: str,
    blob_ref: BlobRef,
    size: int,
    executable: bool,
) -> TreeManifest:
    """Return ``manifest`` with one exact regular-file entry replaced."""

    if not isinstance(manifest, TreeManifest):
        raise TypeError("manifest must be a TreeManifest.")
    relative_path = validate_portable_path(relative_path)
    if not isinstance(blob_ref, BlobRef):
        raise TypeError("blob_ref must be a BlobRef.")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("file size must be a nonnegative integer.")
    if not isinstance(executable, bool):
        raise TypeError("executable must be a boolean.")

    entries = {entry.path: entry for entry in manifest.entries}
    previous = entries.get(relative_path)
    if previous is not None and previous.kind != "file":
        raise RealmConflict("Workspace file path is an existing directory.")
    components = relative_path.split("/")
    for index in range(1, len(components)):
        ancestor = "/".join(components[:index])
        existing = entries.get(ancestor)
        if existing is not None and existing.kind != "directory":
            raise RealmConflict(
                "Workspace file path has a regular-file ancestor."
            )
        entries.setdefault(ancestor, TreeEntry.directory(ancestor))
    entries[relative_path] = TreeEntry.file(
        relative_path,
        blob_ref=blob_ref,
        size=size,
        executable=executable,
    )
    return TreeManifest.build(tuple(entries.values()))


def _operation_id(value: object) -> str:
    return _required_text(value, "operation id", max_bytes=512)


def _required_text(value: object, label: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string.")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8.") from error
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _lower_hex(value: object, label: str, *, length: int) -> str:
    text = _required_text(value, label, max_bytes=length)
    if len(text) != length or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters.")
    return text


__all__ = [
    "EditableWorkspaceCheckout",
    "EditableWorkspaceCreateReceipt",
    "EditableWorkspaceCommitReceipt",
    "EditableWorkspaceCommitStatus",
    "EditableWorkspaceDeleteReceipt",
    "EditableWorkspaceOpenReceipt",
    "RealmEditableWorkspaceService",
]
