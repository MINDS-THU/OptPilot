"""Immutable records for Realm-owned local provider trust decisions.

Provider trust is host policy, not package metadata.  These records deliberately
do not import a concrete provider implementation so the Realm authority and CLI
can inspect policy without constructing a container runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping

from ._validation import finite_time, positive_int, required_text
from .errors import RealmIntegrityError


JsonDict = Dict[str, Any]

PROVIDER_TRUST_POLICY_ID = "local-container-gateway-images-v1"
PROVIDER_TRUST_POLICY_OWNER_ID = "realm-policy:local-container-gateway-images-v1"
PROVIDER_TRUST_POLICY_OWNER_KIND = "provider-trust-policy"
PROVIDER_TRUST_GATEWAY_CONTRACT = "optpilot-stdlib-gateway-v1"
PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE = "python3"

IMMUTABLE_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$"
)
# Retained for callers that predate the exported name.
_IMMUTABLE_IMAGE_RE = IMMUTABLE_IMAGE_RE
_CONTAINER_EXECUTABLE_RE = re.compile(
    r"^(?:/[A-Za-z0-9_.+-]+)+$|^[A-Za-z0-9_.+-]+$"
)


class ProviderTrustState(str, Enum):
    APPROVED = "approved"
    REVOKED = "revoked"


def validate_provider_image_ref(value: Any) -> str:
    image_ref = required_text(
        value,
        "provider trust immutable image ref",
        max_bytes=1024,
    )
    if _IMMUTABLE_IMAGE_RE.fullmatch(image_ref) is None:
        raise ValueError("provider trust image_ref must be pinned by sha256.")
    return image_ref


def validate_provider_python_executable(value: Any) -> str:
    executable = required_text(
        value,
        "provider trust Python executable",
        max_bytes=512,
    )
    if _CONTAINER_EXECUTABLE_RE.fullmatch(executable) is None:
        raise ValueError("provider trust Python executable is invalid.")
    return executable


def validate_provider_contract(value: Any) -> str:
    contract = required_text(value, "provider trust contract", max_bytes=128)
    if contract != PROVIDER_TRUST_GATEWAY_CONTRACT:
        raise ValueError("provider trust contract is unsupported.")
    return contract


def validate_provider_trust_reason(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("provider trust reason must be a string.")
    if not value:
        return ""
    return required_text(value, "provider trust reason", max_bytes=4096)


@dataclass(frozen=True)
class ProviderTrustDecision:
    """One immutable approve or revoke decision for an exact image digest."""

    decision_id: str
    policy_id: str
    sequence: int
    image_ref: str
    python_executable: str
    contract: str
    state: ProviderTrustState
    reason: str
    actor_principal_id: str
    previous_decision_id: str | None
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.decision_id, "provider trust decision id", max_bytes=128)
        if self.policy_id != PROVIDER_TRUST_POLICY_ID:
            raise ValueError("provider trust policy id is unsupported.")
        positive_int(self.sequence, "provider trust decision sequence")
        object.__setattr__(
            self,
            "image_ref",
            validate_provider_image_ref(self.image_ref),
        )
        object.__setattr__(
            self,
            "python_executable",
            validate_provider_python_executable(self.python_executable),
        )
        object.__setattr__(
            self,
            "contract",
            validate_provider_contract(self.contract),
        )
        if not isinstance(self.state, ProviderTrustState):
            raise ValueError("provider trust state is invalid.")
        object.__setattr__(
            self,
            "reason",
            validate_provider_trust_reason(self.reason),
        )
        required_text(
            self.actor_principal_id,
            "provider trust actor principal id",
        )
        if self.previous_decision_id is not None:
            required_text(
                self.previous_decision_id,
                "provider trust previous decision id",
                max_bytes=128,
            )
            if self.previous_decision_id == self.decision_id:
                raise ValueError(
                    "provider trust decision cannot reference itself as previous."
                )
        if (self.sequence == 1) != (self.previous_decision_id is None):
            raise ValueError(
                "provider trust decision sequence and previous decision disagree."
            )
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "provider trust decision created_at"),
        )

    def to_dict(self) -> JsonDict:
        return {
            "decision_id": self.decision_id,
            "policy_id": self.policy_id,
            "sequence": self.sequence,
            "image_ref": self.image_ref,
            "python_executable": self.python_executable,
            "contract": self.contract,
            "state": self.state.value,
            "reason": self.reason,
            "actor_principal_id": self.actor_principal_id,
            "previous_decision_id": self.previous_decision_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderTrustDecision":
        try:
            if set(value) != {
                "decision_id",
                "policy_id",
                "sequence",
                "image_ref",
                "python_executable",
                "contract",
                "state",
                "reason",
                "actor_principal_id",
                "previous_decision_id",
                "created_at",
            }:
                raise ValueError("provider trust decision fields are invalid")
            result = cls(
                decision_id=value["decision_id"],
                policy_id=value["policy_id"],
                sequence=value["sequence"],
                image_ref=value["image_ref"],
                python_executable=value["python_executable"],
                contract=value["contract"],
                state=ProviderTrustState(value["state"]),
                reason=value["reason"],
                actor_principal_id=value["actor_principal_id"],
                previous_decision_id=value["previous_decision_id"],
                created_at=value["created_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted provider trust decision is malformed."
            ) from error
        if result.to_dict() != dict(value):
            raise RealmIntegrityError(
                "Persisted provider trust decision is not canonical."
            )
        return result


@dataclass(frozen=True)
class ProviderTrustHead:
    """The current decision for one exact image reference."""

    decision: ProviderTrustDecision
    updated_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ProviderTrustDecision):
            raise TypeError("provider trust head decision is invalid.")
        updated_at = finite_time(
            self.updated_at,
            "provider trust head updated_at",
        )
        if updated_at != self.decision.created_at:
            raise ValueError(
                "provider trust head timestamp must equal its decision timestamp."
            )
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def decision_id(self) -> str:
        return self.decision.decision_id

    @property
    def policy_id(self) -> str:
        return self.decision.policy_id

    @property
    def sequence(self) -> int:
        return self.decision.sequence

    @property
    def image_ref(self) -> str:
        return self.decision.image_ref

    @property
    def python_executable(self) -> str:
        return self.decision.python_executable

    @property
    def contract(self) -> str:
        return self.decision.contract

    @property
    def state(self) -> ProviderTrustState:
        return self.decision.state

    @property
    def reason(self) -> str:
        return self.decision.reason

    @property
    def actor_principal_id(self) -> str:
        return self.decision.actor_principal_id

    @property
    def created_at(self) -> float:
        return self.decision.created_at

    def to_dict(self) -> JsonDict:
        return {
            "decision": self.decision.to_dict(),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderTrustHead":
        try:
            if set(value) != {"decision", "updated_at"}:
                raise ValueError("provider trust head fields are invalid")
            result = cls(
                decision=ProviderTrustDecision.from_dict(value["decision"]),
                updated_at=value["updated_at"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                "Persisted provider trust head is malformed."
            ) from error
        if result.to_dict() != dict(value):
            raise RealmIntegrityError("Persisted provider trust head is not canonical.")
        return result


__all__ = [
    "PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE",
    "PROVIDER_TRUST_GATEWAY_CONTRACT",
    "PROVIDER_TRUST_POLICY_ID",
    "PROVIDER_TRUST_POLICY_OWNER_ID",
    "PROVIDER_TRUST_POLICY_OWNER_KIND",
    "ProviderTrustDecision",
    "ProviderTrustHead",
    "ProviderTrustState",
    "validate_provider_contract",
    "validate_provider_image_ref",
    "validate_provider_python_executable",
    "validate_provider_trust_reason",
]
