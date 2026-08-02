"""Typed managed-workspace records for the internal realm authority.

Workspace identity and revision history are metadata, not filesystem paths.
The corresponding SQLite rows point only at an owner and an immutable tree
reference; a projection provider supplies any process-visible checkout later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, TypeAlias

from ._validation import (
    finite_time,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .owners import OwnerCommitReceipt, OwnerMembership
from .refs import SnapshotRef, canonical_json_bytes
from .selections import SelectionEligibility, SelectionRef
from .workspace_assembly import (
    WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA,
    WorkspaceAssemblyLineage,
)


JsonDict = Dict[str, Any]

WORKSPACE_LINEAGE_SCHEMA = "optpilot.workspace-lineage.v1"
WORKSPACE_SELECTION_LINEAGE_SCHEMA = "optpilot.workspace-selection-lineage.v1"
WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE = "workspace-assembly-root"
WORKSPACE_REVISION_ROLE = "workspace-revision"
_MAX_LINEAGE_BYTES = 64 * 1024


class WorkspaceState(str, Enum):
    """Durable lifecycle state of a managed editable workspace."""

    ACTIVE = "active"
    DELETED = "deleted"


@dataclass(frozen=True)
class WorkspaceLineage:
    """Exact revision anchor from which a workspace revision was derived.

    The first authority-backed schema deliberately accepts only
    ``source_kind="owner-revision"`` with ``source_id == source_owner_id``.
    The ledger validates that revision and retained content before committing
    a domain record.  Future selection kinds should be introduced only with an
    equally resolvable authority contract.  Stable domain selections use the
    separate :class:`WorkspaceSelectionLineage`; no host path participates in
    either lineage identity.
    """

    source_kind: str
    source_owner_id: str
    source_id: str
    source_revision: int
    source_store_id: str
    source_ref: SnapshotRef

    def __post_init__(self) -> None:
        required_text(self.source_kind, "workspace lineage source kind", max_bytes=128)
        required_text(self.source_owner_id, "workspace lineage source owner id")
        required_text(self.source_id, "workspace lineage source id")
        if self.source_kind != "owner-revision":
            raise ValueError(
                "workspace lineage source_kind must be 'owner-revision'."
            )
        if self.source_id != self.source_owner_id:
            raise ValueError(
                "workspace lineage source_id must equal source_owner_id."
            )
        nonnegative_int(self.source_revision, "workspace lineage source revision")
        required_text(self.source_store_id, "workspace lineage source store id", max_bytes=128)
        if not isinstance(self.source_ref, SnapshotRef):
            raise ValueError("workspace lineage source_ref must be a SnapshotRef.")

    def to_dict(self) -> JsonDict:
        return {
            "schema": WORKSPACE_LINEAGE_SCHEMA,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_owner_id": self.source_owner_id,
            "source_ref": str(self.source_ref),
            "source_revision": self.source_revision,
            "source_store_id": self.source_store_id,
        }

    def to_json(self) -> str:
        """Return the one canonical UTF-8 JSON representation stored in SQLite."""

        encoded = canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_LINEAGE_BYTES:
            # Field-level bounds make this unreachable under the current
            # schema.  Keep the aggregate guard so a future schema extension
            # cannot silently create unbounded operation requests or rows.
            raise ValueError("workspace lineage exceeds the maximum encoded size.")
        return encoded.decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceLineage":
        try:
            _require_exact_keys(
                payload,
                {
                    "schema",
                    "source_id",
                    "source_kind",
                    "source_owner_id",
                    "source_ref",
                    "source_revision",
                    "source_store_id",
                },
                "workspace lineage",
            )
            if payload["schema"] != WORKSPACE_LINEAGE_SCHEMA:
                raise ValueError("workspace lineage schema is unsupported.")
            result = cls(
                source_kind=payload["source_kind"],
                source_owner_id=payload["source_owner_id"],
                source_id=payload["source_id"],
                source_revision=payload["source_revision"],
                source_store_id=payload["source_store_id"],
                source_ref=SnapshotRef.parse(payload["source_ref"]),
            )
            # This also rejects mappings whose values cannot be encoded as the
            # canonical persisted form.
            result.to_json()
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted workspace lineage is invalid: {error}") from error

    @classmethod
    def from_json(cls, payload: str) -> "WorkspaceLineage":
        """Load lineage only if the stored bytes are already canonical JSON."""

        try:
            if not isinstance(payload, str) or not payload:
                raise ValueError("workspace lineage JSON must be a non-empty string.")
            encoded = payload.encode("utf-8", errors="strict")
            if len(encoded) > _MAX_LINEAGE_BYTES:
                raise ValueError("workspace lineage JSON exceeds the maximum encoded size.")
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise ValueError("workspace lineage JSON must contain an object.")
            result = cls.from_dict(decoded)
            if result.to_json() != payload:
                raise ValueError("workspace lineage JSON is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (UnicodeEncodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted workspace lineage is invalid: {error}") from error


@dataclass(frozen=True)
class WorkspaceSelectionLineage:
    """Exact stable selection from which an editable workspace was kept.

    Unlike :class:`WorkspaceLineage`, this anchor deliberately refers to a
    historical domain revision.  The ledger revalidates the selected entity at
    that revision and separately requires the selected tree to remain retained
    and verified at commit time.  Later unrelated source-owner revisions do not
    invalidate the selection.
    """

    selection: SelectionRef
    source_store_id: str
    source_ref: SnapshotRef

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise ValueError("workspace selection lineage requires a SelectionRef.")
        required_text(
            self.source_store_id,
            "workspace selection lineage source store id",
            max_bytes=128,
        )
        if not isinstance(self.source_ref, SnapshotRef):
            raise ValueError(
                "workspace selection lineage source_ref must be a SnapshotRef."
            )

    @property
    def source_kind(self) -> str:
        return "selection"

    @property
    def source_owner_id(self) -> str:
        return self.selection.source_owner_id

    @property
    def source_id(self) -> str:
        return self.selection.selection_digest

    @property
    def source_revision(self) -> int:
        return self.selection.owner_revision

    def to_dict(self) -> JsonDict:
        return {
            "schema": WORKSPACE_SELECTION_LINEAGE_SCHEMA,
            "selection": self.selection.to_dict(),
            "source_store_id": self.source_store_id,
            "source_ref": str(self.source_ref),
        }

    def to_json(self) -> str:
        encoded = canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_LINEAGE_BYTES:
            raise ValueError("workspace selection lineage exceeds the maximum encoded size.")
        return encoded.decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceSelectionLineage":
        try:
            _require_exact_keys(
                payload,
                {"schema", "selection", "source_store_id", "source_ref"},
                "workspace selection lineage",
            )
            if payload["schema"] != WORKSPACE_SELECTION_LINEAGE_SCHEMA:
                raise ValueError("workspace selection lineage schema is unsupported.")
            selection = payload["selection"]
            if not isinstance(selection, Mapping):
                raise TypeError("workspace selection lineage selection must be an object.")
            result = cls(
                selection=SelectionRef.from_dict(selection),
                source_store_id=payload["source_store_id"],
                source_ref=SnapshotRef.parse(payload["source_ref"]),
            )
            result.to_json()
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace selection lineage is invalid: {error}"
            ) from error

    @classmethod
    def from_json(cls, payload: str) -> "WorkspaceSelectionLineage":
        try:
            if not isinstance(payload, str) or not payload:
                raise ValueError(
                    "workspace selection lineage JSON must be a non-empty string."
                )
            encoded = payload.encode("utf-8", errors="strict")
            if len(encoded) > _MAX_LINEAGE_BYTES:
                raise ValueError(
                    "workspace selection lineage JSON exceeds the maximum encoded size."
                )
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise ValueError("workspace selection lineage JSON must contain an object.")
            result = cls.from_dict(decoded)
            if result.to_json() != payload:
                raise ValueError("workspace selection lineage JSON is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (UnicodeEncodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace selection lineage is invalid: {error}"
            ) from error


WorkspaceLineageLike: TypeAlias = (
    WorkspaceLineage | WorkspaceSelectionLineage | WorkspaceAssemblyLineage
)


def workspace_lineage_from_dict(payload: Mapping[str, Any]) -> WorkspaceLineageLike:
    schema = payload.get("schema") if isinstance(payload, Mapping) else None
    if schema == WORKSPACE_LINEAGE_SCHEMA:
        return WorkspaceLineage.from_dict(payload)
    if schema == WORKSPACE_SELECTION_LINEAGE_SCHEMA:
        return WorkspaceSelectionLineage.from_dict(payload)
    if schema == WORKSPACE_ASSEMBLY_LINEAGE_SCHEMA:
        try:
            return WorkspaceAssemblyLineage.from_dict(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace assembly lineage is invalid: {error}"
            ) from error
    raise RealmIntegrityError("Persisted workspace lineage schema is unsupported.")


def workspace_lineage_to_json(lineage: WorkspaceLineageLike) -> str:
    """Return the bounded canonical JSON stored for any typed lineage."""

    if not isinstance(
        lineage,
        (WorkspaceLineage, WorkspaceSelectionLineage, WorkspaceAssemblyLineage),
    ):
        raise TypeError("workspace lineage must use a supported typed schema.")
    encoded = canonical_json_bytes(lineage.to_dict())
    if len(encoded) > _MAX_LINEAGE_BYTES:
        raise ValueError("workspace lineage exceeds the maximum encoded size.")
    return encoded.decode("utf-8")


def workspace_lineage_from_json(payload: str) -> WorkspaceLineageLike:
    try:
        if not isinstance(payload, str) or not payload:
            raise ValueError("workspace lineage JSON must be a non-empty string.")
        encoded = payload.encode("utf-8", errors="strict")
        if len(encoded) > _MAX_LINEAGE_BYTES:
            raise ValueError("workspace lineage JSON exceeds the maximum encoded size.")
        decoded = json.loads(payload)
        if not isinstance(decoded, Mapping):
            raise ValueError("workspace lineage JSON must contain an object.")
        result = workspace_lineage_from_dict(decoded)
        if workspace_lineage_to_json(result) != payload:
            raise ValueError("workspace lineage JSON is not canonical.")
        return result
    except RealmIntegrityError:
        raise
    except (UnicodeEncodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RealmIntegrityError(f"Persisted workspace lineage is invalid: {error}") from error


@dataclass(frozen=True)
class WorkspaceRecord:
    """Current durable head of one managed editable workspace."""

    workspace_id: str
    owner_id: str
    title: str
    state: WorkspaceState
    current_revision: int
    created_txn_id: int
    created_at: float
    updated_at: float
    metadata_revision: int = 1

    def __post_init__(self) -> None:
        required_text(self.workspace_id, "workspace id")
        required_text(self.owner_id, "workspace owner id")
        required_text(self.title, "workspace title", max_bytes=512)
        if not isinstance(self.state, WorkspaceState):
            raise ValueError("workspace state must be a WorkspaceState.")
        positive_int(self.current_revision, "workspace current revision")
        positive_int(self.metadata_revision, "workspace metadata revision")
        positive_int(self.created_txn_id, "workspace created transaction id")
        created = finite_time(self.created_at, "workspace created_at")
        updated = finite_time(self.updated_at, "workspace updated_at")
        if updated < created:
            raise ValueError("workspace updated_at must not precede created_at.")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    def to_dict(self) -> JsonDict:
        return {
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "title": self.title,
            "state": self.state.value,
            "current_revision": self.current_revision,
            "created_txn_id": self.created_txn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata_revision": self.metadata_revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceRecord":
        try:
            _require_exact_keys(
                payload,
                {
                    "workspace_id",
                    "owner_id",
                    "title",
                    "state",
                    "current_revision",
                    "created_txn_id",
                    "created_at",
                    "updated_at",
                },
                "workspace record",
                optional={"metadata_revision", "receipt_version"},
            )
            if (
                "receipt_version" in payload
                and payload["receipt_version"] != 1
            ):
                raise ValueError("workspace record receipt version is unsupported.")
            return cls(
                workspace_id=payload["workspace_id"],
                owner_id=payload["owner_id"],
                title=payload["title"],
                state=WorkspaceState(payload["state"]),
                current_revision=payload["current_revision"],
                created_txn_id=payload["created_txn_id"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                metadata_revision=payload.get("metadata_revision", 1),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted workspace record is invalid: {error}") from error


@dataclass(frozen=True)
class WorkspaceRevision:
    """One immutable workspace root and its exact owner-revision anchor."""

    workspace_id: str
    revision: int
    owner_revision: int
    root_store_id: str
    root_ref: SnapshotRef
    lineage: WorkspaceLineageLike
    txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.workspace_id, "workspace revision workspace id")
        positive_int(self.revision, "workspace revision")
        nonnegative_int(self.owner_revision, "workspace revision owner revision")
        required_text(self.root_store_id, "workspace root store id", max_bytes=128)
        if not isinstance(self.root_ref, SnapshotRef):
            raise ValueError("workspace root_ref must be a SnapshotRef.")
        if not isinstance(
            self.lineage,
            (WorkspaceLineage, WorkspaceSelectionLineage, WorkspaceAssemblyLineage),
        ):
            raise ValueError("workspace lineage must be a supported typed lineage.")
        positive_int(self.txn_id, "workspace revision transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "workspace revision created_at"),
        )

    @property
    def lineage_json(self) -> str:
        return workspace_lineage_to_json(self.lineage)

    def to_dict(self) -> JsonDict:
        return {
            "workspace_id": self.workspace_id,
            "revision": self.revision,
            "owner_revision": self.owner_revision,
            "root_store_id": self.root_store_id,
            "root_ref": str(self.root_ref),
            "lineage": self.lineage.to_dict(),
            "txn_id": self.txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceRevision":
        try:
            _require_exact_keys(
                payload,
                {
                    "workspace_id",
                    "revision",
                    "owner_revision",
                    "root_store_id",
                    "root_ref",
                    "lineage",
                    "txn_id",
                    "created_at",
                },
                "workspace revision",
            )
            lineage = payload["lineage"]
            if not isinstance(lineage, Mapping):
                raise TypeError("workspace revision lineage must be an object.")
            return cls(
                workspace_id=payload["workspace_id"],
                revision=payload["revision"],
                owner_revision=payload["owner_revision"],
                root_store_id=payload["root_store_id"],
                root_ref=SnapshotRef.parse(payload["root_ref"]),
                lineage=workspace_lineage_from_dict(lineage),
                txn_id=payload["txn_id"],
                created_at=payload["created_at"],
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted workspace revision is invalid: {error}") from error


@dataclass(frozen=True)
class WorkspaceCommitReceipt:
    """Atomic result for owner membership plus one workspace revision."""

    owner_commit: OwnerCommitReceipt
    workspace: WorkspaceRecord
    revision: WorkspaceRevision

    def __post_init__(self) -> None:
        if not isinstance(self.owner_commit, OwnerCommitReceipt):
            raise ValueError("owner_commit must be an OwnerCommitReceipt.")
        if not isinstance(self.workspace, WorkspaceRecord):
            raise ValueError("workspace must be a WorkspaceRecord.")
        if not isinstance(self.revision, WorkspaceRevision):
            raise ValueError("revision must be a WorkspaceRevision.")
        if self.workspace.owner_id != self.owner_commit.owner_id:
            raise ValueError("workspace and owner commit refer to different owners.")
        if self.revision.workspace_id != self.workspace.workspace_id:
            raise ValueError("workspace record and revision have different workspace ids.")
        if self.revision.revision != self.workspace.current_revision:
            raise ValueError("workspace head does not point at the committed revision.")
        if self.revision.owner_revision != self.owner_commit.owner_revision:
            raise ValueError("workspace revision is not anchored to the committed owner revision.")

    @property
    def operation_id(self) -> str:
        return self.owner_commit.operation_id

    @property
    def change_id(self) -> str:
        return self.owner_commit.change_id

    def to_dict(self) -> JsonDict:
        return {
            "owner_commit": self.owner_commit.to_dict(),
            "workspace": self.workspace.to_dict(),
            "revision": self.revision.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceCommitReceipt":
        try:
            _require_exact_keys(
                payload,
                {"owner_commit", "workspace", "revision"},
                "workspace commit receipt",
                optional={"receipt_version"},
            )
            if "receipt_version" in payload and payload["receipt_version"] != 1:
                raise ValueError("workspace commit receipt version is unsupported.")
            for key in ("owner_commit", "workspace", "revision"):
                if not isinstance(payload[key], Mapping):
                    raise TypeError(f"workspace commit receipt {key} must be an object.")
            return cls(
                owner_commit=OwnerCommitReceipt.from_dict(payload["owner_commit"]),
                workspace=WorkspaceRecord.from_dict(payload["workspace"]),
                revision=WorkspaceRevision.from_dict(payload["revision"]),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace commit receipt is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class WorkspaceRetirementReceipt:
    """Atomic tombstone for one workspace and only its dedicated owner.

    Workspace revision history remains immutable.  Retirement advances the
    owner exactly once to release its active memberships and records the
    workspace tombstone; it never mutates the lineage source owner.
    """

    workspace: WorkspaceRecord
    previous_owner_revision: int
    owner_revision: int
    released_memberships: tuple[OwnerMembership, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, WorkspaceRecord):
            raise ValueError("workspace must be a WorkspaceRecord.")
        if self.workspace.state is not WorkspaceState.DELETED:
            raise ValueError("retired workspace must be deleted.")
        previous = nonnegative_int(
            self.previous_owner_revision, "previous owner revision"
        )
        current = nonnegative_int(self.owner_revision, "owner revision")
        if current != previous + 1:
            raise ValueError("workspace retirement must advance its owner once.")
        if not isinstance(self.released_memberships, tuple) or not all(
            isinstance(item, OwnerMembership) for item in self.released_memberships
        ):
            raise ValueError(
                "released_memberships must be a tuple of OwnerMembership values."
            )
        if len(set(self.released_memberships)) != len(self.released_memberships):
            raise ValueError("released workspace memberships must not repeat.")

    def to_dict(self) -> JsonDict:
        return {
            "workspace": self.workspace.to_dict(),
            "previous_owner_revision": self.previous_owner_revision,
            "owner_revision": self.owner_revision,
            "released_memberships": [
                item.to_dict() for item in self.released_memberships
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkspaceRetirementReceipt":
        try:
            _require_exact_keys(
                payload,
                {
                    "workspace",
                    "previous_owner_revision",
                    "owner_revision",
                    "released_memberships",
                },
                "workspace retirement receipt",
                optional={"receipt_version"},
            )
            if "receipt_version" in payload and payload["receipt_version"] != 1:
                raise ValueError("workspace retirement receipt version is unsupported.")
            workspace = payload["workspace"]
            memberships = payload["released_memberships"]
            if not isinstance(workspace, Mapping):
                raise TypeError("workspace retirement workspace must be an object.")
            if not isinstance(memberships, list):
                raise TypeError(
                    "workspace retirement memberships must be an array."
                )
            return cls(
                workspace=WorkspaceRecord.from_dict(workspace),
                previous_owner_revision=payload["previous_owner_revision"],
                owner_revision=payload["owner_revision"],
                released_memberships=tuple(
                    OwnerMembership.from_dict(item) for item in memberships
                ),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace retirement receipt is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class WorkspaceSelectionKeepReceipt:
    """Replayable result of one atomic selection-to-workspace operation."""

    selection: SelectionRef
    eligibility: SelectionEligibility
    workspace: WorkspaceCommitReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.workspace is not None):
            raise ValueError("selection keep workspace differs from its eligibility.")

    def to_dict(self) -> JsonDict:
        return {
            "selection": self.selection.to_dict(),
            "eligibility": self.eligibility.to_dict(),
            "workspace": (
                None if self.workspace is None else self.workspace.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "WorkspaceSelectionKeepReceipt":
        try:
            _require_exact_keys(
                payload,
                {"selection", "eligibility", "workspace"},
                "workspace selection keep receipt",
                optional={"receipt_version"},
            )
            if "receipt_version" in payload and payload["receipt_version"] != 1:
                raise ValueError(
                    "workspace selection keep receipt version is unsupported."
                )
            selection = payload["selection"]
            eligibility = payload["eligibility"]
            workspace = payload["workspace"]
            if not isinstance(selection, Mapping):
                raise TypeError("selection keep selection must be an object.")
            if not isinstance(eligibility, Mapping):
                raise TypeError("selection keep eligibility must be an object.")
            if workspace is not None and not isinstance(workspace, Mapping):
                raise TypeError("selection keep workspace must be an object or null.")
            return cls(
                selection=SelectionRef.from_dict(selection),
                eligibility=SelectionEligibility.from_dict(eligibility),
                workspace=(
                    None
                    if workspace is None
                    else WorkspaceCommitReceipt.from_dict(workspace)
                ),
            )
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted workspace selection keep receipt is invalid: {error}"
            ) from error


def _require_exact_keys(
    payload: Mapping[str, Any],
    required: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object.")
    optional = optional or set()
    actual = set(payload)
    missing = required - actual
    unexpected = actual - required - optional
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unexpected:
            details.append(f"unexpected={sorted(unexpected)}")
        raise ValueError(f"{label} has invalid fields ({', '.join(details)}).")


__all__ = [
    "WORKSPACE_LINEAGE_SCHEMA",
    "WORKSPACE_SELECTION_LINEAGE_SCHEMA",
    "WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE",
    "WORKSPACE_REVISION_ROLE",
    "WorkspaceCommitReceipt",
    "WorkspaceAssemblyLineage",
    "WorkspaceLineage",
    "WorkspaceLineageLike",
    "WorkspaceRecord",
    "WorkspaceRetirementReceipt",
    "WorkspaceRevision",
    "WorkspaceSelectionLineage",
    "WorkspaceSelectionKeepReceipt",
    "WorkspaceState",
    "workspace_lineage_from_dict",
    "workspace_lineage_from_json",
    "workspace_lineage_to_json",
]
