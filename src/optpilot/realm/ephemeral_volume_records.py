"""Durable records for managed ephemeral writable volumes.

Volumes are operational runtime storage, not content, evidence, or public
workspaces.  Consequently their local provider placement is represented only
by root and namespace records and is deliberately absent from portable
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    optional_text,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .filesystem_quota import FilesystemQuota
from .leases import LeaseRecord


JsonDict = Dict[str, Any]


class EphemeralVolumeState(str, Enum):
    ALLOCATING = "allocating"
    ACTIVE = "active"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class EphemeralVolumeRootRecord:
    volume_root_id: str
    backend_kind: str
    canonical_path: str
    marker_digest: str
    claim_nonce: str
    device_id: int
    inode: int
    state: str
    registered_by_principal_id: str
    created_at: float
    state_changed_at: Optional[float] = None

    def __post_init__(self) -> None:
        required_text(self.volume_root_id, "ephemeral volume root id")
        required_text(self.backend_kind, "ephemeral volume root backend kind", max_bytes=128)
        required_text(self.canonical_path, "ephemeral volume root canonical path", max_bytes=4096)
        lower_hex_digest(self.marker_digest, "ephemeral volume root marker digest")
        lower_hex_digest(self.claim_nonce, "ephemeral volume root claim nonce")
        nonnegative_int(self.device_id, "ephemeral volume root device id")
        positive_int(self.inode, "ephemeral volume root inode")
        if self.state not in {"active", "degraded", "disabled"}:
            raise ValueError("ephemeral volume root state is invalid.")
        required_text(
            self.registered_by_principal_id,
            "ephemeral volume root principal id",
        )
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "volume root created_at")
        )
        if self.state_changed_at is not None:
            object.__setattr__(
                self,
                "state_changed_at",
                finite_time(self.state_changed_at, "volume root state_changed_at"),
            )
        if self.state != "active" and self.state_changed_at is None:
            raise ValueError("non-active ephemeral volume root requires state_changed_at.")

    def to_dict(self) -> JsonDict:
        return {
            "volume_root_id": self.volume_root_id,
            "backend_kind": self.backend_kind,
            "canonical_path": self.canonical_path,
            "marker_digest": self.marker_digest,
            "claim_nonce": self.claim_nonce,
            "device_id": self.device_id,
            "inode": self.inode,
            "state": self.state,
            "registered_by_principal_id": self.registered_by_principal_id,
            "created_at": self.created_at,
            "state_changed_at": self.state_changed_at,
        }

    def portable_record(self) -> JsonDict:
        return {}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EphemeralVolumeRootRecord":
        try:
            return cls(
                volume_root_id=payload["volume_root_id"],
                backend_kind=payload["backend_kind"],
                canonical_path=payload["canonical_path"],
                marker_digest=payload["marker_digest"],
                claim_nonce=payload["claim_nonce"],
                device_id=payload["device_id"],
                inode=payload["inode"],
                state=payload["state"],
                registered_by_principal_id=payload["registered_by_principal_id"],
                created_at=payload["created_at"],
                state_changed_at=payload.get("state_changed_at"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted ephemeral volume root is malformed."
            ) from error


@dataclass(frozen=True)
class EphemeralVolumeRecord:
    """One local lifecycle record.

    ``volume_id`` is a Realm-local operational cleanup handle.  It identifies
    one physical lifecycle for fencing and reconciliation; it is not canonical
    runtime evidence and is therefore excluded from :meth:`portable_record`.
    """

    volume_id: str
    volume_root_id: str
    owner_id: str
    parent_lease_id: str
    usage_lease_id: str
    provider_kind: str
    quota: FilesystemQuota
    quota_enforcement: str
    claim_nonce: str
    relative_name: str
    state: EphemeralVolumeState
    wrapper_device_id: Optional[int]
    wrapper_inode: Optional[int]
    data_device_id: Optional[int]
    data_inode: Optional[int]
    cleanup_lease_id: Optional[str]
    cleanup_generation: int
    cleanup_token: Optional[str]
    quarantine_reason: Optional[str]
    created_at: float
    active_at: Optional[float]
    cleanup_pending_at: Optional[float]
    cleanup_started_at: Optional[float]
    cleaned_at: Optional[float]
    quarantined_at: Optional[float]
    updated_at: float

    def __post_init__(self) -> None:
        required_text(self.volume_id, "ephemeral volume id")
        required_text(self.volume_root_id, "ephemeral volume root id")
        required_text(self.owner_id, "ephemeral volume owner id")
        required_text(self.parent_lease_id, "ephemeral volume parent lease id")
        required_text(self.usage_lease_id, "ephemeral volume usage lease id")
        required_text(self.provider_kind, "ephemeral volume provider kind", max_bytes=128)
        if not isinstance(self.quota, FilesystemQuota):
            raise TypeError("ephemeral volume quota must be FilesystemQuota.")
        if self.quota_enforcement != "advisory":
            raise ValueError(
                "local ephemeral volumes support advisory quota enforcement only."
            )
        lower_hex_digest(self.claim_nonce, "ephemeral volume claim nonce")
        name = required_text(self.relative_name, "ephemeral volume relative name", max_bytes=255)
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("ephemeral volume relative name must be one safe component.")
        if not isinstance(self.state, EphemeralVolumeState):
            raise ValueError("ephemeral volume state is invalid.")
        for prefix in ("wrapper", "data"):
            device = getattr(self, f"{prefix}_device_id")
            inode = getattr(self, f"{prefix}_inode")
            if (device is None) != (inode is None):
                raise ValueError(f"ephemeral volume {prefix} identity must be complete.")
            if device is not None:
                nonnegative_int(device, f"ephemeral volume {prefix} device id")
                positive_int(inode, f"ephemeral volume {prefix} inode")
        if (self.wrapper_inode is None) != (self.data_inode is None):
            raise ValueError("ephemeral volume wrapper and data identities are atomic.")
        optional_text(self.cleanup_lease_id, "ephemeral volume cleanup lease id")
        nonnegative_int(self.cleanup_generation, "ephemeral volume cleanup generation")
        if self.cleanup_token is not None:
            lower_hex_digest(self.cleanup_token, "ephemeral volume cleanup token")
        optional_text(
            self.quarantine_reason,
            "ephemeral volume quarantine reason",
            max_bytes=4096,
        )
        for field in (
            "created_at",
            "active_at",
            "cleanup_pending_at",
            "cleanup_started_at",
            "cleaned_at",
            "quarantined_at",
            "updated_at",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self,
                    field,
                    finite_time(value, f"ephemeral volume {field}"),
                )
        if self.updated_at < self.created_at:
            raise ValueError("ephemeral volume updated_at precedes created_at.")
        has_identity = self.wrapper_inode is not None
        has_cleanup = (
            self.cleanup_lease_id is not None
            and self.cleanup_generation > 0
            and self.cleanup_token is not None
            and self.cleanup_pending_at is not None
            and self.cleanup_started_at is not None
        )
        if self.state is EphemeralVolumeState.ALLOCATING:
            valid = (
                not has_identity
                and self.cleanup_lease_id is None
                and self.cleanup_generation == 0
                and self.cleanup_token is None
                and self.quarantine_reason is None
                and self.active_at is None
                and self.cleanup_pending_at is None
                and self.cleanup_started_at is None
                and self.cleaned_at is None
                and self.quarantined_at is None
            )
        elif self.state is EphemeralVolumeState.ACTIVE:
            valid = (
                has_identity
                and self.cleanup_lease_id is None
                and self.cleanup_generation == 0
                and self.cleanup_token is None
                and self.quarantine_reason is None
                and self.active_at is not None
                and self.cleanup_pending_at is None
                and self.cleanup_started_at is None
                and self.cleaned_at is None
                and self.quarantined_at is None
            )
        elif self.state is EphemeralVolumeState.CLEANUP_PENDING:
            valid = (
                self.cleanup_lease_id is None
                and self.cleanup_generation == 0
                and self.cleanup_token is None
                and self.quarantine_reason is None
                and self.cleanup_pending_at is not None
                and self.cleanup_started_at is None
                and self.cleaned_at is None
                and self.quarantined_at is None
            )
        elif self.state is EphemeralVolumeState.CLEANING:
            valid = (
                has_cleanup
                and self.quarantine_reason is None
                and self.cleaned_at is None
                and self.quarantined_at is None
            )
        elif self.state is EphemeralVolumeState.CLEANED:
            valid = (
                has_cleanup
                and self.quarantine_reason is None
                and self.cleaned_at is not None
                and self.quarantined_at is None
            )
        else:
            valid = (
                self.quarantine_reason is not None
                and self.quarantined_at is not None
            )
        if not valid:
            raise ValueError(
                "ephemeral volume state and lifecycle facts are inconsistent."
            )

    def to_dict(self) -> JsonDict:
        return {
            "volume_id": self.volume_id,
            "volume_root_id": self.volume_root_id,
            "owner_id": self.owner_id,
            "parent_lease_id": self.parent_lease_id,
            "usage_lease_id": self.usage_lease_id,
            "provider_kind": self.provider_kind,
            "quota": self.quota.to_dict(),
            "quota_enforcement": self.quota_enforcement,
            "claim_nonce": self.claim_nonce,
            "relative_name": self.relative_name,
            "state": self.state.value,
            "wrapper_device_id": self.wrapper_device_id,
            "wrapper_inode": self.wrapper_inode,
            "data_device_id": self.data_device_id,
            "data_inode": self.data_inode,
            "cleanup_lease_id": self.cleanup_lease_id,
            "cleanup_generation": self.cleanup_generation,
            "cleanup_token": self.cleanup_token,
            "quarantine_reason": self.quarantine_reason,
            "created_at": self.created_at,
            "active_at": self.active_at,
            "cleanup_pending_at": self.cleanup_pending_at,
            "cleanup_started_at": self.cleanup_started_at,
            "cleaned_at": self.cleaned_at,
            "quarantined_at": self.quarantined_at,
            "updated_at": self.updated_at,
        }

    def portable_record(self) -> JsonDict:
        """Return stable policy facts, excluding this local cleanup handle."""

        return {
            "format": "optpilot.ephemeral-volume.v1",
            "policy": "ephemeral",
            "quota": self.quota.to_dict(),
            "quota_enforcement": self.quota_enforcement,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EphemeralVolumeRecord":
        try:
            return cls(
                volume_id=payload["volume_id"],
                volume_root_id=payload["volume_root_id"],
                owner_id=payload["owner_id"],
                parent_lease_id=payload["parent_lease_id"],
                usage_lease_id=payload["usage_lease_id"],
                provider_kind=payload["provider_kind"],
                quota=FilesystemQuota.from_dict(payload["quota"]),
                quota_enforcement=payload["quota_enforcement"],
                claim_nonce=payload["claim_nonce"],
                relative_name=payload["relative_name"],
                state=EphemeralVolumeState(payload["state"]),
                wrapper_device_id=payload.get("wrapper_device_id"),
                wrapper_inode=payload.get("wrapper_inode"),
                data_device_id=payload.get("data_device_id"),
                data_inode=payload.get("data_inode"),
                cleanup_lease_id=payload.get("cleanup_lease_id"),
                cleanup_generation=payload["cleanup_generation"],
                cleanup_token=payload.get("cleanup_token"),
                quarantine_reason=payload.get("quarantine_reason"),
                created_at=payload["created_at"],
                active_at=payload.get("active_at"),
                cleanup_pending_at=payload.get("cleanup_pending_at"),
                cleanup_started_at=payload.get("cleanup_started_at"),
                cleaned_at=payload.get("cleaned_at"),
                quarantined_at=payload.get("quarantined_at"),
                updated_at=payload["updated_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted ephemeral volume is malformed."
            ) from error


@dataclass(frozen=True)
class EphemeralVolumeReceipt:
    volume: EphemeralVolumeRecord
    usage_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {
            "volume": self.volume.to_dict(),
            "usage_lease": self.usage_lease.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EphemeralVolumeReceipt":
        try:
            return cls(
                EphemeralVolumeRecord.from_dict(payload["volume"]),
                LeaseRecord.from_dict(payload["usage_lease"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted ephemeral volume receipt is malformed."
            ) from error


@dataclass(frozen=True)
class EphemeralVolumeCleanupReceipt:
    volume: EphemeralVolumeRecord
    cleanup_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {
            "volume": self.volume.to_dict(),
            "cleanup_lease": self.cleanup_lease.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "EphemeralVolumeCleanupReceipt":
        try:
            return cls(
                EphemeralVolumeRecord.from_dict(payload["volume"]),
                LeaseRecord.from_dict(payload["cleanup_lease"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted ephemeral volume cleanup receipt is malformed."
            ) from error


__all__ = [
    "EphemeralVolumeCleanupReceipt",
    "EphemeralVolumeReceipt",
    "EphemeralVolumeRecord",
    "EphemeralVolumeRootRecord",
    "EphemeralVolumeState",
]
