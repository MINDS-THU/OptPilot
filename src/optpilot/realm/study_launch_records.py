"""Path-free durable records for retained-study launch authority transfer.

The launch handoff is the exact point at which an Operator Job stops owning
startup/cancellation and a fenced RunController starts owning the canonical
run.  These records carry immutable logical identities and authority evidence;
they deliberately contain no host paths, process ids, or provider-private
runtime coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ._validation import (
    finite_time,
    lower_hex_digest,
    nonnegative_int,
    positive_int,
    required_text,
)
from .errors import RealmIntegrityError
from .refs import request_digest
from .run_records import RunCreateReceipt


STUDY_LAUNCH_HANDOFF_SCHEMA = "optpilot.study-launch-handoff.v1"
STUDY_LAUNCH_CONTROLLER_CONFIRMATION_SCHEMA = (
    "optpilot.study-launch-controller-confirmation.v1"
)
RUN_CANCELLATION_REQUEST_SCHEMA = "optpilot.run-cancellation-request.v1"
RUN_CANCELLATION_REASON_CODES = frozenset(
    {"user_cancelled", "signal_cancelled", "admin_cancelled"}
)


def _path_free_identifier(value: Any, label: str) -> str:
    result = required_text(value, label)
    if (
        "/" in result
        or "\\" in result
        or result.startswith((".", "~"))
        or (len(result) >= 2 and result[1] == ":" and result[0].isalpha())
    ):
        raise ValueError(f"{label} must be a path-free logical identifier.")
    return result


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _handoff_payload(
    *,
    job_id: str,
    plan_digest: str,
    launch_token: str,
    study_definition_owner_id: str,
    study_definition_owner_revision: int,
    study_definition_manifest_digest: str,
    run_definition_digest: str,
    run_id: str,
    run_owner_id: str,
    controller_lease_id: str,
    controller_holder_id: str,
    controller_fencing_token: int,
    controller_generation: int,
    created_by_principal_id: str,
    created_txn_id: int,
    created_at: float,
) -> dict[str, Any]:
    return {
        "schema": STUDY_LAUNCH_HANDOFF_SCHEMA,
        "job_id": job_id,
        "plan_digest": plan_digest,
        "launch_token": launch_token,
        "study_definition_owner_id": study_definition_owner_id,
        "study_definition_owner_revision": study_definition_owner_revision,
        "study_definition_manifest_digest": study_definition_manifest_digest,
        "run_definition_digest": run_definition_digest,
        "run_id": run_id,
        "run_owner_id": run_owner_id,
        "controller_lease_id": controller_lease_id,
        "controller_holder_id": controller_holder_id,
        "controller_fencing_token": controller_fencing_token,
        "controller_generation": controller_generation,
        "created_by_principal_id": created_by_principal_id,
        "created_txn_id": created_txn_id,
        "created_at": created_at,
    }


@dataclass(frozen=True)
class StudyLaunchHandoffRecord:
    """Immutable proof of one exact Operator Job-to-run authority handoff."""

    job_id: str
    plan_digest: str
    launch_token: str
    study_definition_owner_id: str
    study_definition_owner_revision: int
    study_definition_manifest_digest: str
    run_definition_digest: str
    run_id: str
    run_owner_id: str
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int
    controller_generation: int
    created_by_principal_id: str
    created_txn_id: int
    created_at: float
    handoff_digest: str

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        plan_digest: str,
        launch_token: str,
        study_definition_owner_id: str,
        study_definition_owner_revision: int,
        study_definition_manifest_digest: str,
        run_definition_digest: str,
        run_id: str,
        run_owner_id: str,
        controller_lease_id: str,
        controller_holder_id: str,
        controller_fencing_token: int,
        controller_generation: int,
        created_by_principal_id: str,
        created_txn_id: int,
        created_at: float,
    ) -> "StudyLaunchHandoffRecord":
        payload = _handoff_payload(
            job_id=job_id,
            plan_digest=plan_digest,
            launch_token=launch_token,
            study_definition_owner_id=study_definition_owner_id,
            study_definition_owner_revision=study_definition_owner_revision,
            study_definition_manifest_digest=study_definition_manifest_digest,
            run_definition_digest=run_definition_digest,
            run_id=run_id,
            run_owner_id=run_owner_id,
            controller_lease_id=controller_lease_id,
            controller_holder_id=controller_holder_id,
            controller_fencing_token=controller_fencing_token,
            controller_generation=controller_generation,
            created_by_principal_id=created_by_principal_id,
            created_txn_id=created_txn_id,
            created_at=created_at,
        )
        return cls(
            **{key: value for key, value in payload.items() if key != "schema"},
            handoff_digest=request_digest(payload),
        )

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "launch_token",
            "study_definition_owner_id",
            "run_id",
            "run_owner_id",
            "controller_lease_id",
            "controller_holder_id",
            "created_by_principal_id",
        ):
            _path_free_identifier(
                getattr(self, field_name), field_name.replace("_", " ")
            )
        lower_hex_digest(self.plan_digest, "study launch plan digest")
        nonnegative_int(
            self.study_definition_owner_revision,
            "study definition owner revision",
        )
        if self.study_definition_owner_revision != 0:
            raise ValueError("study launch requires definition revision zero.")
        lower_hex_digest(
            self.study_definition_manifest_digest,
            "study definition manifest digest",
        )
        lower_hex_digest(self.run_definition_digest, "run definition digest")
        positive_int(
            self.controller_fencing_token, "controller fencing token"
        )
        positive_int(self.controller_generation, "controller generation")
        positive_int(self.created_txn_id, "study launch handoff transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "study launch handoff created_at"),
        )
        lower_hex_digest(self.handoff_digest, "study launch handoff digest")
        expected = request_digest(
            _handoff_payload(
                job_id=self.job_id,
                plan_digest=self.plan_digest,
                launch_token=self.launch_token,
                study_definition_owner_id=self.study_definition_owner_id,
                study_definition_owner_revision=(
                    self.study_definition_owner_revision
                ),
                study_definition_manifest_digest=(
                    self.study_definition_manifest_digest
                ),
                run_definition_digest=self.run_definition_digest,
                run_id=self.run_id,
                run_owner_id=self.run_owner_id,
                controller_lease_id=self.controller_lease_id,
                controller_holder_id=self.controller_holder_id,
                controller_fencing_token=self.controller_fencing_token,
                controller_generation=self.controller_generation,
                created_by_principal_id=self.created_by_principal_id,
                created_txn_id=self.created_txn_id,
                created_at=self.created_at,
            )
        )
        if self.handoff_digest != expected:
            raise ValueError("study launch handoff digest differs from its facts.")

    def to_dict(self) -> dict[str, Any]:
        payload = _handoff_payload(
            job_id=self.job_id,
            plan_digest=self.plan_digest,
            launch_token=self.launch_token,
            study_definition_owner_id=self.study_definition_owner_id,
            study_definition_owner_revision=self.study_definition_owner_revision,
            study_definition_manifest_digest=self.study_definition_manifest_digest,
            run_definition_digest=self.run_definition_digest,
            run_id=self.run_id,
            run_owner_id=self.run_owner_id,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            controller_generation=self.controller_generation,
            created_by_principal_id=self.created_by_principal_id,
            created_txn_id=self.created_txn_id,
            created_at=self.created_at,
        )
        payload["handoff_digest"] = self.handoff_digest
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudyLaunchHandoffRecord":
        try:
            _exact_keys(
                payload,
                {"schema"} | set(cls.__dataclass_fields__),
                "study launch handoff",
            )
            if payload["schema"] != STUDY_LAUNCH_HANDOFF_SCHEMA:
                raise ValueError("study launch handoff schema is unsupported.")
            result = cls(**{
                key: value for key, value in payload.items() if key != "schema"
            })
            if result.to_dict() != dict(payload):
                raise ValueError("study launch handoff is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted study launch handoff is invalid: {error}"
            ) from error


@dataclass(frozen=True)
class StudyLaunchHandoffReceipt:
    """The handoff proof paired with the exact canonical run creation."""

    handoff: StudyLaunchHandoffRecord
    creation: RunCreateReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, StudyLaunchHandoffRecord):
            raise TypeError("handoff must be a StudyLaunchHandoffRecord.")
        if not isinstance(self.creation, RunCreateReceipt):
            raise TypeError("creation must be a RunCreateReceipt.")
        if (
            self.handoff.run_id != self.creation.run.run_id
            or self.handoff.run_owner_id != self.creation.run.owner_id
            or self.handoff.controller_lease_id
            != self.creation.controller_lease.lease_id
            or self.handoff.controller_holder_id
            != self.creation.controller_lease.holder_id
            or self.handoff.controller_fencing_token
            != self.creation.controller_lease.fencing_token
            or self.handoff.controller_generation
            != self.creation.run.controller_generation
            or self.handoff.run_definition_digest
            != self.creation.definition_digest
        ):
            raise ValueError("study launch handoff and run creation differ.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "creation": self.creation.to_dict(),
            "handoff": self.handoff.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudyLaunchHandoffReceipt":
        try:
            _exact_keys(payload, {"creation", "handoff"}, "study launch receipt")
            result = cls(
                handoff=StudyLaunchHandoffRecord.from_dict(payload["handoff"]),
                creation=RunCreateReceipt.from_dict(payload["creation"]),
            )
            if result.to_dict() != dict(payload):
                raise ValueError("study launch receipt is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, RealmIntegrityError):
                raise
            raise RealmIntegrityError(
                f"Persisted study launch receipt is invalid: {error}"
            ) from error


def study_launch_controller_proof_digest(
    *,
    job_id: str,
    run_id: str,
    controller_lease_id: str,
    controller_holder_id: str,
    controller_fencing_token: int,
    controller_generation: int,
) -> str:
    """Digest the exact fenced RunController term confirmed by a launch."""

    return request_digest(
        {
            "controller_fencing_token": controller_fencing_token,
            "controller_generation": controller_generation,
            "controller_holder_id": controller_holder_id,
            "controller_lease_id": controller_lease_id,
            "job_id": job_id,
            "run_id": run_id,
            "schema": STUDY_LAUNCH_CONTROLLER_CONFIRMATION_SCHEMA,
        }
    )


def _controller_confirmation_payload(
    *,
    job_id: str,
    handoff_digest: str,
    run_id: str,
    run_definition_digest: str,
    controller_lease_id: str,
    controller_holder_id: str,
    controller_fencing_token: int,
    controller_generation: int,
    terminal_job_revision: int,
    terminal_proof_digest: str,
    result_digest: str,
    result_detail_digest: str,
    outcome_digest: str,
    created_by_principal_id: str,
    created_txn_id: int,
    created_at: float,
) -> dict[str, Any]:
    return {
        "schema": STUDY_LAUNCH_CONTROLLER_CONFIRMATION_SCHEMA,
        "job_id": job_id,
        "handoff_digest": handoff_digest,
        "run_id": run_id,
        "run_definition_digest": run_definition_digest,
        "controller_lease_id": controller_lease_id,
        "controller_holder_id": controller_holder_id,
        "controller_fencing_token": controller_fencing_token,
        "controller_generation": controller_generation,
        "terminal_job_revision": terminal_job_revision,
        "terminal_proof_digest": terminal_proof_digest,
        "result_digest": result_digest,
        "result_detail_digest": result_detail_digest,
        "outcome_digest": outcome_digest,
        "created_by_principal_id": created_by_principal_id,
        "created_txn_id": created_txn_id,
        "created_at": created_at,
    }


@dataclass(frozen=True)
class StudyLaunchControllerConfirmationRecord:
    """Atomic proof that a handed-off launch confirmed one live controller."""

    job_id: str
    handoff_digest: str
    run_id: str
    run_definition_digest: str
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int
    controller_generation: int
    terminal_job_revision: int
    terminal_proof_digest: str
    result_digest: str
    result_detail_digest: str
    outcome_digest: str
    created_by_principal_id: str
    created_txn_id: int
    created_at: float
    confirmation_digest: str

    @classmethod
    def build(
        cls,
        *,
        job_id: str,
        handoff_digest: str,
        run_id: str,
        run_definition_digest: str,
        controller_lease_id: str,
        controller_holder_id: str,
        controller_fencing_token: int,
        controller_generation: int,
        terminal_job_revision: int,
        result_digest: str,
        result_detail_digest: str,
        outcome_digest: str,
        created_by_principal_id: str,
        created_txn_id: int,
        created_at: float,
    ) -> "StudyLaunchControllerConfirmationRecord":
        terminal_proof_digest = study_launch_controller_proof_digest(
            job_id=job_id,
            run_id=run_id,
            controller_lease_id=controller_lease_id,
            controller_holder_id=controller_holder_id,
            controller_fencing_token=controller_fencing_token,
            controller_generation=controller_generation,
        )
        payload = _controller_confirmation_payload(
            job_id=job_id,
            handoff_digest=handoff_digest,
            run_id=run_id,
            run_definition_digest=run_definition_digest,
            controller_lease_id=controller_lease_id,
            controller_holder_id=controller_holder_id,
            controller_fencing_token=controller_fencing_token,
            controller_generation=controller_generation,
            terminal_job_revision=terminal_job_revision,
            terminal_proof_digest=terminal_proof_digest,
            result_digest=result_digest,
            result_detail_digest=result_detail_digest,
            outcome_digest=outcome_digest,
            created_by_principal_id=created_by_principal_id,
            created_txn_id=created_txn_id,
            created_at=created_at,
        )
        return cls(
            **{key: value for key, value in payload.items() if key != "schema"},
            confirmation_digest=request_digest(payload),
        )

    def __post_init__(self) -> None:
        for field_name in (
            "job_id",
            "run_id",
            "controller_lease_id",
            "controller_holder_id",
            "created_by_principal_id",
        ):
            _path_free_identifier(
                getattr(self, field_name), field_name.replace("_", " ")
            )
        for field_name in (
            "handoff_digest",
            "run_definition_digest",
            "terminal_proof_digest",
            "result_digest",
            "result_detail_digest",
            "outcome_digest",
            "confirmation_digest",
        ):
            lower_hex_digest(
                getattr(self, field_name), field_name.replace("_", " ")
            )
        positive_int(self.controller_fencing_token, "controller fencing token")
        positive_int(self.controller_generation, "controller generation")
        positive_int(self.terminal_job_revision, "terminal job revision")
        positive_int(self.created_txn_id, "controller confirmation transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "controller confirmation created_at"),
        )
        expected_proof = study_launch_controller_proof_digest(
            job_id=self.job_id,
            run_id=self.run_id,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            controller_generation=self.controller_generation,
        )
        if self.terminal_proof_digest != expected_proof:
            raise ValueError(
                "study launch confirmation proof differs from its controller term."
            )
        expected_digest = request_digest(
            _controller_confirmation_payload(
                job_id=self.job_id,
                handoff_digest=self.handoff_digest,
                run_id=self.run_id,
                run_definition_digest=self.run_definition_digest,
                controller_lease_id=self.controller_lease_id,
                controller_holder_id=self.controller_holder_id,
                controller_fencing_token=self.controller_fencing_token,
                controller_generation=self.controller_generation,
                terminal_job_revision=self.terminal_job_revision,
                terminal_proof_digest=self.terminal_proof_digest,
                result_digest=self.result_digest,
                result_detail_digest=self.result_detail_digest,
                outcome_digest=self.outcome_digest,
                created_by_principal_id=self.created_by_principal_id,
                created_txn_id=self.created_txn_id,
                created_at=self.created_at,
            )
        )
        if self.confirmation_digest != expected_digest:
            raise ValueError(
                "study launch confirmation digest differs from its facts."
            )

    def to_dict(self) -> dict[str, Any]:
        payload = _controller_confirmation_payload(
            job_id=self.job_id,
            handoff_digest=self.handoff_digest,
            run_id=self.run_id,
            run_definition_digest=self.run_definition_digest,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            controller_generation=self.controller_generation,
            terminal_job_revision=self.terminal_job_revision,
            terminal_proof_digest=self.terminal_proof_digest,
            result_digest=self.result_digest,
            result_detail_digest=self.result_detail_digest,
            outcome_digest=self.outcome_digest,
            created_by_principal_id=self.created_by_principal_id,
            created_txn_id=self.created_txn_id,
            created_at=self.created_at,
        )
        payload["confirmation_digest"] = self.confirmation_digest
        return payload

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "StudyLaunchControllerConfirmationRecord":
        try:
            _exact_keys(
                payload,
                {"schema"} | set(cls.__dataclass_fields__),
                "study launch controller confirmation",
            )
            if payload["schema"] != STUDY_LAUNCH_CONTROLLER_CONFIRMATION_SCHEMA:
                raise ValueError(
                    "study launch controller confirmation schema is unsupported."
                )
            result = cls(**{
                key: value for key, value in payload.items() if key != "schema"
            })
            if result.to_dict() != dict(payload):
                raise ValueError(
                    "study launch controller confirmation is not canonical."
                )
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                "Persisted study launch controller confirmation is invalid: "
                f"{error}"
            ) from error


def _cancellation_payload(
    *,
    run_id: str,
    job_id: str,
    handoff_digest: str,
    reason_code: str,
    observed_controller_lease_id: str,
    observed_controller_holder_id: str,
    observed_controller_fencing_token: int,
    observed_controller_generation: int,
    requested_by_principal_id: str,
    created_txn_id: int,
    created_at: float,
) -> dict[str, Any]:
    return {
        "schema": RUN_CANCELLATION_REQUEST_SCHEMA,
        "run_id": run_id,
        "job_id": job_id,
        "handoff_digest": handoff_digest,
        "reason_code": reason_code,
        "observed_controller_lease_id": observed_controller_lease_id,
        "observed_controller_holder_id": observed_controller_holder_id,
        "observed_controller_fencing_token": observed_controller_fencing_token,
        "observed_controller_generation": observed_controller_generation,
        "requested_by_principal_id": requested_by_principal_id,
        "created_txn_id": created_txn_id,
        "created_at": created_at,
    }


@dataclass(frozen=True)
class RunCancellationRequestRecord:
    """One routed cancellation intent anchored to the observed controller term."""

    run_id: str
    job_id: str
    handoff_digest: str
    reason_code: str
    observed_controller_lease_id: str
    observed_controller_holder_id: str
    observed_controller_fencing_token: int
    observed_controller_generation: int
    requested_by_principal_id: str
    created_txn_id: int
    created_at: float
    request_digest: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        job_id: str,
        handoff_digest: str,
        reason_code: str,
        observed_controller_lease_id: str,
        observed_controller_holder_id: str,
        observed_controller_fencing_token: int,
        observed_controller_generation: int,
        requested_by_principal_id: str,
        created_txn_id: int,
        created_at: float,
    ) -> "RunCancellationRequestRecord":
        payload = _cancellation_payload(
            run_id=run_id,
            job_id=job_id,
            handoff_digest=handoff_digest,
            reason_code=reason_code,
            observed_controller_lease_id=observed_controller_lease_id,
            observed_controller_holder_id=observed_controller_holder_id,
            observed_controller_fencing_token=observed_controller_fencing_token,
            observed_controller_generation=observed_controller_generation,
            requested_by_principal_id=requested_by_principal_id,
            created_txn_id=created_txn_id,
            created_at=created_at,
        )
        return cls(
            **{key: value for key, value in payload.items() if key != "schema"},
            request_digest=request_digest(payload),
        )

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "job_id",
            "observed_controller_lease_id",
            "observed_controller_holder_id",
            "requested_by_principal_id",
        ):
            _path_free_identifier(
                getattr(self, field_name), field_name.replace("_", " ")
            )
        lower_hex_digest(self.handoff_digest, "study launch handoff digest")
        required_text(self.reason_code, "run cancellation reason", max_bytes=128)
        if self.reason_code not in RUN_CANCELLATION_REASON_CODES:
            raise ValueError("run cancellation reason is unsupported.")
        positive_int(
            self.observed_controller_fencing_token,
            "observed controller fencing token",
        )
        positive_int(
            self.observed_controller_generation,
            "observed controller generation",
        )
        positive_int(self.created_txn_id, "run cancellation transaction id")
        object.__setattr__(
            self,
            "created_at",
            finite_time(self.created_at, "run cancellation created_at"),
        )
        lower_hex_digest(self.request_digest, "run cancellation request digest")
        expected = request_digest(
            _cancellation_payload(
                run_id=self.run_id,
                job_id=self.job_id,
                handoff_digest=self.handoff_digest,
                reason_code=self.reason_code,
                observed_controller_lease_id=self.observed_controller_lease_id,
                observed_controller_holder_id=self.observed_controller_holder_id,
                observed_controller_fencing_token=(
                    self.observed_controller_fencing_token
                ),
                observed_controller_generation=self.observed_controller_generation,
                requested_by_principal_id=self.requested_by_principal_id,
                created_txn_id=self.created_txn_id,
                created_at=self.created_at,
            )
        )
        if self.request_digest != expected:
            raise ValueError("run cancellation digest differs from its facts.")

    def to_dict(self) -> dict[str, Any]:
        payload = _cancellation_payload(
            run_id=self.run_id,
            job_id=self.job_id,
            handoff_digest=self.handoff_digest,
            reason_code=self.reason_code,
            observed_controller_lease_id=self.observed_controller_lease_id,
            observed_controller_holder_id=self.observed_controller_holder_id,
            observed_controller_fencing_token=(
                self.observed_controller_fencing_token
            ),
            observed_controller_generation=self.observed_controller_generation,
            requested_by_principal_id=self.requested_by_principal_id,
            created_txn_id=self.created_txn_id,
            created_at=self.created_at,
        )
        payload["request_digest"] = self.request_digest
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunCancellationRequestRecord":
        try:
            _exact_keys(
                payload,
                {"schema"} | set(cls.__dataclass_fields__),
                "run cancellation request",
            )
            if payload["schema"] != RUN_CANCELLATION_REQUEST_SCHEMA:
                raise ValueError("run cancellation request schema is unsupported.")
            result = cls(**{
                key: value for key, value in payload.items() if key != "schema"
            })
            if result.to_dict() != dict(payload):
                raise ValueError("run cancellation request is not canonical.")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted run cancellation request is invalid: {error}"
            ) from error


__all__ = [
    "RUN_CANCELLATION_REASON_CODES",
    "RUN_CANCELLATION_REQUEST_SCHEMA",
    "STUDY_LAUNCH_CONTROLLER_CONFIRMATION_SCHEMA",
    "STUDY_LAUNCH_HANDOFF_SCHEMA",
    "RunCancellationRequestRecord",
    "StudyLaunchControllerConfirmationRecord",
    "StudyLaunchHandoffReceipt",
    "StudyLaunchHandoffRecord",
    "study_launch_controller_proof_digest",
]
