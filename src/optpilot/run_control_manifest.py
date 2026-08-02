"""Canonical, path-free inputs for deterministic run-control reconstruction.

The records in this module are deliberately independent of the Realm ledger,
the runner, schedulers, and filesystem projections.  A run can retain one
immutable :class:`RunControlManifest` and later resolve the candidate contract
and normalizer implementation named by its versions before rebuilding the pure
``RunController`` cache.

Submission control is represented as a tiny append chain.  Its three states
are intentionally distinct from logical-trial state and evaluator outcome:
``accepting`` means new proposals are allowed, ``draining`` means accepted work
is being resolved after submissions close, and ``terminal`` means no more
control work remains.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from .method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from .realm._validation import freeze_json, thaw_json
from .realm.refs import canonical_json_bytes
from .run_controller import CandidateNormalizer, LogicalTrialIdFactory, RunController
from .run_terminal_policy import METHOD_EXCHANGE_ABANDON_STOP_CODES


RUN_CONTROL_MANIFEST_SCHEMA = "optpilot.run-control-manifest.v1"
SUBMISSION_CONTROL_RECORD_SCHEMA = "optpilot.submission-control-record.v1"

METHOD_PROTOCOLS = frozenset(
    {
        "optpilot.method.batch.v1",
        "optpilot.method.session.v1",
        "optpilot.method.session.v2",
    }
)
SUBMISSION_CONTROL_STATES = ("accepting", "draining", "terminal")
RETRYABLE_OUTCOMES = frozenset({"invalid", "failed", "timeout", "partial"})

_MANIFEST_DIGEST_DOMAIN = b"optpilot/run-control-manifest/v1"
_CONTROL_RECORD_DIGEST_DOMAIN = b"optpilot/submission-control-record/v1"
_CANDIDATE_CONTRACT_DIGEST_DOMAIN = b"optpilot/candidate-contract/v1"
_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STOP_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_RECORD_BYTES = 1024 * 1024


class RunControlIntegrityError(ValueError):
    """A serialized run-control value is malformed or non-canonical."""


def _required_text(value: Any, label: str, *, max_bytes: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    if len(value.encode("utf-8", errors="strict")) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes.")
    return value


def _positive_int_or_none(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer or None.")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer.")
    return value


def _lower_hex_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 64-character lowercase hexadecimal digest.")
    return value


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _digest(domain: bytes, value: Mapping[str, Any]) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_json_bytes(value)).hexdigest()


def _require_canonical_dict(
    payload: Mapping[str, Any], canonical: Mapping[str, Any], label: str
) -> None:
    if canonical_json_bytes(dict(payload)) != canonical_json_bytes(dict(canonical)):
        raise ValueError(f"{label} is not canonical.")


def _decode_canonical_object(payload: bytes, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError(f"{label} bytes must be bytes.")
    if len(payload) > _MAX_RECORD_BYTES:
        raise RunControlIntegrityError(f"{label} exceeds the maximum encoded size.")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunControlIntegrityError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise RunControlIntegrityError(f"{label} must encode a JSON object.")
    if canonical_json_bytes(value) != payload:
        raise RunControlIntegrityError(f"{label} bytes are not canonical JSON.")
    return value


def candidate_contract_digest(candidate_contract: Mapping[str, Any]) -> str:
    """Return the store- and path-independent identity of a candidate contract."""

    if not isinstance(candidate_contract, Mapping):
        raise TypeError("candidate_contract must be a mapping.")
    try:
        frozen = freeze_json(candidate_contract, label="candidate contract")
        encoded = canonical_json_bytes(thaw_json(frozen))
    except (TypeError, ValueError) as error:
        raise ValueError(f"candidate_contract must contain canonical JSON values: {error}") from error
    return hashlib.sha256(
        _CANDIDATE_CONTRACT_DIGEST_DOMAIN + b"\0" + encoded
    ).hexdigest()


@dataclass(frozen=True)
class ConvergencePolicy:
    """The deterministic convergence inputs consumed by ``RunController``."""

    patience_trials: Optional[int] = None
    min_delta: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "patience_trials",
            _positive_int_or_none(self.patience_trials, "convergence patience_trials"),
        )
        if isinstance(self.min_delta, bool) or not isinstance(self.min_delta, (int, float)):
            raise ValueError("convergence min_delta must be a finite, nonnegative number.")
        min_delta = float(self.min_delta)
        if not math.isfinite(min_delta) or min_delta < 0:
            raise ValueError("convergence min_delta must be a finite, nonnegative number.")
        if min_delta == 0:
            min_delta = 0.0
        if self.patience_trials is None and min_delta != 0.0:
            raise ValueError("convergence min_delta must be zero when patience_trials is None.")
        object.__setattr__(self, "min_delta", min_delta)

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_delta": self.min_delta,
            "patience_trials": self.patience_trials,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ConvergencePolicy":
        _exact_keys(payload, {"min_delta", "patience_trials"}, "convergence policy")
        result = cls(
            patience_trials=payload["patience_trials"],
            min_delta=payload["min_delta"],
        )
        _require_canonical_dict(payload, result.to_dict(), "convergence policy")
        return result


@dataclass(frozen=True)
class RetryPolicy:
    """Attempt retry semantics retained with the controller inputs."""

    max_attempts: int = 1
    retryable_outcomes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("retry max_attempts must be a positive integer.")
        if self.max_attempts <= 0:
            raise ValueError("retry max_attempts must be a positive integer.")
        if isinstance(self.retryable_outcomes, (str, bytes)) or not isinstance(
            self.retryable_outcomes, Sequence
        ):
            raise TypeError("retryable_outcomes must be a sequence of outcome names.")
        outcomes = tuple(self.retryable_outcomes)
        if any(not isinstance(item, str) or item not in RETRYABLE_OUTCOMES for item in outcomes):
            raise ValueError(
                "retryable_outcomes must use invalid, failed, timeout, or partial."
            )
        if len(set(outcomes)) != len(outcomes):
            raise ValueError("retryable_outcomes must not contain duplicates.")
        object.__setattr__(
            self,
            "retryable_outcomes",
            tuple(sorted(outcomes, key=lambda item: item.encode("utf-8"))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "retryable_outcomes": list(self.retryable_outcomes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetryPolicy":
        _exact_keys(payload, {"max_attempts", "retryable_outcomes"}, "retry policy")
        if not isinstance(payload["retryable_outcomes"], list):
            raise TypeError("retry policy retryable_outcomes must be a list.")
        result = cls(
            max_attempts=payload["max_attempts"],
            retryable_outcomes=tuple(payload["retryable_outcomes"]),
        )
        _require_canonical_dict(payload, result.to_dict(), "retry policy")
        return result


@dataclass(frozen=True)
class RunControlManifest:
    """Exact immutable inputs needed to reconstruct a fresh ``RunController``."""

    method_id: str
    method_protocol: str
    compiler_version: str
    normalizer_version: str
    proposal_width: int
    objective_metric: str
    objective_direction: str
    max_trials: Optional[int]
    max_failures: Optional[int]
    convergence: ConvergencePolicy
    retry_policy: RetryPolicy
    candidate_contract_digest: str

    def __post_init__(self) -> None:
        _required_text(self.method_id, "method id")
        _required_text(self.method_protocol, "method protocol", max_bytes=128)
        if self.method_protocol not in METHOD_PROTOCOLS:
            raise ValueError("method_protocol is not a supported canonical protocol id.")
        _required_text(self.compiler_version, "compiler version", max_bytes=128)
        _required_text(self.normalizer_version, "normalizer version", max_bytes=128)
        if isinstance(self.proposal_width, bool) or not isinstance(self.proposal_width, int):
            raise ValueError("proposal_width must be a positive integer.")
        if not 1 <= self.proposal_width <= MAX_BATCH_EXCHANGE_ITEMS:
            raise ValueError(
                "proposal_width must be between 1 and "
                f"{MAX_BATCH_EXCHANGE_ITEMS}."
            )
        _required_text(self.objective_metric, "objective metric")
        if self.objective_direction not in {"minimize", "maximize"}:
            raise ValueError("objective_direction must be 'minimize' or 'maximize'.")
        object.__setattr__(
            self, "max_trials", _positive_int_or_none(self.max_trials, "max_trials")
        )
        object.__setattr__(
            self, "max_failures", _positive_int_or_none(self.max_failures, "max_failures")
        )
        if not isinstance(self.convergence, ConvergencePolicy):
            raise TypeError("convergence must be a ConvergencePolicy.")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be a RetryPolicy.")
        _lower_hex_digest(self.candidate_contract_digest, "candidate contract digest")
        if len(self.to_bytes()) > _MAX_RECORD_BYTES:
            raise ValueError("run control manifest exceeds the maximum encoded size.")

    @property
    def digest(self) -> str:
        return _digest(_MANIFEST_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": {
                "max_failures": self.max_failures,
                "max_trials": self.max_trials,
            },
            "candidate_contract_digest": self.candidate_contract_digest,
            "compiler_version": self.compiler_version,
            "convergence": self.convergence.to_dict(),
            "method": {"id": self.method_id, "protocol": self.method_protocol},
            "normalizer_version": self.normalizer_version,
            "objective": {
                "direction": self.objective_direction,
                "metric": self.objective_metric,
            },
            "proposal_width": self.proposal_width,
            "retry_policy": self.retry_policy.to_dict(),
            "schema": RUN_CONTROL_MANIFEST_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_digest: Optional[str] = None,
    ) -> "RunControlManifest":
        try:
            _exact_keys(
                payload,
                {
                    "budget",
                    "candidate_contract_digest",
                    "compiler_version",
                    "convergence",
                    "method",
                    "normalizer_version",
                    "objective",
                    "proposal_width",
                    "retry_policy",
                    "schema",
                },
                "run control manifest",
            )
            if payload["schema"] != RUN_CONTROL_MANIFEST_SCHEMA:
                raise ValueError("run control manifest schema is unsupported.")
            method = payload["method"]
            budget = payload["budget"]
            objective = payload["objective"]
            _exact_keys(method, {"id", "protocol"}, "run control method")
            _exact_keys(budget, {"max_failures", "max_trials"}, "run control budget")
            _exact_keys(objective, {"direction", "metric"}, "run control objective")
            result = cls(
                method_id=method["id"],
                method_protocol=method["protocol"],
                compiler_version=payload["compiler_version"],
                normalizer_version=payload["normalizer_version"],
                proposal_width=payload["proposal_width"],
                objective_metric=objective["metric"],
                objective_direction=objective["direction"],
                max_trials=budget["max_trials"],
                max_failures=budget["max_failures"],
                convergence=ConvergencePolicy.from_dict(payload["convergence"]),
                retry_policy=RetryPolicy.from_dict(payload["retry_policy"]),
                candidate_contract_digest=payload["candidate_contract_digest"],
            )
            _require_canonical_dict(payload, result.to_dict(), "run control manifest")
            if expected_digest is not None:
                _lower_hex_digest(expected_digest, "expected run control manifest digest")
                if result.digest != expected_digest:
                    raise ValueError("run control manifest digest does not match.")
            return result
        except RunControlIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RunControlIntegrityError(f"Run control manifest is invalid: {error}") from error

    @classmethod
    def from_bytes(
        cls, payload: bytes, *, expected_digest: Optional[str] = None
    ) -> "RunControlManifest":
        value = _decode_canonical_object(payload, "run control manifest")
        result = cls.from_dict(value, expected_digest=expected_digest)
        if result.to_bytes() != payload:  # Defensive if numeric canonicalization evolves.
            raise RunControlIntegrityError("Run control manifest bytes are not canonical.")
        return result


@dataclass(frozen=True)
class SubmissionControlRecord:
    """One immutable append in the submission-control state chain."""

    manifest_digest: str
    run_revision: int
    previous_run_revision: Optional[int]
    previous_record_digest: Optional[str]
    previous_state: Optional[str]
    state: str
    stop_code: Optional[str]

    def __post_init__(self) -> None:
        _lower_hex_digest(self.manifest_digest, "submission manifest digest")
        _nonnegative_int(self.run_revision, "submission run_revision")
        allowed_transition = (self.previous_state, self.state) in {
            (None, "accepting"),
            ("accepting", "draining"),
            ("draining", "draining"),
            ("draining", "terminal"),
        }
        if not allowed_transition:
            raise ValueError(
                "submission control transition must be initial accepting, "
                "accepting -> draining, draining hard-stop escalation, or "
                "draining -> terminal."
            )
        if self.previous_state is None:
            if self.run_revision != 0:
                raise ValueError("initial accepting state must anchor run revision zero.")
            if self.previous_run_revision is not None or self.previous_record_digest is not None:
                raise ValueError("initial accepting state cannot have a predecessor anchor.")
            if self.stop_code is not None:
                raise ValueError("accepting state cannot define a stop_code.")
        else:
            previous_revision = _nonnegative_int(
                self.previous_run_revision, "submission previous_run_revision"
            )
            if previous_revision >= self.run_revision:
                raise ValueError(
                    "submission run_revision must be greater than previous_run_revision."
                )
            _lower_hex_digest(
                self.previous_record_digest, "submission previous record digest"
            )
            if not isinstance(self.stop_code, str) or _STOP_CODE.fullmatch(self.stop_code) is None:
                raise ValueError(
                    "draining and terminal submission states require a structured stop_code."
                )
            if (
                self.previous_state == "draining"
                and self.state == "draining"
                and self.stop_code not in METHOD_EXCHANGE_ABANDON_STOP_CODES
            ):
                raise ValueError(
                    "a draining control append must escalate to a hard stop."
                )
        if len(self.to_bytes()) > _MAX_RECORD_BYTES:
            raise ValueError("submission control record exceeds the maximum encoded size.")

    @classmethod
    def initial(
        cls, *, manifest_digest: str, run_revision: int = 0
    ) -> "SubmissionControlRecord":
        return cls(
            manifest_digest=manifest_digest,
            run_revision=run_revision,
            previous_run_revision=None,
            previous_record_digest=None,
            previous_state=None,
            state="accepting",
            stop_code=None,
        )

    def transition(
        self,
        *,
        state: str,
        run_revision: int,
        stop_code: str,
    ) -> "SubmissionControlRecord":
        """Build the next append with exact predecessor anchors."""

        return SubmissionControlRecord(
            manifest_digest=self.manifest_digest,
            run_revision=run_revision,
            previous_run_revision=self.run_revision,
            previous_record_digest=self.digest,
            previous_state=self.state,
            state=state,
            stop_code=stop_code,
        )

    @property
    def digest(self) -> str:
        return _digest(_CONTROL_RECORD_DIGEST_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "previous_record_digest": self.previous_record_digest,
            "previous_run_revision": self.previous_run_revision,
            "previous_state": self.previous_state,
            "run_revision": self.run_revision,
            "schema": SUBMISSION_CONTROL_RECORD_SCHEMA,
            "state": self.state,
            "stop_code": self.stop_code,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_digest: Optional[str] = None,
    ) -> "SubmissionControlRecord":
        try:
            _exact_keys(
                payload,
                {
                    "manifest_digest",
                    "previous_record_digest",
                    "previous_run_revision",
                    "previous_state",
                    "run_revision",
                    "schema",
                    "state",
                    "stop_code",
                },
                "submission control record",
            )
            if payload["schema"] != SUBMISSION_CONTROL_RECORD_SCHEMA:
                raise ValueError("submission control record schema is unsupported.")
            result = cls(
                manifest_digest=payload["manifest_digest"],
                run_revision=payload["run_revision"],
                previous_run_revision=payload["previous_run_revision"],
                previous_record_digest=payload["previous_record_digest"],
                previous_state=payload["previous_state"],
                state=payload["state"],
                stop_code=payload["stop_code"],
            )
            _require_canonical_dict(payload, result.to_dict(), "submission control record")
            if expected_digest is not None:
                _lower_hex_digest(expected_digest, "expected submission record digest")
                if result.digest != expected_digest:
                    raise ValueError("submission control record digest does not match.")
            return result
        except RunControlIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RunControlIntegrityError(
                f"Submission control record is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(
        cls, payload: bytes, *, expected_digest: Optional[str] = None
    ) -> "SubmissionControlRecord":
        value = _decode_canonical_object(payload, "submission control record")
        result = cls.from_dict(value, expected_digest=expected_digest)
        if result.to_bytes() != payload:
            raise RunControlIntegrityError("Submission control record bytes are not canonical.")
        return result


def validate_submission_control_chain(
    records: Sequence[SubmissionControlRecord],
    *,
    manifest_digest: Optional[str] = None,
) -> SubmissionControlRecord:
    """Validate predecessor digests/revisions and return the current state record."""

    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("records must be a sequence of SubmissionControlRecord values.")
    chain = tuple(records)
    if not chain:
        raise ValueError("submission control chain must not be empty.")
    if any(not isinstance(item, SubmissionControlRecord) for item in chain):
        raise TypeError("submission control chain contains a non-record value.")
    if manifest_digest is not None:
        _lower_hex_digest(manifest_digest, "expected submission manifest digest")
    for index, record in enumerate(chain):
        if manifest_digest is not None and record.manifest_digest != manifest_digest:
            raise RunControlIntegrityError("submission record targets a different manifest.")
        if index == 0:
            if record.previous_state is not None:
                raise RunControlIntegrityError("submission control chain does not start at accepting.")
            continue
        previous = chain[index - 1]
        if record.manifest_digest != previous.manifest_digest:
            raise RunControlIntegrityError("submission control chain changes manifest digest.")
        if (
            record.previous_run_revision != previous.run_revision
            or record.previous_record_digest != previous.digest
            or record.previous_state != previous.state
        ):
            raise RunControlIntegrityError("submission control predecessor anchor does not match.")
        if record.previous_state == "draining" and record.state == "draining":
            if (
                previous.stop_code in METHOD_EXCHANGE_ABANDON_STOP_CODES
                or record.stop_code not in METHOD_EXCHANGE_ABANDON_STOP_CODES
                or previous.stop_code == record.stop_code
            ):
                raise RunControlIntegrityError(
                    "submission control hard-stop escalation is invalid."
                )
    return chain[-1]


def build_run_controller(
    manifest: RunControlManifest,
    *,
    candidate_contract: Mapping[str, Any],
    candidate_normalizer: CandidateNormalizer,
    normalizer_version: str,
    logical_trial_id_factory: Optional[LogicalTrialIdFactory] = None,
) -> RunController:
    """Construct a fresh controller after resolving the manifest's exact inputs.

    The normalizer is intentionally never imported or selected by this helper.
    The caller must resolve the implementation named by
    ``manifest.normalizer_version`` and supply it explicitly.  The resolved
    candidate contract is checked against the manifest before any controller is
    returned.
    """

    if not isinstance(manifest, RunControlManifest):
        raise TypeError("manifest must be a RunControlManifest.")
    if not isinstance(candidate_contract, Mapping):
        raise TypeError("candidate_contract must be a mapping.")
    if candidate_contract_digest(candidate_contract) != manifest.candidate_contract_digest:
        raise ValueError("candidate_contract does not match the run control manifest digest.")
    if not callable(candidate_normalizer):
        raise TypeError("candidate_normalizer must be an explicitly supplied callable.")
    _required_text(normalizer_version, "resolved normalizer version", max_bytes=128)
    if normalizer_version != manifest.normalizer_version:
        raise ValueError("candidate_normalizer version does not match the run control manifest.")
    if logical_trial_id_factory is not None and not callable(logical_trial_id_factory):
        raise TypeError("logical_trial_id_factory must be callable or None.")
    return RunController(
        method_id=manifest.method_id,
        # Realm value records freeze nested JSON as mapping proxies and tuples;
        # RunController owns an ordinary private JSON copy.
        candidate_contract=thaw_json(
            freeze_json(candidate_contract, label="candidate contract")
        ),
        objective_metric=manifest.objective_metric,
        objective_direction=manifest.objective_direction,
        proposal_width=manifest.proposal_width,
        max_trials=manifest.max_trials,
        max_failures=manifest.max_failures,
        patience_trials=manifest.convergence.patience_trials,
        min_delta=manifest.convergence.min_delta,
        candidate_normalizer=candidate_normalizer,
        logical_trial_id_factory=logical_trial_id_factory,
    )


__all__ = [
    "ConvergencePolicy",
    "METHOD_PROTOCOLS",
    "RETRYABLE_OUTCOMES",
    "RUN_CONTROL_MANIFEST_SCHEMA",
    "RunControlIntegrityError",
    "RunControlManifest",
    "SUBMISSION_CONTROL_RECORD_SCHEMA",
    "SUBMISSION_CONTROL_STATES",
    "RetryPolicy",
    "SubmissionControlRecord",
    "build_run_controller",
    "candidate_contract_digest",
    "validate_submission_control_chain",
]
