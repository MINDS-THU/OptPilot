"""Bounded presentation read model for the generic Run Workbench.

The first implementation deliberately derives its indexes from one complete
``RunLedgerSnapshot``.  That is an internal implementation limitation, not the
public API: callers receive only bounded pages anchored to one canonical run
head.  A later ledger-native query implementation can preserve this response
shape without turning these presentation selections into persisted authority.

Every row and correlation uses the same exact-head selection shape.  Selection
digests are stable identifiers for UI context and future action requests, but a
mutation provider must resolve and revalidate them against canonical authority;
the read model itself grants no content or execution capability.
"""

from __future__ import annotations

import base64
import heapq
import hashlib
import json
import math
import re
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ._validation import freeze_json, positive_int, required_text, thaw_json
from .run_candidate_results import (
    RUN_CANDIDATE_RESULT_ORDER,
    RUN_CANDIDATE_RESULT_SCHEMA,
    CandidateResultIndex,
)
from .run_projection import RunSummaryProjection
from .run_snapshot import RunLedgerSnapshot


RUN_WORKBENCH_PAGE_SCHEMA = "optpilot.run-workbench-page.v2"
RUN_WORKBENCH_SELECTION_SCHEMA = "optpilot.run-workbench-selection.v1"
RUN_WORKBENCH_PAGE_TOKEN_SCHEMA = "optpilot.run-workbench-page-token.v2"

RUN_WORKBENCH_KINDS = (
    "candidate",
    "logical_trial",
    "attempt",
    "observation",
    "artifact",
)

RUN_WORKBENCH_ACTIONS = (
    "select",
    "inspect",
    "debug_run",
    "environment_preview",
    "open_read_only",
    "keep_editable",
    "evaluate_child_run",
    "compare",
    "ask_assistant",
)

RUN_WORKBENCH_DEFAULT_PAGE_SIZE = 50
RUN_WORKBENCH_MAX_PAGE_SIZE = 100
RUN_WORKBENCH_MAX_CORRELATIONS = 4
RUN_WORKBENCH_MAX_TEXT_BYTES = 512
RUN_WORKBENCH_MAX_OBSERVATION_METRICS = 16
RUN_WORKBENCH_MAX_OBSERVATION_CONSTRAINTS = 16
RUN_WORKBENCH_MAX_MEASUREMENT_VALUE_BYTES = 128

_CANONICAL_ENTITY_ORDER = "canonical-entity-order.v1"
_ORDER_BY_KIND = MappingProxyType(
    {
        "candidate": RUN_CANDIDATE_RESULT_ORDER,
        "logical_trial": _CANONICAL_ENTITY_ORDER,
        "attempt": _CANONICAL_ENTITY_ORDER,
        "observation": _CANONICAL_ENTITY_ORDER,
        "artifact": _CANONICAL_ENTITY_ORDER,
    }
)

_UNSUPPORTED_ACTION_REASONS = MappingProxyType(
    {
        "inspect": "inspection_projection_provider_unavailable",
        "debug_run": "debug_run_provider_unavailable",
        "environment_preview": "contextual_interface_provider_unavailable",
        "open_read_only": "selection_view_provider_unavailable",
        "keep_editable": "editable_derivation_provider_unavailable",
        "evaluate_child_run": "child_run_provider_unavailable",
        "compare": "comparison_provider_unavailable",
        "ask_assistant": "assistant_selection_provider_unavailable",
    }
)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _bounded_measurement_name(value: str) -> tuple[str, bool]:
    return _bounded_text(value)


def _bounded_observation_metric_value(value: Any) -> int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > RUN_WORKBENCH_MAX_MEASUREMENT_VALUE_BYTES * 4:
            return None
        try:
            encoded = str(value).encode("ascii")
        except (UnicodeError, ValueError):
            return None
        return (
            value
            if len(encoded) <= RUN_WORKBENCH_MAX_MEASUREMENT_VALUE_BYTES
            else None
        )
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _bounded_observation_metrics(values: Mapping[str, Any]) -> dict[str, Any]:
    selected = heapq.nsmallest(
        RUN_WORKBENCH_MAX_OBSERVATION_METRICS,
        values,
        key=lambda value: value.encode("utf-8"),
    )
    rows = []
    for name in selected:
        display_name, truncated = _bounded_measurement_name(name)
        public_value = _bounded_observation_metric_value(values[name])
        rows.append(
            {
                "name": display_name,
                "name_truncated": truncated,
                "value": public_value,
                "supported": public_value is not None,
                "reason": (
                    None
                    if public_value is not None
                    else "metric_result_not_finite_number_or_boolean"
                ),
            }
        )
    omitted = len(values) - len(selected)
    return {
        "total": len(values),
        "returned": len(rows),
        "omitted": omitted,
        "truncated": omitted > 0,
        "rows": rows,
    }


