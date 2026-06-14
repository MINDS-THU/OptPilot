"""User-facing v3alpha config compiler.

The runner consumes the expanded internal StudySpec. This module keeps the
small authoring configs separate and compiles them into that canonical shape.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml


AUTHORING_API_VERSION = "optpilot.io/v3alpha1"

ENVIRONMENT_KIND = "EnvironmentConfig"
METHOD_KIND = "MethodConfig"
STUDY_KIND = "StudyConfig"

EVALUATE_TYPES = {"command", "python", "custom"}
CANDIDATE_TYPES = {"parameters", "files", "opaque"}
METRIC_SOURCES = {"return", "file", "stdout", "sqlite", "custom"}
INSTANCE_SOURCES = {"none", "inline", "files", "sampler"}
OBJECTIVE_DIRECTIONS = {"maximize", "minimize"}
AGGREGATIONS = {"mean", "median", "min", "max", "sum", "last"}
BACKENDS = {"local", "local_subprocess", "custom"}
EVIDENCE_LEVELS = {"minimal", "standard", "full"}
RECORD_SOURCES = {"jsonl", "csv", "sqlite_table", "sqlite_query", "custom"}


def compile_authoring_config(path: str | Path) -> Dict[str, Any]:
    """Compile a StudyConfig into the internal StudySpec dictionary."""

    config_path = Path(path).resolve()
    study = _load_yaml(config_path)
    _require_kind(study, STUDY_KIND, config_path)

    environment, environment_path = _load_referenced_config(
        study.get("environment"),
        expected_kind=ENVIRONMENT_KIND,
        parent_path=config_path,
        field="environment",
    )
    method, method_path = _load_referenced_config(
        study.get("method"),
        expected_kind=METHOD_KIND,
        parent_path=config_path,
        field="method",
    )

    _validate_environment(environment, environment_path)
    _validate_method(method, method_path)
    _validate_study(study, config_path)

    candidate = _normalize_candidate(environment["candidate"])
    _validate_method_environment_compatibility(method, environment, candidate, method_path, environment_path)
    metric_keys = set(environment.get("metrics", {}).get("keys", []) or [])
    objective_metric = study["objective"]["metric"]
    if metric_keys and objective_metric not in metric_keys:
        raise ValueError(
            f"StudyConfig objective.metric {objective_metric!r} is not declared by "
            f"EnvironmentConfig metrics.keys {sorted(metric_keys)!r}."
        )

    engines = [_compile_engine(method, candidate)]
    controllers = [_compile_controller(method)]
    execution = _compile_execution(study)

    spec = {
        "apiVersion": "optpilot/v1",
        "kind": "StudySpec",
        "metadata": {
            "name": str(study["name"]),
            "description": str(study.get("description", "")),
            "tags": list(study.get("tags", [])),
        },
        "target": _compile_target(environment, environment_path, candidate),
        "objective": _compile_objective(study["objective"]),
        "evaluationScope": _compile_instances(study.get("instances"), config_path),
        "artifacts": {
            "primaryArtifact": _compile_primary_artifact(environment, environment_path, candidate),
        },
        "controllers": controllers,
        "engines": engines,
        "execution": execution,
        "evidence": _compile_evidence(study, config_path),
        "reproducibility": _compile_reproducibility(study),
        "stopping": _compile_stopping(study),
        "extensions": {
            "authoringConfig": {
                "apiVersion": AUTHORING_API_VERSION,
                "kind": STUDY_KIND,
                "studyConfigPath": str(config_path),
                "environmentConfigPath": str(environment_path) if environment_path else None,
                "methodConfigPath": str(method_path) if method_path else None,
                "environmentId": environment.get("id"),
                "methodId": method.get("id"),
            }
        },
    }
    return spec


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return raw


def _require_kind(raw: Dict[str, Any], expected_kind: str, path: Path) -> None:
    api_version = raw.get("apiVersion")
    if api_version != AUTHORING_API_VERSION:
        raise ValueError(f"{path} must use apiVersion {AUTHORING_API_VERSION!r}; got {api_version!r}.")
    kind = raw.get("kind")
    if kind != expected_kind:
        raise ValueError(f"{path} must have kind {expected_kind!r}; got {kind!r}.")


def _load_referenced_config(
    value: Any,
    *,
    expected_kind: str,
    parent_path: Path,
    field: str,
) -> Tuple[Dict[str, Any], Path | None]:
    if value is None:
        raise ValueError(f"StudyConfig must define {field}.")
    if isinstance(value, str):
        path = _resolve_path(value, parent_path)
        raw = _load_yaml(path)
        _require_kind(raw, expected_kind, path)
        return raw, path
    if isinstance(value, dict) and "ref" in value:
        path = _resolve_path(str(value["ref"]), parent_path)
        raw = _load_yaml(path)
        _require_kind(raw, expected_kind, path)
        return raw, path
    if isinstance(value, dict):
        _require_kind(value, expected_kind, parent_path)
        return value, None
    raise ValueError(f"StudyConfig {field} must be a path, {{ref: path}}, or inline {expected_kind}.")


def _validate_environment(environment: Dict[str, Any], path: Path | None) -> None:
    location = str(path or f"<inline {ENVIRONMENT_KIND}>")
    _require_field(environment, "id", location)
    evaluate = _require_mapping(environment, "evaluate", location)
    candidate = _require_mapping(environment, "candidate", location)
    metrics = _require_mapping(environment, "metrics", location)

    evaluate_type = evaluate.get("type")
    if evaluate_type not in EVALUATE_TYPES:
        raise ValueError(f"{location} evaluate.type must be one of {sorted(EVALUATE_TYPES)}.")
    if evaluate_type == "command":
        command = evaluate.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ValueError(f"{location} evaluate.command must be a non-empty list of strings.")
    if evaluate_type == "python":
        _require_field(evaluate, "callable", f"{location} evaluate")
    if evaluate_type == "custom":
        _require_field(evaluate, "implementation", f"{location} evaluate")

    normalized_candidate = _normalize_candidate(candidate)
    candidate_type = normalized_candidate.get("type")
    if candidate_type not in CANDIDATE_TYPES:
        raise ValueError(f"{location} candidate.type must be one of {sorted(CANDIDATE_TYPES)}.")
    _require_field(normalized_candidate, "artifactKind", f"{location} candidate")
    _require_field(normalized_candidate, "description", f"{location} candidate")
    if candidate_type == "parameters":
        _validate_parameter_schema(normalized_candidate.get("parameters", {}).get("schema", {}), location)
        _validate_parameter_constraints(normalized_candidate.get("parameters", {}).get("constraints", []), location)
    if candidate_type == "files":
        _validate_file_candidate(normalized_candidate.get("files", {}), location)
    _validate_exposure(normalized_candidate.get("exposure", {}), location)

    metric_source = metrics.get("source")
    if metric_source not in METRIC_SOURCES:
        raise ValueError(f"{location} metrics.source must be one of {sorted(METRIC_SOURCES)}.")
    if metric_source == "file":
        _require_field(metrics, "path", f"{location} metrics")
    if metric_source == "sqlite":
        _require_field(metrics, "database", f"{location} metrics")
        _require_field(metrics, "query", f"{location} metrics")
    if metric_source == "custom":
        _require_field(metrics, "implementation", f"{location} metrics")

    for record in environment.get("recordsToExtract", []) or []:
        if not isinstance(record, dict):
            raise ValueError(f"{location} recordsToExtract entries must be objects.")
        source = record.get("source")
        if source not in RECORD_SOURCES:
            raise ValueError(f"{location} recordsToExtract.source must be one of {sorted(RECORD_SOURCES)}.")
        _require_field(record, "name", f"{location} recordsToExtract")
        if source != "custom":
            _require_field(record, "path", f"{location} recordsToExtract")
        if source == "sqlite_table":
            _require_field(record, "table", f"{location} recordsToExtract")
        if source == "sqlite_query":
            _require_field(record, "query", f"{location} recordsToExtract")
        if source == "custom":
            _require_field(record, "implementation", f"{location} recordsToExtract")

    for interface in environment.get("interfaces", []) or []:
        if not isinstance(interface, dict):
            raise ValueError(f"{location} interfaces entries must be objects.")
        _require_field(interface, "id", f"{location} interfaces")
        _require_field(interface, "capability", f"{location} interfaces")
        adapter = interface.get("adapter")
        if adapter is not None:
            if not isinstance(adapter, dict):
                raise ValueError(f"{location} interfaces.adapter must be an object.")
            if not adapter.get("implementation") and not adapter.get("command"):
                raise ValueError(f"{location} interfaces.adapter must define implementation or command.")


def _validate_parameter_schema(schema: Any, location: str) -> None:
    if schema is None:
        return
    if not isinstance(schema, dict):
        raise ValueError(f"{location} candidate.parameters.schema must be an object.")
    allowed_types = {"float", "int", "categorical", "bool", "string"}
    for name, definition in schema.items():
        if not isinstance(definition, dict):
            raise ValueError(f"{location} candidate.parameters.schema.{name} must be an object.")
        param_type = definition.get("type")
        if param_type not in allowed_types:
            raise ValueError(f"{location} candidate.parameters.schema.{name}.type must be one of {sorted(allowed_types)}.")
        if param_type == "categorical" and not isinstance(definition.get("values"), list):
            raise ValueError(f"{location} candidate.parameters.schema.{name}.values must be a list.")


def _validate_parameter_constraints(constraints: Any, location: str) -> None:
    if constraints is None:
        return
    if not isinstance(constraints, list):
        raise ValueError(f"{location} candidate.parameters.constraints must be a list.")
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, dict):
            raise ValueError(f"{location} candidate.parameters.constraints[{index}] must be an object.")
        _require_field(constraint, "id", f"{location} candidate.parameters.constraints[{index}]")
        _require_field(constraint, "description", f"{location} candidate.parameters.constraints[{index}]")
        if "expr" not in constraint:
            raise ValueError(f"{location} candidate.parameters.constraints[{index}] must define expr.")
        _validate_constraint_expr(constraint["expr"], location)


def _validate_constraint_expr(expr: Any, location: str) -> None:
    if not isinstance(expr, dict) or not expr:
        raise ValueError(f"{location} constraint expr must be a non-empty object.")
    keys = set(expr)
    if "compare" in keys:
        compare = expr["compare"]
        if not isinstance(compare, dict):
            raise ValueError(f"{location} constraint compare must be an object.")
        if compare.get("op") not in {"<", "<=", ">", ">=", "==", "!=", "in", "not_in"}:
            raise ValueError(f"{location} constraint compare.op is not supported.")
        if "left" not in compare or "right" not in compare:
            raise ValueError(f"{location} constraint compare must define left and right.")
        _validate_scalar_expr(compare["left"], location)
        _validate_scalar_expr(compare["right"], location)
        return
    if "all" in keys or "any" in keys:
        values = expr.get("all", expr.get("any"))
        if not isinstance(values, list) or not values:
            raise ValueError(f"{location} constraint all/any must be a non-empty list.")
        for item in values:
            _validate_constraint_expr(item, location)
        return
    if "not" in keys:
        _validate_constraint_expr(expr["not"], location)
        return
    raise ValueError(f"{location} constraint expr uses an unsupported node: {sorted(keys)}.")


def _validate_scalar_expr(expr: Any, location: str) -> None:
    if not isinstance(expr, dict) or not expr:
        raise ValueError(f"{location} scalar constraint expression must be a non-empty object.")
    if "param" in expr or "const" in expr:
        return
    if expr.get("op") in {"add", "sub", "mul", "div"}:
        args = expr.get("args")
        if not isinstance(args, list) or not args:
            raise ValueError(f"{location} numeric constraint op requires non-empty args.")
        for arg in args:
            _validate_scalar_expr(arg, location)
        return
    raise ValueError(f"{location} scalar constraint expression uses an unsupported node.")


def _validate_file_candidate(files: Dict[str, Any], location: str) -> None:
    if not isinstance(files, dict):
        raise ValueError(f"{location} candidate.files must be an object.")
    _require_field(files, "root", f"{location} candidate.files")
    for key in ("editable", "required", "allow", "deny"):
        value = files.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{location} candidate.files.{key} must be a list.")
    for index, item in enumerate(files.get("editable", []) or []):
        if not isinstance(item, dict):
            raise ValueError(f"{location} candidate.files.editable[{index}] must be an object.")
        _require_field(item, "path", f"{location} candidate.files.editable[{index}]")


def _validate_exposure(exposure: Any, location: str) -> None:
    if exposure is None:
        return
    if not isinstance(exposure, dict):
        raise ValueError(f"{location} candidate.exposure must be an object.")
    for key in ("instructions", "contextFiles", "contextRecords", "contextArtifacts"):
        value = exposure.get(key, [])
        if value is not None and not isinstance(value, list):
            raise ValueError(f"{location} candidate.exposure.{key} must be a list.")
    for index, artifact in enumerate(exposure.get("contextArtifacts", []) or []):
        if not isinstance(artifact, dict):
            raise ValueError(f"{location} candidate.exposure.contextArtifacts[{index}] must be an object.")
        _require_field(artifact, "id", f"{location} candidate.exposure.contextArtifacts[{index}]")
        _require_field(artifact, "path", f"{location} candidate.exposure.contextArtifacts[{index}]")


def _validate_method(method: Dict[str, Any], path: Path | None) -> None:
    location = str(path or f"<inline {METHOD_KIND}>")
    _require_field(method, "id", location)
    engine = _require_mapping(method, "engine", location)
    _require_field(engine, "implementation", f"{location} engine")
    compatibility = _require_mapping(method, "compatibility", location)
    controller = method.get("controller")
    if controller is not None:
        if not isinstance(controller, dict):
            raise ValueError(f"{location} controller must be an object.")
        _require_field(controller, "implementation", f"{location} controller")
    candidate_types = compatibility.get("candidateTypes", [])
    if not isinstance(candidate_types, list) or not candidate_types:
        raise ValueError(f"{location} compatibility.candidateTypes must be a non-empty list.")
    if any(item not in CANDIDATE_TYPES for item in candidate_types):
        raise ValueError(f"{location} compatibility.candidateTypes must use {sorted(CANDIDATE_TYPES)}.")
    for key in ("artifactKinds", "requiredContext", "optionalContext", "requiredCapabilities"):
        value = compatibility.get(key, [])
        if value is not None and not isinstance(value, list):
            raise ValueError(f"{location} compatibility.{key} must be a list.")


def _validate_method_environment_compatibility(
    method: Dict[str, Any],
    environment: Dict[str, Any],
    candidate: Dict[str, Any],
    method_path: Path | None,
    environment_path: Path | None,
) -> None:
    compatibility = method["compatibility"]
    method_location = str(method_path or f"<inline {METHOD_KIND}>")
    environment_location = str(environment_path or f"<inline {ENVIRONMENT_KIND}>")

    candidate_types = compatibility.get("candidateTypes", []) or []
    if candidate_types and candidate["type"] not in candidate_types:
        raise ValueError(
            f"{method_location} is incompatible with {environment_location}: "
            f"candidate.type {candidate['type']!r} is not in {candidate_types!r}."
        )

    artifact_kinds = compatibility.get("artifactKinds", []) or []
    if artifact_kinds and candidate["artifactKind"] not in artifact_kinds:
        raise ValueError(
            f"{method_location} is incompatible with {environment_location}: "
            f"candidate.artifactKind {candidate['artifactKind']!r} is not in {artifact_kinds!r}."
        )

    context_paths = _candidate_context_paths(_build_candidate_context(candidate, environment, environment_path))
    for required in compatibility.get("requiredContext", []) or []:
        if required not in context_paths:
            raise ValueError(
                f"{method_location} is incompatible with {environment_location}: "
                f"requiredContext {required!r} is not provided."
            )

    capabilities = {str(item.get("capability")) for item in environment.get("interfaces", []) or [] if isinstance(item, dict)}
    for required in compatibility.get("requiredCapabilities", []) or []:
        if required not in capabilities:
            raise ValueError(
                f"{method_location} is incompatible with {environment_location}: "
                f"required capability {required!r} is not provided."
            )


def _validate_study(study: Dict[str, Any], path: Path) -> None:
    location = str(path)
    _require_field(study, "name", location)
    objective = _require_mapping(study, "objective", location)
    _require_field(objective, "metric", f"{location} objective")
    if objective.get("direction") not in OBJECTIVE_DIRECTIONS:
        raise ValueError(f"{location} objective.direction must be one of {sorted(OBJECTIVE_DIRECTIONS)}.")
    aggregation = objective.get("aggregation", "mean")
    if aggregation not in AGGREGATIONS:
        raise ValueError(f"{location} objective.aggregation must be one of {sorted(AGGREGATIONS)}.")
    budget = _require_mapping(study, "budget", location)
    if int(budget.get("maxTrials", 0) or 0) < 1:
        raise ValueError(f"{location} budget.maxTrials must be a positive integer.")

    instances = study.get("instances", {"source": "none"})
    if not isinstance(instances, dict):
        raise ValueError(f"{location} instances must be an object.")
    source = instances.get("source", "none")
    if source not in INSTANCE_SOURCES:
        raise ValueError(f"{location} instances.source must be one of {sorted(INSTANCE_SOURCES)}.")

    execution = study.get("execution", {})
    if execution and execution.get("backend", "local") not in BACKENDS:
        raise ValueError(f"{location} execution.backend must be one of {sorted(BACKENDS)}.")
    evidence = study.get("evidence", {})
    if evidence and evidence.get("level", "standard") not in EVIDENCE_LEVELS:
        raise ValueError(f"{location} evidence.level must be one of {sorted(EVIDENCE_LEVELS)}.")


def _normalize_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    candidate_type = candidate.get("type")
    normalized = deepcopy(candidate)
    normalized["type"] = candidate_type

    if candidate_type == "parameters":
        parameters = deepcopy(candidate.get("parameters"))
        if not isinstance(parameters, dict):
            raise ValueError("candidate.parameters must be defined for candidate.type 'parameters'.")
        if not isinstance(parameters.get("schema"), dict) or not parameters.get("schema"):
            raise ValueError("candidate.parameters.schema must be a non-empty object.")
        normalized["parameters"] = parameters
        return normalized

    if candidate_type == "files":
        files = deepcopy(candidate.get("files"))
        if not isinstance(files, dict):
            raise ValueError("candidate.files must be defined for candidate.type 'files'.")
        if not files.get("root"):
            raise ValueError("candidate.files.root must be defined.")
        if not isinstance(files.get("source"), dict):
            raise ValueError("candidate.files.source must be defined as an object.")
        if not isinstance(files.get("editable"), list) or not files.get("editable"):
            raise ValueError("candidate.files.editable must be a non-empty list.")
        files.setdefault("required", [item["path"] for item in files["editable"] if isinstance(item, dict) and item.get("path")])
        files.setdefault("allow", list(files.get("required", []) or []))
        files.setdefault("deny", [])
        normalized["files"] = files
        return normalized

    if candidate_type == "opaque":
        opaque = deepcopy(candidate.get("opaque"))
        if not isinstance(opaque, dict):
            raise ValueError("candidate.opaque must be defined for candidate.type 'opaque'.")
        _require_field(opaque, "family", "candidate.opaque")
        normalized["opaque"] = opaque
        return normalized

    return normalized


def _build_candidate_context(
    candidate: Dict[str, Any],
    environment: Dict[str, Any],
    environment_path: Path | None,
) -> Dict[str, Any]:
    workspace = _resolve_workspace_paths(environment.get("workspace", {}), environment_path or Path.cwd())
    context = {
        "type": candidate["type"],
        "artifactKind": candidate["artifactKind"],
        "description": candidate["description"],
        "tags": list(candidate.get("tags", [])),
        "exposure": _resolve_exposure(candidate.get("exposure", {}), environment_path or Path.cwd()),
        "workspace": workspace,
        "interfaces": _resolve_interfaces(environment.get("interfaces", []) or [], workspace, environment_path or Path.cwd()),
    }
    if candidate["type"] == "parameters":
        context["parameters"] = deepcopy(candidate.get("parameters", {}))
    elif candidate["type"] == "files":
        context["files"] = deepcopy(candidate.get("files", {}))
    elif candidate["type"] == "opaque":
        context["opaque"] = deepcopy(candidate.get("opaque", {}))
    return context


def _candidate_context_paths(context: Dict[str, Any]) -> set:
    paths = {"type", "artifactKind"}
    for top_level in ("parameters", "files", "opaque", "exposure", "workspace", "interfaces"):
        value = context.get(top_level)
        if isinstance(value, dict) and value:
            paths.add(top_level)
            for key, nested in value.items():
                if nested not in (None, [], {}):
                    paths.add(f"{top_level}.{key}")
        elif isinstance(value, list) and value:
            paths.add(top_level)
    return paths


def _resolve_exposure(exposure: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    resolved = deepcopy(exposure or {})
    resolved["instructions"] = [
        str(_resolve_path(path, base_path)) for path in resolved.get("instructions", []) or []
    ]
    resolved["contextFiles"] = [
        str(_resolve_path(path, base_path)) for path in resolved.get("contextFiles", []) or []
    ]
    context_records = []
    for record in resolved.get("contextRecords", []) or []:
        item = deepcopy(record)
        if isinstance(item, dict) and item.get("path"):
            item["path"] = str(_resolve_path(item["path"], base_path))
        context_records.append(item)
    resolved["contextRecords"] = context_records
    resolved.setdefault("contextArtifacts", [])
    return resolved


def _resolve_interfaces(interfaces: list, workspace: Dict[str, Any], base_path: Path) -> list:
    resolved = []
    destination_to_source = {
        str(entry.get("to")): str(entry.get("from"))
        for entry in workspace.get("copy", []) or []
        if isinstance(entry, dict) and entry.get("from") and entry.get("to")
    }
    for interface in interfaces:
        item = deepcopy(interface)
        adapter = item.get("adapter")
        config = adapter.get("config") if isinstance(adapter, dict) else None
        if isinstance(config, dict):
            for key in ("path", "database"):
                if not config.get(key):
                    continue
                original = str(config[key])
                if original in destination_to_source:
                    config[f"{key}WorkspacePath"] = original
                    config[key] = destination_to_source[original]
                    continue
                path = Path(original)
                if not path.is_absolute():
                    config[key] = str(_resolve_path(original, base_path))
        resolved.append(item)
    return resolved


def _compile_target(environment: Dict[str, Any], environment_path: Path | None, candidate: Dict[str, Any]) -> Dict[str, Any]:
    evaluate = deepcopy(environment["evaluate"])
    evaluate_type = evaluate["type"]
    if evaluate_type == "custom":
        adapter = {
            "type": "custom",
            "implementation": evaluate["implementation"],
            "config": dict(evaluate.get("config", {})),
        }
    else:
        adapter = {
            "type": "configured_environment",
            "implementation": "builtin.configured_environment",
            "config": _configured_environment_adapter_config(environment, environment_path),
        }
    return {
        "targetId": str(environment["id"]),
        "adapter": adapter,
        "accessPolicy": "CodeAwareReadOnly" if candidate["type"] == "files" else "SchemaAware",
        "mutationPolicy": "StudyArtifactOnly" if candidate["type"] in {"files", "opaque"} else "NoMutation",
        "runtimeContract": {
            "timeoutSeconds": int(evaluate.get("timeoutSeconds", 600) or 600),
        },
    }


def _configured_environment_adapter_config(environment: Dict[str, Any], environment_path: Path | None) -> Dict[str, Any]:
    base_path = environment_path or Path.cwd()
    candidate = _normalize_candidate(environment["candidate"])
    evaluate = _resolve_evaluate_paths(environment["evaluate"], base_path)
    workspace = _resolve_workspace_paths(environment.get("workspace", {}), base_path)
    config = {
        "evaluate": evaluate,
        "candidate": deepcopy(candidate),
        "candidateContext": _build_candidate_context(candidate, environment, environment_path),
        "interfaces": _resolve_interfaces(environment.get("interfaces", []) or [], workspace, base_path),
        "metrics": deepcopy(environment["metrics"]),
        "workspace": workspace,
        "filesToSave": list(environment.get("filesToSave", []) or []),
        "recordsToExtract": deepcopy(environment.get("recordsToExtract", []) or []),
    }
    return config


def _resolve_evaluate_paths(evaluate: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    resolved = deepcopy(evaluate)
    if resolved.get("type") == "python":
        resolved["pythonPath"] = [str(_resolve_path(path, base_path)) for path in resolved.get("pythonPath", []) or []]
    return resolved


def _resolve_workspace_paths(workspace: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    if not workspace:
        return {}
    resolved = deepcopy(workspace)
    copy_entries = []
    for entry in workspace.get("copy", []) or []:
        if not isinstance(entry, dict):
            raise ValueError("workspace.copy entries must be objects.")
        copy_entries.append(
            {
                "from": str(_resolve_path(str(entry["from"]), base_path)),
                "to": str(entry["to"]),
                "role": str(entry.get("role", "")) if entry.get("role") is not None else "",
            }
        )
    resolved["copy"] = copy_entries
    return resolved


def _compile_primary_artifact(
    environment: Dict[str, Any],
    environment_path: Path | None,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    candidate_type = candidate["type"]
    if candidate_type == "parameters":
        parameters = candidate.get("parameters", {})
        return {
            "kind": candidate["artifactKind"],
            "candidateContext": _build_candidate_context(candidate, environment, environment_path),
            "materializationPlan": {"implementation": "builtin.parameter_to_config", "config": {}},
            "validationRules": {
                "implementation": "builtin.schema_validation",
                "config": {
                    "enforceBounds": bool(parameters.get("schema")),
                    "constraints": deepcopy(parameters.get("constraints", [])),
                },
            },
        }
    if candidate_type == "files":
        files = candidate.get("files", {})
        workspace = _resolve_workspace_paths(environment.get("workspace", {}), environment_path or Path.cwd())
        materializer_config = {
            "candidateRoot": files.get("root", "."),
            "seedFiles": [
                {"source": item["from"], "destination": item["to"]}
                for item in workspace.get("copy", []) or []
            ],
            "readonlyFiles": list(workspace.get("readonly", []) or []),
            "allowAbsoluteContentRefs": True,
        }
        return {
            "kind": candidate.get("artifactKind", "files"),
            "candidateContext": _build_candidate_context(candidate, environment, environment_path),
            "materializationPlan": {
                "implementation": "builtin.workspace_bundle",
                "config": materializer_config,
            },
            "validationRules": {
                "implementation": "builtin.workspace_policy",
                "config": {
                    "requireHashes": True,
                    "requireExistingRefs": True,
                    "allowAbsoluteContentRefs": True,
                    "requiredFiles": list(files.get("required", []) or []),
                    "allow": list(files.get("allow", []) or []),
                    "deny": list(files.get("deny", []) or []),
                },
            },
        }
    return {
        "kind": candidate["artifactKind"],
        "candidateContext": _build_candidate_context(candidate, environment, environment_path),
        "materializationPlan": {"implementation": "builtin.parameter_to_config", "config": {}},
        "validationRules": {"implementation": "builtin.schema_validation", "config": {"enforceBounds": False}},
    }


def _compile_engine(method: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    engine = deepcopy(method["engine"])
    config = dict(engine.get("config", {}))
    parameters = candidate.get("parameters", {})
    if candidate["type"] == "parameters" and parameters.get("schema") and "searchSpace" not in config:
        config["searchSpace"] = deepcopy(parameters["schema"])
    return {
        "id": str(engine.get("id", "engine_main")),
        "type": str(engine.get("type", "user_engine")),
        "implementation": engine["implementation"],
        "config": config,
        "resourceProfile": dict(engine.get("resourceProfile", {})),
        "sandboxSpec": dict(engine.get("sandboxSpec", {})),
    }


def _compile_controller(method: Dict[str, Any]) -> Dict[str, Any]:
    controller = deepcopy(method.get("controller", {}))
    return {
        "id": str(controller.get("id", "controller_main")),
        "type": str(controller.get("type", "single_engine")),
        "implementation": controller.get("implementation", "builtin.single_engine_controller"),
        "config": dict(controller.get("config", {})),
        "inputs": {"evidenceViews": ["summary_metrics"]},
        "outputs": {"allowedActions": ["launch_engine", "stop_study"]},
    }


def _compile_objective(objective: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "primaryMetric": {
            "name": str(objective["metric"]),
            "direction": str(objective["direction"]),
        },
        "secondaryMetrics": list(objective.get("secondaryMetrics", [])),
        "aggregation": {"mode": objective.get("aggregation", "mean")},
    }


def _compile_instances(instances: Any, study_path: Path) -> Dict[str, Any]:
    data = dict(instances or {"source": "none"})
    source = data.get("source", "none")
    if source == "none":
        return {"mode": "FixedInstance", "definition": {"instance": {}}}
    if source == "inline":
        return {"mode": "FixedInstance", "definition": {"instance": dict(data.get("value", {}))}}
    if source == "files":
        paths = [str(_resolve_path(path, study_path)) for path in data.get("paths", []) or []]
        if not paths:
            raise ValueError("instances.source files requires non-empty paths.")
        if len(paths) == 1:
            return {"mode": "FixedInstance", "definition": {"instanceRef": paths[0]}}
        return {"mode": "InstanceSet", "definition": {"instanceRefs": paths}}
    if source == "sampler":
        return {
            "mode": "Distribution",
            "definition": {
                "sampler": {
                    "implementation": data.get("implementation", "builtin.parameter_sampler"),
                    "config": dict(data.get("config", {})),
                },
                "sampleCount": int(data.get("count", 1)),
            },
        }
    raise ValueError(f"Unsupported instances.source: {source}")


def _compile_execution(study: Dict[str, Any]) -> Dict[str, Any]:
    execution = dict(study.get("execution", {}))
    backend = execution.get("backend", "local")
    if backend == "custom":
        backend_impl = execution["implementation"]
    elif backend == "local_subprocess":
        backend_impl = "builtin.local_subprocess_backend"
    else:
        backend_impl = "builtin.local_backend"
    timeout = int(execution.get("timeoutSeconds", 600) or 600)
    parallelism = int(execution.get("parallelism", 1) or 1)
    retry = dict(execution.get("retry", {}))
    return {
        "backend": {
            "type": backend,
            "implementation": backend_impl,
            "config": dict(execution.get("config", {})),
        },
        "scheduler": {
            "type": "local",
            "implementation": "builtin.local_scheduler",
            "config": {},
        },
        "defaults": {
            "resourceProfile": {
                "cpu": 1,
                "memoryGiB": 1,
                "gpu": 0,
                "timeoutSeconds": timeout,
            },
            "sandboxSpec": {
                "runtimeType": "process",
                "networkPolicy": "disabled",
                "cleanupPolicy": "always",
            },
            "retryPolicy": {
                "maxRetries": int(retry.get("maxRetries", 0) or 0),
            },
        },
        "parallelism": {
            "candidateParallelism": parallelism,
            "rolloutParallelism": 1,
            "engineParallelism": 1,
        },
    }


def _compile_evidence(study: Dict[str, Any], study_path: Path) -> Dict[str, Any]:
    evidence = dict(study.get("evidence", {}))
    level = evidence.get("level", "standard")
    compiled = {
        "store": {
            "metadataBackend": "local_json",
            "artifactBackend": "local_fs",
        },
        "retention": _retention_for_level(level),
        "capture": {
            "controllerDecisions": level in {"standard", "full"},
            "engineSnapshots": level in {"standard", "full"},
            "validationOutputs": level in {"standard", "full"},
            "resourceAssignments": level in {"standard", "full"},
        },
    }
    if evidence.get("outputDir"):
        compiled["outputDir"] = str(_resolve_path(str(evidence["outputDir"]), study_path))
    return compiled


def _retention_for_level(level: str) -> Dict[str, str]:
    if level == "minimal":
        return {"prompts": "none", "logs": "summary", "traces": "none", "checkpoints": "none", "intermediateTables": "none"}
    if level == "full":
        return {"prompts": "full", "logs": "full", "traces": "full", "checkpoints": "full", "intermediateTables": "full"}
    return {"prompts": "refs", "logs": "full", "traces": "selected", "checkpoints": "selected", "intermediateTables": "full"}


def _compile_reproducibility(study: Dict[str, Any]) -> Dict[str, Any]:
    reproducibility = dict(study.get("reproducibility", {}))
    return {
        "seedPolicy": {
            "globalSeed": int(reproducibility.get("seed", 0) or 0),
            "perTrialDerivation": "deterministic_hash",
        },
        "environmentSnapshot": "required",
        "dependencySnapshot": "required",
        "recordAssignedResources": True,
        "recordSandboxConfig": True,
        "recordModelInvocations": False,
    }


def _compile_stopping(study: Dict[str, Any]) -> Dict[str, Any]:
    budget = dict(study["budget"])
    stopping = {
        "maxTrials": int(budget["maxTrials"]),
        "convergenceRule": {
            "implementation": "builtin.no_improvement",
            "config": {
                "patienceTrials": int(budget.get("maxTrials", 1)),
                "minDelta": 0.0,
            },
        },
    }
    if budget.get("maxWallClockSeconds") is not None:
        stopping["maxWallClockSeconds"] = int(budget["maxWallClockSeconds"])
    if budget.get("maxFailures") is not None:
        stopping["maxFailures"] = int(budget["maxFailures"])
    return stopping


def _resolve_path(value: Any, base_path: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path.resolve()
    base_dir = base_path.parent if base_path.is_file() else base_path
    return (base_dir / path).resolve()


def _require_field(data: Dict[str, Any], field: str, location: str) -> None:
    if field not in data or data[field] in {None, ""}:
        raise ValueError(f"{location} must define {field}.")


def _require_mapping(data: Dict[str, Any], field: str, location: str) -> Dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{location} must define {field} as an object.")
    return value
