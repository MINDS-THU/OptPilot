"""Path-free checkpoints for retained batch-method exchanges.

The records in this module describe the durable boundary around a method
callback.  A preparation fixes the exact proposal input, or the exact ordered
terminal-transition evidence and filtered DTOs delivered to ``observe``.  A
completion records only bounded semantic results.  ``response_digest`` always
means SHA-256 of the full canonical worker response object, as implemented by
``method_worker_response_digest``.  Host paths, tracebacks, and process handles
do not belong in this stream.

Exchange identity is deliberately independent of the mutable run revision.
Controller replacement may therefore resume an at-least-once callback while
the ledger still fences completion against the current controller term.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Tuple, Union

from ..method_protocol_limits import (
    MAX_BATCH_EXCHANGE_ITEMS,
    MAX_DURABLE_METHOD_BYTES,
)
from ._validation import (
    finite_time,
    freeze_json,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .refs import canonical_json_bytes, request_digest
from .run_control_records import RunSubmissionControlReceipt
from .run_records import LogicalTrialTransitionRecord, RunAdmissionReceipt


JsonDict = dict[str, Any]
METHOD_EXCHANGE_KINDS = frozenset({"proposal", "observation"})
METHOD_PROPOSAL_OUTCOMES = frozenset(
    {"admitted", "empty", "method_failed", "protocol_error"}
)
METHOD_OBSERVATION_OUTCOMES = frozenset(
    {"acknowledged", "method_failed", "protocol_error"}
)
METHOD_OBSERVATION_OUTCOME = "acknowledged"
METHOD_OBSERVATION_ACK_RESULT_DIGEST = request_digest(
    {"outcome": METHOD_OBSERVATION_OUTCOME}
)
METHOD_OBSERVATION_METHOD_FAILED_RESULT_DIGEST = request_digest(
    {"outcome": "method_failed"}
)
METHOD_OBSERVATION_PROTOCOL_ERROR_RESULT_DIGEST = request_digest(
    {"outcome": "protocol_error"}
)
# Leave headroom for the operation envelope, controller coordinates, and the
# replay receipt under RealmLedger's one-megabyte request boundary.
_PROPOSAL_INPUT_SCHEMA = "optpilot.method-proposal-exchange-input.v1"
_OBSERVATION_INPUT_SCHEMA = "optpilot.method-observation-exchange-input.v2"
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_METHOD_OBSERVATION_STATUSES = frozenset(
    {"success", "invalid", "failed", "timeout", "partial", "cancelled"}
)


def method_exchange_id(*, run_id: str, round_index: int, kind: str) -> str:
    """Return the stable coordinate identity for one method exchange."""

    required_text(run_id, "run id", max_bytes=512)
    positive_int(round_index, "method round index")
    if kind not in METHOD_EXCHANGE_KINDS:
        raise ValueError("method exchange kind is unsupported.")
    payload = canonical_json_bytes(
        {"kind": kind, "round_index": round_index, "run_id": run_id}
    )
    return "method-exchange:sha256:" + hashlib.sha256(payload).hexdigest()


def method_exchange_sequence(*, round_index: int, kind: str) -> int:
    """Return the dense worker protocol sequence for one round/kind coordinate."""

    positive_int(round_index, "method round index")
    if kind not in METHOD_EXCHANGE_KINDS:
        raise ValueError("method exchange kind is unsupported.")
    return 2 * round_index - (1 if kind == "proposal" else 0)


def _bounded_json_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """Freeze one bounded opaque mapping without interpreting its strings.

    The surrounding typed record has no operational path/argv/environment
    fields.  Values inside method state and filtered evidence remain opaque
    domain JSON: a legitimate parameter or metric label may itself look like a
    POSIX or Windows path and must not be guessed to be a host capability.
    """

    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    frozen = freeze_json(value, label=label)
    if not isinstance(frozen, MappingProxyType):
        raise TypeError(f"{label} must be a mapping.")

    encoded = canonical_json_bytes(thaw_json(frozen))
    if len(encoded) > MAX_DURABLE_METHOD_BYTES:
        raise ValueError(f"{label} is too large.")
    return frozen


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def method_worker_response_digest(response: Any) -> str:
    """Hash the full bounded canonical JSON object returned by a method worker.

    This is the sole response-digest semantic for both proposal and observation
    callbacks.  It deliberately does not interpret path-looking domain strings.
    """

    frozen = freeze_json(response, label="method worker response")
    encoded = canonical_json_bytes(thaw_json(frozen))
    if len(encoded) > MAX_DURABLE_METHOD_BYTES:
        raise ValueError("method worker response is too large.")
    return hashlib.sha256(encoded).hexdigest()


def method_observation_result_digest(outcome: str) -> str:
    """Return the canonical semantic result digest for an observe completion."""

    try:
        return {
            "acknowledged": METHOD_OBSERVATION_ACK_RESULT_DIGEST,
            "method_failed": METHOD_OBSERVATION_METHOD_FAILED_RESULT_DIGEST,
            "protocol_error": METHOD_OBSERVATION_PROTOCOL_ERROR_RESULT_DIGEST,
        }[outcome]
    except (KeyError, TypeError) as error:
        raise ValueError("method observation outcome is unsupported.") from error


@dataclass(frozen=True)
class MethodProposalExchangeInput:
    """Exact bounded input to one retained batch ``propose`` callback."""

    requested_width: int
    study_state: Mapping[str, Any]
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        positive_int(self.requested_width, "requested proposal width")
        if self.requested_width > MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError("requested proposal width is too large.")
        object.__setattr__(
            self,
            "study_state",
            _bounded_json_mapping(self.study_state, "method proposal study state"),
        )
        object.__setattr__(
            self,
            "evidence",
            _bounded_json_mapping(self.evidence, "method proposal evidence"),
        )
        if len(self.canonical_bytes) > MAX_DURABLE_METHOD_BYTES:
            raise ValueError("method proposal exchange input is too large.")

    @property
    def kind(self) -> str:
        return "proposal"

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def to_dict(self) -> JsonDict:
        return {
            "schema": _PROPOSAL_INPUT_SCHEMA,
            "requested_width": self.requested_width,
            "study_state": thaw_json(self.study_state),
            "evidence": thaw_json(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MethodProposalExchangeInput":
        try:
            _exact_keys(
                payload,
                {"schema", "requested_width", "study_state", "evidence"},
                "method proposal exchange input",
            )
            if payload["schema"] != _PROPOSAL_INPUT_SCHEMA:
                raise ValueError("method proposal exchange input schema is unsupported.")
            result = cls(
                requested_width=payload["requested_width"],
                study_state=payload["study_state"],
                evidence=payload["evidence"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("method proposal exchange input is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted method proposal exchange input is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class MethodTerminalTransitionRef:
    """One ordered logical completion delivered to the method."""

    transition: LogicalTrialTransitionRecord

    def __post_init__(self) -> None:
        if not isinstance(self.transition, LogicalTrialTransitionRecord):
            raise TypeError("transition must be a LogicalTrialTransitionRecord.")
        if self.transition.to_state != "terminal":
            raise ValueError("method observation requires a terminal logical transition.")

    @property
    def logical_trial_id(self) -> str:
        return self.transition.logical_trial_id

    @property
    def terminal_transition_digest(self) -> str:
        return request_digest(self.transition.to_dict())

    def to_dict(self) -> JsonDict:
        return self.transition.to_dict()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MethodTerminalTransitionRef":
        return cls(LogicalTrialTransitionRecord.from_dict(payload))


@dataclass(frozen=True)
class MethodObservationPayload:
    """Exact filtered DTO delivered for one terminal logical trial.

    The fields are public method semantics rather than execution authority.
    Artifact values are immutable content references; callers must project and
    sanitize diagnostics before constructing this record.
    """

    logical_trial_id: str
    candidate_id: str
    status: str
    metric_values: Mapping[str, Any]
    constraint_results: Mapping[str, Any]
    resource_usage: Mapping[str, Any]
    artifacts: Tuple[Mapping[str, Any], ...]
    error: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        required_text(self.logical_trial_id, "logical trial id", max_bytes=512)
        required_text(self.candidate_id, "candidate id", max_bytes=512)
        if self.status not in _METHOD_OBSERVATION_STATUSES:
            raise ValueError("method observation status is unsupported.")
        for field_name in (
            "metric_values",
            "constraint_results",
            "resource_usage",
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded_json_mapping(
                    getattr(self, field_name), f"method observation {field_name}"
                ),
            )
        artifacts = tuple(
            _bounded_json_mapping(value, "method observation artifact")
            for value in self.artifacts
        )
        if len(artifacts) > MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError("method observation contains too many artifacts.")
        object.__setattr__(self, "artifacts", artifacts)
        if self.error is not None:
            object.__setattr__(
                self,
                "error",
                _bounded_json_mapping(self.error, "method observation error"),
            )
        if self.status == "success" and self.error is not None:
            raise ValueError("successful method observation cannot contain an error.")
        if self.status != "success" and self.error is None:
            raise ValueError("non-success method observation requires a public error.")

    def to_dict(self) -> JsonDict:
        return {
            "logical_trial_id": self.logical_trial_id,
            "candidate_id": self.candidate_id,
            "status": self.status,
            "metric_values": thaw_json(self.metric_values),
            "constraint_results": thaw_json(self.constraint_results),
            "resource_usage": thaw_json(self.resource_usage),
            "artifacts": [thaw_json(value) for value in self.artifacts],
            "error": None if self.error is None else thaw_json(self.error),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MethodObservationPayload":
        _exact_keys(
            payload,
            {
                "logical_trial_id",
                "candidate_id",
                "status",
                "metric_values",
                "constraint_results",
                "resource_usage",
                "artifacts",
                "error",
            },
            "method observation payload",
        )
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, list):
            raise TypeError("method observation artifacts must be a list.")
        result = cls(
            logical_trial_id=payload["logical_trial_id"],
            candidate_id=payload["candidate_id"],
            status=payload["status"],
            metric_values=payload["metric_values"],
            constraint_results=payload["constraint_results"],
            resource_usage=payload["resource_usage"],
            artifacts=tuple(artifacts),
            error=payload["error"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("method observation payload is not canonical.")
        return result


@dataclass(frozen=True)
class MethodObservationExchangeInput:
    """Ordered terminal evidence and exact DTOs fixed before ``observe``.

    Replaying a retained exchange reads ``observations`` directly.  It never
    needs to re-project mutable code over a later snapshot merely to discover
    what the original worker saw.
    """

    terminal_transitions: Tuple[MethodTerminalTransitionRef, ...]
    observations: Tuple[MethodObservationPayload, ...]

    def __post_init__(self) -> None:
        transitions = tuple(self.terminal_transitions)
        observations = tuple(self.observations)
        if not transitions:
            raise ValueError("method observation exchange cannot be empty.")
        if len(transitions) > MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError("method observation exchange contains too many items.")
        if any(not isinstance(item, MethodTerminalTransitionRef) for item in transitions):
            raise TypeError(
                "terminal_transitions must contain MethodTerminalTransitionRef values."
            )
        if any(not isinstance(item, MethodObservationPayload) for item in observations):
            raise TypeError(
                "observations must contain MethodObservationPayload values."
            )
        if len(observations) != len(transitions):
            raise ValueError(
                "method observations must align one-for-one with terminal transitions."
            )
        trial_ids = [item.logical_trial_id for item in transitions]
        if len(set(trial_ids)) != len(trial_ids):
            raise ValueError("method observation logical trial ids must be unique.")
        for transition, observation in zip(transitions, observations):
            if observation.logical_trial_id != transition.logical_trial_id:
                raise ValueError(
                    "method observation order differs from terminal transition order."
                )
            if observation.status != transition.transition.outcome:
                raise ValueError(
                    "method observation status differs from its terminal outcome."
                )
        object.__setattr__(self, "terminal_transitions", transitions)
        object.__setattr__(self, "observations", observations)
        if len(self.canonical_bytes) > MAX_DURABLE_METHOD_BYTES:
            raise ValueError("method observation exchange input is too large.")

    @property
    def kind(self) -> str:
        return "observation"

    @property
    def logical_trial_ids(self) -> Tuple[str, ...]:
        return tuple(item.logical_trial_id for item in self.terminal_transitions)

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def worker_request(self) -> JsonDict:
        """Return the exact canonical request body fixed for ``observe``."""

        return {"observations": [item.to_dict() for item in self.observations]}

    def to_dict(self) -> JsonDict:
        return {
            "schema": _OBSERVATION_INPUT_SCHEMA,
            "terminal_transitions": [
                item.to_dict() for item in self.terminal_transitions
            ],
            "observations": [item.to_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "MethodObservationExchangeInput":
        try:
            _exact_keys(
                payload,
                {"schema", "terminal_transitions", "observations"},
                "method observation exchange input",
            )
            if payload["schema"] != _OBSERVATION_INPUT_SCHEMA:
                raise ValueError(
                    "method observation exchange input schema is unsupported."
                )
            values = payload["terminal_transitions"]
            if not isinstance(values, list):
                raise TypeError("terminal_transitions must be a list.")
            observations = payload["observations"]
            if not isinstance(observations, list):
                raise TypeError("observations must be a list.")
            result = cls(
                tuple(MethodTerminalTransitionRef.from_dict(item) for item in values),
                tuple(MethodObservationPayload.from_dict(item) for item in observations),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("method observation exchange input is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted method observation exchange input is invalid: {error}"
            ) from error


MethodExchangeInput = Union[
    MethodProposalExchangeInput,
    MethodObservationExchangeInput,
]


def method_exchange_input_from_dict(
    payload: Mapping[str, Any],
) -> MethodExchangeInput:
    schema = payload.get("schema") if isinstance(payload, Mapping) else None
    if schema == _PROPOSAL_INPUT_SCHEMA:
        return MethodProposalExchangeInput.from_dict(payload)
    if schema == _OBSERVATION_INPUT_SCHEMA:
        return MethodObservationExchangeInput.from_dict(payload)
    raise RealmIntegrityError("Persisted method exchange input schema is unsupported.")


@dataclass(frozen=True)
class RunMethodExchangePreparationRecord:
    exchange_id: str
    run_id: str
    round_index: int
    kind: str
    exchange_input: MethodExchangeInput
    input_digest: str
    prepared_run_revision: int
    controller_generation: int
    controller_lease_id: str
    controller_fencing_token: int
    prepared_by_principal_id: str
    prepared_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.exchange_id, "method exchange id", max_bytes=512)
        required_text(self.run_id, "run id", max_bytes=512)
        positive_int(self.round_index, "method round index")
        if self.kind not in METHOD_EXCHANGE_KINDS:
            raise ValueError("method exchange kind is unsupported.")
        if not isinstance(
            self.exchange_input,
            (MethodProposalExchangeInput, MethodObservationExchangeInput),
        ):
            raise TypeError("exchange_input is not a supported method exchange input.")
        if self.exchange_input.kind != self.kind:
            raise ValueError("method exchange input kind does not match its record.")
        lower_hex_digest(self.input_digest, "method exchange input digest")
        if self.input_digest != self.exchange_input.digest:
            raise ValueError("method exchange input digest does not match its input.")
        if self.exchange_id != method_exchange_id(
            run_id=self.run_id,
            round_index=self.round_index,
            kind=self.kind,
        ):
            raise ValueError("method exchange id differs from its stable coordinate.")
        nonnegative_int(self.prepared_run_revision, "prepared run revision")
        positive_int(self.controller_generation, "controller generation")
        required_text(self.controller_lease_id, "controller lease id", max_bytes=512)
        positive_int(self.controller_fencing_token, "controller fencing token")
        required_text(
            self.prepared_by_principal_id,
            "method exchange principal id",
            max_bytes=512,
        )
        positive_int(self.prepared_txn_id, "method exchange preparation transaction id")
        object.__setattr__(self, "created_at", finite_time(self.created_at, "created_at"))

    def to_dict(self) -> JsonDict:
        return {
            "exchange_id": self.exchange_id,
            "run_id": self.run_id,
            "round_index": self.round_index,
            "kind": self.kind,
            "exchange_input": self.exchange_input.to_dict(),
            "input_digest": self.input_digest,
            "prepared_run_revision": self.prepared_run_revision,
            "controller_generation": self.controller_generation,
            "controller_lease_id": self.controller_lease_id,
            "controller_fencing_token": self.controller_fencing_token,
            "prepared_by_principal_id": self.prepared_by_principal_id,
            "prepared_txn_id": self.prepared_txn_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunMethodExchangePreparationRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "method exchange preparation")
        values = dict(payload)
        values["exchange_input"] = method_exchange_input_from_dict(
            values["exchange_input"]
        )
        return cls(**values)


@dataclass(frozen=True)
class RunMethodProposalCompletion:
    """Bounded caller assertion paired with an atomic semantic commit."""

    round_index: int
    prepared_input_digest: str
    outcome: str
    response_digest: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        positive_int(self.round_index, "method round index")
        lower_hex_digest(self.prepared_input_digest, "prepared method input digest")
        if self.outcome not in METHOD_PROPOSAL_OUTCOMES:
            raise ValueError("method proposal outcome is unsupported.")
        lower_hex_digest(self.response_digest, "method proposal response digest")
        if self.outcome in {"method_failed", "protocol_error"}:
            if self.error_code is None:
                raise ValueError("failed method proposal completion requires error_code.")
            required_text(self.error_code, "method proposal error code", max_bytes=128)
            if _ERROR_CODE.fullmatch(self.error_code) is None:
                raise ValueError("method proposal error_code must be a lowercase token.")
        elif self.error_code is not None:
            raise ValueError("successful method proposal completion cannot have error_code.")

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunMethodProposalCompletion":
        _exact_keys(payload, set(cls.__dataclass_fields__), "method proposal completion")
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunMethodObservationCompletion:
    """Typed result of invoking one prepared ``observe`` callback."""

    round_index: int
    prepared_input_digest: str
    outcome: str
    response_digest: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        positive_int(self.round_index, "method round index")
        lower_hex_digest(self.prepared_input_digest, "prepared method input digest")
        if self.outcome not in METHOD_OBSERVATION_OUTCOMES:
            raise ValueError("method observation outcome is unsupported.")
        lower_hex_digest(self.response_digest, "method observation response digest")
        if self.outcome in {"method_failed", "protocol_error"}:
            if self.error_code is None:
                raise ValueError("failed method observation requires error_code.")
            required_text(self.error_code, "method observation error code", max_bytes=128)
            if _ERROR_CODE.fullmatch(self.error_code) is None:
                raise ValueError(
                    "method observation error_code must be a lowercase token."
                )
        elif self.error_code is not None:
            raise ValueError(
                "acknowledged method observation cannot have error_code."
            )

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunMethodObservationCompletion":
        _exact_keys(
            payload, set(cls.__dataclass_fields__), "method observation completion"
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class RunMethodExchangeCompletionRecord:
    exchange_id: str
    run_id: str
    round_index: int
    kind: str
    prepared_input_digest: str
    outcome: str
    response_digest: str
    result_digest: str
    error_code: str | None
    logical_trial_ids: Tuple[str, ...]
    committed_run_revision: int
    controller_generation: int
    controller_lease_id: str
    controller_fencing_token: int
    completed_by_principal_id: str
    completed_txn_id: int
    created_at: float

    def __post_init__(self) -> None:
        required_text(self.exchange_id, "method exchange id", max_bytes=512)
        required_text(self.run_id, "run id", max_bytes=512)
        positive_int(self.round_index, "method round index")
        if self.kind not in METHOD_EXCHANGE_KINDS:
            raise ValueError("method exchange kind is unsupported.")
        if self.exchange_id != method_exchange_id(
            run_id=self.run_id,
            round_index=self.round_index,
            kind=self.kind,
        ):
            raise ValueError("method exchange completion id is not canonical.")
        lower_hex_digest(self.prepared_input_digest, "prepared method input digest")
        if self.kind == "proposal":
            if self.outcome not in METHOD_PROPOSAL_OUTCOMES:
                raise ValueError("method proposal outcome is unsupported.")
        elif self.outcome not in METHOD_OBSERVATION_OUTCOMES:
            raise ValueError("method observation outcome is unsupported.")
        lower_hex_digest(self.response_digest, "method exchange response digest")
        lower_hex_digest(self.result_digest, "method exchange result digest")
        if self.outcome in {"method_failed", "protocol_error"}:
            if self.error_code is None:
                raise ValueError("failed method exchange requires error_code.")
            required_text(self.error_code, "method exchange error code", max_bytes=128)
            if _ERROR_CODE.fullmatch(self.error_code) is None:
                raise ValueError("method exchange error_code must be a lowercase token.")
        elif self.error_code is not None:
            raise ValueError("nonfailed method exchange cannot have error_code.")
        if self.kind == "observation" and self.result_digest != (
            method_observation_result_digest(self.outcome)
        ):
            raise ValueError("method observation result digest is not canonical.")
        trial_ids = tuple(self.logical_trial_ids)
        if len(trial_ids) > MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError("method exchange completion contains too many trial ids.")
        if any(not isinstance(item, str) for item in trial_ids):
            raise TypeError("logical_trial_ids must contain strings.")
        for item in trial_ids:
            required_text(item, "logical trial id", max_bytes=512)
        if len(set(trial_ids)) != len(trial_ids):
            raise ValueError("method exchange completion trial ids must be unique.")
        if self.outcome == "admitted" or self.kind == "observation":
            if not trial_ids:
                raise ValueError("completed method exchange requires logical trial ids.")
        elif trial_ids:
            raise ValueError("nonadmitted proposal completion cannot contain trial ids.")
        object.__setattr__(self, "logical_trial_ids", trial_ids)
        nonnegative_int(self.committed_run_revision, "committed run revision")
        positive_int(self.controller_generation, "controller generation")
        required_text(self.controller_lease_id, "controller lease id", max_bytes=512)
        positive_int(self.controller_fencing_token, "controller fencing token")
        required_text(
            self.completed_by_principal_id,
            "method exchange principal id",
            max_bytes=512,
        )
        positive_int(self.completed_txn_id, "method exchange completion transaction id")
        object.__setattr__(self, "created_at", finite_time(self.created_at, "created_at"))

    def to_dict(self) -> JsonDict:
        result = dict(self.__dict__)
        result["logical_trial_ids"] = list(self.logical_trial_ids)
        return result

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunMethodExchangeCompletionRecord":
        _exact_keys(payload, set(cls.__dataclass_fields__), "method exchange completion")
        values = dict(payload)
        values["logical_trial_ids"] = tuple(values["logical_trial_ids"])
        return cls(**values)


@dataclass(frozen=True)
class RunMethodObservationAckReceipt:
    completion: RunMethodExchangeCompletionRecord

    def __post_init__(self) -> None:
        if not isinstance(self.completion, RunMethodExchangeCompletionRecord):
            raise TypeError("completion must be a RunMethodExchangeCompletionRecord.")
        if (
            self.completion.kind != "observation"
            or self.completion.outcome != METHOD_OBSERVATION_OUTCOME
        ):
            raise ValueError("method observation ack receipt has the wrong completion.")

    def to_dict(self) -> JsonDict:
        return {"completion": self.completion.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunMethodObservationAckReceipt":
        values = dict(payload)
        if values.pop("receipt_version", None) != 1:
            raise ValueError("receipt_version is unsupported.")
        _exact_keys(values, {"completion"}, "method observation ack receipt")
        return cls(RunMethodExchangeCompletionRecord.from_dict(values["completion"]))


@dataclass(frozen=True)
class RunMethodObservationCompletionReceipt:
    completion: RunMethodExchangeCompletionRecord
    control: RunSubmissionControlReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completion, RunMethodExchangeCompletionRecord):
            raise TypeError("completion must be a RunMethodExchangeCompletionRecord.")
        if self.completion.kind != "observation":
            raise ValueError("method observation receipt has the wrong exchange kind.")
        if self.completion.outcome == METHOD_OBSERVATION_OUTCOME:
            if self.control is not None:
                raise ValueError(
                    "acknowledged method observation cannot close submissions."
                )
        else:
            if self.control is not None:
                if not isinstance(self.control, RunSubmissionControlReceipt):
                    raise TypeError(
                        "method observation control must be a submission close receipt."
                    )
                if (
                    self.control.revision.txn_id
                    != self.completion.completed_txn_id
                    or self.control.revision.revision
                    != self.completion.committed_run_revision
                ):
                    raise ValueError("method observation close anchors do not agree.")

    def to_dict(self) -> JsonDict:
        return {
            "completion": self.completion.to_dict(),
            "control": (
                None
                if self.control is None
                else {"receipt_version": 1, **self.control.to_dict()}
            ),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RunMethodObservationCompletionReceipt":
        values = dict(payload)
        if values.pop("receipt_version", None) != 1:
            raise ValueError("receipt_version is unsupported.")
        _exact_keys(
            values,
            {"completion", "control"},
            "method observation completion receipt",
        )
        return cls(
            RunMethodExchangeCompletionRecord.from_dict(values["completion"]),
            (
                None
                if values["control"] is None
                else RunSubmissionControlReceipt.from_dict(values["control"])
            ),
        )


@dataclass(frozen=True)
class RunMethodProposalCompletionReceipt:
    completion: RunMethodExchangeCompletionRecord
    admission: RunAdmissionReceipt | None = None
    control: RunSubmissionControlReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.completion, RunMethodExchangeCompletionRecord):
            raise TypeError("completion must be a RunMethodExchangeCompletionRecord.")
        if self.completion.kind != "proposal":
            raise ValueError("method proposal receipt has the wrong exchange kind.")
        if self.completion.outcome == "admitted":
            if not isinstance(self.admission, RunAdmissionReceipt) or self.control is not None:
                raise ValueError("admitted method proposal requires only an admission receipt.")
            if (
                self.admission.revision.txn_id != self.completion.completed_txn_id
                or self.admission.revision.revision
                != self.completion.committed_run_revision
                or tuple(
                    item.admission.logical_trial_id
                    for item in self.admission.logical_trials
                )
                != self.completion.logical_trial_ids
            ):
                raise ValueError("method proposal admission anchors do not agree.")
        else:
            if not isinstance(self.control, RunSubmissionControlReceipt) or self.admission is not None:
                raise ValueError("closed method proposal requires only a control receipt.")
            if (
                self.control.revision.txn_id != self.completion.completed_txn_id
                or self.control.revision.revision
                != self.completion.committed_run_revision
            ):
                raise ValueError("method proposal close anchors do not agree.")


__all__ = [
    "METHOD_EXCHANGE_KINDS",
    "METHOD_OBSERVATION_ACK_RESULT_DIGEST",
    "METHOD_OBSERVATION_METHOD_FAILED_RESULT_DIGEST",
    "METHOD_OBSERVATION_OUTCOMES",
    "METHOD_OBSERVATION_PROTOCOL_ERROR_RESULT_DIGEST",
    "METHOD_PROPOSAL_OUTCOMES",
    "MethodExchangeInput",
    "MethodObservationExchangeInput",
    "MethodObservationPayload",
    "MethodProposalExchangeInput",
    "MethodTerminalTransitionRef",
    "RunMethodExchangeCompletionRecord",
    "RunMethodExchangePreparationRecord",
    "RunMethodObservationAckReceipt",
    "RunMethodObservationCompletion",
    "RunMethodObservationCompletionReceipt",
    "RunMethodProposalCompletion",
    "RunMethodProposalCompletionReceipt",
    "method_exchange_id",
    "method_exchange_input_from_dict",
    "method_exchange_sequence",
    "method_observation_result_digest",
    "method_worker_response_digest",
]
