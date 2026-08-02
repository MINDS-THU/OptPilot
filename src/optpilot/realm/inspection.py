"""Immutable semantic target for a noncanonical candidate inspection.

An inspection target is not a workspace and owns no copied tree. It combines
one stable :class:`SelectionRef` with the exact candidate and retained
environment/runtime closure needed to compile the same :class:`EvaluationSpec`
as a canonical trial. Physical content bindings describe current provider
availability only; a later Operator Job realizes them under leased ephemeral
volumes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..attempts import EvaluationSpec
from .evaluation_compiler import compile_candidate_evaluation_spec
from .owners import OwnerMembership
from .run_closure import ResolvedRunEvaluationClosure
from .run_definition import RunDefinitionManifest
from .run_records import RUN_CANDIDATE_ROLE, RunCandidateRecord
from .selections import SelectionRef


@dataclass(frozen=True)
class ResolvedCandidateInspectionTarget:
    """One authorized candidate selection and its current runnable closure."""

    selection: SelectionRef
    candidate: RunCandidateRecord
    candidate_bindings: tuple[OwnerMembership, ...]
    run_definition: RunDefinitionManifest
    evaluation: ResolvedRunEvaluationClosure

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if self.selection.kind != "candidate" or self.selection.source_kind != "run":
            raise ValueError("inspection target requires a run candidate selection.")
        if self.selection.relative_path is not None:
            raise ValueError("inspection target cannot select a candidate subtree.")
        if not isinstance(self.candidate, RunCandidateRecord):
            raise TypeError("candidate must be a RunCandidateRecord.")
        if not isinstance(self.run_definition, RunDefinitionManifest):
            raise TypeError("run_definition must be a RunDefinitionManifest.")
        if not isinstance(self.evaluation, ResolvedRunEvaluationClosure):
            raise TypeError("evaluation must be a ResolvedRunEvaluationClosure.")
        bindings = tuple(sorted(set(self.candidate_bindings)))
        if any(item.role != RUN_CANDIDATE_ROLE for item in bindings):
            raise ValueError("candidate bindings require the run-candidate role.")
        expected_refs = set(self.candidate.admission.envelope.content_refs)
        if not {item.content_ref for item in bindings}.issubset(expected_refs):
            raise ValueError("candidate bindings contain undeclared content refs.")
        if (
            self.candidate.run_id != self.selection.source_id
            or self.candidate.candidate_id != self.selection.entity_id
            or str(self.candidate.candidate_ref) != self.selection.entity_ref
            or self.candidate.accepted_sequence != self.selection.entity_sequence
            or self.run_definition.evaluation_closure != self.evaluation.closure
            or self.evaluation.closure.evaluation_template.digest
            != self.selection.context_digest
        ):
            raise ValueError("inspection target differs from its immutable selection.")
        object.__setattr__(self, "candidate_bindings", bindings)

    @property
    def candidate_available(self) -> bool:
        return {
            item.content_ref for item in self.candidate_bindings
        } == set(self.candidate.admission.envelope.content_refs)

    @property
    def runnable(self) -> bool:
        return self.candidate_available and self.evaluation.availability == "available"

    def compile_evaluation_spec(
        self,
        *,
        seed: Any = None,
        repetition_index: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationSpec:
        """Compile the shared canonical/inspection semantic evaluation."""

        if not self.runnable:
            raise ValueError("Inspection target content is unavailable.")
        return compile_candidate_evaluation_spec(
            closure=self.evaluation.closure,
            candidate=self.candidate,
            seed=seed,
            repetition_index=repetition_index,
            metadata=metadata,
        )


__all__ = ["ResolvedCandidateInspectionTarget"]
