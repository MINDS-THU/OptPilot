"""Typed owner, ACL, and membership records for the internal realm authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Tuple

from ._validation import finite_time, lower_hex_digest, nonnegative_int, required_text
from .errors import RealmIntegrityError
from .leases import LeaseRecord, LeaseState
from .refs import BlobRef, PhysicalContentRef, SnapshotRef, parse_physical_content_ref


JsonDict = Dict[str, Any]


class OwnerState(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    DELETED = "deleted"


class OwnerPermission(str, Enum):
    METADATA_READ = "metadata_read"
    BYTES_READ = "bytes_read"
    DERIVE = "derive"
    ADMIN = "admin"


class OwnerChangeState(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EXPIRED = "expired"


@dataclass(frozen=True, order=True)
class OwnerMembership:
    """One exact owner-to-content membership in a store namespace."""

    store_id: str
    content_ref: PhysicalContentRef
    role: str

    def __post_init__(self) -> None:
        required_text(self.store_id, "store_id", max_bytes=128)
        required_text(self.role, "membership role", max_bytes=128)
        if not isinstance(self.content_ref, (BlobRef, SnapshotRef)):
            raise ValueError("content_ref must be a physical blob or tree reference.")

    def to_dict(self) -> JsonDict:
        return {
            "store_id": self.store_id,
            "content_ref": str(self.content_ref),
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerMembership":
        try:
            return cls(
                store_id=payload["store_id"],
                content_ref=parse_physical_content_ref(payload["content_ref"]),
                role=payload["role"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted owner membership is invalid: {error}") from error


def owner_membership_sort_key(
    membership: OwnerMembership,
) -> tuple[str, str, str]:
    """Return the canonical order for heterogeneous content memberships.

    ``PhysicalContentRef`` is a union of independently typed blob and tree
    references.  Dataclass ordering must therefore never be allowed to compare
    the reference objects themselves: Python intentionally does not define an
    order between those types.  Their canonical string encodings include the
    content kind and digest, providing one stable order for persistence and
    request normalization.
    """

    if not isinstance(membership, OwnerMembership):
        raise TypeError("membership must be an OwnerMembership.")
    return (
        membership.store_id,
        str(membership.content_ref),
        membership.role,
    )


@dataclass(frozen=True)
class OwnerRecord:
    owner_id: str
    owner_kind: str
    principal_id: str
    revision: int
    state: OwnerState
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        required_text(self.owner_id, "owner_id")
        required_text(self.owner_kind, "owner kind", max_bytes=128)
        required_text(self.principal_id, "principal_id")
        nonnegative_int(self.revision, "owner revision")
        if not isinstance(self.state, OwnerState):
            raise ValueError("owner state must be an OwnerState.")
        created = finite_time(self.created_at, "owner created_at")
        updated = finite_time(self.updated_at, "owner updated_at")
        if updated < created:
            raise ValueError("owner updated_at must not precede created_at.")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    def to_dict(self) -> JsonDict:
        return {
            "owner_id": self.owner_id,
            "owner_kind": self.owner_kind,
            "principal_id": self.principal_id,
            "revision": self.revision,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerRecord":
        try:
            return cls(
                owner_id=payload["owner_id"],
                owner_kind=payload["owner_kind"],
                principal_id=payload["principal_id"],
                revision=payload["revision"],
                state=OwnerState(payload["state"]),
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted owner record is invalid: {error}") from error


@dataclass(frozen=True)
class OwnerGrant:
    owner_id: str
    principal_id: str
    permission: OwnerPermission
    added_revision: int

    def __post_init__(self) -> None:
        required_text(self.owner_id, "owner_id")
        required_text(self.principal_id, "principal_id")
        if not isinstance(self.permission, OwnerPermission):
            raise ValueError("permission must be an OwnerPermission.")
        nonnegative_int(self.added_revision, "grant added_revision")

    def to_dict(self) -> JsonDict:
        return {
            "owner_id": self.owner_id,
            "principal_id": self.principal_id,
            "permission": self.permission.value,
            "added_revision": self.added_revision,
        }


@dataclass(frozen=True)
class OwnerChange:
    change_id: str
    owner_id: str
    base_owner_revision: int
    retention_lease_id: str
    expires_at: float
    state: OwnerChangeState

    def __post_init__(self) -> None:
        required_text(self.change_id, "change_id")
        required_text(self.owner_id, "owner_id")
        nonnegative_int(self.base_owner_revision, "base owner revision")
        required_text(self.retention_lease_id, "retention lease id")
        expires = finite_time(self.expires_at, "owner change expires_at")
        object.__setattr__(self, "expires_at", expires)
        if not isinstance(self.state, OwnerChangeState):
            raise ValueError("owner change state must be an OwnerChangeState.")

    def to_dict(self) -> JsonDict:
        return {
            "change_id": self.change_id,
            "owner_id": self.owner_id,
            "base_owner_revision": self.base_owner_revision,
            "retention_lease_id": self.retention_lease_id,
            "expires_at": self.expires_at,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerChange":
        try:
            return cls(
                change_id=payload["change_id"],
                owner_id=payload["owner_id"],
                base_owner_revision=payload["base_owner_revision"],
                retention_lease_id=payload["retention_lease_id"],
                expires_at=payload["expires_at"],
                state=OwnerChangeState(payload["state"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted owner change is invalid: {error}") from error


@dataclass(frozen=True)
class OwnerChangeHeartbeatReceipt:
    """One atomic renewal of an owner change and its retention lease."""

    change: OwnerChange
    retention_lease: LeaseRecord

    def __post_init__(self) -> None:
        if not isinstance(self.change, OwnerChange):
            raise TypeError("change must be an OwnerChange.")
        if not isinstance(self.retention_lease, LeaseRecord):
            raise TypeError("retention_lease must be a LeaseRecord.")
        if (
            self.change.state is not OwnerChangeState.ACTIVE
            or self.retention_lease.state is not LeaseState.ACTIVE
            or self.retention_lease.lease_id != self.change.retention_lease_id
            or self.retention_lease.owner_id != self.change.owner_id
            or self.retention_lease.lease_kind != "owner-change-retention"
            or self.retention_lease.audience != "realm-ledger"
            or self.retention_lease.scope_key
            != f"owner-change:{self.change.change_id}"
            or self.retention_lease.expires_at != self.change.expires_at
        ):
            raise ValueError(
                "Owner change heartbeat receipt anchors do not agree."
            )

    def to_dict(self) -> JsonDict:
        return {
            "change": self.change.to_dict(),
            "retention_lease": self.retention_lease.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "OwnerChangeHeartbeatReceipt":
        try:
            if set(payload) - {"change", "retention_lease", "receipt_version"}:
                raise ValueError("unexpected receipt fields")
            if payload.get("receipt_version", 1) != 1:
                raise ValueError("unsupported receipt version")
            return cls(
                change=OwnerChange.from_dict(payload["change"]),
                retention_lease=LeaseRecord.from_dict(
                    payload["retention_lease"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted owner change heartbeat receipt is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class OwnerCommitReceipt:
    operation_id: str
    change_id: str
    owner_id: str
    previous_revision: int
    owner_revision: int
    manifest_digest: str
    additions: Tuple[OwnerMembership, ...]
    removals: Tuple[OwnerMembership, ...]

    def __post_init__(self) -> None:
        required_text(self.operation_id, "operation_id")
        required_text(self.change_id, "change_id")
        required_text(self.owner_id, "owner_id")
        previous = nonnegative_int(self.previous_revision, "previous owner revision")
        current = nonnegative_int(self.owner_revision, "owner revision")
        if current not in {previous, previous + 1}:
            raise ValueError("owner revision must stay unchanged or advance exactly once.")
        lower_hex_digest(self.manifest_digest, "owner manifest digest")
        if not isinstance(self.additions, tuple) or not all(
            isinstance(item, OwnerMembership) for item in self.additions
        ):
            raise ValueError("additions must be a tuple of OwnerMembership values.")
        if not isinstance(self.removals, tuple) or not all(
            isinstance(item, OwnerMembership) for item in self.removals
        ):
            raise ValueError("removals must be a tuple of OwnerMembership values.")
        if len(set(self.additions)) != len(self.additions) or len(set(self.removals)) != len(self.removals):
            raise ValueError("owner receipt memberships must not contain duplicates.")
        if set(self.additions) & set(self.removals):
            raise ValueError("one owner membership cannot be added and removed together.")

    def to_dict(self) -> JsonDict:
        return {
            "operation_id": self.operation_id,
            "change_id": self.change_id,
            "owner_id": self.owner_id,
            "previous_revision": self.previous_revision,
            "owner_revision": self.owner_revision,
            "manifest_digest": self.manifest_digest,
            "additions": [item.to_dict() for item in self.additions],
            "removals": [item.to_dict() for item in self.removals],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OwnerCommitReceipt":
        try:
            return cls(
                operation_id=payload["operation_id"],
                change_id=payload["change_id"],
                owner_id=payload["owner_id"],
                previous_revision=payload["previous_revision"],
                owner_revision=payload["owner_revision"],
                manifest_digest=payload["manifest_digest"],
                additions=tuple(OwnerMembership.from_dict(item) for item in payload.get("additions", [])),
                removals=tuple(OwnerMembership.from_dict(item) for item in payload.get("removals", [])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted owner commit receipt is invalid: {error}") from error
