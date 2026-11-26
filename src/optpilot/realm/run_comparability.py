"""Pure, bounded comparability facts for one canonical run head.

This module deliberately stops short of comparing or ranking runs.  It derives
opaque fingerprints and an honest reproducibility report from one already
authorized :class:`RunLedgerSnapshot`; it performs no ledger read, content
resolution, availability probe, persistence, or authority minting.

The v1 environment fingerprint is conservative.  The retained process-study
compiler currently snapshots the whole package for both environment and method
source.  Method identity is excluded from the fingerprint payload, but
method-only byte changes can therefore still change the environment revision
digest and produce a safe false negative for compatibility.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from ._validation import lower_hex_digest, nonnegative_int, required_text, thaw_json
from .refs import canonical_json_bytes
from .run_snapshot import RunLedgerSnapshot


RUN_COMPARABILITY_PROJECTION_SCHEMA = "optpilot.run-comparability-projection.v1"
RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA = (
    "optpilot.environment-evaluation-fingerprint.v1"
)
RUN_OBJECTIVE_FINGERPRINT_SCHEMA = "optpilot.objective-fingerprint.v1"
RUN_REPRODUCIBILITY_REPORT_SCHEMA = "optpilot.run-reproducibility-report.v1"
RUN_COMPARABILITY_MAX_RESPONSE_BYTES = 16 * 1024

RUN_COMPARABILITY_SOURCE_GRANULARITY = "whole_package"
RUN_COMPARABILITY_COMPARISON_STRENGTH = "conservative"

_ENVIRONMENT_FINGERPRINT_DOMAIN = b"optpilot/environment-evaluation-fingerprint/v1"
_OBJECTIVE_FINGERPRINT_DOMAIN = b"optpilot/objective-fingerprint/v1"

_AUTOMATIC_RANKING_BASE_BLOCKERS = (
    "automatic_cross_run_ranking_not_implemented",
    "bytes_availability_not_assessed",
    "runtime_availability_not_assessed",
    "isolation_not_verified",
    "external_replayability_not_verified",
    "seed_derivation_not_verified",
)


def _fingerprint(domain: bytes, payload: Mapping[str, Any]) -> str:
    """Hash one canonical payload under an explicit semantic domain."""

    return hashlib.sha256(domain + b"\0" + canonical_json_bytes(payload)).hexdigest()


def _dimension(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


@dataclass(frozen=True)
class RunComparabilityProjection:
    """Opaque fingerprints and reproducibility facts at one exact run head.

    The compact typed fields are sufficient to regenerate the public response;
    no raw manifest, content ref, owner coordinate, store placement, path, or
    runtime authority is retained in this projection.
    """

    run_id: str
    revision: int
    sequence: int
    environment_evaluation_fingerprint: str
    objective_fingerprint: str
    seed_repetition_status: str
    terminal_evidence_status: str
    source_granularity: str = RUN_COMPARABILITY_SOURCE_GRANULARITY
    comparison_strength: str = RUN_COMPARABILITY_COMPARISON_STRENGTH

    def __post_init__(self) -> None:
        required_text(self.run_id, "comparability run id", max_bytes=512)
        nonnegative_int(self.revision, "comparability revision")
        nonnegative_int(self.sequence, "comparability sequence")
        lower_hex_digest(
            self.environment_evaluation_fingerprint,
            "environment evaluation fingerprint",
        )
        lower_hex_digest(self.objective_fingerprint, "objective fingerprint")
        if self.seed_repetition_status not in {
            "provisional_at_head",
            "complete_at_head",
        }:
            raise ValueError("seed_repetition_status is unsupported.")
        if self.terminal_evidence_status not in {
            "not_terminal",
            "unsealed",
            "verified",
        }:
            raise ValueError("terminal_evidence_status is unsupported.")
        if self.source_granularity != RUN_COMPARABILITY_SOURCE_GRANULARITY:
            raise ValueError("v1 comparability source granularity is immutable.")
        if self.comparison_strength != RUN_COMPARABILITY_COMPARISON_STRENGTH:
            raise ValueError("v1 comparability comparison strength is immutable.")

    @property
    def head(self) -> dict[str, int]:
        return {"revision": self.revision, "sequence": self.sequence}

    @property
    def fingerprints(self) -> dict[str, dict[str, Any]]:
        return {
            "environment_evaluation": {
                "schema": RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA,
                "digest": self.environment_evaluation_fingerprint,
                "source_granularity": self.source_granularity,
                "comparison_strength": self.comparison_strength,
                "method_identity_included": False,
            },
            "objective": {
                "schema": RUN_OBJECTIVE_FINGERPRINT_SCHEMA,
                "digest": self.objective_fingerprint,
                "scope": "primary_metric_direction_and_aggregation",
            },
        }

    @property
    def reproducibility(self) -> dict[str, Any]:
        if self.seed_repetition_status == "complete_at_head":
            seed_reason = "terminal_head_has_complete_coordinates_but_seed_derivation_is_not_verified"
        else:
            seed_reason = "running_head_can_admit_more_coordinates_and_seed_derivation_is_not_verified"
        terminal_reason = {
            "not_terminal": "run_has_no_terminal_head",
            "unsealed": "legacy_terminal_run_has_no_canonical_evidence_seal",
            "verified": "terminal_evidence_seal_recomputed_from_canonical_ledger",
        }[self.terminal_evidence_status]
        return {
            "schema": RUN_REPRODUCIBILITY_REPORT_SCHEMA,
            "dimensions": {
                "semantic_inputs": _dimension(
                    "identified",
                    "retained_run_definition_identifies_declared_semantic_inputs",
                ),
                "bytes_available_now": _dimension(
                    "not_assessed",
                    "live_content_availability_is_not_checked_by_this_projection",
                ),
                "runtime_identity": _dimension(
                    "identified",
                    "prepared_runtime_digest_identifies_the_declared_runtime",
                ),
                "runtime_available_now": _dimension(
                    "not_assessed",
                    "live_runtime_availability_is_not_checked_by_this_projection",
                ),
                "isolation": _dimension(
                    "unverified",
                    "isolation_enforcement_is_not_proven_by_the_run_snapshot",
                ),
                "external_replayability": _dimension(
                    "unverified",
                    "sealed_external_interaction_transcripts_are_not_retained",
                ),
                "seed_repetition_plan": _dimension(
                    self.seed_repetition_status,
                    seed_reason,
                ),
                "terminal_evidence": _dimension(
                    self.terminal_evidence_status,
                    terminal_reason,
                ),
            },
            "operator_attestation": {
                "status": "absent",
                "upgrades_verified_dimensions": False,
            },
        }

    @property
    def automatic_ranking(self) -> dict[str, Any]:
        blockers = list(_AUTOMATIC_RANKING_BASE_BLOCKERS)
        if self.terminal_evidence_status == "not_terminal":
            blockers.insert(1, "run_not_terminal")
        elif self.terminal_evidence_status == "unsealed":
            blockers.insert(1, "terminal_run_seal_unavailable")
        return {
            "eligible": False,
            "reason": "automatic_ranking_requires_matching_fingerprints_and_verified_reproducibility",
            "blocking_reasons": blockers,
        }

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": RUN_COMPARABILITY_PROJECTION_SCHEMA,
            "run_id": self.run_id,
            "head": self.head,
            "fingerprints": self.fingerprints,
            "reproducibility": self.reproducibility,
            "automatic_ranking": self.automatic_ranking,
        }
        if len(canonical_json_bytes(result)) > RUN_COMPARABILITY_MAX_RESPONSE_BYTES:
            raise ValueError("Run comparability projection exceeds its response bound.")
        return result

    @classmethod
    def from_snapshot(cls, snapshot: RunLedgerSnapshot) -> "RunComparabilityProjection":
        """Derive v1 facts without resolving content or touching authority."""

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")

        closure = snapshot.definition.evaluation_closure
        environment = closure.environment_revision
        runtime = closure.prepared_runtime
        template = closure.evaluation_template
        environment_payload = {
            "schema": RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA,
            "environment_revision_digest": environment.digest,
            "prepared_runtime_digest": runtime.digest,
            "resource_profile": thaw_json(template.resource_profile),
            "sandbox_spec": thaw_json(template.sandbox_spec),
        }
        primary = template.objective["primaryMetric"]
        aggregation = template.objective.get("aggregation")
        objective_payload = {
            "schema": RUN_OBJECTIVE_FINGERPRINT_SCHEMA,
            "primary_metric": {
                "name": primary["name"],
                "direction": primary.get("direction"),
            },
            "aggregation": thaw_json(aggregation),
        }
        terminal = snapshot.finalization is not None
        return cls(
            run_id=snapshot.run.run_id,
            revision=snapshot.revision.revision,
            sequence=snapshot.revision.last_sequence,
            environment_evaluation_fingerprint=_fingerprint(
                _ENVIRONMENT_FINGERPRINT_DOMAIN,
                environment_payload,
            ),
            objective_fingerprint=_fingerprint(
                _OBJECTIVE_FINGERPRINT_DOMAIN,
                objective_payload,
            ),
            seed_repetition_status=(
                "complete_at_head" if terminal else "provisional_at_head"
            ),
            terminal_evidence_status=(
                "verified"
                if snapshot.terminal_seal is not None
                else ("unsealed" if terminal else "not_terminal")
            ),
        )


__all__ = [
    "RUN_COMPARABILITY_COMPARISON_STRENGTH",
    "RUN_COMPARABILITY_MAX_RESPONSE_BYTES",
    "RUN_COMPARABILITY_PROJECTION_SCHEMA",
    "RUN_COMPARABILITY_SOURCE_GRANULARITY",
    "RUN_ENVIRONMENT_EVALUATION_FINGERPRINT_SCHEMA",
    "RUN_OBJECTIVE_FINGERPRINT_SCHEMA",
    "RUN_REPRODUCIBILITY_REPORT_SCHEMA",
    "RunComparabilityProjection",
]
