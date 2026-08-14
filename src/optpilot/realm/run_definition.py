"""Complete immutable inputs for one Realm-managed study run.

The environment side of evaluation is already represented by
``RunEvaluationClosure``.  This module adds the corresponding authored method
revision and prepared method runtime, then binds both sides to run control and
the full study policies in one canonical ``RunDefinitionManifest``.

All filesystem dependencies are typed immutable ``ScopeLayer`` values.  Host
paths, provider placement, mutable checkouts, and runtime leases are deliberately
outside these records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from ..run_control_manifest import RunControlManifest, candidate_contract_digest
from ._validation import (
    freeze_json,
    lower_hex_digest,
    optional_text,
    positive_int,
    required_text,
    thaw_json,
)
from .errors import RealmIntegrityError
from .refs import PhysicalContentRef, canonical_json_bytes, request_digest
from .run_closure import RunEvaluationClosure, ScopeLayer, ScopePath
from ..image_reference import IMAGE_DIGEST_RE


JsonDict = Dict[str, Any]

METHOD_CONTRACT_SCHEMA = "optpilot.method-contract.v1"
METHOD_REVISION_MANIFEST_SCHEMA = "optpilot.method-revision-manifest.v1"
PREPARED_METHOD_RUNTIME_MANIFEST_SCHEMA = (
    "optpilot.prepared-method-runtime-manifest.v1"
)
RUN_DEFINITION_MANIFEST_SCHEMA = "optpilot.run-definition-manifest.v1"

RUN_METHOD_SOURCE_ROLE = "run-method-source"
RUN_PREPARED_METHOD_RUNTIME_ROLE = "run-prepared-method-runtime"

_OCI_DIGEST_RE = IMAGE_DIGEST_RE
_RUNTIME_KINDS = frozenset({"process", "container"})
_PORTABILITY_VALUES = frozenset({"portable", "provider-scoped"})
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_REQUIRED_CONTENT_REFS = 2048


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _freeze_mapping(value: Any, label: str, *, nonempty: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty.")
    frozen = freeze_json(value, label=label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError(f"{label} must be a mapping.")
    canonical_json_bytes(thaw_json(frozen))
    return frozen


def _layer_sort_key(layer: ScopeLayer) -> tuple[object, ...]:
    return (
        layer.scope.encode("utf-8"),
        layer.destination_subpath.encode("utf-8"),
        layer.precedence,
        layer.source_subpath.encode("utf-8"),
        str(layer.snapshot_ref),
    )


def _canonical_layers(
    values: Sequence[ScopeLayer], label: str, *, nonempty: bool = False
) -> Tuple[ScopeLayer, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence of ScopeLayer values.")
    layers = tuple(values)
    if any(not isinstance(item, ScopeLayer) for item in layers):
        raise TypeError(f"{label} must contain ScopeLayer values.")
    if nonempty and not layers:
        raise ValueError(f"{label} must not be empty.")
    if len(set(layers)) != len(layers):
        raise ValueError(f"{label} must not contain duplicate layers.")
    targets: set[tuple[str, str, int]] = set()
    for layer in layers:
        target = (layer.scope, layer.destination_subpath, layer.precedence)
        if target in targets:
            raise ValueError(
                f"{label} assigns the same scope, destination, and precedence more than once."
            )
        targets.add(target)
    return tuple(sorted(layers, key=_layer_sort_key))


def _bounded_record(payload: Mapping[str, Any], label: str) -> None:
    if len(canonical_json_bytes(payload)) > _MAX_RECORD_BYTES:
        raise ValueError(f"{label} exceeds the maximum encoded size.")


def _decode_canonical_object(payload: bytes, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError(f"{label} bytes must be bytes.")
    if len(payload) > _MAX_RECORD_BYTES:
        raise RealmIntegrityError(f"{label} exceeds the maximum encoded size.")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(
            f"{label} is not valid UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RealmIntegrityError(f"{label} must encode a JSON object.")
    if canonical_json_bytes(value) != payload:
        raise RealmIntegrityError(f"{label} bytes are not canonical JSON.")
    return value


def _policy_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping.")
    return value


@dataclass(frozen=True)
class MethodRevisionManifest:
    """Exact path-free authored method semantics and immutable source layers."""

    method_id: str
    protocol: str
    compiler_id: str
    compiler_version: str
    authored_config: ScopePath
    source_layers: Tuple[ScopeLayer, ...]
    method_contract: Mapping[str, Any]

    def __post_init__(self) -> None:
        required_text(self.method_id, "method revision id")
        required_text(self.protocol, "method revision protocol", max_bytes=128)
        required_text(self.compiler_id, "method revision compiler id", max_bytes=256)
        required_text(
            self.compiler_version,
            "method revision compiler version",
            max_bytes=128,
        )
        if not isinstance(self.authored_config, ScopePath):
            raise TypeError("method authored_config must be a ScopePath.")
        layers = _canonical_layers(
            self.source_layers, "method source_layers", nonempty=True
        )
        if self.authored_config.scope not in {item.scope for item in layers}:
            raise ValueError("method authored_config scope is absent from source_layers.")
        contract = _freeze_mapping(
            self.method_contract, "method contract", nonempty=True
        )
        expected_contract_fields = {
            "compatibility",
            "config",
            "implementation",
            "runtime_requirements",
            "sandbox_spec",
            "schema",
            "settings",
        }
        if set(contract) != expected_contract_fields:
            raise ValueError("method contract fields differ.")
        if contract.get("schema") != METHOD_CONTRACT_SCHEMA:
            raise ValueError("method contract schema is unsupported.")
        implementation = _policy_mapping(
            contract.get("implementation"), "method contract implementation"
        )
        if implementation.get("protocol") != self.protocol:
            raise ValueError("method contract protocol differs from the revision protocol.")
        object.__setattr__(self, "source_layers", layers)
        object.__setattr__(self, "method_contract", contract)
        _bounded_record(self.to_dict(), "method revision manifest")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def revision_digest(self) -> str:
        return self.digest

    def to_dict(self) -> JsonDict:
        return {
            "authored_config": self.authored_config.to_dict(),
            "compiler": {"id": self.compiler_id, "version": self.compiler_version},
            "method_contract": thaw_json(self.method_contract),
            "method_id": self.method_id,
            "protocol": self.protocol,
            "schema": METHOD_REVISION_MANIFEST_SCHEMA,
            "source_layers": [item.to_dict() for item in self.source_layers],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MethodRevisionManifest":
        try:
            _exact_keys(
                payload,
                {
                    "authored_config",
                    "compiler",
                    "method_contract",
                    "method_id",
                    "protocol",
                    "schema",
                    "source_layers",
                },
                "method revision manifest",
            )
            if payload["schema"] != METHOD_REVISION_MANIFEST_SCHEMA:
                raise ValueError("method revision manifest schema is unsupported.")
            compiler = payload["compiler"]
            _exact_keys(compiler, {"id", "version"}, "method revision compiler")
            if not isinstance(payload["source_layers"], list):
                raise TypeError("method revision source_layers must be a list.")
            result = cls(
                method_id=payload["method_id"],
                protocol=payload["protocol"],
                compiler_id=compiler["id"],
                compiler_version=compiler["version"],
                authored_config=ScopePath.from_dict(payload["authored_config"]),
                source_layers=tuple(
                    ScopeLayer.from_dict(item) for item in payload["source_layers"]
                ),
                method_contract=payload["method_contract"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("method revision manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted method revision manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "MethodRevisionManifest":
        return cls.from_dict(_decode_canonical_object(payload, "method revision manifest"))


@dataclass(frozen=True)
class PreparedMethodRuntimeManifest:
    """Exact prepared method runtime anchored to one method revision."""

    method_revision_digest: str
    runtime_kind: str
    runtime_settings: Mapping[str, Any]
    prepared_layers: Tuple[ScopeLayer, ...] = field(default_factory=tuple)
    workdir: Optional[ScopePath] = None
    oci_image_digest: Optional[str] = None
    platform: Optional[str] = None
    portability: str = "portable"
    builder_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        lower_hex_digest(
            self.method_revision_digest, "method runtime method revision digest"
        )
        required_text(self.runtime_kind, "method runtime kind", max_bytes=64)
        if self.runtime_kind not in _RUNTIME_KINDS:
            raise ValueError("method runtime_kind must be 'process' or 'container'.")
        if self.workdir is not None and not isinstance(self.workdir, ScopePath):
            raise TypeError("method runtime workdir must be a ScopePath or None.")
        image_digest = optional_text(
            self.oci_image_digest, "method runtime OCI image digest", max_bytes=71
        )
        if image_digest is not None and _OCI_DIGEST_RE.fullmatch(image_digest) is None:
            raise ValueError(
                "method runtime OCI image digest must be sha256:<64 lowercase hex>."
            )
        if self.runtime_kind == "container" and image_digest is None:
            raise ValueError(
                "container method runtimes require an immutable OCI image digest."
            )
        if self.runtime_kind == "process" and image_digest is not None:
            raise ValueError("process method runtimes cannot define an OCI image digest.")
        optional_text(self.platform, "method runtime platform", max_bytes=256)
        required_text(self.portability, "method runtime portability", max_bytes=64)
        if self.portability not in _PORTABILITY_VALUES:
            raise ValueError(
                "method runtime portability must be 'portable' or 'provider-scoped'."
            )
        if self.builder_fingerprint is not None:
            lower_hex_digest(
                self.builder_fingerprint, "method runtime builder fingerprint"
            )
        object.__setattr__(
            self,
            "runtime_settings",
            _freeze_mapping(self.runtime_settings, "method runtime settings"),
        )
        object.__setattr__(
            self,
            "prepared_layers",
            _canonical_layers(
                self.prepared_layers, "method runtime prepared_layers"
            ),
        )
        object.__setattr__(self, "oci_image_digest", image_digest)
        _bounded_record(self.to_dict(), "prepared method runtime manifest")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def runtime_digest(self) -> str:
        return self.digest

    def to_dict(self) -> JsonDict:
        return {
            "builder_fingerprint": self.builder_fingerprint,
            "method_revision_digest": self.method_revision_digest,
            "oci_image_digest": self.oci_image_digest,
            "platform": self.platform,
            "portability": self.portability,
            "prepared_layers": [item.to_dict() for item in self.prepared_layers],
            "runtime_kind": self.runtime_kind,
            "runtime_settings": thaw_json(self.runtime_settings),
            "schema": PREPARED_METHOD_RUNTIME_MANIFEST_SCHEMA,
            "workdir": self.workdir.to_dict() if self.workdir is not None else None,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "PreparedMethodRuntimeManifest":
        try:
            _exact_keys(
                payload,
                {
                    "builder_fingerprint",
                    "method_revision_digest",
                    "oci_image_digest",
                    "platform",
                    "portability",
                    "prepared_layers",
                    "runtime_kind",
                    "runtime_settings",
                    "schema",
                    "workdir",
                },
                "prepared method runtime manifest",
            )
            if payload["schema"] != PREPARED_METHOD_RUNTIME_MANIFEST_SCHEMA:
                raise ValueError("prepared method runtime schema is unsupported.")
            if not isinstance(payload["prepared_layers"], list):
                raise TypeError("method runtime prepared_layers must be a list.")
            result = cls(
                method_revision_digest=payload["method_revision_digest"],
                runtime_kind=payload["runtime_kind"],
                runtime_settings=payload["runtime_settings"],
                prepared_layers=tuple(
                    ScopeLayer.from_dict(item) for item in payload["prepared_layers"]
                ),
                workdir=(
                    ScopePath.from_dict(payload["workdir"])
                    if payload["workdir"] is not None
                    else None
                ),
                oci_image_digest=payload["oci_image_digest"],
                platform=payload["platform"],
                portability=payload["portability"],
                builder_fingerprint=payload["builder_fingerprint"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("prepared method runtime manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted prepared method runtime manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "PreparedMethodRuntimeManifest":
        return cls.from_dict(
            _decode_canonical_object(payload, "prepared method runtime manifest")
        )


@dataclass(frozen=True)
class RunDefinitionManifest:
    """Complete path-free semantic definition of one runnable study."""

    evaluation_closure: RunEvaluationClosure
    method_revision: MethodRevisionManifest
    prepared_method_runtime: PreparedMethodRuntimeManifest
    run_control_manifest: RunControlManifest
    evaluator_capacity: int
    execution_policy: Mapping[str, Any]
    evidence_policy: Mapping[str, Any]
    reproducibility_policy: Mapping[str, Any]
    metadata: Mapping[str, Any]
    compiler_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_closure, RunEvaluationClosure):
            raise TypeError("evaluation_closure must be a RunEvaluationClosure.")
        if not isinstance(self.method_revision, MethodRevisionManifest):
            raise TypeError("method_revision must be a MethodRevisionManifest.")
        if not isinstance(
            self.prepared_method_runtime, PreparedMethodRuntimeManifest
        ):
            raise TypeError(
                "prepared_method_runtime must be a PreparedMethodRuntimeManifest."
            )
        if not isinstance(self.run_control_manifest, RunControlManifest):
            raise TypeError("run_control_manifest must be a RunControlManifest.")
        positive_int(self.evaluator_capacity, "evaluator capacity")
        required_text(self.compiler_version, "run definition compiler version", max_bytes=128)

        for field_name in (
            "execution_policy",
            "evidence_policy",
            "reproducibility_policy",
            "metadata",
        ):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, _freeze_mapping(value, field_name))

        method = self.method_revision
        method_runtime = self.prepared_method_runtime
        control = self.run_control_manifest
        closure = self.evaluation_closure
        environment = closure.environment_revision
        template = closure.evaluation_template

        if method_runtime.method_revision_digest != method.digest:
            raise ValueError("prepared method runtime targets a different method revision.")
        method_scopes = {
            layer.scope
            for layer in (*method.source_layers, *method_runtime.prepared_layers)
        }
        if method_runtime.workdir is not None and method_runtime.workdir.scope not in method_scopes:
            raise ValueError(
                "method runtime workdir scope is absent from method source/prepared layers."
            )
        runtime_requirements = _policy_mapping(
            method.method_contract.get("runtime_requirements"),
            "method runtime requirements",
        )
        required_method_runtime_kind = runtime_requirements.get("type", "process")
        if required_method_runtime_kind not in _RUNTIME_KINDS:
            raise ValueError("method contract runtime kind is unsupported.")
        if required_method_runtime_kind != method_runtime.runtime_kind:
            raise ValueError("method contract and prepared method runtime kinds differ.")
        method_sandbox = _policy_mapping(
            method.method_contract.get("sandbox_spec"), "method sandbox spec"
        )
        if (
            method_sandbox.get("runtimeType") is not None
            and method_sandbox.get("runtimeType") != method_runtime.runtime_kind
        ):
            raise ValueError("method sandbox and prepared method runtime kinds differ.")

        if method.method_id != control.method_id or method.protocol != control.method_protocol:
            raise ValueError("run control and method revision differ.")
        if control.compiler_version != self.compiler_version:
            raise ValueError(
                "run-control compiler version differs from the run definition compiler."
            )
        if candidate_contract_digest(environment.candidate_contract) != control.candidate_contract_digest:
            raise ValueError("run control and environment candidate contracts differ.")
        primary = template.objective.get("primaryMetric")
        if (
            not isinstance(primary, Mapping)
            or primary.get("name") != control.objective_metric
            or primary.get("direction") != control.objective_direction
        ):
            raise ValueError("run control and evaluation objectives differ.")

        execution = self.execution_policy
        defaults = _policy_mapping(execution.get("defaults"), "execution defaults")
        resource_profile = _policy_mapping(
            defaults.get("resourceProfile"), "execution resource profile"
        )
        sandbox_spec = _policy_mapping(
            defaults.get("sandboxSpec"), "execution sandbox spec"
        )
        if canonical_json_bytes(thaw_json(resource_profile)) != canonical_json_bytes(
            thaw_json(template.resource_profile)
        ):
            raise ValueError("execution policy and evaluation resource profile differ.")
        if canonical_json_bytes(thaw_json(sandbox_spec)) != canonical_json_bytes(
            thaw_json(template.sandbox_spec)
        ):
            raise ValueError("execution policy and evaluation sandbox spec differ.")
        environment_runtime_kind = closure.prepared_runtime.runtime_kind
        if (
            sandbox_spec.get("runtimeType") is not None
            and sandbox_spec.get("runtimeType") != environment_runtime_kind
        ):
            raise ValueError("execution sandbox and prepared environment runtime kinds differ.")
        backend = _policy_mapping(execution.get("backend"), "execution backend")
        backend_type = backend.get("type")
        backend_runtime_kind = (
            "process" if backend_type == "local" else "container" if backend_type == "container" else None
        )
        if backend_runtime_kind != environment_runtime_kind:
            raise ValueError("execution backend and prepared environment runtime kinds differ.")

        parallelism = _policy_mapping(
            execution.get("parallelism"), "execution parallelism"
        )
        if parallelism.get("candidateParallelism") != self.evaluator_capacity:
            raise ValueError("execution policy and evaluator capacity differ.")
        scheduler = _policy_mapping(execution.get("scheduler"), "execution scheduler")
        scheduler_config = _policy_mapping(
            scheduler.get("config"), "execution scheduler config"
        )
        scheduler_retry = _policy_mapping(
            scheduler_config.get("retryPolicy"), "execution scheduler retry policy"
        )
        legacy_retry = _policy_mapping(
            defaults.get("retryPolicy"), "execution default retry policy"
        )
        retry_statuses = scheduler_retry.get("retryStatuses")
        if not isinstance(retry_statuses, (list, tuple)):
            raise ValueError("execution retryStatuses must be a list.")
        if (
            scheduler_retry.get("maxAttempts") != control.retry_policy.max_attempts
            or len(set(retry_statuses)) != len(retry_statuses)
            or set(retry_statuses) != set(control.retry_policy.retryable_outcomes)
            or legacy_retry.get("maxRetries") != control.retry_policy.max_attempts - 1
        ):
            raise ValueError("execution policy and run-control retry policy differ.")

        method_config = _policy_mapping(
            method.method_contract.get("config"), "method config"
        )
        method_settings = _policy_mapping(
            method.method_contract.get("settings"), "method settings"
        )
        if (
            method_config.get("batchSize") != control.proposal_width
            or method_settings.get("batchSize") != control.proposal_width
        ):
            raise ValueError("method revision and run-control proposal width differ.")

        seed_policy = _policy_mapping(
            self.reproducibility_policy.get("seedPolicy"),
            "reproducibility seed policy",
        )
        if canonical_json_bytes(thaw_json(seed_policy.get("globalSeed"))) != canonical_json_bytes(
            thaw_json(template.default_seed)
        ):
            raise ValueError("reproducibility policy and evaluation seed differ.")
        if seed_policy.get("perTrialDerivation") != "deterministic_hash":
            raise ValueError("reproducibility seed derivation is unsupported.")

        if len(self.required_content_refs) > _MAX_REQUIRED_CONTENT_REFS:
            raise ValueError("run definition requires too many semantic content refs.")
        _bounded_record(self.to_dict(), "run definition manifest")

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @property
    def required_content_refs(
        self,
    ) -> Tuple[Tuple[str, PhysicalContentRef], ...]:
        values = set(self.evaluation_closure.required_content_refs)
        values.update(
            (RUN_METHOD_SOURCE_ROLE, layer.snapshot_ref)
            for layer in self.method_revision.source_layers
        )
        values.update(
            (RUN_PREPARED_METHOD_RUNTIME_ROLE, layer.snapshot_ref)
            for layer in self.prepared_method_runtime.prepared_layers
        )
        return tuple(sorted(values, key=lambda item: (item[0], str(item[1]))))

    @property
    def content_refs_by_role(self) -> Mapping[str, Tuple[PhysicalContentRef, ...]]:
        grouped: dict[str, list[PhysicalContentRef]] = {}
        for role, content_ref in self.required_content_refs:
            grouped.setdefault(role, []).append(content_ref)
        return MappingProxyType(
            {role: tuple(refs) for role, refs in sorted(grouped.items())}
        )

    def _required_content_refs_dict(self) -> list[JsonDict]:
        return [
            {"content_ref": str(content_ref), "role": role}
            for role, content_ref in self.required_content_refs
        ]

    def to_dict(self) -> JsonDict:
        return {
            "compiler_version": self.compiler_version,
            "evaluation_closure": self.evaluation_closure.to_dict(),
            "evaluation_closure_digest": self.evaluation_closure.digest,
            "evaluator_capacity": self.evaluator_capacity,
            "evidence_policy": thaw_json(self.evidence_policy),
            "execution_policy": thaw_json(self.execution_policy),
            "metadata": thaw_json(self.metadata),
            "method_revision": self.method_revision.to_dict(),
            "method_revision_digest": self.method_revision.digest,
            "prepared_method_runtime": self.prepared_method_runtime.to_dict(),
            "prepared_method_runtime_digest": self.prepared_method_runtime.digest,
            "reproducibility_policy": thaw_json(self.reproducibility_policy),
            "required_content_refs": self._required_content_refs_dict(),
            "run_control_manifest": self.run_control_manifest.to_dict(),
            "run_control_manifest_digest": self.run_control_manifest.digest,
            "schema": RUN_DEFINITION_MANIFEST_SCHEMA,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunDefinitionManifest":
        try:
            _exact_keys(
                payload,
                {
                    "compiler_version",
                    "evaluation_closure",
                    "evaluation_closure_digest",
                    "evaluator_capacity",
                    "evidence_policy",
                    "execution_policy",
                    "metadata",
                    "method_revision",
                    "method_revision_digest",
                    "prepared_method_runtime",
                    "prepared_method_runtime_digest",
                    "reproducibility_policy",
                    "required_content_refs",
                    "run_control_manifest",
                    "run_control_manifest_digest",
                    "schema",
                },
                "run definition manifest",
            )
            if payload["schema"] != RUN_DEFINITION_MANIFEST_SCHEMA:
                raise ValueError("run definition manifest schema is unsupported.")
            if not isinstance(payload["required_content_refs"], list):
                raise TypeError("run definition required_content_refs must be a list.")
            closure = RunEvaluationClosure.from_dict(payload["evaluation_closure"])
            method = MethodRevisionManifest.from_dict(payload["method_revision"])
            runtime = PreparedMethodRuntimeManifest.from_dict(
                payload["prepared_method_runtime"]
            )
            control = RunControlManifest.from_dict(payload["run_control_manifest"])
            if payload["evaluation_closure_digest"] != closure.digest:
                raise ValueError("evaluation closure digest does not match.")
            if payload["method_revision_digest"] != method.digest:
                raise ValueError("method revision digest does not match.")
            if payload["prepared_method_runtime_digest"] != runtime.digest:
                raise ValueError("prepared method runtime digest does not match.")
            if payload["run_control_manifest_digest"] != control.digest:
                raise ValueError("run-control manifest digest does not match.")
            result = cls(
                evaluation_closure=closure,
                method_revision=method,
                prepared_method_runtime=runtime,
                run_control_manifest=control,
                evaluator_capacity=payload["evaluator_capacity"],
                execution_policy=payload["execution_policy"],
                evidence_policy=payload["evidence_policy"],
                reproducibility_policy=payload["reproducibility_policy"],
                metadata=payload["metadata"],
                compiler_version=payload["compiler_version"],
            )
            if result.to_dict() != dict(payload):
                raise ValueError("run definition manifest is not canonical.")
            return result
        except RealmIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise RealmIntegrityError(
                f"Persisted run definition manifest is invalid: {error}"
            ) from error

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RunDefinitionManifest":
        return cls.from_dict(_decode_canonical_object(payload, "run definition manifest"))


__all__ = [
    "METHOD_CONTRACT_SCHEMA",
    "METHOD_REVISION_MANIFEST_SCHEMA",
    "PREPARED_METHOD_RUNTIME_MANIFEST_SCHEMA",
    "RUN_DEFINITION_MANIFEST_SCHEMA",
    "RUN_METHOD_SOURCE_ROLE",
    "RUN_PREPARED_METHOD_RUNTIME_ROLE",
    "MethodRevisionManifest",
    "PreparedMethodRuntimeManifest",
    "RunDefinitionManifest",
]
