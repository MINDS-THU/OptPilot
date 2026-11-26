"""Pure method-facing projections over one canonical run snapshot.

The RunLedger is the authority for a retained method exchange.  This module
contains the deterministic read side of that boundary:

* build the bounded state/evidence captured before ``propose``;
* select the exact full round of terminal transitions captured before
  ``observe``; and
* project those transitions into the deliberately narrow public observation
  DTO seen by a method.

No function here opens content, realizes a projection, names a workspace, or
talks to a worker.  In particular, replay uses the persisted exchange input
rather than recomputing what the method was originally shown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .attempts import AttemptFinalization
from .method_protocol_limits import MAX_DURABLE_METHOD_BYTES
from .realm._validation import positive_int, thaw_json
from .realm.method_exchange_records import (
    MethodObservationExchangeInput,
    MethodObservationPayload,
    MethodProposalExchangeInput,
    MethodTerminalTransitionRef,
    RunMethodExchangePreparationRecord,
)
from .realm.refs import canonical_json_bytes
from .realm.run_attempt_records import RunArtifactRecord, RunObservationRecord
from .realm.run_projection import RunSummaryProjection
from .realm.run_records import LogicalTrialTransitionRecord
from .realm.run_snapshot import RunLedgerSnapshot


MAX_METHOD_OBSERVATION_PAYLOAD_BYTES = MAX_DURABLE_METHOD_BYTES
_FAILURE_OUTCOMES = frozenset(
    {"invalid", "failed", "timeout", "partial", "cancelled"}
)
_MAX_PUBLIC_TEXT_BYTES = 4096
_MAX_DETAIL_DEPTH = 6
_MAX_DETAIL_NODES = 512
_UNSAFE_DETAIL_KEYS = frozenset(
    {
        "argv",
        "backend",
        "binding",
        "command",
        "cwd",
        "directory",
        "environment",
        "host",
        "lease",
        "path",
        "process",
        "socket",
        "stack",
        "token",
        "traceback",
        "worker",
    }
)
_FILE_URI = re.compile(r"file://[^\s'\"<>]+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"<>]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9_:])/(?:[^\s/'\"<>]+/)*[^\s'\"<>]*")


class MethodProjectionError(ValueError):
    """Canonical facts cannot form the requested method-facing projection."""


# The durable record is the one public DTO.  Keep the descriptive projection
# alias for callers of this read module without creating a second wire shape.
MethodObservationProjection = MethodObservationPayload


def build_method_study_state(snapshot: RunLedgerSnapshot) -> dict[str, Any]:
    """Return the stable, path-free state supplied to a proposal callback."""

    _snapshot(snapshot)
    summary = RunSummaryProjection.from_snapshot(snapshot)
    candidate_contract = (
        snapshot.definition.evaluation_closure.environment_revision.candidate_contract
    )
    raw_context = candidate_contract.get("context", {})
    if not isinstance(raw_context, Mapping):
        raise MethodProjectionError("The retained candidate context is malformed.")
    return {
        "accepted_trials": summary.accepted_logical_trials,
        "completed_trials": summary.terminal_logical_trials,
        "failure_count": summary.final_logical_failures,
        "attempt_count": summary.attempt_count,
        "observation_count": summary.observation_count,
        "best_metric": summary.best_metric,
        "best_trial_id": summary.best_logical_trial_id,
        "best_candidate_id": summary.best_candidate_id,
        "candidate_context": thaw_json(raw_context),
    }


def build_method_evidence_context(
    snapshot: RunLedgerSnapshot,
    *,
    recent_failure_limit: int = 5,
    recent_artifact_limit: int = 10,
) -> dict[str, Any]:
    """Return a compact method-visible decision context from canonical facts."""

    _snapshot(snapshot)
    _nonnegative_limit(recent_failure_limit, "recent failure limit")
    _nonnegative_limit(recent_artifact_limit, "recent artifact limit")
    summary = RunSummaryProjection.from_snapshot(snapshot)

    terminal = sorted(
        (
            value
            for value in snapshot.logical_transitions
            if value.to_state == "terminal" and value.outcome in _FAILURE_OUTCOMES
        ),
        key=lambda value: value.sequence,
    )
    selected_failures = (
        terminal[-recent_failure_limit:] if recent_failure_limit else []
    )
    recent_failures = []
    for transition in selected_failures:
        projected = _project_terminal_transition(snapshot, transition)
        recent_failures.append(
            {
                "logical_trial_id": projected.logical_trial_id,
                "candidate_id": projected.candidate_id,
                "status": projected.status,
                "error": thaw_json(projected.error),
            }
        )

    method_artifacts = [
        _project_artifact(value)
        for value in reversed(snapshot.artifacts)
        if value.visibility == "method"
    ]
    if recent_artifact_limit:
        method_artifacts = method_artifacts[:recent_artifact_limit]
    else:
        method_artifacts = []
    result = {
        "summary": summary.to_dict(),
        "recent_failure_count": len(recent_failures),
        "recent_failures": recent_failures,
        "recent_artifacts": method_artifacts,
    }
    # MethodProposalExchangeInput applies the durable 512 KiB checkpoint bound.
    # Validate here as well so callers that use this projection directly see a
    # local, descriptive failure.
    try:
        canonical_json_bytes(result)
    except (TypeError, ValueError) as error:
        raise MethodProjectionError("Method evidence is not canonical JSON.") from error
    return result


def build_method_proposal_exchange_input(
    snapshot: RunLedgerSnapshot,
    *,
    requested_width: int,
) -> MethodProposalExchangeInput:
    """Fix the exact input for the next proposal before invoking a method."""

    _snapshot(snapshot)
    positive_int(requested_width, "requested proposal width")
    if snapshot.run.state != "running":
        raise MethodProjectionError("A terminal run cannot prepare a proposal.")
    if snapshot.control.current_submission.state != "accepting":
        raise MethodProjectionError("A non-accepting run cannot prepare a proposal.")
    return MethodProposalExchangeInput(
        requested_width=requested_width,
        study_state=build_method_study_state(snapshot),
        evidence=build_method_evidence_context(snapshot),
    )


def build_method_observation_exchange_input(
    snapshot: RunLedgerSnapshot,
    *,
    round_index: int,
) -> MethodObservationExchangeInput:
    """Select the exact admitted round once every logical trial is terminal."""

    _snapshot(snapshot)
    positive_int(round_index, "method round index")
    proposal = next(
        (
            value
            for value in snapshot.method_exchange_completions
            if value.round_index == round_index and value.kind == "proposal"
        ),
        None,
    )
    if proposal is None or proposal.outcome != "admitted":
        raise MethodProjectionError(
            "Method observation requires an admitted proposal completion."
        )
    if any(
        value.round_index == round_index and value.kind == "observation"
        for value in snapshot.method_exchange_preparations
    ):
        raise MethodProjectionError("The method observation round is already prepared.")

    terminal_by_trial = {
        value.logical_trial_id: value
        for value in snapshot.logical_transitions
        if value.to_state == "terminal"
    }
    missing = [
        trial_id
        for trial_id in proposal.logical_trial_ids
        if trial_id not in terminal_by_trial
    ]
    if missing:
        raise MethodProjectionError(
            "The admitted proposal still has nonterminal logical trials."
        )
    transitions = tuple(
        MethodTerminalTransitionRef(terminal_by_trial[trial_id])
        for trial_id in proposal.logical_trial_ids
    )
    observations = tuple(
        _project_terminal_transition(snapshot, reference.transition)
        for reference in transitions
    )
    try:
        return MethodObservationExchangeInput(transitions, observations)
    except (TypeError, ValueError) as error:
        raise MethodProjectionError(
            "The exact method observation checkpoint is too large or malformed."
        ) from error


def proposal_worker_payload(
    preparation: RunMethodExchangePreparationRecord,
) -> dict[str, Any]:
    """Rebuild a proposal request payload from its persisted checkpoint."""

    if not isinstance(preparation, RunMethodExchangePreparationRecord):
        raise TypeError("preparation must be a method exchange preparation.")
    exchange_input = preparation.exchange_input
    if preparation.kind != "proposal" or not isinstance(
        exchange_input, MethodProposalExchangeInput
    ):
        raise MethodProjectionError("The preparation is not a proposal exchange.")
    return {
        "n_candidates": exchange_input.requested_width,
        "study_state": thaw_json(exchange_input.study_state),
        "evidence": thaw_json(exchange_input.evidence),
    }


def observation_worker_payload(
    snapshot: RunLedgerSnapshot,
    preparation: RunMethodExchangePreparationRecord,
) -> dict[str, Any]:
    """Rebuild an observe payload from its ordered durable checkpoint."""

    _snapshot(snapshot)
    if not isinstance(preparation, RunMethodExchangePreparationRecord):
        raise TypeError("preparation must be a method exchange preparation.")
    exchange_input = preparation.exchange_input
    if preparation.kind != "observation" or not isinstance(
        exchange_input, MethodObservationExchangeInput
    ):
        raise MethodProjectionError("The preparation is not an observation exchange.")
    # The exact filtered DTOs are part of the preparation itself.  Replaying
    # later must not run a possibly upgraded projection implementation merely
    # to rediscover what the original callback saw.
    _validate_observation_transition_heads(snapshot, exchange_input)
    result = exchange_input.worker_request
    if len(canonical_json_bytes(result)) > MAX_METHOD_OBSERVATION_PAYLOAD_BYTES:
        raise MethodProjectionError("The method observation payload is too large.")
    return result


def project_method_observations(
    snapshot: RunLedgerSnapshot,
    exchange_input: MethodObservationExchangeInput,
) -> tuple[MethodObservationProjection, ...]:
    """Project one persisted full-round checkpoint in its exact stored order."""

    _snapshot(snapshot)
    if not isinstance(exchange_input, MethodObservationExchangeInput):
        raise TypeError("exchange_input must be a method observation exchange input.")
    _validate_observation_transition_heads(snapshot, exchange_input)
    encoded = canonical_json_bytes(
        [value.to_dict() for value in exchange_input.observations]
    )
    if len(encoded) > MAX_METHOD_OBSERVATION_PAYLOAD_BYTES:
        raise MethodProjectionError("The method observation payload is too large.")
    return exchange_input.observations


def _validate_observation_transition_heads(
    snapshot: RunLedgerSnapshot,
    exchange_input: MethodObservationExchangeInput,
) -> None:
    terminal_by_trial = {
        value.logical_trial_id: value
        for value in snapshot.logical_transitions
        if value.to_state == "terminal"
    }
    for reference in exchange_input.terminal_transitions:
        current = terminal_by_trial.get(reference.logical_trial_id)
        if current != reference.transition:
            raise MethodProjectionError(
                "The method observation checkpoint differs from the canonical trial head."
            )


def _project_terminal_transition(
    snapshot: RunLedgerSnapshot,
    transition: LogicalTrialTransitionRecord,
) -> MethodObservationPayload:
    if transition.to_state != "terminal" or transition.outcome is None:
        raise MethodProjectionError("Method observations require terminal transitions.")
    trial = next(
        (
            value
            for value in snapshot.logical_trials
            if value.admission.logical_trial_id == transition.logical_trial_id
        ),
        None,
    )
    if trial is None:
        raise MethodProjectionError("The terminal transition has no logical trial.")
    candidate = next(
        (value for value in snapshot.candidates if value.candidate_key == trial.candidate_key),
        None,
    )
    if candidate is None or candidate.candidate_id != trial.admission.candidate_id:
        raise MethodProjectionError("The logical trial has no canonical candidate.")

    observation = _observation_for_transition(snapshot, transition)
    artifacts = tuple(
        _project_artifact(value)
        for value in snapshot.artifacts
        if transition.attempt_id is not None
        and value.attempt_id == transition.attempt_id
        and value.visibility == "method"
    )
    if observation is None:
        metric_values: Mapping[str, Any] = MappingProxyType({})
        constraint_results: Mapping[str, Any] = MappingProxyType({})
        resource_usage: Mapping[str, Any] = MappingProxyType({})
    else:
        metric_values = observation.envelope.metric_values
        constraint_results = observation.envelope.constraint_results
        resource_usage = MappingProxyType(
            {"wall_clock_seconds": observation.envelope.wall_clock_seconds}
        )
    return MethodObservationPayload(
        logical_trial_id=transition.logical_trial_id,
        candidate_id=candidate.candidate_id,
        status=transition.outcome,
        metric_values=metric_values,
        constraint_results=constraint_results,
        resource_usage=resource_usage,
        artifacts=artifacts,
        error=_terminal_error(snapshot, transition, observation),
    )


def _observation_for_transition(
    snapshot: RunLedgerSnapshot,
    transition: LogicalTrialTransitionRecord,
) -> RunObservationRecord | None:
    if transition.attempt_id is None:
        return None
    return next(
        (
            value
            for value in snapshot.observations
            if value.attempt_id == transition.attempt_id
        ),
        None,
    )


def _project_artifact(artifact: RunArtifactRecord) -> dict[str, Any]:
    declaration = artifact.declaration
    return {
        "artifact_id": artifact.artifact_id,
        "declaration_id": declaration.declaration_id,
        "name": declaration.name,
        "kind": declaration.kind,
        "media_type": declaration.media_type,
        "content_ref": str(artifact.content_ref),
        "size_bytes": artifact.size_bytes,
    }


def _terminal_error(
    snapshot: RunLedgerSnapshot,
    transition: LogicalTrialTransitionRecord,
    observation: RunObservationRecord | None,
) -> dict[str, Any] | None:
    if transition.outcome == "success":
        return None

    source: Mapping[str, Any] = MappingProxyType({})
    if observation is not None:
        source = observation.envelope.error
    elif transition.attempt_id is not None:
        head = next(
            (
                value
                for value in reversed(snapshot.attempt_transitions)
                if value.attempt_id == transition.attempt_id
                and value.to_state == "terminal"
            ),
            None,
        )
        if head is not None:
            finalization_payload = head.payload.get("finalization")
            if isinstance(finalization_payload, Mapping):
                try:
                    finalization = AttemptFinalization.from_dict(finalization_payload)
                except (KeyError, TypeError, ValueError):
                    finalization = None
                if finalization is not None and finalization.platform_error is not None:
                    source = finalization.platform_error

    if transition.attempt_id is None:
        phase = "run"
        fallback_message = "The logical trial was cancelled before evaluation."
    elif observation is None:
        phase = "execution"
        fallback_message = "The logical trial ended before environment evaluation."
    elif observation.envelope.outcome != transition.outcome:
        phase = "finalization"
        fallback_message = "The environment result was downgraded during finalization."
    else:
        phase = observation.envelope.phase
        fallback_message = f"The logical trial ended with status {transition.outcome}."

    result: dict[str, Any] = {
        "phase": _public_text(source.get("phase", phase)),
        "code": _public_text(source.get("code", transition.code or transition.outcome)),
        "message": _public_text(source.get("message", fallback_message)),
    }
    error_type = source.get("type")
    if error_type is not None:
        result["type"] = _public_text(error_type)
    details = source.get("details")
    if isinstance(details, Mapping):
        sanitized = _sanitize_details(details)
        if sanitized:
            result["details"] = sanitized
    return result


def _sanitize_details(value: Mapping[str, Any]) -> dict[str, Any]:
    nodes = 0

    def sanitize(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_DETAIL_NODES or depth > _MAX_DETAIL_DEPTH:
            return "[truncated]"
        if current is None or isinstance(current, (bool, int, float)):
            return current
        if isinstance(current, str):
            return _public_text(current)
        if isinstance(current, Mapping):
            result = {}
            for key, child in current.items():
                if not isinstance(key, str):
                    continue
                normalized = key.replace("-", "_").lower()
                if any(token in normalized for token in _UNSAFE_DETAIL_KEYS):
                    continue
                result[_public_text(key, max_bytes=256)] = sanitize(child, depth + 1)
            return result
        if isinstance(current, (list, tuple)):
            return [sanitize(child, depth + 1) for child in current[:256]]
        return _public_text(type(current).__name__)

    result = sanitize(value, 0)
    return result if isinstance(result, dict) else {}


def _public_text(value: Any, *, max_bytes: int = _MAX_PUBLIC_TEXT_BYTES) -> str:
    text = str(value)
    text = _FILE_URI.sub("[redacted-path]", text)
    text = _WINDOWS_PATH.sub("[redacted-path]", text)
    text = _POSIX_PATH.sub("[redacted-path]", text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    suffix = b"..."
    return (encoded[: max_bytes - len(suffix)] + suffix).decode(
        "utf-8", errors="ignore"
    )


def _snapshot(value: RunLedgerSnapshot) -> RunLedgerSnapshot:
    if not isinstance(value, RunLedgerSnapshot):
        raise TypeError("snapshot must be a RunLedgerSnapshot.")
    return value


def _nonnegative_limit(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


__all__ = [
    "MAX_METHOD_OBSERVATION_PAYLOAD_BYTES",
    "MethodObservationProjection",
    "MethodProjectionError",
    "build_method_evidence_context",
    "build_method_observation_exchange_input",
    "build_method_proposal_exchange_input",
    "build_method_study_state",
    "observation_worker_payload",
    "project_method_observations",
    "proposal_worker_payload",
]
