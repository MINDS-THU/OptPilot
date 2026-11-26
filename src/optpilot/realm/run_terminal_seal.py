"""Stable final evidence identities for terminal Realm runs.

The seal is a compact manifest over canonical ledger evidence, not a copy of
that evidence and not a mutable run-head token.  In particular it excludes
current controller-lease state and every revision or event after the
``run.finish`` revision.  This lets a terminal parent keep the same identity
after controller replacement or retention retirement while a reader can still
recompute every component digest from the authoritative ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .refs import canonical_json_bytes, request_digest


RUN_TERMINAL_ANCHOR_SCHEMA = "optpilot.run-terminal-anchor.v1"
RUN_TERMINAL_SEAL_SCHEMA = "optpilot.run-terminal-seal.v1"
RUN_TERMINAL_EVIDENCE_COMPONENT_SCHEMA = "optpilot.run-terminal-evidence-component.v1"

# This list is part of the v1 seal contract.  A missing component must not be
# confused with an empty component, and adding new evidence requires a new seal
# schema rather than silently changing the meaning of an existing digest.
RUN_TERMINAL_EVIDENCE_COMPONENTS = (
    "artifacts",
    "attempt_transitions",
    "attempts",
    "candidates",
    "controller_terms",
    "definition",
    "events",
    "execution_bindings",
    "execution_cleanup_authorizations",
    "execution_launch_intents",
    "execution_terminal_evidence",
    "finalization",
    "logical_transitions",
    "logical_trials",
    "method_exchange_completions",
    "method_exchange_preparations",
    "observations",
    "revisions",
    "run_identity",
    "submission_control",
    "submission_handles",
)


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _terminal_state(value: Any) -> str:
    result = required_text(value, "terminal run state", max_bytes=128)
    if result not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("terminal run state is unsupported.")
    return result


def _optional_code(value: Any) -> str | None:
    if value is None:
        return None
    return required_text(value, "run finalization code", max_bytes=512)


def terminal_evidence_component_digest(name: str, value: Any) -> str:
    """Hash one named canonical evidence component with explicit domain data."""

    name = required_text(name, "terminal evidence component", max_bytes=128)
    # Encoding here is also the bounded JSON-shape validation used by callers.
    canonical_json_bytes(value)
    return request_digest(
        {
            "component": name,
            "schema": RUN_TERMINAL_EVIDENCE_COMPONENT_SCHEMA,
            "value": value,
        }
    )


@dataclass(frozen=True)
class RunTerminalAnchor:
    """Portable stable coordinate for one sealed terminal run."""

    run_id: str
    owner_id: str
    terminal_state: str
    code: str | None
    finalization_revision: int
    finalization_txn_id: int
    owner_revision: int
    last_sequence: int
    accepted_logical_trials: int
    definition_digest: str
    seal_digest: str

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.owner_id, "run owner id")
        object.__setattr__(self, "terminal_state", _terminal_state(self.terminal_state))
        object.__setattr__(self, "code", _optional_code(self.code))
        positive_int(self.finalization_revision, "finalization revision")
        positive_int(self.finalization_txn_id, "finalization transaction id")
        nonnegative_int(self.owner_revision, "terminal owner revision")
        nonnegative_int(self.last_sequence, "terminal last sequence")
        nonnegative_int(
            self.accepted_logical_trials, "terminal accepted logical trials"
        )
        lower_hex_digest(self.definition_digest, "run definition digest")
        lower_hex_digest(self.seal_digest, "run terminal seal digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_logical_trials": self.accepted_logical_trials,
            "code": self.code,
            "definition_digest": self.definition_digest,
            "finalization_revision": self.finalization_revision,
            "finalization_txn_id": self.finalization_txn_id,
            "last_sequence": self.last_sequence,
            "owner_id": self.owner_id,
            "owner_revision": self.owner_revision,
            "run_id": self.run_id,
            "schema": RUN_TERMINAL_ANCHOR_SCHEMA,
            "seal_digest": self.seal_digest,
            "terminal_state": self.terminal_state,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunTerminalAnchor":
        _exact_keys(
            payload,
            {
                "accepted_logical_trials",
                "code",
                "definition_digest",
                "finalization_revision",
                "finalization_txn_id",
                "last_sequence",
                "owner_id",
                "owner_revision",
                "run_id",
                "schema",
                "seal_digest",
                "terminal_state",
            },
            "run terminal anchor",
        )
        if payload["schema"] != RUN_TERMINAL_ANCHOR_SCHEMA:
            raise ValueError("run terminal anchor schema is unsupported.")
        return cls(
            run_id=payload["run_id"],
            owner_id=payload["owner_id"],
            terminal_state=payload["terminal_state"],
            code=payload["code"],
            finalization_revision=payload["finalization_revision"],
            finalization_txn_id=payload["finalization_txn_id"],
            owner_revision=payload["owner_revision"],
            last_sequence=payload["last_sequence"],
            accepted_logical_trials=payload["accepted_logical_trials"],
            definition_digest=payload["definition_digest"],
            seal_digest=payload["seal_digest"],
        )


@dataclass(frozen=True)
class RunTerminalSeal:
    """Immutable manifest that seals canonical evidence through finalization."""

    run_id: str
    owner_id: str
    terminal_state: str
    code: str | None
    finalization_revision: int
    finalization_txn_id: int
    owner_revision: int
    last_sequence: int
    accepted_logical_trials: int
    definition_digest: str
    evidence_digests: Mapping[str, str]
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.run_id, "run id")
        required_text(self.owner_id, "run owner id")
        object.__setattr__(self, "terminal_state", _terminal_state(self.terminal_state))
        object.__setattr__(self, "code", _optional_code(self.code))
        positive_int(self.finalization_revision, "finalization revision")
        positive_int(self.finalization_txn_id, "finalization transaction id")
        nonnegative_int(self.owner_revision, "terminal owner revision")
        nonnegative_int(self.last_sequence, "terminal last sequence")
        nonnegative_int(
            self.accepted_logical_trials, "terminal accepted logical trials"
        )
        lower_hex_digest(self.definition_digest, "run definition digest")
        if not isinstance(self.evidence_digests, Mapping):
            raise TypeError("terminal evidence_digests must be a mapping.")
        if set(self.evidence_digests) != set(RUN_TERMINAL_EVIDENCE_COMPONENTS):
            raise ValueError("terminal evidence digest components differ.")
        normalized = {
            name: lower_hex_digest(
                self.evidence_digests[name], f"{name} evidence digest"
            )
            for name in RUN_TERMINAL_EVIDENCE_COMPONENTS
        }
        object.__setattr__(self, "evidence_digests", MappingProxyType(normalized))
        object.__setattr__(
            self, "created_at", finite_time(self.created_at, "terminal seal created_at")
        )

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        owner_id: str,
        terminal_state: str,
        code: str | None,
        finalization_revision: int,
        finalization_txn_id: int,
        owner_revision: int,
        last_sequence: int,
        accepted_logical_trials: int,
        definition_digest: str,
        evidence_components: Mapping[str, Any],
        created_at: float,
    ) -> "RunTerminalSeal":
        if not isinstance(evidence_components, Mapping):
            raise TypeError("terminal evidence_components must be a mapping.")
        if set(evidence_components) != set(RUN_TERMINAL_EVIDENCE_COMPONENTS):
            raise ValueError("terminal evidence components differ.")
        digests = {
            name: terminal_evidence_component_digest(name, evidence_components[name])
            for name in RUN_TERMINAL_EVIDENCE_COMPONENTS
        }
        return cls(
            run_id=run_id,
            owner_id=owner_id,
            terminal_state=terminal_state,
            code=code,
            finalization_revision=finalization_revision,
            finalization_txn_id=finalization_txn_id,
            owner_revision=owner_revision,
            last_sequence=last_sequence,
            accepted_logical_trials=accepted_logical_trials,
            definition_digest=definition_digest,
            evidence_digests=digests,
            created_at=created_at,
        )

    @property
    def digest(self) -> str:
        """Bare lowercase SHA-256 identity of this complete seal manifest."""

        return request_digest(self.to_dict())

    @property
    def anchor(self) -> RunTerminalAnchor:
        return RunTerminalAnchor(
            run_id=self.run_id,
            owner_id=self.owner_id,
            terminal_state=self.terminal_state,
            code=self.code,
            finalization_revision=self.finalization_revision,
            finalization_txn_id=self.finalization_txn_id,
            owner_revision=self.owner_revision,
            last_sequence=self.last_sequence,
            accepted_logical_trials=self.accepted_logical_trials,
            definition_digest=self.definition_digest,
            seal_digest=self.digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_logical_trials": self.accepted_logical_trials,
            "code": self.code,
            "created_at": self.created_at,
            "definition_digest": self.definition_digest,
            "evidence_digests": dict(self.evidence_digests),
            "finalization_revision": self.finalization_revision,
            "finalization_txn_id": self.finalization_txn_id,
            "last_sequence": self.last_sequence,
            "owner_id": self.owner_id,
            "owner_revision": self.owner_revision,
            "run_id": self.run_id,
            "schema": RUN_TERMINAL_SEAL_SCHEMA,
            "terminal_state": self.terminal_state,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunTerminalSeal":
        _exact_keys(
            payload,
            {
                "accepted_logical_trials",
                "code",
                "created_at",
                "definition_digest",
                "evidence_digests",
                "finalization_revision",
                "finalization_txn_id",
                "last_sequence",
                "owner_id",
                "owner_revision",
                "run_id",
                "schema",
                "terminal_state",
            },
            "run terminal seal",
        )
        if payload["schema"] != RUN_TERMINAL_SEAL_SCHEMA:
            raise ValueError("run terminal seal schema is unsupported.")
        return cls(
            run_id=payload["run_id"],
            owner_id=payload["owner_id"],
            terminal_state=payload["terminal_state"],
            code=payload["code"],
            finalization_revision=payload["finalization_revision"],
            finalization_txn_id=payload["finalization_txn_id"],
            owner_revision=payload["owner_revision"],
            last_sequence=payload["last_sequence"],
            accepted_logical_trials=payload["accepted_logical_trials"],
            definition_digest=payload["definition_digest"],
            evidence_digests=payload["evidence_digests"],
            created_at=payload["created_at"],
        )


__all__ = [
    "RUN_TERMINAL_ANCHOR_SCHEMA",
    "RUN_TERMINAL_EVIDENCE_COMPONENTS",
    "RUN_TERMINAL_SEAL_SCHEMA",
    "RunTerminalAnchor",
    "RunTerminalSeal",
    "terminal_evidence_component_digest",
]