def _bounded_observation_constraints(values: Mapping[str, Any]) -> dict[str, Any]:
    selected = heapq.nsmallest(
        RUN_WORKBENCH_MAX_OBSERVATION_CONSTRAINTS,
        values,
        key=lambda value: value.encode("utf-8"),
    )
    rows = []
    for name in selected:
        display_name, truncated = _bounded_measurement_name(name)
        value = values[name]
        supported = isinstance(value, bool)
        rows.append(
            {
                "name": display_name,
                "name_truncated": truncated,
                "value": value if supported else None,
                "supported": supported,
                "reason": None if supported else "constraint_result_not_boolean",
            }
        )
    omitted = len(values) - len(selected)
    return {
        "semantics": "boolean_satisfied",
        "total": len(values),
        "returned": len(rows),
        "omitted": omitted,
        "truncated": omitted > 0,
        "rows": rows,
    }


def _selection(
    *,
    run_id: str,
    revision: int,
    sequence: int,
    kind: str,
    entity_id: str,
) -> dict[str, Any]:
    if kind not in RUN_WORKBENCH_KINDS:
        raise ValueError(f"Unsupported workbench selection kind: {kind!r}.")
    identity = {
        "schema": RUN_WORKBENCH_SELECTION_SCHEMA,
        "run_id": run_id,
        "revision": revision,
        "sequence": sequence,
        "kind": kind,
        "entity_id": entity_id,
    }
    digest = hashlib.sha256(
        b"optpilot/run-workbench-selection/v1\0" + _canonical_json_bytes(identity)
    ).hexdigest()
    return {"selection_id": f"sha256:{digest}", **identity}


