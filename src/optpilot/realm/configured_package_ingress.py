"""Path-free records for typed configured-package ingress."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._validation import lower_hex_digest, nonnegative_int, required_text
from .catalog_publication import (
    MAX_CATALOG_OWNED_PATHS,
    CatalogPackageHead,
    canonical_catalog_paths,
)
from .errors import RealmConflict, RealmIntegrityError
from .manifests import TreeManifest
from .refs import SnapshotRef, canonical_json_bytes, request_digest


CONFIGURED_PACKAGE_INGRESS_REQUEST_SCHEMA = (
    "optpilot.configured-package-ingress-request.v1"
)
CONFIGURED_PACKAGE_VALIDATION_SCHEMA = "optpilot.configured-package-validation.v1"
CONFIGURED_PACKAGE_INGRESS_RECEIPT_SCHEMA = (
    "optpilot.configured-package-ingress-receipt.v1"
)
CONFIGURED_PACKAGE_INGRESS_ARTIFACT_ROLE = "configured-package-ingress-artifact"
CONFIGURED_PACKAGE_INGRESS_ARTIFACT_OWNER_KIND = "configured-package-ingress-artifact"
MAX_CONFIGURED_PACKAGE_VALIDATION_FACTS = 256
MAX_CONFIGURED_PACKAGE_VALIDATION_BYTES = 64 * 1024
MAX_CONFIGURED_PACKAGE_VALIDATION_FACT_COUNT = 1_000_000
MAX_CONFIGURED_PACKAGE_OWNED_PATHS_BYTES = 64 * 1024
MAX_CONFIGURED_PACKAGE_INGRESS_RECEIPT_BYTES = 256 * 1024
_FACT_CODE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,127})\Z")


class ConfiguredPackageIngressOutcome(str, Enum):
    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class ConfiguredPackageIngressTerminalConflict(RealmConflict):
    """A durably replayed ingress disposition that requires a new request."""

    code = "configured_package_ingress_conflict"

    def __init__(self, message: str = "Configured package ingress conflicted.") -> None:
        super().__init__(message)


class ConfiguredPackageIngressInProgress(RealmConflict):
    """Retryable follower disposition; the same request id remains valid."""

    code = "configured_package_ingress_in_progress"

    def __init__(self) -> None:
        super().__init__(
            "Configured package ingress is still in progress; retry the same operation."
        )


class ConfiguredPackageHeadChanged(ConfiguredPackageIngressTerminalConflict):
    code = "configured_package_head_changed"

    def __init__(self) -> None:
        super().__init__(
            "The Realm catalog package head changed; refresh and start a new "
            "publication request."
        )


class ConfiguredPackageOwnershipConflict(ConfiguredPackageIngressTerminalConflict):
    code = "configured_package_ownership_conflict"

    def __init__(self) -> None:
        super().__init__(
            "Another publisher owns overlapping catalog paths; explicit curation "
            "or ownership transfer is required."
        )


def configured_package_source_identity_digest(source_id: str) -> str:
    """Hash one server-authorized opaque source id before durable use."""

    source_id = required_text(
        source_id, "configured package opaque source id", max_bytes=4096
    )
    return request_digest(
        {
            "schema": "optpilot.configured-package-source-identity.v1",
            "source_id": source_id,
        }
    )


def configured_package_publisher_id(
    package_id: str, source_identity_digest: str
) -> str:
    """Derive stable update authority without retaining the source coordinate."""

    _package_id(package_id)
    source_identity_digest = lower_hex_digest(
        source_identity_digest, "configured package source identity digest"
    )
    identity = request_digest(
        {
            "package_id": package_id,
            "schema": "optpilot.configured-package-publisher.v1",
            "source_identity_digest": source_identity_digest,
        }
    )
    return f"configured-package-ingress/{identity}"


def whole_tree_catalog_paths(manifest: TreeManifest) -> tuple[str, ...]:
    """Return exact non-overlapping claims covering every entry in a tree."""

    if not isinstance(manifest, TreeManifest):
        raise TypeError("configured package manifest must be a TreeManifest.")
    roots = tuple(
        sorted(
            {entry.path.split("/", 1)[0] for entry in manifest.entries},
            key=lambda value: value.encode("utf-8"),
        )
    )
    if not roots:
        raise ValueError("Configured package tree is empty.")
    if len(roots) > MAX_CATALOG_OWNED_PATHS:
        raise ValueError("Configured package has too many top-level paths.")
    result = canonical_catalog_paths(roots)
    if (
        len(canonical_json_bytes(list(result)))
        > MAX_CONFIGURED_PACKAGE_OWNED_PATHS_BYTES
    ):
        raise ValueError("Configured package top-level path claims are too large.")
    return result


@dataclass(frozen=True)
class ConfiguredPackageValidationFact:
    """One bounded, path-free aggregate emitted by a trusted static validator."""

    code: str
    severity: str
    count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _FACT_CODE.fullmatch(self.code) is None:
            raise ValueError("configured package validation fact code is invalid.")
        if self.severity not in {"info", "warning", "error"}:
            raise ValueError("configured package validation severity is unsupported.")
        nonnegative_int(self.count, "configured package validation fact count")
        if self.count > MAX_CONFIGURED_PACKAGE_VALIDATION_FACT_COUNT:
            raise ValueError("configured package validation fact count is too large.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "count": self.count,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConfiguredPackageValidationFact":
        _exact_keys(payload, {"code", "count", "severity"}, "validation fact")
        return cls(
            code=payload["code"],
            severity=payload["severity"],
            count=payload["count"],
        )


@dataclass(frozen=True)
class ConfiguredPackageValidationResult:
    """Canonical static validation evidence over one immutable projection."""

    accepted: bool
    facts: tuple[ConfiguredPackageValidationFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("configured package validation accepted must be boolean.")
        facts = tuple(self.facts)
        if len(facts) > MAX_CONFIGURED_PACKAGE_VALIDATION_FACTS:
            raise ValueError("configured package validation has too many facts.")
        if any(not isinstance(item, ConfiguredPackageValidationFact) for item in facts):
            raise TypeError("configured package validation facts are invalid.")
        ordered = tuple(
            sorted(facts, key=lambda item: (item.code, item.severity, item.count))
        )
        if facts != ordered or len(
            {(item.code, item.severity) for item in facts}
        ) != len(facts):
            raise ValueError("configured package validation facts are not canonical.")
        object.__setattr__(self, "facts", facts)
        if self.accepted and any(
            item.severity == "error" and item.count for item in facts
        ):
            raise ValueError("accepted validation cannot contain counted errors.")
        if (
            len(canonical_json_bytes(self.to_dict()))
            > MAX_CONFIGURED_PACKAGE_VALIDATION_BYTES
        ):
            raise ValueError("configured package validation result is too large.")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "facts": [item.to_dict() for item in self.facts],
            "schema": CONFIGURED_PACKAGE_VALIDATION_SCHEMA,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ConfiguredPackageValidationResult":
        _exact_keys(payload, {"accepted", "facts", "schema"}, "validation result")
        if payload["schema"] != CONFIGURED_PACKAGE_VALIDATION_SCHEMA:
            raise ValueError("configured package validation schema is unsupported.")
        if not isinstance(payload["facts"], list):
            raise TypeError("configured package validation facts must be a list.")
        result = cls(
            accepted=payload["accepted"],
            facts=tuple(
                ConfiguredPackageValidationFact.from_dict(item)
                for item in payload["facts"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("configured package validation result is not canonical.")
        return result


@dataclass(frozen=True)
class ConfiguredPackageIngressRequest:
    operation_id: str
    actor_principal_id: str
    package_id: str
    source_identity_digest: str
    capture_policy_digest: str
    validation_policy_digest: str
    expected_head: CatalogPackageHead | None

    def __post_init__(self) -> None:
        required_text(
            self.operation_id, "configured package operation id", max_bytes=512
        )
        required_text(
            self.actor_principal_id,
            "configured package actor principal id",
            max_bytes=512,
        )
        _package_id(self.package_id)
        lower_hex_digest(
            self.source_identity_digest, "configured package source identity digest"
        )
        lower_hex_digest(
            self.capture_policy_digest,
            "configured package capture policy digest",
        )
        lower_hex_digest(
            self.validation_policy_digest,
            "configured package validation policy digest",
        )
        if self.expected_head is not None:
            if not isinstance(self.expected_head, CatalogPackageHead):
                raise TypeError("configured package expected_head is invalid.")
            if self.expected_head.package_id != self.package_id:
                raise ValueError("configured package expected head belongs elsewhere.")
        if len(canonical_json_bytes(self.to_dict())) > 64 * 1024:
            raise ValueError("configured package ingress request is too large.")

    @property
    def publisher_id(self) -> str:
        return configured_package_publisher_id(
            self.package_id, self.source_identity_digest
        )

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_principal_id": self.actor_principal_id,
            "capture_policy_digest": self.capture_policy_digest,
            "expected_head": (
                None if self.expected_head is None else self.expected_head.to_dict()
            ),
            "operation_id": self.operation_id,
            "package_id": self.package_id,
            "publisher_id": self.publisher_id,
            "schema": CONFIGURED_PACKAGE_INGRESS_REQUEST_SCHEMA,
            "source_identity_digest": self.source_identity_digest,
            "validation_policy_digest": self.validation_policy_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConfiguredPackageIngressRequest":
        _exact_keys(
            payload,
            {
                "actor_principal_id",
                "capture_policy_digest",
                "expected_head",
                "operation_id",
                "package_id",
                "publisher_id",
                "schema",
                "source_identity_digest",
                "validation_policy_digest",
            },
            "configured package ingress request",
        )
        if payload["schema"] != CONFIGURED_PACKAGE_INGRESS_REQUEST_SCHEMA:
            raise ValueError(
                "configured package ingress request schema is unsupported."
            )
        raw_head = payload["expected_head"]
        if raw_head is not None and not isinstance(raw_head, Mapping):
            raise TypeError("configured package expected head is invalid.")
        result = cls(
            operation_id=payload["operation_id"],
            actor_principal_id=payload["actor_principal_id"],
            package_id=payload["package_id"],
            source_identity_digest=payload["source_identity_digest"],
            capture_policy_digest=payload["capture_policy_digest"],
            validation_policy_digest=payload["validation_policy_digest"],
            expected_head=(
                None if raw_head is None else CatalogPackageHead.from_dict(raw_head)
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("configured package ingress request is not canonical.")
        return result


@dataclass(frozen=True)
class ConfiguredPackageIngressAttempt:
    attempt_id: str
    request_digest: str
    owner_id: str
    change_id: str
    store_id: str
    capture_operation_id: str
    state: str
    worker_id: str
    worker_generation: int
    worker_expires_at: float
    source_ref: SnapshotRef | None = None
    owned_paths: tuple[str, ...] = ()
    publication_operation_id: str | None = None

    def __post_init__(self) -> None:
        required_text(self.attempt_id, "configured package attempt id", max_bytes=512)
        lower_hex_digest(self.request_digest, "configured package request digest")
        required_text(
            self.owner_id, "configured package artifact owner id", max_bytes=512
        )
        required_text(
            self.change_id, "configured package artifact change id", max_bytes=512
        )
        required_text(
            self.store_id, "configured package artifact store id", max_bytes=128
        )
        required_text(
            self.capture_operation_id,
            "configured package capture operation id",
            max_bytes=512,
        )
        required_text(self.worker_id, "configured package worker id", max_bytes=512)
        if (
            isinstance(self.worker_generation, bool)
            or not isinstance(self.worker_generation, int)
            or self.worker_generation <= 0
        ):
            raise ValueError("configured package worker generation must be positive.")
        if isinstance(self.worker_expires_at, bool) or not isinstance(
            self.worker_expires_at, (int, float)
        ):
            raise ValueError("configured package worker expiry is invalid.")
        if self.state not in {
            "active",
            "adoptable",
            "captured",
            "validated",
            "aborted",
            "completed",
        }:
            raise ValueError("configured package attempt state is unsupported.")
        if self.state in {"active", "aborted"}:
            if self.source_ref is not None or self.owned_paths:
                raise ValueError(
                    "uncaptured configured package attempt cannot name capture."
                )
        elif self.state == "adoptable":
            if not isinstance(self.source_ref, SnapshotRef) or self.owned_paths:
                raise ValueError(
                    "adoptable configured package attempt needs only its exact root."
                )
        else:
            if not isinstance(self.source_ref, SnapshotRef):
                raise TypeError("configured package captured source_ref is required.")
            object.__setattr__(
                self, "owned_paths", canonical_catalog_paths(self.owned_paths)
            )
        if self.publication_operation_id is not None:
            required_text(
                self.publication_operation_id,
                "configured package catalog publication operation id",
                max_bytes=512,
            )
            if self.state not in {"validated", "completed"}:
                raise ValueError(
                    "only validated configured package attempts may publish."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "change_id": self.change_id,
            "capture_operation_id": self.capture_operation_id,
            "owned_paths": list(self.owned_paths),
            "owner_id": self.owner_id,
            "publication_operation_id": self.publication_operation_id,
            "request_digest": self.request_digest,
            "source_ref": None if self.source_ref is None else str(self.source_ref),
            "state": self.state,
            "store_id": self.store_id,
            "worker_expires_at": float(self.worker_expires_at),
            "worker_generation": self.worker_generation,
            "worker_id": self.worker_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConfiguredPackageIngressAttempt":
        try:
            _exact_keys(
                payload,
                {
                    "attempt_id",
                    "capture_operation_id",
                    "change_id",
                    "owned_paths",
                    "owner_id",
                    "publication_operation_id",
                    "request_digest",
                    "source_ref",
                    "state",
                    "store_id",
                    "worker_expires_at",
                    "worker_generation",
                    "worker_id",
                },
                "configured package ingress attempt",
            )
            if not isinstance(payload["owned_paths"], list):
                raise TypeError("configured package attempt paths are invalid.")
            source_ref = payload["source_ref"]
            result = cls(
                attempt_id=payload["attempt_id"],
                request_digest=payload["request_digest"],
                owner_id=payload["owner_id"],
                change_id=payload["change_id"],
                store_id=payload["store_id"],
                capture_operation_id=payload["capture_operation_id"],
                state=payload["state"],
                worker_id=payload["worker_id"],
                worker_generation=payload["worker_generation"],
                worker_expires_at=payload["worker_expires_at"],
                source_ref=(
                    None if source_ref is None else SnapshotRef.parse(source_ref)
                ),
                owned_paths=tuple(payload["owned_paths"]),
                publication_operation_id=payload["publication_operation_id"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("configured package ingress attempt is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted configured package ingress attempt is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class ConfiguredPackageIngressAttemptClaim:
    attempt: ConfiguredPackageIngressAttempt
    leader: bool


@dataclass(frozen=True)
class ConfiguredPackageIngressReceipt:
    request_digest: str
    package_id: str
    publisher_id: str
    outcome: ConfiguredPackageIngressOutcome
    validation: ConfiguredPackageValidationResult | None
    source_ref: SnapshotRef | None
    owned_paths: tuple[str, ...]
    head: CatalogPackageHead | None
    conflict_code: str | None = None
    rejection_stage: str | None = None
    rejection_code: str | None = None

    def __post_init__(self) -> None:
        lower_hex_digest(self.request_digest, "configured package request digest")
        _package_id(self.package_id)
        required_text(
            self.publisher_id, "configured package publisher id", max_bytes=512
        )
        if not isinstance(self.outcome, ConfiguredPackageIngressOutcome):
            raise TypeError("configured package ingress outcome is invalid.")
        if self.validation is not None and not isinstance(
            self.validation, ConfiguredPackageValidationResult
        ):
            raise TypeError("configured package validation receipt is invalid.")
        if self.source_ref is not None and not isinstance(self.source_ref, SnapshotRef):
            raise TypeError("configured package source ref is invalid.")
        paths = tuple(self.owned_paths)
        if paths:
            paths = canonical_catalog_paths(paths)
        object.__setattr__(self, "owned_paths", paths)
        if self.head is not None and (
            not isinstance(self.head, CatalogPackageHead)
            or self.head.package_id != self.package_id
        ):
            raise ValueError("configured package receipt head is invalid.")
        if self.outcome is ConfiguredPackageIngressOutcome.CONFLICT:
            required_text(
                self.conflict_code,
                "configured package conflict code",
                max_bytes=128,
            )
            if self.head is not None:
                raise ValueError("conflicted ingress cannot claim a catalog head.")
        elif self.conflict_code is not None:
            raise ValueError("non-conflict ingress receipt cannot name a conflict.")
        if self.outcome is ConfiguredPackageIngressOutcome.REJECTED:
            if self.head is not None:
                raise ValueError("rejected ingress cannot claim a catalog head.")
            if self.rejection_stage not in {"capture", "validation"}:
                raise ValueError("rejected ingress stage is unsupported.")
            if (
                not isinstance(self.rejection_code, str)
                or _FACT_CODE.fullmatch(self.rejection_code) is None
            ):
                raise ValueError("rejected ingress code is invalid.")
            if self.rejection_stage == "capture":
                if self.rejection_code not in {
                    "capture.source_changed",
                    "capture.content_rejected",
                    "capture.package_tree_invalid",
                }:
                    raise ValueError(
                        "capture rejection code is not emitted by the capture policy."
                    )
                if (
                    self.validation is not None
                    or self.source_ref is not None
                    or self.owned_paths
                ):
                    raise ValueError(
                        "capture rejection cannot claim immutable validation."
                    )
            else:
                if self.rejection_code != "validation.static_rejected":
                    raise ValueError(
                        "validation rejection code is not emitted by the static policy."
                    )
                if (
                    self.validation is None
                    or self.validation.accepted
                    or self.source_ref is None
                    or not self.owned_paths
                ):
                    raise ValueError(
                        "validation rejection must name its exact captured root and evidence."
                    )
        elif self.rejection_stage is not None or self.rejection_code is not None:
            raise ValueError("non-rejected ingress cannot name rejection facts.")
        if self.outcome in {
            ConfiguredPackageIngressOutcome.PUBLISHED,
            ConfiguredPackageIngressOutcome.UNCHANGED,
        } and (
            self.validation is None
            or not self.validation.accepted
            or self.source_ref is None
            or not self.owned_paths
            or self.head is None
        ):
            raise ValueError("successful ingress receipt is incomplete.")
        if (
            len(canonical_json_bytes(self.to_dict()))
            > MAX_CONFIGURED_PACKAGE_INGRESS_RECEIPT_BYTES
        ):
            raise ValueError("configured package ingress receipt is too large.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_code": self.conflict_code,
            "head": None if self.head is None else self.head.to_dict(),
            "outcome": self.outcome.value,
            "owned_paths": list(self.owned_paths),
            "package_id": self.package_id,
            "publisher_id": self.publisher_id,
            "rejection_code": self.rejection_code,
            "rejection_stage": self.rejection_stage,
            "request_digest": self.request_digest,
            "schema": CONFIGURED_PACKAGE_INGRESS_RECEIPT_SCHEMA,
            "source_ref": None if self.source_ref is None else str(self.source_ref),
            "validation": (
                None if self.validation is None else self.validation.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConfiguredPackageIngressReceipt":
        try:
            _exact_keys(
                payload,
                {
                    "conflict_code",
                    "head",
                    "outcome",
                    "owned_paths",
                    "package_id",
                    "publisher_id",
                    "rejection_code",
                    "rejection_stage",
                    "request_digest",
                    "schema",
                    "source_ref",
                    "validation",
                },
                "configured package ingress receipt",
            )
            if payload["schema"] != CONFIGURED_PACKAGE_INGRESS_RECEIPT_SCHEMA:
                raise ValueError(
                    "configured package ingress receipt schema is unsupported."
                )
            raw_validation = payload["validation"]
            raw_head = payload["head"]
            if raw_validation is not None and not isinstance(raw_validation, Mapping):
                raise TypeError("configured package validation receipt is invalid.")
            if raw_head is not None and not isinstance(raw_head, Mapping):
                raise TypeError("configured package receipt head is invalid.")
            if not isinstance(payload["owned_paths"], list):
                raise TypeError("configured package receipt paths are invalid.")
            result = cls(
                request_digest=payload["request_digest"],
                package_id=payload["package_id"],
                publisher_id=payload["publisher_id"],
                outcome=ConfiguredPackageIngressOutcome(payload["outcome"]),
                validation=(
                    None
                    if raw_validation is None
                    else ConfiguredPackageValidationResult.from_dict(raw_validation)
                ),
                source_ref=(
                    None
                    if payload["source_ref"] is None
                    else SnapshotRef.parse(payload["source_ref"])
                ),
                owned_paths=tuple(payload["owned_paths"]),
                head=(
                    None if raw_head is None else CatalogPackageHead.from_dict(raw_head)
                ),
                conflict_code=payload["conflict_code"],
                rejection_stage=payload["rejection_stage"],
                rejection_code=payload["rejection_code"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("configured package ingress receipt is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted configured package ingress receipt is invalid: {error}"
            ) from error

    def raise_for_conflict(self) -> None:
        if self.outcome is not ConfiguredPackageIngressOutcome.CONFLICT:
            return
        if self.conflict_code == ConfiguredPackageHeadChanged.code:
            raise ConfiguredPackageHeadChanged()
        if self.conflict_code == ConfiguredPackageOwnershipConflict.code:
            raise ConfiguredPackageOwnershipConflict()
        raise ConfiguredPackageIngressTerminalConflict()


def _package_id(value: Any) -> str:
    result = required_text(value, "configured package id", max_bytes=256)
    if "/" in result or "\\" in result or result.startswith((".", "~")):
        raise ValueError("configured package id must be one portable component.")
    return result


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError(f"{label} fields differ.")


__all__ = [
    "CONFIGURED_PACKAGE_INGRESS_ARTIFACT_OWNER_KIND",
    "CONFIGURED_PACKAGE_INGRESS_ARTIFACT_ROLE",
    "ConfiguredPackageHeadChanged",
    "ConfiguredPackageIngressAttempt",
    "ConfiguredPackageIngressAttemptClaim",
    "ConfiguredPackageIngressOutcome",
    "ConfiguredPackageIngressInProgress",
    "ConfiguredPackageIngressReceipt",
    "ConfiguredPackageIngressRequest",
    "ConfiguredPackageIngressTerminalConflict",
    "ConfiguredPackageOwnershipConflict",
    "ConfiguredPackageValidationFact",
    "ConfiguredPackageValidationResult",
    "MAX_CONFIGURED_PACKAGE_INGRESS_RECEIPT_BYTES",
    "MAX_CONFIGURED_PACKAGE_OWNED_PATHS_BYTES",
    "configured_package_publisher_id",
    "configured_package_source_identity_digest",
    "whole_tree_catalog_paths",
]
