"""Reconstruct the pure retained-batch candidate normalization contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .candidate_materialization import normalize_candidate_against_contract
from .realm.errors import RealmIntegrityError
from .realm.run_definition import RunDefinitionManifest
from .study_realm_compiler import CANDIDATE_NORMALIZER_VERSION


CandidateNormalizer = Callable[[dict[str, Any]], dict[str, Any]]


def candidate_normalizer_for_run_definition(
    definition: RunDefinitionManifest,
) -> CandidateNormalizer:
    """Return the exact normalizer named by a retained run definition."""

    if not isinstance(definition, RunDefinitionManifest):
        raise TypeError("definition must be a RunDefinitionManifest.")
    control = definition.run_control_manifest
    if control.normalizer_version != CANDIDATE_NORMALIZER_VERSION:
        raise RealmIntegrityError(
            "Retained run names an unsupported candidate normalizer."
        )
    candidate_contract = (
        definition.evaluation_closure.environment_revision.candidate_contract
    )
    method_id = control.method_id

    def normalize(candidate: dict[str, Any]) -> dict[str, Any]:
        return normalize_candidate_against_contract(
            candidate,
            candidate_contract,
            method_id,
        )

    return normalize


__all__ = ["candidate_normalizer_for_run_definition"]
