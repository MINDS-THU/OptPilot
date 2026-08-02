"""Pure compilation of one candidate against a retained evaluation closure.

Canonical trials and noncanonical inspection jobs intentionally enter through
this same function. Callers choose the seed and repetition coordinate, while
the environment, prepared runtime, candidate contract, objective, and resource
policy remain anchored to the immutable run definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..attempts import EvaluationSpec
from ._validation import thaw_json
from .run_closure import RunEvaluationClosure
from .run_records import RunCandidateRecord


def compile_candidate_evaluation_spec(
    *,
    closure: RunEvaluationClosure,
    candidate: RunCandidateRecord,
    seed: Any = None,
    repetition_index: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> EvaluationSpec:
    """Compile the portable semantic inputs for one exact candidate evaluation.

    ``None`` preserves the established run behavior of selecting the retained
    template's default seed. No run, trial, attempt, job, workspace, provider,
    or host-path identity enters the result.
    """

    if not isinstance(closure, RunEvaluationClosure):
        raise TypeError("closure must be a RunEvaluationClosure.")
    if not isinstance(candidate, RunCandidateRecord):
        raise TypeError("candidate must be a RunCandidateRecord.")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping or None.")

    contract = thaw_json(closure.environment_revision.candidate_contract)
    if not isinstance(contract, Mapping):  # pragma: no cover - manifest invariant
        raise ValueError("Environment candidate contract must be a mapping.")
    candidate_format = candidate.admission.envelope.candidate_format
    if contract.get("format") != candidate_format:
        raise ValueError(
            "Run candidate format differs from the retained environment contract."
        )
    validation = contract.get("validation", {})
    materialization = contract.get("materialization", {})
    if not isinstance(validation, Mapping) or not isinstance(
        materialization, Mapping
    ):
        raise ValueError(
            "Environment validation and materialization contracts must be mappings."
        )

    template = closure.evaluation_template
    selected_seed = template.default_seed if seed is None else seed
    return EvaluationSpec(
        environment_id=closure.environment_revision.environment_id,
        environment_revision_digest=closure.environment_revision.digest,
        prepared_runtime_digest=closure.prepared_runtime.digest,
        candidate_ref=str(candidate.candidate_ref),
        candidate={
            "candidate_id": candidate.candidate_id,
            "format": candidate_format,
            "spec": thaw_json(candidate.admission.envelope.spec),
            "lineage": thaw_json(candidate.admission.lineage),
            "generator": thaw_json(candidate.admission.generator),
            "validation": thaw_json(validation),
            "materialization": thaw_json(materialization),
        },
        objective=thaw_json(template.objective),
        resource_profile=thaw_json(template.resource_profile),
        sandbox_spec=thaw_json(template.sandbox_spec),
        seed=thaw_json(selected_seed),
        repetition_index=repetition_index,
        metadata={} if metadata is None else thaw_json(metadata),
    )


__all__ = ["compile_candidate_evaluation_spec"]
