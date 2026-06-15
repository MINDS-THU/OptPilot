"""Study specification loading and minimal validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


REQUIRED_TOP_LEVEL = {
    "apiVersion",
    "kind",
    "metadata",
    "target",
    "objective",
    "evaluationScope",
    "artifacts",
    "method",
    "execution",
    "evidence",
    "reproducibility",
    "stopping",
}

SUPPORTED_ACCESS_POLICIES = {
    "InvocationOnly",
    "SchemaAware",
    "TraceAware",
    "CodeAwareReadOnly",
    "FullStudyContext",
}
SUPPORTED_MUTATION_POLICIES = {
    "NoMutation",
    "StudyArtifactOnly",
    "StudyWorkspaceOnly",
    "MethodConfigOnly",
}
SUPPORTED_EVALUATION_SCOPES = {"FixedInstance", "InstanceSet", "Distribution"}
SUPPORTED_DIRECTIONS = {"maximize", "minimize"}
SUPPORTED_AGGREGATIONS = {"mean", "median", "min", "max", "sum", "last", "weighted_mean"}


@dataclass
class StudySpec:
    path: Path
    raw: Dict[str, Any]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def metadata(self) -> Dict[str, Any]:
        return self.raw["metadata"]

    @property
    def name(self) -> str:
        return self.metadata["name"]

    @property
    def target(self) -> Dict[str, Any]:
        return self.raw["target"]

    @property
    def objective(self) -> Dict[str, Any]:
        return self.raw["objective"]

    @property
    def evaluation_scope(self) -> Dict[str, Any]:
        return self.raw["evaluationScope"]

    @property
    def artifacts(self) -> Dict[str, Any]:
        return self.raw["artifacts"]

    @property
    def method(self) -> Dict[str, Any]:
        return self.raw["method"]

    @property
    def execution(self) -> Dict[str, Any]:
        return self.raw["execution"]

    @property
    def evidence(self) -> Dict[str, Any]:
        return self.raw["evidence"]

    @property
    def reproducibility(self) -> Dict[str, Any]:
        return self.raw["reproducibility"]

    @property
    def stopping(self) -> Dict[str, Any]:
        return self.raw["stopping"]

    @property
    def primary_artifact(self) -> Dict[str, Any]:
        return self.artifacts["primaryArtifact"]

    @property
    def primary_metric_name(self) -> str:
        return self.objective["primaryMetric"]["name"]

    @property
    def primary_metric_direction(self) -> str:
        return self.objective["primaryMetric"]["direction"]

    @property
    def candidate_parallelism(self) -> int:
        return int(self.execution.get("parallelism", {}).get("candidateParallelism", 1))

    def resolve_path(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        return (self.base_dir / candidate).resolve()

    def load_ref_or_inline(self, field: str, ref_key: str = "instanceRef") -> Dict[str, Any]:
        data = self.evaluation_scope.get("definition", {})
        if ref_key in data:
            with self.resolve_path(data[ref_key]).open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        return dict(data.get(field, {}))

    def build_instance_batch(self, rng) -> List[Dict[str, Any]]:
        mode = self.evaluation_scope["mode"]
        definition = self.evaluation_scope.get("definition", {})
        if mode == "FixedInstance":
            if "instance" in definition:
                return [dict(definition["instance"])]
            if "instanceRef" in definition:
                with self.resolve_path(definition["instanceRef"]).open("r", encoding="utf-8") as handle:
                    return [yaml.safe_load(handle) or {}]
            raise ValueError("FixedInstance scope requires 'instance' or 'instanceRef'.")
        if mode == "InstanceSet":
            refs = definition.get("instanceRefs", [])
            if not refs:
                raise ValueError("InstanceSet scope requires 'instanceRefs'.")
            instances = []
            for ref in refs:
                with self.resolve_path(ref).open("r", encoding="utf-8") as handle:
                    instances.append(yaml.safe_load(handle) or {})
            return instances
        if mode == "Distribution":
            sampler = definition.get("sampler", {})
            config = sampler.get("config", {})
            sample_count = int(definition.get("sampleCount", 1))
            return [_sample_distribution_instance(config, rng) for _ in range(sample_count)]
        raise NotImplementedError(f"Unsupported evaluationScope mode: {mode}")


def _sample_distribution_instance(config: Dict[str, Any], rng) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, list) and len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
            low, high = value
            result[key] = rng.uniform(low, high)
            continue
        if isinstance(value, list):
            result[key] = rng.choice(value)
            continue
        result[key] = value
    return result


def load_study_spec(path: str) -> StudySpec:
    spec_path = Path(path).resolve()
    with spec_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if raw.get("kind") != "StudyConfig":
        raise ValueError("OptPilot user config must be kind 'StudyConfig'. Expanded StudySpec is internal.")
    from .config import compile_authoring_config

    return study_spec_from_raw(spec_path, compile_authoring_config(spec_path))


def load_expanded_study_spec(path: str) -> StudySpec:
    """Load an already-expanded internal StudySpec.

    This is intentionally separate from the public user-facing loader. It is
    useful for tests, workers, and internal import/validation paths that already
    operate on the canonical execution representation.
    """

    spec_path = Path(path).resolve()
    with spec_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return study_spec_from_raw(spec_path, raw)


def study_spec_from_raw(spec_path: Path, raw: Dict[str, Any]) -> StudySpec:
    missing = REQUIRED_TOP_LEVEL - raw.keys()
    if missing:
        raise ValueError(f"StudySpec missing required top-level keys: {sorted(missing)}")
    if raw.get("kind") != "StudySpec":
        raise ValueError("kind must be 'StudySpec'")
    if not raw.get("method"):
        raise ValueError("StudySpec must define method")
    _validate_study_spec(raw)
    return StudySpec(path=spec_path, raw=raw)


def _validate_study_spec(raw: Dict[str, Any]) -> None:
    target = raw["target"]
    access_policy = target.get("accessPolicy")
    if access_policy not in SUPPORTED_ACCESS_POLICIES:
        raise ValueError(
            f"Unsupported target.accessPolicy {access_policy!r}; expected one of {sorted(SUPPORTED_ACCESS_POLICIES)}"
        )
    mutation_policy = target.get("mutationPolicy")
    if mutation_policy not in SUPPORTED_MUTATION_POLICIES:
        raise ValueError(
            f"Unsupported target.mutationPolicy {mutation_policy!r}; expected one of {sorted(SUPPORTED_MUTATION_POLICIES)}"
        )

    direction = raw["objective"].get("primaryMetric", {}).get("direction")
    if direction not in SUPPORTED_DIRECTIONS:
        raise ValueError(f"Unsupported objective direction {direction!r}; expected 'maximize' or 'minimize'.")
    aggregation = raw["objective"].get("aggregation", {}).get("mode", "mean")
    if aggregation not in SUPPORTED_AGGREGATIONS:
        raise ValueError(
            f"Unsupported objective aggregation {aggregation!r}; expected one of {sorted(SUPPORTED_AGGREGATIONS)}."
        )

    scope_mode = raw["evaluationScope"].get("mode")
    if scope_mode not in SUPPORTED_EVALUATION_SCOPES:
        raise NotImplementedError(
            f"Unsupported evaluationScope mode {scope_mode!r}; implemented modes are {sorted(SUPPORTED_EVALUATION_SCOPES)}"
        )

    _require_component("target.adapter", target.get("adapter", {}))
    _require_component("execution.backend", raw["execution"].get("backend", {}))
    if "scheduler" in raw["execution"]:
        _require_component("execution.scheduler", raw["execution"].get("scheduler", {}))
    _require_method("method", raw["method"])

    candidate_parallelism = int(raw["execution"].get("parallelism", {}).get("candidateParallelism", 1))
    if candidate_parallelism < 1:
        raise ValueError("execution.parallelism.candidateParallelism must be >= 1.")


def _require_component(location: str, definition: Dict[str, Any]) -> None:
    implementation = definition.get("implementation")
    if not implementation:
        raise ValueError(f"{location} must define an implementation.")
    if not (implementation.startswith("builtin.") or implementation.startswith("python:")):
        raise ValueError(
        f"{location}.implementation must start with 'builtin.' or 'python:'; got {implementation!r}."
    )


def _require_method(location: str, definition: Dict[str, Any]) -> None:
    implementation = definition.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError(f"{location}.implementation must be an object.")
    implementation_type = implementation.get("type")
    if implementation_type not in {"python", "command"}:
        raise ValueError(f"{location}.implementation.type must be one of ['command', 'python'].")
    if implementation_type == "python":
        callable_ref = implementation.get("callable") or implementation.get("implementation")
        if not callable_ref:
            raise ValueError(f"{location}.implementation must define callable for type 'python'.")
        if not (str(callable_ref).startswith("builtin.") or str(callable_ref).startswith("python:")):
            raise ValueError(
                f"{location}.implementation.callable must start with 'builtin.' or 'python:'; got {callable_ref!r}."
            )
    if implementation_type == "command":
        command = implementation.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ValueError(f"{location}.implementation.command must be a non-empty list of strings.")
