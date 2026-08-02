"""One-attempt evaluation semantics without evidence or budget side effects.

This module is deliberately below both the canonical run adopter and Studio's
inspection adopter.  Callers provide an already-resolved candidate, configured
validator/materializer/environment adapter, caller-owned identifiers, and an
existing fresh workspace binding.  :class:`AttemptExecutor` executes exactly
one validation -> materialization -> environment chain and returns an immutable
envelope; it never creates a run/trial id, reserves budget, writes evidence, or
retains declared outputs.

The current evaluator has not been cut over yet, but both paths use the neutral
normalization contract in :mod:`optpilot.attempt_semantics`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Dict, Optional, Tuple

from .attempt_semantics import (
    error_payload as _existing_error_payload,
    exception_evaluation_result as _existing_exception_evaluation_result,
    status_for_exception as _existing_status_for_exception,
    validate_environment_result as _existing_validate_environment_result,
    validation_exception_report as _existing_validation_exception_report,
)
from .candidate_materialization import MaterializationRecord


JsonDict = Dict[str, Any]
_EVALUATION_SPEC_SCHEMA = "optpilot.evaluation-spec.v3"
_ATTEMPT_ENVELOPE_SCHEMA = "optpilot.attempt.envelope.v2"
_ATTEMPT_FINALIZATION_SCHEMA = "optpilot.attempt.finalization.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOWER_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_REF_RE = re.compile(r"^(?:blob|tree):sha256:[0-9a-f]{64}$")
_PUBLIC_OUTCOMES = frozenset(
    {"success", "invalid", "failed", "timeout", "partial", "cancelled"}
)
_RESERVED_CONTEXT_FIELDS = frozenset(
    {
        "attempt_id",
        "evaluation_spec_digest",
        "binding_id",
        "trial_id",
        "parent_trial_id",
        "attempt_index",
        "study_id",
        "workspace",
        "resource_profile",
        "sandbox_spec",
        "backend_identity",
        "backend_worker",
        "seed",
        "repetition_index",
    }
)


@dataclass(frozen=True)
class EvaluationSpec:
    """Portable semantic inputs for exactly one environment evaluation.

    The spec deliberately excludes run, study, logical-trial, retry, scheduler,
    and backend identity.  The same semantic evaluation therefore has the same
    digest whether it is executed for a canonical run, an inspection, or a
    retry on a different worker.  The resolved candidate is included until a
    caller can provide it through an authorized projection; ``candidate_ref``
    is its stable identity.
    """

    environment_id: str
    environment_revision_digest: str
    prepared_runtime_digest: str
    candidate: Mapping[str, Any]
    objective: Mapping[str, Any]
    resource_profile: Mapping[str, Any]
    sandbox_spec: Mapping[str, Any]
    candidate_ref: str = ""
    seed: Any = None
    repetition_index: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.environment_id, "environment_id")
        _lower_hex_digest(
            self.environment_revision_digest,
            "environment_revision_digest",
        )
        _lower_hex_digest(
            self.prepared_runtime_digest,
            "prepared_runtime_digest",
        )
        _optional_text(self.candidate_ref, "candidate_ref")
        _nonnegative_int(self.repetition_index, "repetition_index")

        candidate = _mapping_record(self.candidate, "candidate")
        _required_text(candidate.get("candidate_id"), "candidate.candidate_id")
        _required_text(candidate.get("format"), "candidate.format")
        if not isinstance(candidate.get("spec"), Mapping):
            raise ValueError("candidate.spec must be a mapping.")
        for name in ("lineage", "generator", "validation", "materialization"):
            if name in candidate and not isinstance(candidate[name], Mapping):
                raise ValueError(f"candidate.{name} must be a mapping.")

        objective = _mapping_record(self.objective, "objective")
        primary_metric = objective.get("primaryMetric")
        if not isinstance(primary_metric, Mapping):
            raise ValueError("objective.primaryMetric must be a mapping.")
        _required_text(primary_metric.get("name"), "objective.primaryMetric.name")

        object.__setattr__(self, "candidate", _freeze(candidate, "candidate"))
        object.__setattr__(self, "objective", _freeze(objective, "objective"))
        object.__setattr__(
            self,
            "resource_profile",
            _freeze(_mapping_record(self.resource_profile, "resource_profile"), "resource_profile"),
        )
        object.__setattr__(
            self,
            "sandbox_spec",
            _freeze(_mapping_record(self.sandbox_spec, "sandbox_spec"), "sandbox_spec"),
        )
        object.__setattr__(
            self,
            "metadata",
            _freeze(_mapping_record(self.metadata, "metadata"), "metadata"),
        )
        object.__setattr__(self, "seed", _freeze(self.seed, "seed"))

    @property
    def candidate_id(self) -> str:
        return str(self.candidate["candidate_id"])

    @property
    def candidate_format(self) -> str:
        return str(self.candidate["format"])

    @property
    def primary_metric_name(self) -> str:
        return str(self.objective["primaryMetric"]["name"])

    @property
    def digest(self) -> str:
        payload = _canonical_json_bytes(self.to_dict())
        value = hashlib.sha256(b"optpilot/evaluation-spec/v3\0" + payload).hexdigest()
        return f"sha256:{value}"

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": _EVALUATION_SPEC_SCHEMA,
            "environment_id": self.environment_id,
            "environment_revision_digest": self.environment_revision_digest,
            "prepared_runtime_digest": self.prepared_runtime_digest,
            "candidate": _thaw(self.candidate),
            "objective": _thaw(self.objective),
            "resource_profile": _thaw(self.resource_profile),
            "sandbox_spec": _thaw(self.sandbox_spec),
            "candidate_ref": self.candidate_ref,
            "seed": _thaw(self.seed),
            "repetition_index": self.repetition_index,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationSpec":
        expected = {
            "schema_version",
            "environment_id",
            "environment_revision_digest",
            "prepared_runtime_digest",
            "candidate",
            "objective",
            "resource_profile",
            "sandbox_spec",
            "candidate_ref",
            "seed",
            "repetition_index",
            "metadata",
        }
        _require_exact_keys(payload, expected, "evaluation spec")
        if payload["schema_version"] != _EVALUATION_SPEC_SCHEMA:
            raise ValueError("Evaluation spec schema is unsupported.")
        return cls(
            environment_id=payload["environment_id"],
            environment_revision_digest=payload["environment_revision_digest"],
            prepared_runtime_digest=payload["prepared_runtime_digest"],
            candidate=payload["candidate"],
            objective=payload["objective"],
            resource_profile=payload["resource_profile"],
            sandbox_spec=payload["sandbox_spec"],
            candidate_ref=payload["candidate_ref"],
            seed=payload["seed"],
            repetition_index=payload["repetition_index"],
            metadata=payload["metadata"],
        )


@dataclass(frozen=True)
class AttemptWorkspaceBinding:
    """Caller-owned physical realization for one attempt.

    The directory must already exist and must be dedicated to this binding.
    It may contain provider-projected immutable inputs, so emptiness is not a
    valid freshness test.  The executor neither creates nor cleans it.
    """

    binding_id: str
    workspace: Path
    backend_identity: Mapping[str, Any] = field(default_factory=dict)
    backend_worker: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.binding_id, "binding_id")
        raw_workspace = Path(self.workspace).expanduser()
        if not raw_workspace.is_absolute():
            raise ValueError("workspace must be an absolute path.")
        if raw_workspace.is_symlink():
            raise ValueError("workspace must not be a symlink.")
        workspace = raw_workspace.resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError("workspace must be an existing directory.")

        context = _mapping_record(self.context, "binding context")
        reserved = sorted(_RESERVED_CONTEXT_FIELDS.intersection(context))
        if reserved:
            raise ValueError(
                "binding context cannot replace executor-owned fields: "
                + ", ".join(reserved)
            )
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(
            self,
            "backend_identity",
            _freeze(
                _mapping_record(self.backend_identity, "backend_identity"),
                "backend_identity",
            ),
        )
        object.__setattr__(
            self,
            "backend_worker",
            _freeze(_mapping_record(self.backend_worker, "backend_worker"), "backend_worker"),
        )
        object.__setattr__(self, "context", _freeze(context, "binding context"))


@dataclass(frozen=True)
class OutputDeclaration:
    """A portable declaration of an output inside the attempt workspace."""

    declaration_id: str
    name: str
    path: str
    kind: str = "file"
    media_type: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.declaration_id, "output declaration id")
        _required_text(self.name, "output declaration name")
        object.__setattr__(self, "path", _safe_relative_path(self.path))
        if self.kind not in {"file", "tree"}:
            raise ValueError("output declaration kind must be 'file' or 'tree'.")
        _optional_text(self.media_type, "output declaration media_type")
        object.__setattr__(
            self,
            "metadata",
            _freeze(
                _mapping_record(self.metadata, "output declaration metadata"),
                "output declaration metadata",
            ),
        )

    def to_dict(self) -> JsonDict:
        return {
            "declaration_id": self.declaration_id,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "media_type": self.media_type,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OutputDeclaration":
        _require_exact_keys(
            payload,
            {
                "declaration_id",
                "name",
                "path",
                "kind",
                "media_type",
                "metadata",
            },
            "output declaration",
        )
        return cls(
            declaration_id=payload["declaration_id"],
            name=payload["name"],
            path=payload["path"],
            kind=payload["kind"],
            media_type=payload["media_type"],
            metadata=payload["metadata"],
        )


@dataclass(frozen=True)
class CapturedArtifact:
    """One retained declaration and its store-neutral content identity.

    ``bindings`` identify stores containing the exact content ref.  They are
    availability descriptors only; owner authority and retention policy belong
    to the adopter and are intentionally absent from this transport record.
    """

    declaration: OutputDeclaration
    content_ref: str
    size_bytes: int
    bindings: Tuple[Mapping[str, Any], ...]
    visibility: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.declaration, OutputDeclaration):
            raise TypeError("captured artifact declaration must be an OutputDeclaration.")
        _physical_content_ref(self.content_ref, "captured artifact content_ref")
        _nonnegative_int(self.size_bytes, "captured artifact size_bytes")
        if self.visibility not in {"operator", "method"}:
            raise ValueError("captured artifact visibility must be 'operator' or 'method'.")
        bindings = tuple(
            _capture_binding(item, self.content_ref, index)
            for index, item in enumerate(self.bindings)
        )
        if not bindings:
            raise ValueError("captured artifact requires at least one content binding.")
        identities = {(item["store_id"], item["content_ref"]) for item in bindings}
        if len(identities) != len(bindings):
            raise ValueError("captured artifact content bindings must be unique.")
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(
            self,
            "metadata",
            _freeze(
                _mapping_record(self.metadata, "captured artifact metadata"),
                "captured artifact metadata",
            ),
        )

    def to_dict(self) -> JsonDict:
        return {
            "declaration": self.declaration.to_dict(),
            "content_ref": self.content_ref,
            "size_bytes": self.size_bytes,
            "bindings": [_thaw(item) for item in self.bindings],
            "visibility": self.visibility,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CapturedArtifact":
        _require_exact_keys(
            payload,
            {
                "declaration",
                "content_ref",
                "size_bytes",
                "bindings",
                "visibility",
                "metadata",
            },
            "captured artifact",
        )
        bindings = payload["bindings"]
        if not isinstance(bindings, (list, tuple)):
            raise TypeError("captured artifact bindings must be a sequence.")
        return cls(
            declaration=OutputDeclaration.from_dict(payload["declaration"]),
            content_ref=payload["content_ref"],
            size_bytes=payload["size_bytes"],
            bindings=tuple(bindings),
            visibility=payload["visibility"],
            metadata=payload["metadata"],
        )


@dataclass(frozen=True)
class AttemptEnvelope:
    """Immutable neutral execution results for exactly one attempt."""

    attempt_id: str
    evaluation_spec_digest: str
    binding_id: str
    outcome: str
    phase: str
    wall_clock_seconds: float
    validation: Mapping[str, Any]
    materialization: Mapping[str, Any]
    metric_values: Mapping[str, Any]
    constraint_results: Mapping[str, Any]
    output_declarations: Tuple[OutputDeclaration, ...]
    event_summary: Mapping[str, Any]
    execution_metadata: Mapping[str, Any]
    error: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = _ATTEMPT_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        _required_text(self.attempt_id, "attempt_id")
        _sha256_digest(self.evaluation_spec_digest, "evaluation_spec_digest")
        _required_text(self.binding_id, "binding_id")
        if self.schema_version != _ATTEMPT_ENVELOPE_SCHEMA:
            raise ValueError("Attempt envelope schema is unsupported.")
        if self.outcome not in _PUBLIC_OUTCOMES:
            raise ValueError(f"Unsupported attempt outcome: {self.outcome!r}.")
        _required_text(self.phase, "phase")
        elapsed = float(self.wall_clock_seconds)
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("wall_clock_seconds must be finite and non-negative.")
        object.__setattr__(self, "wall_clock_seconds", elapsed)

        for name in (
            "validation",
            "materialization",
            "metric_values",
            "constraint_results",
            "event_summary",
            "execution_metadata",
            "error",
        ):
            value = _mapping_record(getattr(self, name), name)
            object.__setattr__(self, name, _freeze(value, name))
        declarations = tuple(self.output_declarations)
        if any(not isinstance(item, OutputDeclaration) for item in declarations):
            raise TypeError(
                "attempt envelope output_declarations must contain OutputDeclaration records."
            )
        declaration_ids = {item.declaration_id for item in declarations}
        if len(declaration_ids) != len(declarations):
            raise ValueError("attempt envelope output declaration ids must be unique.")
        object.__setattr__(self, "output_declarations", declarations)

    @property
    def digest(self) -> str:
        value = hashlib.sha256(
            b"optpilot/attempt-envelope/v2\0" + _canonical_json_bytes(self.to_dict())
        ).hexdigest()
        return f"sha256:{value}"

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "evaluation_spec_digest": self.evaluation_spec_digest,
            "binding_id": self.binding_id,
            "outcome": self.outcome,
            "phase": self.phase,
            "wall_clock_seconds": self.wall_clock_seconds,
            "validation": _thaw(self.validation),
            "materialization": _thaw(self.materialization),
            "metric_values": _thaw(self.metric_values),
            "constraint_results": _thaw(self.constraint_results),
            "output_declarations": [item.to_dict() for item in self.output_declarations],
            "event_summary": _thaw(self.event_summary),
            "execution_metadata": _thaw(self.execution_metadata),
            "error": _thaw(self.error),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AttemptEnvelope":
        expected = {
            "schema_version",
            "attempt_id",
            "evaluation_spec_digest",
            "binding_id",
            "outcome",
            "phase",
            "wall_clock_seconds",
            "validation",
            "materialization",
            "metric_values",
            "constraint_results",
            "output_declarations",
            "event_summary",
            "execution_metadata",
            "error",
        }
        _require_exact_keys(payload, expected, "attempt envelope")
        if payload["schema_version"] != _ATTEMPT_ENVELOPE_SCHEMA:
            raise ValueError("Attempt envelope schema is unsupported.")
        declarations = payload["output_declarations"]
        if not isinstance(declarations, (list, tuple)):
            raise TypeError("Attempt envelope output_declarations must be a sequence.")
        return cls(
            schema_version=payload["schema_version"],
            attempt_id=payload["attempt_id"],
            evaluation_spec_digest=payload["evaluation_spec_digest"],
            binding_id=payload["binding_id"],
            outcome=payload["outcome"],
            phase=payload["phase"],
            wall_clock_seconds=payload["wall_clock_seconds"],
            validation=payload["validation"],
            materialization=payload["materialization"],
            metric_values=payload["metric_values"],
            constraint_results=payload["constraint_results"],
            output_declarations=tuple(
                OutputDeclaration.from_dict(item) for item in declarations
            ),
            event_summary=payload["event_summary"],
            execution_metadata=payload["execution_metadata"],
            error=payload["error"],
        )


@dataclass(frozen=True)
class AttemptFinalization:
    """Neutral artifact-capture result ready for an adopter transaction.

    Exactly one of ``envelope`` and ``platform_error`` is present.  This record
    carries content identities and store availability, but never run ids,
    logical-trial ids, owner ids, owner revisions, or retention authority.
    """

    attempt_id: str
    evaluation_spec_digest: str
    binding_id: str
    effective_outcome: str
    effective_code: Optional[str]
    captured_artifacts: Tuple[CapturedArtifact, ...]
    envelope: Optional[AttemptEnvelope] = None
    platform_error: Optional[Mapping[str, Any]] = None
    schema_version: str = _ATTEMPT_FINALIZATION_SCHEMA

    def __post_init__(self) -> None:
        _required_text(self.attempt_id, "attempt finalization attempt_id")
        _sha256_digest(
            self.evaluation_spec_digest,
            "attempt finalization evaluation_spec_digest",
        )
        _required_text(self.binding_id, "attempt finalization binding_id")
        if self.schema_version != _ATTEMPT_FINALIZATION_SCHEMA:
            raise ValueError("Attempt finalization schema is unsupported.")
        if self.effective_outcome not in _PUBLIC_OUTCOMES:
            raise ValueError(
                f"Unsupported effective attempt outcome: {self.effective_outcome!r}."
            )
        _optional_text(self.effective_code, "attempt finalization effective_code")
        if self.effective_outcome == "success" and self.effective_code is not None:
            raise ValueError("A successful attempt finalization cannot have an effective_code.")
        if self.effective_outcome != "success" and not self.effective_code:
            raise ValueError("A non-success attempt finalization requires an effective_code.")

        has_envelope = self.envelope is not None
        has_platform_error = self.platform_error is not None
        if has_envelope == has_platform_error:
            raise ValueError(
                "Attempt finalization requires exactly one of envelope or platform_error."
            )
        if has_envelope:
            if not isinstance(self.envelope, AttemptEnvelope):
                raise TypeError("attempt finalization envelope must be an AttemptEnvelope.")
            if (
                self.envelope.attempt_id != self.attempt_id
                or self.envelope.evaluation_spec_digest != self.evaluation_spec_digest
                or self.envelope.binding_id != self.binding_id
            ):
                raise ValueError("Attempt finalization identity must match its envelope.")
            if (
                self.envelope.outcome != self.effective_outcome
                and not self.effective_code
            ):
                raise ValueError("An outcome override requires an effective_code.")
        else:
            error = _mapping_record(self.platform_error, "platform_error")
            _require_exact_keys(error, {"code", "message", "details"}, "platform_error")
            _required_text(error["code"], "platform_error.code")
            _required_text(error["message"], "platform_error.message")
            details = _mapping_record(error["details"], "platform_error.details")
            if error["code"] != self.effective_code:
                raise ValueError("platform_error.code must equal effective_code.")
            object.__setattr__(
                self,
                "platform_error",
                _freeze(
                    {"code": error["code"], "message": error["message"], "details": details},
                    "platform_error",
                ),
            )

        captures = tuple(self.captured_artifacts)
        if any(not isinstance(item, CapturedArtifact) for item in captures):
            raise TypeError(
                "attempt finalization captured_artifacts must contain CapturedArtifact records."
            )
        capture_ids = {item.declaration.declaration_id for item in captures}
        if len(capture_ids) != len(captures):
            raise ValueError("attempt finalization cannot capture a declaration twice.")
        if self.envelope is not None:
            declarations = self.envelope.output_declarations
            if any(item.declaration not in declarations for item in captures):
                raise ValueError(
                    "captured artifacts must match declarations in the attempt envelope."
                )
        object.__setattr__(self, "captured_artifacts", captures)

    @property
    def digest(self) -> str:
        value = hashlib.sha256(
            b"optpilot/attempt-finalization/v1\0"
            + _canonical_json_bytes(self.to_dict())
        ).hexdigest()
        return f"sha256:{value}"

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "evaluation_spec_digest": self.evaluation_spec_digest,
            "binding_id": self.binding_id,
            "effective_outcome": self.effective_outcome,
            "effective_code": self.effective_code,
            "captured_artifacts": [item.to_dict() for item in self.captured_artifacts],
            "envelope": None if self.envelope is None else self.envelope.to_dict(),
            "platform_error": None if self.platform_error is None else _thaw(self.platform_error),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AttemptFinalization":
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "attempt_id",
                "evaluation_spec_digest",
                "binding_id",
                "effective_outcome",
                "effective_code",
                "captured_artifacts",
                "envelope",
                "platform_error",
            },
            "attempt finalization",
        )
        captures = payload["captured_artifacts"]
        if not isinstance(captures, (list, tuple)):
            raise TypeError("attempt finalization captured_artifacts must be a sequence.")
        envelope = payload["envelope"]
        return cls(
            schema_version=payload["schema_version"],
            attempt_id=payload["attempt_id"],
            evaluation_spec_digest=payload["evaluation_spec_digest"],
            binding_id=payload["binding_id"],
            effective_outcome=payload["effective_outcome"],
            effective_code=payload["effective_code"],
            captured_artifacts=tuple(CapturedArtifact.from_dict(item) for item in captures),
            envelope=None if envelope is None else AttemptEnvelope.from_dict(envelope),
            platform_error=payload["platform_error"],
        )


class AttemptExecutor:
    """Execute exactly one environment attempt and return an envelope."""

    def __init__(
        self,
        candidate_validator: Any,
        materializer: Any,
        environment_adapter: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.candidate_validator = candidate_validator
        self.materializer = materializer
        self.environment_adapter = environment_adapter
        self._clock = clock

    def execute(
        self,
        spec: EvaluationSpec,
        binding: AttemptWorkspaceBinding,
        *,
        attempt_id: str,
    ) -> AttemptEnvelope:
        if not isinstance(spec, EvaluationSpec):
            raise TypeError("spec must be an EvaluationSpec.")
        if not isinstance(binding, AttemptWorkspaceBinding):
            raise TypeError("binding must be an AttemptWorkspaceBinding.")
        _required_text(attempt_id, "attempt_id")

        started = float(self._clock())
        context = _attempt_context(spec, binding, attempt_id=attempt_id)

        try:
            validation_report = self.candidate_validator.validate(
                _thaw(spec.candidate),
                _thaw(context),
            )
            _validation_payload(validation_report)
        except Exception as exc:
            validation_report = _existing_validation_exception_report(exc)
            materialization = MaterializationRecord(
                runtime_spec={},
                metadata={"skipped": True},
            )
            error = _existing_error_payload(exc, "validation")
            return self._terminal_envelope(
                spec,
                binding,
                attempt_id,
                validation_report,
                materialization,
                outcome="failed",
                phase="validation",
                error=error,
                started=started,
            )

        if not bool(validation_report.accepted):
            materialization = MaterializationRecord(
                runtime_spec={},
                metadata={"skipped": True},
            )
            errors = list(validation_report.errors)
            error = {
                "phase": "validation",
                "type": "ValidationError",
                "message": "; ".join(str(item) for item in errors)
                or "Candidate validation failed.",
                "errors": errors,
            }
            return self._terminal_envelope(
                spec,
                binding,
                attempt_id,
                validation_report,
                materialization,
                outcome="invalid",
                phase="validation",
                error=error,
                started=started,
            )

        try:
            materialization = self.materializer.materialize(
                _thaw(spec.candidate),
                binding.workspace,
                _thaw(context),
            )
            _materialization_payload(materialization)
            materialization_declarations = _output_declarations(
                materialization.output_files,
                "materialization output_files",
            )
        except Exception as exc:
            materialization = MaterializationRecord(
                runtime_spec={},
                metadata={"failed": True},
            )
            error = _existing_error_payload(exc, "materialization")
            return self._terminal_envelope(
                spec,
                binding,
                attempt_id,
                validation_report,
                materialization,
                outcome=_existing_status_for_exception(exc),
                phase="materialization",
                error=error,
                started=started,
            )

        try:
            result = self.environment_adapter.evaluate(
                _thaw(materialization.runtime_spec),
                _thaw(context),
            )
            result = _existing_validate_environment_result(result)
            environment_declarations = _output_declarations(
                result.get("output_files", []),
                "environment output_files",
            )
            output_declarations = _combine_output_declarations(
                materialization_declarations,
                environment_declarations,
            )
        except Exception as exc:
            result = _existing_exception_evaluation_result(
                exc,
                "environment_evaluation",
                binding.workspace,
            )
            environment_declarations = ()
            output_declarations = materialization_declarations

        event_summary = dict(result.get("event_summary", {}))
        event_summary.setdefault("primary_metric", spec.primary_metric_name)
        event_summary["materialization"] = _thaw(
            _freeze(materialization.metadata, "materialization metadata")
        )
        if event_summary.get("error") and "errors" not in event_summary:
            event_summary["errors"] = [event_summary["error"]]
        error = event_summary.get("error")
        return self._build_envelope(
            spec,
            binding,
            attempt_id,
            validation_report,
            materialization,
            outcome=str(result.get("status", "success")),
            phase="environment_evaluation",
            metric_values=dict(result.get("metric_values", {})),
            constraint_results=dict(result.get("constraint_results", {})),
            output_declarations=output_declarations,
            event_summary=event_summary,
            error=dict(error) if isinstance(error, Mapping) else {},
            started=started,
        )

    def _terminal_envelope(
        self,
        spec: EvaluationSpec,
        binding: AttemptWorkspaceBinding,
        attempt_id: str,
        validation_report: Any,
        materialization: Any,
        *,
        outcome: str,
        phase: str,
        error: Mapping[str, Any],
        started: float,
    ) -> AttemptEnvelope:
        event_summary = {
            "primary_metric": spec.primary_metric_name,
            "materialization": dict(materialization.metadata),
            "error": dict(error),
            "errors": [dict(error)],
        }
        return self._build_envelope(
            spec,
            binding,
            attempt_id,
            validation_report,
            materialization,
            outcome=outcome,
            phase=phase,
            metric_values={},
            constraint_results={},
            output_declarations=_output_declarations(
                materialization.output_files,
                "materialization output_files",
            ),
            event_summary=event_summary,
            error=error,
            started=started,
        )

    def _build_envelope(
        self,
        spec: EvaluationSpec,
        binding: AttemptWorkspaceBinding,
        attempt_id: str,
        validation_report: Any,
        materialization: Any,
        *,
        outcome: str,
        phase: str,
        metric_values: Mapping[str, Any],
        constraint_results: Mapping[str, Any],
        output_declarations: Sequence[OutputDeclaration],
        event_summary: Mapping[str, Any],
        error: Mapping[str, Any],
        started: float,
    ) -> AttemptEnvelope:
        elapsed = max(0.0, float(self._clock()) - started)
        validation = _validation_payload(validation_report)
        materialization_payload = _materialization_payload(materialization)
        candidate = _thaw(spec.candidate)
        resource_profile = _thaw(spec.resource_profile)
        sandbox_spec = _thaw(spec.sandbox_spec)
        backend_identity = _thaw(binding.backend_identity)
        backend_worker = _thaw(binding.backend_worker)

        execution_metadata: JsonDict = {
            "candidate_ref": spec.candidate_ref,
            "environment_revision_digest": spec.environment_revision_digest,
            "prepared_runtime_digest": spec.prepared_runtime_digest,
            "seed": _thaw(spec.seed),
            "repetition_index": spec.repetition_index,
            "resource_profile": resource_profile,
            "sandbox_spec": sandbox_spec,
            "backend_identity": backend_identity,
            "backend_worker": backend_worker,
            "candidate_lineage": dict(candidate.get("lineage", {})),
            "generator": dict(candidate.get("generator", {})),
            "binding_id": binding.binding_id,
        }
        if spec.metadata:
            execution_metadata["evaluation_metadata"] = _thaw(spec.metadata)

        return AttemptEnvelope(
            attempt_id=attempt_id,
            evaluation_spec_digest=spec.digest,
            binding_id=binding.binding_id,
            outcome=outcome,
            phase=phase,
            wall_clock_seconds=elapsed,
            validation=validation,
            materialization=materialization_payload,
            metric_values=dict(metric_values),
            constraint_results=dict(constraint_results),
            output_declarations=tuple(output_declarations),
            event_summary=dict(event_summary),
            execution_metadata=execution_metadata,
            error=dict(error),
        )


def _attempt_context(
    spec: EvaluationSpec,
    binding: AttemptWorkspaceBinding,
    *,
    attempt_id: str,
) -> Mapping[str, Any]:
    context: JsonDict = {
        "attempt_id": attempt_id,
        "evaluation_spec_digest": spec.digest,
        "binding_id": binding.binding_id,
        "workspace": str(binding.workspace),
        "resource_profile": _thaw(spec.resource_profile),
        "sandbox_spec": _thaw(spec.sandbox_spec),
        "backend_identity": _thaw(binding.backend_identity),
        "backend_worker": _thaw(binding.backend_worker),
        "seed": _thaw(spec.seed),
        "repetition_index": spec.repetition_index,
    }
    context.update(_thaw(binding.context))
    return _freeze(context, "attempt context")


def _validation_payload(report: Any) -> JsonDict:
    if not isinstance(getattr(report, "accepted", None), bool):
        raise TypeError("Candidate validator must return a report with boolean accepted.")
    if not isinstance(getattr(report, "errors", None), list):
        raise TypeError("Candidate validator report errors must be a list.")
    payload = report.to_dict() if callable(getattr(report, "to_dict", None)) else None
    return _mapping_record(payload, "validation report")


def _materialization_payload(record: Any) -> JsonDict:
    if not isinstance(getattr(record, "runtime_spec", None), Mapping):
        raise TypeError("Candidate materializer must return a mapping runtime_spec.")
    if not isinstance(getattr(record, "output_files", None), list):
        raise TypeError("Candidate materializer output_files must be a list.")
    if not isinstance(getattr(record, "metadata", None), Mapping):
        raise TypeError("Candidate materializer metadata must be a mapping.")
    payload = record.to_dict() if callable(getattr(record, "to_dict", None)) else None
    return _mapping_record(payload, "materialization record")


def _output_declarations(
    values: Any,
    label: str,
) -> Tuple[OutputDeclaration, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{label} must be a sequence.")
    declarations = tuple(
        item
        if isinstance(item, OutputDeclaration)
        else OutputDeclaration.from_dict(_mapping_record(item, f"{label}[{index}]"))
        for index, item in enumerate(values)
    )
    declaration_ids = {item.declaration_id for item in declarations}
    if len(declaration_ids) != len(declarations):
        raise ValueError(f"{label} declaration ids must be unique.")
    return declarations


def _combine_output_declarations(
    first: Sequence[OutputDeclaration],
    second: Sequence[OutputDeclaration],
) -> Tuple[OutputDeclaration, ...]:
    declarations = (*tuple(first), *tuple(second))
    declaration_ids = {item.declaration_id for item in declarations}
    if len(declaration_ids) != len(declarations):
        raise ValueError(
            "Materializer and environment output declaration ids must be unique."
        )
    return declarations


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("output declaration path must be a non-empty string.")
    if "\\" in value or "\x00" in value:
        raise ValueError("output declaration path must use a safe portable relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or path.as_posix() == "."
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and re.match(r"^[A-Za-z]:", path.parts[0]))
    ):
        raise ValueError("output declaration path must use a safe portable relative path.")
    return path.as_posix()


def _sha256_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be sha256:<64 lowercase hex>.")
    return value


def _lower_hex_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _LOWER_HEX_DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters.")
    return value


def _physical_content_ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _CONTENT_REF_RE.fullmatch(value):
        raise ValueError(
            f"{label} must be blob:sha256:<64 lowercase hex> or "
            "tree:sha256:<64 lowercase hex>."
        )
    return value


def _capture_binding(
    value: Any,
    content_ref: str,
    index: int,
) -> Mapping[str, Any]:
    payload = _mapping_record(value, f"captured artifact bindings[{index}]")
    _require_exact_keys(
        payload,
        {"store_id", "content_ref"},
        f"captured artifact bindings[{index}]",
    )
    _required_text(payload["store_id"], f"captured artifact bindings[{index}].store_id")
    _physical_content_ref(
        payload["content_ref"],
        f"captured artifact bindings[{index}].content_ref",
    )
    if payload["content_ref"] != content_ref:
        raise ValueError("captured artifact binding content_ref must match the artifact.")
    return _freeze(payload, f"captured artifact bindings[{index}]")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TypeError(f"Value is not canonical JSON: {error}") from error


def _mapping_record(value: Any, label: str) -> JsonDict:
    if callable(getattr(value, "to_dict", None)) and not isinstance(value, Mapping):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    return {key: child for key, child in value.items()}


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"{label} fields differ; missing={missing!r}, extra={extra!r}."
        )


def _freeze(value: Any, label: str) -> Any:
    if isinstance(value, Mapping):
        frozen: JsonDict = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} keys must be strings.")
            frozen[key] = _freeze(child, f"{label}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child, f"{label}[]") for child in value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError(f"{label} must contain finite JSON numbers.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{label} must contain JSON-like values; got {type(value).__name__}.")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value


def _optional_text(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string when provided.")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer.")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer.")
    return value


__all__ = [
    "AttemptEnvelope",
    "AttemptExecutor",
    "AttemptFinalization",
    "AttemptWorkspaceBinding",
    "CapturedArtifact",
    "EvaluationSpec",
    "OutputDeclaration",
]