def validate_run_workbench_selection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one exact-head presentation selection without authorizing it.

    Workbench selection ids are integrity checks over presentation coordinates,
    not bearer credentials.  Callers must still resolve the returned coordinates
    through ``RealmLedger`` before deriving bytes or starting an action.
    """

    expected_fields = {
        "selection_id",
        "schema",
        "run_id",
        "revision",
        "sequence",
        "kind",
        "entity_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("Workbench selection fields are invalid.")
    if value["schema"] != RUN_WORKBENCH_SELECTION_SCHEMA:
        raise ValueError("Workbench selection schema is unsupported.")
    run_id = value["run_id"]
    kind = value["kind"]
    entity_id = value["entity_id"]
    revision = value["revision"]
    sequence = value["sequence"]
    required_text(run_id, "Workbench selection run id", max_bytes=512)
    required_text(kind, "Workbench selection kind", max_bytes=128)
    required_text(entity_id, "Workbench selection entity id", max_bytes=512)
    positive_int(revision, "Workbench selection revision")
    positive_int(sequence, "Workbench selection sequence")
    expected = _selection(
        run_id=run_id,
        revision=revision,
        sequence=sequence,
        kind=kind,
        entity_id=entity_id,
    )
    if dict(value) != expected:
        raise ValueError("Workbench selection integrity check failed.")
    return expected


def run_workbench_action_capabilities() -> list[dict[str, Any]]:
    """Return fresh, truthful capability rows for generic Workbench actions."""

    result = []
    for action in RUN_WORKBENCH_ACTIONS:
        supported = action == "select"
        result.append(
            {
                "action": action,
                "supported": supported,
                "eligible": supported,
                "reason": None if supported else _UNSUPPORTED_ACTION_REASONS[action],
            }
        )
    return result


def _action_eligibility() -> list[dict[str, Any]]:
    result = []
    for capability in run_workbench_action_capabilities():
        result.append(
            {
                "action": capability["action"],
                "supported": capability["supported"],
                "eligible": capability["eligible"],
                "reason": capability["reason"],
            }
        )
    return result


def _correlation(
    *,
    relation: str,
    run_id: str,
    revision: int,
    sequence: int,
    kind: str,
    entity_id: str,
) -> dict[str, Any]:
    return {
        "relation": relation,
        "selection": _selection(
            run_id=run_id,
            revision=revision,
            sequence=sequence,
            kind=kind,
            entity_id=entity_id,
        ),
    }


def _row(
    *,
    run_id: str,
    revision: int,
    sequence: int,
    kind: str,
    entity_id: str,
    correlations: Sequence[Mapping[str, Any]],
    data: Mapping[str, Any],
) -> Mapping[str, Any]:
    if len(correlations) > RUN_WORKBENCH_MAX_CORRELATIONS:
        raise ValueError("Workbench row correlations exceed the fixed bound.")
    value = {
        "kind": kind,
        "id": entity_id,
        "selection": _selection(
            run_id=run_id,
            revision=revision,
            sequence=sequence,
            kind=kind,
            entity_id=entity_id,
        ),
        "correlations": list(correlations),
        "eligibility": _action_eligibility(),
        "data": dict(data),
    }
    frozen = freeze_json(value, label="run workbench row")
    if not isinstance(frozen, Mapping):  # Defensive: the root is always a mapping.
        raise TypeError("run workbench row must be a mapping.")
    return frozen


def _finite_objective_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _bounded_text(value: str | None) -> tuple[str | None, bool]:
    """Project arbitrary envelope/declaration text without an unbounded row."""

    if value is None:
        return None, False
    if not isinstance(value, str):
        raise TypeError("projected text must be a string or None.")
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= RUN_WORKBENCH_MAX_TEXT_BYTES:
        return value, False
    return (
        encoded[:RUN_WORKBENCH_MAX_TEXT_BYTES].decode("utf-8", errors="ignore"),
        True,
    )


_TRACEBACK_FRAME = re.compile(r'^\s*File "')
_SOURCE_ANCHOR = re.compile(r"[\s^~]+")

# One ordered pass over host-identifying text.  The `keep` branches are tried
# first and pass through untouched: a runtime-scope label is already logical,
# and an endpoint URL is usually the whole diagnosis.  Everything in `drop`
# names this machine.  A POSIX path may contain spaces (`Application Support`
# is on the default macOS realm root), so it absorbs a space only when a later
# separator proves the space is inside the path rather than the end of it.
_SCRUBBED = re.compile(
    r"""
      (?P<keep>
          \[runtime-scope-\d+\][^\s"'<>]*
        | (?<![\w.-])(?!file:)[A-Za-z][A-Za-z0-9+.-]*://(?![^\s"'<>/]*@)[^\s"'<>]*
      )
    | (?P<drop>
          [A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>/]*@[^\s"'<>]*
        | file://[^\s"'<>]*
        | (?<!\w)[A-Za-z]:[\\/][^\s"'<>]*
        | \\\\[^\s"'<>]+
        | (?<![\]:])/(?:[^\s"'<>]|[ ](?=[^\s"'<>]*/)){6,}
      )
    """,
    re.VERBOSE,
)

# Scrubbing and bounding happen on the tail, so only a small multiple of the
# row bound is ever scanned by the regex; a captured stderr can be megabytes.
_DIAGNOSTIC_SCAN_BYTES = RUN_WORKBENCH_MAX_TEXT_BYTES * 4


def _scrub_paths(value: str) -> str:
    return _SCRUBBED.sub(lambda match: match.group("keep") or "<path>", value)


def _bounded_tail_text(value: str) -> tuple[str, bool]:
    """Bound diagnostic text from the END, where its meaning lives.

    ``_bounded_text`` keeps the head, which is right for a label.  A failure
    reads the other way round: the frames come first and the exception line
    last, so head truncation returns the part that says nothing.
    """

    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= RUN_WORKBENCH_MAX_TEXT_BYTES:
        return value, False
    return (
        encoded[-RUN_WORKBENCH_MAX_TEXT_BYTES:].decode("utf-8", errors="ignore"),
        True,
    )


_EXCEPTION_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _diagnostic_type(error: Mapping[str, Any]) -> str | None:
    """Name the exception class an evaluator failed with, if it reported one.

    The whole error mapping is worker-supplied -- an adapter chooses its own
    ``event_summary`` -- so this half cannot be trusted either.  A real class
    name is a dotted identifier and so cannot contain a separator; anything
    else is prose from a misbehaving adapter and is scrubbed like prose.
    """

    if not isinstance(error, Mapping):
        return None
    value = error.get("type")
    if not isinstance(value, str) or not value:
        return None
    if _EXCEPTION_TYPE.match(value):
        return _bounded_text(value)[0]
    return _bounded_text(_scrub_paths(value))[0]


def _diagnostic_summary(error: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Project why an evaluation failed as bounded, host-path-free text.

    Traceback frame lines carry the host paths; the exception lines carry the
    meaning.  Dropping the frames makes path-freedom structural rather than a
    scrub that has to be right, and it spends the row bound on the diagnosis
    instead of on the stack that precedes it.
    """

    if not isinstance(error, Mapping):
        return None, False
    message = error.get("message")
    if not isinstance(message, str):
        return None, False
    kept: deque[str] = deque()
    kept_bytes = 0
    dropped = False
    for line in message.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or _TRACEBACK_FRAME.match(line)
            or _SOURCE_ANCHOR.fullmatch(stripped)
        ):
            continue
        kept.append(line)
        kept_bytes += len(line.encode("utf-8")) + 1
        while kept_bytes > _DIAGNOSTIC_SCAN_BYTES and len(kept) > 1:
            dropped = True
            kept_bytes -= len(kept.popleft().encode("utf-8")) + 1
    if not kept:
        return None, False
    text, truncated = _bounded_tail_text(_scrub_paths("\n".join(kept)))
    return text, truncated or dropped


def reduce_run_diagnostic(
    error: Mapping[str, Any],
) -> tuple[str | None, str | None, bool]:
    """Reduce one worker-reported error to (type, summary, truncated).

    The same reduction a failed evaluation gets, exposed for the method side:
    a method's failure is recorded through a different path but has to obey
    the same two rules -- bounded, and free of host paths.
    """

    summary, truncated = _diagnostic_summary(error)
    return _diagnostic_type(error), summary, truncated


def _encode_page_token(payload: Mapping[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(_canonical_json_bytes(payload))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_page_token(value: str) -> Mapping[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("page_token must be a non-empty bounded string.")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("page_token is malformed.") from error
    expected = {
        "schema",
        "run_id",
        "revision",
        "sequence",
        "kind",
        "order",
        "offset",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("page_token fields are invalid.")
    if payload["schema"] != RUN_WORKBENCH_PAGE_TOKEN_SCHEMA:
        raise ValueError("page_token schema is unsupported.")
    offset = payload["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("page_token offset is invalid.")
    return payload


@dataclass(frozen=True)
class RunWorkbenchReadModel:
    """Immutable indexes for bounded Workbench presentation queries."""

    summary: RunSummaryProjection
    candidate_result_summary: Mapping[str, Any]
    _rows: Mapping[str, tuple[Mapping[str, Any], ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.summary, RunSummaryProjection):
            raise TypeError("summary must be a RunSummaryProjection.")
        candidate_result_summary = freeze_json(
            self.candidate_result_summary,
            label="candidate result summary",
        )
        if not isinstance(candidate_result_summary, Mapping):
            raise TypeError("candidate_result_summary must be a mapping.")
        object.__setattr__(
            self, "candidate_result_summary", candidate_result_summary
        )
        if not isinstance(self._rows, Mapping) or set(self._rows) != set(
            RUN_WORKBENCH_KINDS
        ):
            raise ValueError("Workbench rows must contain every supported kind.")
        frozen_rows: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for kind in RUN_WORKBENCH_KINDS:
            rows = []
            for row in self._rows[kind]:
                if not isinstance(row, Mapping):
                    raise TypeError("Workbench rows must be mappings.")
                frozen = freeze_json(row, label="run workbench row")
                if not isinstance(frozen, Mapping):  # Defensive mapping root.
                    raise TypeError("Workbench rows must be mappings.")
                rows.append(frozen)
            frozen_rows[kind] = tuple(rows)
        object.__setattr__(self, "_rows", MappingProxyType(frozen_rows))

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunLedgerSnapshot,
        *,
        summary: RunSummaryProjection | None = None,
    ) -> "RunWorkbenchReadModel":
        """Derive bounded-query indexes from one immutable canonical snapshot.

        Supplying an already-derived summary avoids duplicate summary work, but
        it must describe the exact same run head.  No snapshot record is kept
        after construction; only compact immutable rows remain.
        """

        if not isinstance(snapshot, RunLedgerSnapshot):
            raise TypeError("snapshot must be a RunLedgerSnapshot.")
        selected_summary = (
            RunSummaryProjection.from_snapshot(snapshot)
            if summary is None
            else summary
        )
        if not isinstance(selected_summary, RunSummaryProjection):
            raise TypeError("summary must be a RunSummaryProjection or None.")
        if (
            selected_summary.run_id != snapshot.run.run_id
            or selected_summary.cursor.revision != snapshot.revision.revision
            or selected_summary.cursor.sequence != snapshot.revision.last_sequence
        ):
            raise ValueError("summary and snapshot do not describe the same run head.")

        run_id = snapshot.run.run_id
        revision = snapshot.revision.revision
        sequence = snapshot.revision.last_sequence
        candidate_results = CandidateResultIndex.from_snapshot(snapshot)
        candidates_by_key = {
            candidate.candidate_key: candidate for candidate in snapshot.candidates
        }
        trials_by_id = {
            trial.admission.logical_trial_id: trial
            for trial in snapshot.logical_trials
        }
        attempts_by_id = {attempt.attempt_id: attempt for attempt in snapshot.attempts}
        trial_head = {}
        for transition in snapshot.logical_transitions:
            trial_head[transition.logical_trial_id] = transition
        observation_by_attempt = {
            observation.attempt_id: observation for observation in snapshot.observations
        }
        attempts_per_trial: dict[str, int] = {
            trial_id: 0 for trial_id in trials_by_id
        }
        for attempt in snapshot.attempts:
            attempts_per_trial[attempt.logical_trial_id] += 1
        trials_per_candidate: dict[str, int] = {
            candidate.candidate_key: 0 for candidate in snapshot.candidates
        }
        for trial in snapshot.logical_trials:
            trials_per_candidate[trial.candidate_key] += 1
        artifacts_per_attempt: dict[str, int] = {
            attempt_id: 0 for attempt_id in attempts_by_id
        }
        artifacts_per_observation: dict[str, int] = {
            observation.observation_id: 0 for observation in snapshot.observations
        }
        for artifact in snapshot.artifacts:
            artifacts_per_attempt[artifact.attempt_id] += 1
            if artifact.observation_id is not None:
                artifacts_per_observation[artifact.observation_id] += 1

        rows: dict[str, list[Mapping[str, Any]]] = {
            kind: [] for kind in RUN_WORKBENCH_KINDS
        }
        for candidate_key in candidate_results.ordered_candidate_keys:
            candidate = candidates_by_key[candidate_key]
            rows["candidate"].append(
                _row(
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="candidate",
                    entity_id=candidate.candidate_id,
                    correlations=(),
                    data={
                        "format": candidate.admission.envelope.candidate_format,
                        "logical_trial_count": trials_per_candidate[
                            candidate.candidate_key
                        ],
                        "accepted_sequence": candidate.accepted_sequence,
                        "created_at": candidate.created_at,
                        "result": candidate_results.for_candidate_key(
                            candidate.candidate_key
                        ),
                    },
                )
            )

        for trial in snapshot.logical_trials:
            trial_id = trial.admission.logical_trial_id
            candidate = candidates_by_key[trial.candidate_key]
            head = trial_head[trial_id]
            rows["logical_trial"].append(
                _row(
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="logical_trial",
                    entity_id=trial_id,
                    correlations=(
                        _correlation(
                            relation="candidate",
                            run_id=run_id,
                            revision=revision,
                            sequence=sequence,
                            kind="candidate",
                            entity_id=candidate.candidate_id,
                        ),
                    ),
                    data={
                        "candidate_id": candidate.candidate_id,
                        "budget_slot": trial.budget_slot,
                        "state": trial.state,
                        "outcome": head.outcome,
                        "code": head.code,
                        "terminal_attempt_id": (
                            head.attempt_id if head.to_state == "terminal" else None
                        ),
                        "attempt_count": attempts_per_trial[trial_id],
                        "accepted_sequence": trial.accepted_sequence,
                        "head_sequence": head.sequence,
                    },
                )
            )

        for attempt in snapshot.attempts:
            trial = trials_by_id[attempt.logical_trial_id]
            candidate = candidates_by_key[trial.candidate_key]
            observation = observation_by_attempt.get(attempt.attempt_id)
            attempt_error: Mapping[str, Any] = (
                {} if observation is None else observation.envelope.error
            )
            attempt_cause, attempt_cause_truncated = _diagnostic_summary(attempt_error)
            correlations = [
                _correlation(
                    relation="candidate",
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="candidate",
                    entity_id=candidate.candidate_id,
                ),
                _correlation(
                    relation="logical_trial",
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="logical_trial",
                    entity_id=attempt.logical_trial_id,
                ),
            ]
            if observation is not None:
                correlations.append(
                    _correlation(
                        relation="observation",
                        run_id=run_id,
                        revision=revision,
                        sequence=sequence,
                        kind="observation",
                        entity_id=observation.observation_id,
                    )
                )
            rows["attempt"].append(
                _row(
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="attempt",
                    entity_id=attempt.attempt_id,
                    correlations=correlations,
                    data={
                        "candidate_id": candidate.candidate_id,
                        "logical_trial_id": attempt.logical_trial_id,
                        "attempt_index": attempt.attempt_index,
                        "state": attempt.state,
                        "outcome": attempt.outcome,
                        "code": attempt.code,
                        "error_type": _diagnostic_type(attempt_error),
                        "error_summary": attempt_cause,
                        "error_summary_truncated": attempt_cause_truncated,
                        "head_transition_index": attempt.head_transition_index,
                        "observation_id": (
                            None if observation is None else observation.observation_id
                        ),
                        "artifact_count": artifacts_per_attempt[attempt.attempt_id],
                        "prepared_run_revision": attempt.prepared_run_revision,
                        "prepared_sequence": attempt.prepared_sequence,
                        "prepared_at": attempt.prepared_at,
                        "updated_at": attempt.updated_at,
                    },
                )
            )

        metric_name = selected_summary.objective_metric
        for observation in snapshot.observations:
            attempt = attempts_by_id[observation.attempt_id]
            trial = trials_by_id[attempt.logical_trial_id]
            candidate = candidates_by_key[trial.candidate_key]
            phase, phase_truncated = _bounded_text(observation.envelope.phase)
            cause, cause_truncated = _diagnostic_summary(observation.envelope.error)
            rows["observation"].append(
                _row(
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="observation",
                    entity_id=observation.observation_id,
                    correlations=(
                        _correlation(
                            relation="candidate",
                            run_id=run_id,
                            revision=revision,
                            sequence=sequence,
                            kind="candidate",
                            entity_id=candidate.candidate_id,
                        ),
                        _correlation(
                            relation="logical_trial",
                            run_id=run_id,
                            revision=revision,
                            sequence=sequence,
                            kind="logical_trial",
                            entity_id=attempt.logical_trial_id,
                        ),
                        _correlation(
                            relation="attempt",
                            run_id=run_id,
                            revision=revision,
                            sequence=sequence,
                            kind="attempt",
                            entity_id=attempt.attempt_id,
                        ),
                    ),
                    data={
                        "candidate_id": candidate.candidate_id,
                        "logical_trial_id": attempt.logical_trial_id,
                        "attempt_id": attempt.attempt_id,
                        "outcome": observation.status,
                        "phase": phase,
                        "phase_truncated": phase_truncated,
                        "error_type": _diagnostic_type(observation.envelope.error),
                        "error_summary": cause,
                        "error_summary_truncated": cause_truncated,
                        "wall_clock_seconds": observation.envelope.wall_clock_seconds,
                        "objective_metric": metric_name,
                        "objective_value": _finite_objective_value(
                            observation.envelope.metric_values.get(metric_name)
                        ),
                        "metric_count": len(observation.envelope.metric_values),
                        "metrics": _bounded_observation_metrics(
                            observation.envelope.metric_values
                        ),
                        "constraint_count": len(
                            observation.envelope.constraint_results
                        ),
                        "constraints": _bounded_observation_constraints(
                            observation.envelope.constraint_results
                        ),
                        "output_declaration_count": len(
                            observation.envelope.output_declarations
                        ),
                        "artifact_count": artifacts_per_observation[
                            observation.observation_id
                        ],
                        "adopted_run_revision": observation.adopted_run_revision,
                        "adopted_sequence": observation.adopted_sequence,
                        "created_at": observation.created_at,
                    },
                )
            )

        for artifact in snapshot.artifacts:
            attempt = attempts_by_id[artifact.attempt_id]
            trial = trials_by_id[attempt.logical_trial_id]
            candidate = candidates_by_key[trial.candidate_key]
            correlations = [
                _correlation(
                    relation="candidate",
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="candidate",
                    entity_id=candidate.candidate_id,
                ),
                _correlation(
                    relation="logical_trial",
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="logical_trial",
                    entity_id=attempt.logical_trial_id,
                ),
                _correlation(
                    relation="attempt",
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="attempt",
                    entity_id=attempt.attempt_id,
                ),
            ]
            if artifact.observation_id is not None:
                correlations.append(
                    _correlation(
                        relation="observation",
                        run_id=run_id,
                        revision=revision,
                        sequence=sequence,
                        kind="observation",
                        entity_id=artifact.observation_id,
                    )
                )
            declaration_id, declaration_id_truncated = _bounded_text(
                artifact.declaration_id
            )
            name, name_truncated = _bounded_text(artifact.declaration.name)
            path, path_truncated = _bounded_text(artifact.declaration.path)
            media_type, media_type_truncated = _bounded_text(
                artifact.declaration.media_type
            )
            rows["artifact"].append(
                _row(
                    run_id=run_id,
                    revision=revision,
                    sequence=sequence,
                    kind="artifact",
                    entity_id=artifact.artifact_id,
                    correlations=correlations,
                    data={
                        "candidate_id": candidate.candidate_id,
                        "logical_trial_id": attempt.logical_trial_id,
                        "attempt_id": attempt.attempt_id,
                        "observation_id": artifact.observation_id,
                        "declaration_id": declaration_id,
                        "name": name,
                        "path": path,
                        "kind": artifact.declaration.kind,
                        "media_type": media_type,
                        "presentation_text_truncated": any(
                            (
                                declaration_id_truncated,
                                name_truncated,
                                path_truncated,
                                media_type_truncated,
                            )
                        ),
                        "size_bytes": artifact.size_bytes,
                        "visibility": artifact.visibility,
                        "adopted_run_revision": artifact.adopted_run_revision,
                        "adopted_sequence": artifact.adopted_sequence,
                        "created_at": artifact.created_at,
                    },
                )
            )

        return cls(
            summary=selected_summary,
            candidate_result_summary=candidate_results.summary,
            _rows={kind: tuple(rows[kind]) for kind in RUN_WORKBENCH_KINDS},
        )

    def page(
        self,
        kind: str,
        *,
        page_token: str | None = None,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Return one JSON-compatible page at this model's exact run head."""

        if kind not in RUN_WORKBENCH_KINDS:
            raise ValueError(f"Unsupported workbench page kind: {kind!r}.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > RUN_WORKBENCH_MAX_PAGE_SIZE
        ):
            raise ValueError(
                f"limit must be between 1 and {RUN_WORKBENCH_MAX_PAGE_SIZE}."
            )
        offset = 0
        order = _ORDER_BY_KIND[kind]
        if page_token is not None:
            token = _decode_page_token(page_token)
            if (
                token["run_id"] != self.summary.run_id
                or token["revision"] != self.summary.cursor.revision
                or token["sequence"] != self.summary.cursor.sequence
                or token["kind"] != kind
                or token["order"] != order
            ):
                raise ValueError(
                    "page_token belongs to a different run head or kind or order."
                )
            offset = token["offset"]

        rows = self._rows[kind]
        if offset > len(rows):
            raise ValueError("page_token offset is beyond the available rows.")
        selected = rows[offset : offset + limit]
        next_offset = offset + len(selected)
        has_more = next_offset < len(rows)
        next_page_token = None
        if has_more:
            next_page_token = _encode_page_token(
                {
                    "schema": RUN_WORKBENCH_PAGE_TOKEN_SCHEMA,
                    "run_id": self.summary.run_id,
                    "revision": self.summary.cursor.revision,
                    "sequence": self.summary.cursor.sequence,
                    "kind": kind,
                    "order": order,
                    "offset": next_offset,
                }
            )

        ranked_groups = self.candidate_result_summary["counts"]["ranked_groups"]
        ranking_eligible = kind == "candidate" and ranked_groups > 0
        ranking_reason = (
            "candidate_page_required"
            if kind != "candidate"
            else None if ranking_eligible else "no_ranked_candidate_group"
        )
        result = {
            "schema": RUN_WORKBENCH_PAGE_SCHEMA,
            "run_id": self.summary.run_id,
            "head": self.summary.cursor.to_dict(),
            "summary": self.summary.to_dict(),
            "query": {"kind": kind, "limit": limit, "order": order},
            "items": [thaw_json(row) for row in selected],
            "page": {
                "count": len(selected),
                "has_more": has_more,
                "next_page_token": next_page_token,
            },
            "capabilities": {
                "selection_schema": RUN_WORKBENCH_SELECTION_SCHEMA,
                "selection_kinds": list(RUN_WORKBENCH_KINDS),
                "actions": run_workbench_action_capabilities(),
                "candidate_results": {
                    "supported": True,
                    "eligible": kind == "candidate",
                    "reason": None if kind == "candidate" else "candidate_page_required",
                    "schema": RUN_CANDIDATE_RESULT_SCHEMA,
                    "order": RUN_CANDIDATE_RESULT_ORDER,
                    "ranking": {
                        "supported": True,
                        "eligible": ranking_eligible,
                        "scope": "within_run_evaluation_plan",
                        "finality": self.candidate_result_summary["finality"],
                        "reason": ranking_reason,
                    },
                },
            },
            "limitations": {
                "bounded_public_page": True,
                "max_page_size": RUN_WORKBENCH_MAX_PAGE_SIZE,
                "max_correlations_per_item": RUN_WORKBENCH_MAX_CORRELATIONS,
                "max_projected_text_bytes": RUN_WORKBENCH_MAX_TEXT_BYTES,
                "internal_full_snapshot_materialization": True,
                "selection_authority": "presentation_only_revalidate_before_action",
                "live_event_delta_query": True,
                "timeline_query": "separate_actor_authorized_exact_head_page",
            },
        }
        if kind == "candidate":
            result["candidate_result_summary"] = thaw_json(
                self.candidate_result_summary
            )
        return result

    def contains_selection(self, value: Mapping[str, Any]) -> bool:
        """Return whether a validated presentation selection names this head.

        This is still not authorization.  It lets an action bridge reject a
        caller-created kind/entity pair before asking Realm authority to mint a
        real immutable selection.
        """

        return self.selection_row(value) is not None

    def entity_row(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        """Resolve one stable entity identity at this exact run head.

        Unlike :meth:`selection_row`, this lookup does not require a caller to
        have first seen and retained a presentation selection.  It exists for
        refresh-safe, read-only navigation such as a Run-local Candidate URL.
        The result is still bounded presentation data and grants no authority;
        every mutation must use and revalidate the returned exact-head
        selection in the ordinary action bridge.
        """

        if kind not in RUN_WORKBENCH_KINDS:
            raise ValueError(f"Unsupported workbench entity kind: {kind!r}.")
        entity_id = required_text(
            entity_id,
            "workbench entity id",
            max_bytes=512,
        )
        for row in self._rows[kind]:
            if row["id"] == entity_id:
                return thaw_json(row)
        return None

    def selection_row(
        self, value: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Resolve one exact-head presentation selection to its bounded row.

        This remains a read-model operation, not authorization.  The returned
        row is the same bounded projection exposed by :meth:`page`; callers
        must first perform their actor-authorized Realm read and must not use
        this result as a content or execution capability.
        """

        selection = validate_run_workbench_selection(value)
        if (
            selection["run_id"] != self.summary.run_id
            or selection["revision"] != self.summary.cursor.revision
            or selection["sequence"] != self.summary.cursor.sequence
        ):
            return None
        for row in self._rows[selection["kind"]]:
            if row["selection"] == selection:
                return thaw_json(row)
        return None


__all__ = [
    "RUN_WORKBENCH_ACTIONS",
    "RUN_WORKBENCH_DEFAULT_PAGE_SIZE",
    "RUN_WORKBENCH_KINDS",
    "RUN_WORKBENCH_MAX_CORRELATIONS",
    "RUN_WORKBENCH_MAX_MEASUREMENT_VALUE_BYTES",
    "RUN_WORKBENCH_MAX_OBSERVATION_CONSTRAINTS",
    "RUN_WORKBENCH_MAX_OBSERVATION_METRICS",
    "RUN_WORKBENCH_MAX_PAGE_SIZE",
    "RUN_WORKBENCH_MAX_TEXT_BYTES",
    "reduce_run_diagnostic",
    "RUN_WORKBENCH_PAGE_SCHEMA",
    "RUN_WORKBENCH_SELECTION_SCHEMA",
    "RunWorkbenchReadModel",
    "run_workbench_action_capabilities",
    "validate_run_workbench_selection",
]
