"""Path-free records for Realm-wide Operator Job capacity admission.

Capacity is a control-plane fact, not a provider coordinate.  These records
therefore contain only logical pool, job, plan, resource, holder, and fencing
identities.  Host paths, process ids, devices, and provider-private handles do
not belong in the Realm ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .refs import canonical_json_bytes


JsonDict = Dict[str, Any]
MAX_OPERATOR_CAPACITY_RESOURCES = 128
MAX_OPERATOR_CAPACITY_AMOUNT = (1 << 63) - 1


def _path_free_identifier(value: Any, label: str, *, max_bytes: int = 512) -> str:
    result = required_text(value, label, max_bytes=max_bytes)
    if (
        "/" in result
        or "\\" in result
        or result.startswith((".", "~"))
        or (len(result) >= 2 and result[1] == ":" and result[0].isalpha())
    ):
        raise ValueError(f"{label} must be a path-free logical identifier.")
    return result


def normalize_capacity_resources(
    value: Mapping[str, int],
    *,
    label: str,
    allow_zero: bool,
) -> Mapping[str, int]:
    """Return a canonical immutable resource map with SQLite-safe amounts."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if not value:
        raise ValueError(f"{label} must not be empty.")
    if len(value) > MAX_OPERATOR_CAPACITY_RESOURCES:
        raise ValueError(
            f"{label} exceeds {MAX_OPERATOR_CAPACITY_RESOURCES} resources."
        )
    result: Dict[str, int] = {}
    for name, amount in value.items():
        name = _path_free_identifier(name, f"{label} name", max_bytes=128)
        if allow_zero:
            amount = nonnegative_int(amount, f"{label} {name}")
        else:
            amount = positive_int(amount, f"{label} {name}")
        if amount > MAX_OPERATOR_CAPACITY_AMOUNT:
            raise ValueError(f"{label} {name} exceeds the durable integer limit.")
        result[name] = amount
    return MappingProxyType(dict(sorted(result.items())))


