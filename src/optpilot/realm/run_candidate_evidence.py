"""Internal exact-plan evidence shared by candidate result projections.

This module resolves each accepted candidate to its ordered logical-trial
coordinates and the final attempt/observation selected by the canonical
logical-trial head.  It is deliberately an in-memory implementation detail:
the records below are never serialized as operator authority and retain no
content or workspace capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._validation import thaw_json
from .refs import canonical_json_bytes
from .run_snapshot import RunLedgerSnapshot


_EVALUATION_PLAN_SCHEMA = "optpilot.candidate-evaluation-plan.v1"


def evaluation_plan_digest(coordinates: list[dict[str, Any]]) -> str:
    payload = {
        "schema": _EVALUATION_PLAN_SCHEMA,
        "coordinates": coordinates,
    }
    digest = hashlib.sha256(
        b"optpilot/candidate-evaluation-plan/v1\0" + canonical_json_bytes(payload)
    ).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class CandidateEvaluationCoordinate:
    """One budget-ordered logical-trial coordinate at a run head."""

    budget_slot: int
    logical_trial_id: str
    seed: Any
    repetition_index: int
    state: str
    terminal_outcome: str | None
    attempt: Any | None
    observation: Any | None
    result_reason: str | None


@dataclass(frozen=True)
class CandidateEvaluationEvidence:
    """Resolved logical-trial evidence for one canonical candidate."""

    candidate: Any
    coordinates: tuple[CandidateEvaluationCoordinate, ...]
    plan_digest: str
    active: int
    terminal: int
    successful: int
    total_attempts: int
    retries: int


@dataclass(frozen=True)
class CandidateEvaluationEvidenceIndex:
    """Exact-head candidate evidence index used only by pure read models."""

    run_id: str
    revision: int
    sequence: int
    _evidence: Mapping[str, CandidateEvaluationEvidence]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_evidence", MappingProxyType(dict(self._evidence)))

    def for_candidate_key(self, candidate_key: str) -> CandidateEvaluationEvidence:
        try:
            return self._evidence[candidate_key]
        except KeyError as error:
            raise KeyError(f"Unknown candidate key: {candidate_key!r}.") from error

    def matches_snapshot(self, snapshot: RunLedgerSnapshot) -> bool:
        return (
            self.run_id == snapshot.run.run_id
            and self.revision == snapshot.revision.revision
            and self.sequence == snapshot.revision.last_sequence
        )

    @classmethod
    def from_snapshot(
        cls, snapshot: RunLedgerSnapshot
    ) -> "CandidateEvaluationEvidenceIndex":
        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")

        template = snapshot.evaluation_closure.evaluation_template
        trials_by_candidate: dict[str, list[Any]] = {
            candidate.candidate_key: [] for candidate in snapshot.candidates
        }
        for trial in snapshot.logical_trials:
            trials_by_candidate[trial.candidate_key].append(trial)
        for trials in trials_by_candidate.values():
            trials.sort(key=lambda trial: trial.budget_slot)

        logical_head = {
            transition.logical_trial_id: transition
            for transition in snapshot.logical_transitions
        }
        attempts_by_id = {attempt.attempt_id: attempt for attempt in snapshot.attempts}
        observations_by_attempt = {
            observation.attempt_id: observation for observation in snapshot.observations
        }
        attempts_by_trial: dict[str, list[Any]] = {
            trial.admission.logical_trial_id: [] for trial in snapshot.logical_trials
        }
        for attempt in snapshot.attempts:
            attempts_by_trial[attempt.logical_trial_id].append(attempt)

        evidence_by_key: dict[str, CandidateEvaluationEvidence] = {}
        for candidate in snapshot.candidates:
            coordinates: list[CandidateEvaluationCoordinate] = []
            digest_coordinates: list[dict[str, Any]] = []
            total_attempts = 0
            retries = 0
            active = 0
            successful = 0
            trials = trials_by_candidate[candidate.candidate_key]
            for trial in trials:
                trial_id = trial.admission.logical_trial_id
                seed = trial.admission.seed
                if seed is None:
                    seed = template.default_seed
                digest_coordinates.append(
                    {
                        "seed": thaw_json(seed),
                        "repetition_index": trial.admission.repetition_index,
                    }
                )
                trial_attempts = attempts_by_trial[trial_id]
                total_attempts += len(trial_attempts)
                retries += max(0, len(trial_attempts) - 1)
                head = logical_head[trial_id]
                attempt = None
                observation = None
                reason = None
                if head.to_state != "terminal":
                    active += 1
                else:
                    if head.outcome == "success":
                        successful += 1
                    if head.attempt_id is not None:
                        attempt = attempts_by_id.get(head.attempt_id)
                        if attempt is not None:
                            observation = observations_by_attempt.get(
                                attempt.attempt_id
                            )
                    if head.outcome != "success":
                        reason = "terminal_result_not_successful"
                    elif (
                        attempt is None
                        or attempt.logical_trial_id != trial_id
                        or attempt.state != "terminal"
                        or attempt.outcome != "success"
                    ):
                        reason = "terminal_result_not_successful"
                    elif observation is None:
                        reason = "terminal_observation_missing"
                    elif observation.status != "success":
                        reason = "terminal_observation_not_successful"
                coordinates.append(
                    CandidateEvaluationCoordinate(
                        budget_slot=trial.budget_slot,
                        logical_trial_id=trial_id,
                        seed=seed,
                        repetition_index=trial.admission.repetition_index,
                        state=trial.state,
                        terminal_outcome=head.outcome,
                        attempt=attempt,
                        observation=observation,
                        result_reason=reason,
                    )
                )
            evidence_by_key[candidate.candidate_key] = CandidateEvaluationEvidence(
                candidate=candidate,
                coordinates=tuple(coordinates),
                plan_digest=evaluation_plan_digest(digest_coordinates),
                active=active,
                terminal=len(trials) - active,
                successful=successful,
                total_attempts=total_attempts,
                retries=retries,
            )
        return cls(
            run_id=snapshot.run.run_id,
            revision=snapshot.revision.revision,
            sequence=snapshot.revision.last_sequence,
            _evidence=evidence_by_key,
        )


__all__ = [
    "CandidateEvaluationCoordinate",
    "CandidateEvaluationEvidence",
    "CandidateEvaluationEvidenceIndex",
    "evaluation_plan_digest",
]
