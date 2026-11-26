"""Durable records for local immutable-content projection lifecycles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from ._validation import (
    finite_time,
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    optional_text,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .leases import LeaseRecord
from .refs import request_digest as _request_digest

JsonDict = Dict[str, Any]


class ProjectionRealizationState(str, Enum):
    CREATING = "creating"
    MATERIALIZING = "materializing"
    READY = "ready"
    CLOSING = "closing"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ProjectionRootRecord:
    projection_root_id: str
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
        required_text(self.projection_root_id, "projection root id")
        required_text(self.backend_kind, "projection root backend kind", max_bytes=128)
        required_text(self.canonical_path, "projection root canonical path", max_bytes=4096)
        lower_hex_digest(self.marker_digest, "projection root marker digest")
        lower_hex_digest(self.claim_nonce, "projection root claim nonce")
        nonnegative_int(self.device_id, "projection root device id")
        positive_int(self.inode, "projection root inode")
        if self.state not in {"active", "degraded", "disabled"}:
            raise ValueError("projection root state is invalid.")
        required_text(self.registered_by_principal_id, "projection root principal id")
        object.__setattr__(self, "created_at", finite_time(self.created_at, "root created_at"))
        if self.state_changed_at is not None:
            object.__setattr__(
                self,
                "state_changed_at",
                finite_time(self.state_changed_at, "root state_changed_at"),
            )
        if self.state != "active" and self.state_changed_at is None:
            raise ValueError("non-active projection root requires state_changed_at.")

    def to_dict(self) -> JsonDict:
        return {
            "projection_root_id": self.projection_root_id,
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
        """Return only provider identity; all anti-substitution facts stay local."""

        return {}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionRootRecord":
        try:
            return cls(
                projection_root_id=payload["projection_root_id"],
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
            raise RealmIntegrityError("Persisted projection root is malformed.") from error


@dataclass(frozen=True)
class ProjectionRealizationRecord:
    realization_id: str
    projection_root_id: str
    owner_id: str
    store_id: str
    spec: Mapping[str, Any]
    spec_digest: str
    availability_resolution: Mapping[str, Any]
    availability_resolution_digest: str
    request_digest: str
    provider_kind: str
    claim_nonce: str
    relative_name: str
    state: ProjectionRealizationState
    owner_lease_id: str
    owner_generation: int
    materialization_builder_lease_id: Optional[str]
    cleanup_builder_lease_id: Optional[str]
    wrapper_device_id: Optional[int]
    wrapper_inode: Optional[int]
    exposed_tree_device_id: Optional[int]
    exposed_tree_inode: Optional[int]
    plan_digest: Optional[str]
    copied_logical_bytes: Optional[int]
    copied_file_count: Optional[int]
    cleanup_token: Optional[str]
    quarantine_reason: Optional[str]
    created_at: float
    materialization_started_at: Optional[float]
    ready_at: Optional[float]
    closing_at: Optional[float]
    cleanup_started_at: Optional[float]
    cleaned_at: Optional[float]
    quarantined_at: Optional[float]
    updated_at: float

    def __post_init__(self) -> None:
        required_text(self.realization_id, "projection realization id")
        required_text(self.projection_root_id, "projection root id")
        required_text(self.owner_id, "projection owner id")
        required_text(self.store_id, "projection store id", max_bytes=128)
        object.__setattr__(self, "spec", freeze_json(self.spec, label="projection spec"))
        object.__setattr__(
            self,
            "availability_resolution",
            freeze_json(self.availability_resolution, label="projection availability resolution"),
        )
        lower_hex_digest(self.spec_digest, "projection spec digest")
        lower_hex_digest(
            self.availability_resolution_digest, "projection availability resolution digest"
        )
        lower_hex_digest(self.request_digest, "projection request digest")
        required_text(self.provider_kind, "projection provider kind", max_bytes=128)
        if _request_digest(thaw_json(self.spec)) != self.spec_digest:
            raise ValueError("projection spec digest does not match canonical spec.")
        if (
            _request_digest(thaw_json(self.availability_resolution))
            != self.availability_resolution_digest
        ):
            raise ValueError(
                "projection availability resolution digest does not match its value."
            )
        expected_request_digest = _request_digest(
            {
                "format": "optpilot.projection-request.v1",
                "spec_digest": self.spec_digest,
                "availability_resolution_digest": self.availability_resolution_digest,
                "provider_kind": self.provider_kind,
            }
        )
        if self.request_digest != expected_request_digest:
            raise ValueError("projection request digest does not match its semantic request.")
        lower_hex_digest(self.claim_nonce, "projection claim nonce")
        name = required_text(self.relative_name, "projection relative name", max_bytes=255)
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("projection relative name must be one safe path component.")
        if not isinstance(self.state, ProjectionRealizationState):
            raise ValueError("projection realization state is invalid.")
        required_text(self.owner_lease_id, "projection owner lease id")
        positive_int(self.owner_generation, "projection owner generation")
        optional_text(self.materialization_builder_lease_id, "materialization builder lease")
        optional_text(self.cleanup_builder_lease_id, "cleanup builder lease")
        for prefix in ("wrapper", "exposed_tree"):
            device = getattr(self, f"{prefix}_device_id")
            inode = getattr(self, f"{prefix}_inode")
            if (device is None) != (inode is None):
                raise ValueError(f"projection {prefix} identity must be recorded together.")
            if device is not None:
                nonnegative_int(device, f"projection {prefix} device id")
                positive_int(inode, f"projection {prefix} inode")
        if self.plan_digest is not None:
            lower_hex_digest(self.plan_digest, "projection plan digest")
        if self.copied_logical_bytes is not None:
            nonnegative_int(self.copied_logical_bytes, "projection copied logical bytes")
        if self.copied_file_count is not None:
            nonnegative_int(self.copied_file_count, "projection copied file count")
        if self.cleanup_token is not None:
            lower_hex_digest(self.cleanup_token, "projection cleanup token")
        optional_text(self.quarantine_reason, "projection quarantine reason", max_bytes=4096)
        for field in (
            "created_at", "materialization_started_at", "ready_at", "closing_at",
            "cleanup_started_at", "cleaned_at", "quarantined_at", "updated_at",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, finite_time(value, f"projection {field}"))
        if self.updated_at < self.created_at:
            raise ValueError("projection updated_at precedes created_at.")

    def to_dict(self) -> JsonDict:
        return {
            "realization_id": self.realization_id,
            "projection_root_id": self.projection_root_id,
            "owner_id": self.owner_id,
            "store_id": self.store_id,
            "spec": thaw_json(self.spec),
            "spec_digest": self.spec_digest,
            "availability_resolution": thaw_json(self.availability_resolution),
            "availability_resolution_digest": self.availability_resolution_digest,
            "request_digest": self.request_digest,
            "provider_kind": self.provider_kind,
            "claim_nonce": self.claim_nonce,
            "relative_name": self.relative_name,
            "state": self.state.value,
            "owner_lease_id": self.owner_lease_id,
            "owner_generation": self.owner_generation,
            "materialization_builder_lease_id": self.materialization_builder_lease_id,
            "cleanup_builder_lease_id": self.cleanup_builder_lease_id,
            "wrapper_device_id": self.wrapper_device_id,
            "wrapper_inode": self.wrapper_inode,
            "exposed_tree_device_id": self.exposed_tree_device_id,
            "exposed_tree_inode": self.exposed_tree_inode,
            "plan_digest": self.plan_digest,
            "copied_logical_bytes": self.copied_logical_bytes,
            "copied_file_count": self.copied_file_count,
            "cleanup_token": self.cleanup_token,
            "quarantine_reason": self.quarantine_reason,
            "created_at": self.created_at,
            "materialization_started_at": self.materialization_started_at,
            "ready_at": self.ready_at,
            "closing_at": self.closing_at,
            "cleanup_started_at": self.cleanup_started_at,
            "cleaned_at": self.cleaned_at,
            "quarantined_at": self.quarantined_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionRealizationRecord":
        try:
            fields = {
                key: payload.get(key)
                for key in (
                    "materialization_builder_lease_id", "cleanup_builder_lease_id",
                    "wrapper_device_id", "wrapper_inode", "exposed_tree_device_id",
                    "exposed_tree_inode", "plan_digest", "copied_logical_bytes",
                    "copied_file_count", "cleanup_token", "quarantine_reason",
                    "materialization_started_at", "ready_at", "closing_at",
                    "cleanup_started_at", "cleaned_at", "quarantined_at",
                )
            }
            return cls(
                realization_id=payload["realization_id"],
                projection_root_id=payload["projection_root_id"],
                owner_id=payload["owner_id"],
                store_id=payload["store_id"],
                spec=payload["spec"],
                spec_digest=payload["spec_digest"],
                availability_resolution=payload["availability_resolution"],
                availability_resolution_digest=payload["availability_resolution_digest"],
                request_digest=payload["request_digest"],
                provider_kind=payload["provider_kind"],
                claim_nonce=payload["claim_nonce"],
                relative_name=payload["relative_name"],
                state=ProjectionRealizationState(payload["state"]),
                owner_lease_id=payload["owner_lease_id"],
                owner_generation=payload["owner_generation"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                **fields,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("Persisted projection realization is malformed.") from error


@dataclass(frozen=True)
class ProjectionConsumerRecord:
    consumer_id: str
    realization_id: str
    lease_id: str
    consumer_kind: str
    metadata: Mapping[str, Any]
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.consumer_id, "projection consumer id")
        required_text(self.realization_id, "projection realization id")
        required_text(self.lease_id, "projection consumer lease id")
        required_text(self.consumer_kind, "projection consumer kind", max_bytes=128)
        object.__setattr__(self, "metadata", freeze_json(self.metadata, label="consumer metadata"))
        object.__setattr__(self, "created_at", finite_time(self.created_at, "consumer created_at"))

    def to_dict(self) -> JsonDict:
        return {
            "consumer_id": self.consumer_id,
            "realization_id": self.realization_id,
            "lease_id": self.lease_id,
            "consumer_kind": self.consumer_kind,
            "metadata": thaw_json(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionConsumerRecord":
        try:
            return cls(
                consumer_id=payload["consumer_id"], realization_id=payload["realization_id"],
                lease_id=payload["lease_id"], consumer_kind=payload["consumer_kind"],
                metadata=payload.get("metadata", {}), created_at=payload["created_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError("Persisted projection consumer is malformed.") from error


@dataclass(frozen=True)
class ProjectionCreateReceipt:
    realization: ProjectionRealizationRecord
    owner_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {"realization": self.realization.to_dict(), "owner_lease": self.owner_lease.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionCreateReceipt":
        return cls(ProjectionRealizationRecord.from_dict(payload["realization"]), LeaseRecord.from_dict(payload["owner_lease"]))


@dataclass(frozen=True)
class ProjectionClaimReceipt:
    realization: ProjectionRealizationRecord
    owner_lease: LeaseRecord
    builder_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {"realization": self.realization.to_dict(), "owner_lease": self.owner_lease.to_dict(), "builder_lease": self.builder_lease.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionClaimReceipt":
        return cls(ProjectionRealizationRecord.from_dict(payload["realization"]), LeaseRecord.from_dict(payload["owner_lease"]), LeaseRecord.from_dict(payload["builder_lease"]))


@dataclass(frozen=True)
class ProjectionConsumerReceipt:
    realization: ProjectionRealizationRecord
    consumer: ProjectionConsumerRecord
    consumer_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {
            "realization": self.realization.to_dict(),
            "consumer": self.consumer.to_dict(),
            "consumer_lease": self.consumer_lease.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionConsumerReceipt":
        return cls(
            ProjectionRealizationRecord.from_dict(payload["realization"]),
            ProjectionConsumerRecord.from_dict(payload["consumer"]),
            LeaseRecord.from_dict(payload["consumer_lease"]),
        )


@dataclass(frozen=True)
class ProjectionHeartbeatReceipt:
    realization: ProjectionRealizationRecord
    owner_lease: LeaseRecord
    child_lease: LeaseRecord

    def to_dict(self) -> JsonDict:
        return {"realization": self.realization.to_dict(), "owner_lease": self.owner_lease.to_dict(), "child_lease": self.child_lease.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionHeartbeatReceipt":
        return cls(ProjectionRealizationRecord.from_dict(payload["realization"]), LeaseRecord.from_dict(payload["owner_lease"]), LeaseRecord.from_dict(payload["child_lease"]))


__all__ = ["ProjectionClaimReceipt", "ProjectionConsumerReceipt", "ProjectionConsumerRecord", "ProjectionCreateReceipt", "ProjectionHeartbeatReceipt", "ProjectionRealizationRecord", "ProjectionRealizationState", "ProjectionRootRecord"]