def capacity_resources_digest(value: Mapping[str, int]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


def operator_capacity_reservation_id(pool_name: str, job_id: str) -> str:
    """Return the stable current-reservation identity for one pool/job pair."""

    pool_name = _path_free_identifier(
        pool_name, "operator capacity pool name", max_bytes=128
    )
    job_id = _path_free_identifier(job_id, "operator capacity job id")
    digest = hashlib.sha256(
        b"optpilot/operator-capacity-reservation/v1\0"
        + pool_name.encode("utf-8")
        + b"\0"
        + job_id.encode("utf-8")
    ).hexdigest()
    return f"operator-capacity-{digest[:32]}"


class OperatorCapacityReservationState(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class OperatorCapacityPoolState(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class OperatorCapacityPoolRecord:
    pool_name: str
    limits: Mapping[str, int]
    limits_digest: str
    revision: int
    state: OperatorCapacityPoolState
    created_by_principal_id: str
    created_txn_id: int
    created_at: float
    updated_by_principal_id: str
    updated_txn_id: int
    updated_at: float

    def __post_init__(self) -> None:
        _path_free_identifier(
            self.pool_name, "operator capacity pool name", max_bytes=128
        )
        limits = normalize_capacity_resources(
            self.limits,
            label="operator capacity pool limits",
            allow_zero=True,
        )
        object.__setattr__(self, "limits", limits)
        lower_hex_digest(self.limits_digest, "operator capacity limits digest")
        if self.limits_digest != capacity_resources_digest(limits):
            raise ValueError("operator capacity limits digest is inconsistent.")
        nonnegative_int(self.revision, "operator capacity pool revision")
        if not isinstance(self.state, OperatorCapacityPoolState):
            raise ValueError("operator capacity pool state is invalid.")
        _path_free_identifier(
            self.created_by_principal_id,
            "operator capacity pool creator principal id",
        )
        positive_int(
            self.created_txn_id, "operator capacity pool creation transaction id"
        )
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "operator capacity pool creation time"),
        )
        _path_free_identifier(
            self.updated_by_principal_id,
            "operator capacity pool updater principal id",
        )
        positive_int(
            self.updated_txn_id, "operator capacity pool update transaction id"
        )
        updated_at = finite_time(
            self.updated_at, "operator capacity pool update time"
        )
        if updated_at < self.created_at:
            raise ValueError("operator capacity pool timestamps are inconsistent.")
        object.__setattr__(self, "updated_at", updated_at)
        if self.revision == 0 and (
            self.updated_by_principal_id != self.created_by_principal_id
            or self.updated_txn_id != self.created_txn_id
            or self.updated_at != self.created_at
        ):
            raise ValueError("initial operator capacity pool update facts differ.")

    def to_dict(self) -> JsonDict:
        return {
            "created_at": self.created_at,
            "created_by_principal_id": self.created_by_principal_id,
            "created_txn_id": self.created_txn_id,
            "limits": dict(self.limits),
            "limits_digest": self.limits_digest,
            "pool_name": self.pool_name,
            "revision": self.revision,
            "state": self.state.value,
            "updated_at": self.updated_at,
            "updated_by_principal_id": self.updated_by_principal_id,
            "updated_txn_id": self.updated_txn_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperatorCapacityPoolRecord":
        try:
            values = dict(payload)
            receipt_version = values.pop("receipt_version", None)
            if receipt_version not in {None, 1}:
                raise ValueError("operator capacity pool receipt version is unsupported.")
            if set(values) != set(cls.__dataclass_fields__):
                raise ValueError("operator capacity pool fields differ.")
            values["state"] = OperatorCapacityPoolState(values["state"])
            result = cls(**values)
            canonical_values = dict(values)
            canonical_values["state"] = values["state"].value
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                canonical_values
            ):
                raise ValueError("operator capacity pool is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted operator capacity pool is malformed."
            ) from error


@dataclass(frozen=True)
class OperatorCapacityReservationRecord:
    reservation_id: str
    pool_name: str
    pool_revision: int
    job_id: str
    plan_digest: str
    claims: Mapping[str, int]
    claims_digest: str
    holder_id: str
    fencing_token: int
    generation: int
    heartbeat_revision: int
    state: OperatorCapacityReservationState
    expires_at: float
    acquired_by_principal_id: str
    acquired_txn_id: int
    updated_txn_id: int
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _path_free_identifier(
            self.reservation_id, "operator capacity reservation id"
        )
        _path_free_identifier(
            self.pool_name, "operator capacity pool name", max_bytes=128
        )
        nonnegative_int(self.pool_revision, "operator capacity pool revision")
        _path_free_identifier(self.job_id, "operator capacity job id")
        if self.reservation_id != operator_capacity_reservation_id(
            self.pool_name, self.job_id
        ):
            raise ValueError("operator capacity reservation id is inconsistent.")
        lower_hex_digest(self.plan_digest, "operator capacity plan digest")
        claims = normalize_capacity_resources(
            self.claims,
            label="operator capacity claims",
            allow_zero=False,
        )
        object.__setattr__(self, "claims", claims)
        lower_hex_digest(self.claims_digest, "operator capacity claims digest")
        if self.claims_digest != capacity_resources_digest(claims):
            raise ValueError("operator capacity claims digest is inconsistent.")
        _path_free_identifier(self.holder_id, "operator capacity holder id")
        positive_int(self.fencing_token, "operator capacity fencing token")
        positive_int(self.generation, "operator capacity generation")
        nonnegative_int(
            self.heartbeat_revision, "operator capacity heartbeat revision"
        )
        if not isinstance(self.state, OperatorCapacityReservationState):
            raise ValueError("operator capacity reservation state is invalid.")
        expires_at = finite_time(
            self.expires_at, "operator capacity reservation expiry"
        )
        created_at = finite_time(
            self.created_at, "operator capacity reservation creation time"
        )
        updated_at = finite_time(
            self.updated_at, "operator capacity reservation update time"
        )
        if expires_at <= 0 or updated_at < created_at:
            raise ValueError("operator capacity reservation timestamps are inconsistent.")
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        _path_free_identifier(
            self.acquired_by_principal_id,
            "operator capacity acquiring principal id",
        )
        positive_int(
            self.acquired_txn_id, "operator capacity acquisition transaction id"
        )
        positive_int(self.updated_txn_id, "operator capacity update transaction id")

    def to_dict(self) -> JsonDict:
        return {
            "acquired_by_principal_id": self.acquired_by_principal_id,
            "acquired_txn_id": self.acquired_txn_id,
            "claims": dict(self.claims),
            "claims_digest": self.claims_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "fencing_token": self.fencing_token,
            "generation": self.generation,
            "heartbeat_revision": self.heartbeat_revision,
            "holder_id": self.holder_id,
            "job_id": self.job_id,
            "plan_digest": self.plan_digest,
            "pool_name": self.pool_name,
            "pool_revision": self.pool_revision,
            "reservation_id": self.reservation_id,
            "state": self.state.value,
            "updated_at": self.updated_at,
            "updated_txn_id": self.updated_txn_id,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "OperatorCapacityReservationRecord":
        try:
            values = dict(payload)
            receipt_version = values.pop("receipt_version", None)
            if receipt_version not in {None, 1}:
                raise ValueError(
                    "operator capacity reservation receipt version is unsupported."
                )
            if set(values) != set(cls.__dataclass_fields__):
                raise ValueError("operator capacity reservation fields differ.")
            values["state"] = OperatorCapacityReservationState(values["state"])
            result = cls(**values)
            canonical_values = dict(values)
            canonical_values["state"] = values["state"].value
            if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(
                canonical_values
            ):
                raise ValueError("operator capacity reservation is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted operator capacity reservation is malformed."
            ) from error


__all__ = [
    "MAX_OPERATOR_CAPACITY_AMOUNT",
    "MAX_OPERATOR_CAPACITY_RESOURCES",
    "OperatorCapacityPoolRecord",
    "OperatorCapacityPoolState",
    "OperatorCapacityReservationRecord",
    "OperatorCapacityReservationState",
    "capacity_resources_digest",
    "normalize_capacity_resources",
    "operator_capacity_reservation_id",
]
