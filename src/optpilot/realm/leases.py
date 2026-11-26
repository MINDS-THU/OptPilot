"""Typed lease and fencing records for the internal realm authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from ._validation import (
    finite_time,
    freeze_json,
    nonnegative_int,
    optional_text,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError


JsonDict = Dict[str, Any]


class LeaseState(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    owner_id: str
    parent_lease_id: Optional[str]
    lease_kind: str
    audience: str
    holder_id: str
    scope_key: str
    fencing_token: int
    heartbeat_revision: int
    state: LeaseState
    expires_at: float
    created_at: float
    updated_at: float
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        required_text(self.lease_id, "lease_id")
        required_text(self.owner_id, "owner_id")
        optional_text(self.parent_lease_id, "parent lease id")
        required_text(self.lease_kind, "lease kind", max_bytes=128)
        required_text(self.audience, "lease audience", max_bytes=256)
        required_text(self.holder_id, "lease holder id")
        required_text(self.scope_key, "lease scope key")
        positive_int(self.fencing_token, "lease fencing token")
        nonnegative_int(self.heartbeat_revision, "lease heartbeat revision")
        if not isinstance(self.state, LeaseState):
            raise ValueError("lease state must be a LeaseState.")
        expires = finite_time(self.expires_at, "lease expires_at")
        created = finite_time(self.created_at, "lease created_at")
        updated = finite_time(self.updated_at, "lease updated_at")
        if updated < created:
            raise ValueError("lease updated_at must not precede created_at.")
        if expires < created:
            raise ValueError("lease expires_at must not precede created_at.")
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "metadata", freeze_json(self.metadata, label="lease metadata"))

    def to_dict(self) -> JsonDict:
        return {
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "parent_lease_id": self.parent_lease_id,
            "lease_kind": self.lease_kind,
            "audience": self.audience,
            "holder_id": self.holder_id,
            "scope_key": self.scope_key,
            "fencing_token": self.fencing_token,
            "heartbeat_revision": self.heartbeat_revision,
            "state": self.state.value,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LeaseRecord":
        try:
            return cls(
                lease_id=payload["lease_id"],
                owner_id=payload["owner_id"],
                parent_lease_id=payload.get("parent_lease_id"),
                lease_kind=payload["lease_kind"],
                audience=payload["audience"],
                holder_id=payload["holder_id"],
                scope_key=payload["scope_key"],
                fencing_token=payload["fencing_token"],
                heartbeat_revision=payload["heartbeat_revision"],
                state=LeaseState(payload["state"]),
                expires_at=payload["expires_at"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                metadata=payload.get("metadata", {}),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(f"Persisted lease record is invalid: {error}") from error
