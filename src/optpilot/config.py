"""Public config compiler.

OptPilot users author public YAML files with `config: environment`,
`config: method`, and `config: study`. The runner consumes an expanded internal
compiled study spec. This module validates the public configs and compiles them into that
internal representation.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import yaml

from .schema_validation import require_public_config_schema, validate_public_config_schema


AUTHORING_API_VERSION = "optpilot.io/v1"

CONFIG_ENVIRONMENT = "environment"
CONFIG_METHOD = "method"
CONFIG_RESOURCE = "resource"
CONFIG_STUDY = "study"

METHOD_PROTOCOLS = {"batch", "session"}
CANDIDATE_FORMATS = {"parameters", "files", "opaque"}
METRIC_SOURCES = {"custom", "return", "file", "stdout", "sqlite"}
OBJECTIVE_DIRECTIONS = {"maximize", "minimize"}
AGGREGATIONS = {"mean", "median", "min", "max", "sum", "last", "weighted_mean"}
RUNTIME_SANDBOXES = {"process", "container"}
SETUP_STEP_TYPES = {"uv", "python-venv", "npm", "command"}
EVIDENCE_LEVELS = {"minimal", "standard", "full"}
OUTPUT_FILE_STORAGE_MODES = {"reference", "copy"}
RECORD_SOURCES = {"custom", "jsonl", "csv", "sqlite_table", "sqlite_query"}
PARAMETER_VALUE_TYPES = {"float", "int", "categorical", "bool", "string", "array", "object"}


def compile_authoring_config(path: str | Path) -> Dict[str, Any]:
    """Compile a public study config into the internal StudySpec dictionary."""

    config_path = Path(path).resolve()
    study = _load_and_validate_public_config(config_path, CONFIG_STUDY)

    environment, environment_path = _load_referenced_config(
        study.get("environmentConfig"),
        expected_config=CONFIG_ENVIRONMENT,
        parent_path=config_path,
        field="environmentConfig",
    )
    method, method_path = _load_referenced_config(
        study.get("methodConfig"),
        expected_config=CONFIG_METHOD,
        parent_path=config_path,
        field="methodConfig",
    )

    _validate_environment_semantics(environment, environment_path)
    _validate_method_semantics(method, method_path)
    _validate_study_semantics(study, config_path)

    candidate = _normalize_candidate(environment["candidate"])
    _validate_method_environment_compatibility(method, environment, candidate, method_path, environment_path)
    metric_keys = set(environment.get("metrics", {}).get("keys", []) or [])
    objective_metric = study["objective"]["metric"]
    if metric_keys and objective_metric not in metric_keys:
        raise ValueError(
            f"{config_path} objective.metric {objective_metric!r} is not declared by "
            f"{environment_path or '<inline environment>'} metrics.keys {sorted(metric_keys)!r}."
        )

    compiled_method = _compile_method(method, method_path, candidate)
    execution = _compile_execution(study, config_path, environment, environment_path)

    return {
        "apiVersion": "optpilot/v1",
        "config": "run_spec",
        "metadata": {
            "name": str(study["name"]),
            "description": str(study.get("description", "")),
            "tags": list(study.get("tags", [])),
        },
        "environment": _compile_environment(environment, environment_path, candidate),
        "objective": _compile_objective(study["objective"]),
        "candidate": _compile_candidate_contract(environment, environment_path, candidate),
        "method": compiled_method,
        "execution": execution,
        "evidence": _compile_evidence(study, config_path),
        "reproducibility": _compile_reproducibility(study),
        "stopping": _compile_stopping(study),
        "extensions": {
            "authoringConfig": {
                "apiVersion": AUTHORING_API_VERSION,
                "config": CONFIG_STUDY,
                "studyConfigPath": str(config_path),
                "environmentConfigPath": str(environment_path) if environment_path else None,
                "methodConfigPath": str(method_path) if method_path else None,
                "environmentId": environment.get("id"),
                "methodId": method.get("id"),
            }
        },
    }


def validate_authoring_config(path: str | Path) -> Dict[str, Any]:
    """Validate one public config file and return a structured result."""

    config_path = Path(path).resolve()
    try:
        raw = _load_yaml(config_path)
        schema_result = validate_public_config_schema(raw, config_path=config_path)
        if not schema_result.valid:
            return {
                "valid": False,
                "path": str(config_path),
                "errors": [f"{issue.path}: {issue.message}" for issue in schema_result.errors],
            }
        config = raw.get("config")
        if config == CONFIG_STUDY:
            compile_authoring_config(config_path)
        elif config == CONFIG_ENVIRONMENT:
            _validate_environment_semantics(raw, config_path)
        elif config == CONFIG_METHOD:
            _validate_method_semantics(raw, config_path)
        elif config == CONFIG_RESOURCE:
            _validate_resource_semantics(raw, config_path)
        else:
            raise ValueError("config must be environment, method, resource, or study.")
    except Exception as exc:
        return {"valid": False, "path": str(config_path), "errors": [str(exc)]}
    return {"valid": True, "path": str(config_path), "errors": []}


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return raw


def _load_and_validate_public_config(path: Path, expected_config: str) -> Dict[str, Any]:
    raw = _load_yaml(path)
    _require_config(raw, expected_config, path)
    require_public_config_schema(raw, config_path=path)
    return raw


def _require_config(raw: Dict[str, Any], expected_config: str, path: Path) -> None:
    api_version = raw.get("apiVersion")
    if api_version != AUTHORING_API_VERSION:
        raise ValueError(f"{path} must use apiVersion {AUTHORING_API_VERSION!r}; got {api_version!r}.")
    config = raw.get("config")
    if config != expected_config:
        raise ValueError(f"{path} must have config {expected_config!r}; got {config!r}.")


def _load_referenced_config(
    value: Any,
    *,
    expected_config: str,
    parent_path: Path,
    field: str,
) -> Tuple[Dict[str, Any], Path | None]:
    if value is None:
        raise ValueError(f"{parent_path} must define {field}.")
    if not isinstance(value, str):
        raise ValueError(f"{parent_path} {field} must be a path string.")
    path = _resolve_path(value, parent_path)
    return _load_and_validate_public_config(path, expected_config), path


def _validate_environment_semantics(environment: Dict[str, Any], path: Path | None) -> None:
    location = str(path or "<inline environment>")
    _require_field(environment, "id", location)
    evaluator = _require_mapping(environment, "evaluator", location)
    candidate = _normalize_candidate(_require_mapping(environment, "candidate", location))
    metrics = _require_mapping(environment, "metrics", location)

    evaluator_modes = [key for key in ("python", "command", "adapter") if evaluator.get(key)]
    if len(evaluator_modes) != 1:
        raise ValueError(f"{location} evaluator must define exactly one of python, command, or adapter.")
    if evaluator.get("python"):
        _require_plain_python_import(evaluator["python"], f"{location} evaluator.python")
    if evaluator.get("adapter"):
        _require_plain_python_import(evaluator["adapter"], f"{location} evaluator.adapter")
    if evaluator.get("command"):
        _require_string_list(evaluator["command"], f"{location} evaluator.command", non_empty=True)

    runtime = environment.get("runtime", {}) or {}
    _validate_runtime(runtime, f"{location} runtime")
    if environment.get("interface") is not None:
        _validate_interface(environment["interface"], f"{location} interface")

    if candidate["format"] == "parameters":
        _validate_parameter_schema(candidate.get("parameters", {}).get("schema", {}), location)
        _validate_parameter_constraints(candidate.get("parameters", {}).get("constraints", []), location)
    if candidate["format"] == "files":
        _validate_file_candidate(candidate, location)
    if candidate["format"] == "opaque":
        _require_mapping(candidate, "opaque", f"{location} candidate")

    metric_source = metrics.get("source")
    if metric_source not in METRIC_SOURCES:
        raise ValueError(f"{location} metrics.source must be one of {sorted(METRIC_SOURCES)}.")
    if metric_source == "return" and not (evaluator.get("python") or evaluator.get("adapter")):
        raise ValueError(f"{location} metrics.source return requires evaluator.python or evaluator.adapter.")
    if metric_source == "file":
        _require_field(metrics, "path", f"{location} metrics")
    if metric_source == "stdout" and not (evaluator.get("command") or evaluator.get("adapter")):
        raise ValueError(f"{location} metrics.source stdout requires evaluator.command or evaluator.adapter.")
    if metric_source == "sqlite":
        _require_field(metrics, "database", f"{location} metrics")
        _require_field(metrics, "query", f"{location} metrics")
    if metric_source == "custom":
        _require_plain_python_import(_require_field(metrics, "extractor", f"{location} metrics"), f"{location} metrics.extractor")

    for record in environment.get("records", []) or []:
        source = record.get("source")
        if source not in RECORD_SOURCES:
            raise ValueError(f"{location} records.source must be one of {sorted(RECORD_SOURCES)}.")
        _require_field(record, "name", f"{location} records")
        if source == "custom":
            _require_plain_python_import(_require_field(record, "extractor", f"{location} records"), f"{location} records.extractor")
        elif source in {"jsonl", "csv"}:
            _require_field(record, "path", f"{location} records")
        else:
            _require_field(record, "database", f"{location} records")
        if source == "sqlite_table":
            _require_field(record, "table", f"{location} records")
        if source == "sqlite_query":
            _require_field(record, "query", f"{location} records")

    for entry in environment.get("trialWorkspace", []) or []:
        _require_field(entry, "from", f"{location} trialWorkspace")
        _require_field(entry, "to", f"{location} trialWorkspace")
        _ensure_safe_relative(entry["to"], f"{location} trialWorkspace.to", allow_dot=True)

    for item in environment.get("outputFiles", []) or []:
        if isinstance(item, str):
            continue
        if isinstance(item, dict):
            _require_field(item, "path", f"{location} outputFiles")
            continue
        raise ValueError(f"{location} outputFiles entries must be strings or objects.")


def _validate_method_semantics(method: Dict[str, Any], path: Path | None) -> None:
    location = str(path or "<inline method>")
    _require_field(method, "id", location)
    entrypoint = _require_mapping(method, "entrypoint", location)
    modes = [key for key in ("python", "command") if entrypoint.get(key)]
    if len(modes) != 1:
        raise ValueError(f"{location} entrypoint must define exactly one of python or command.")
    protocol = entrypoint.get("protocol", "batch")
    if protocol not in METHOD_PROTOCOLS:
        raise ValueError(f"{location} entrypoint.protocol must be one of {sorted(METHOD_PROTOCOLS)}.")
    if entrypoint.get("python"):
        _require_plain_python_import(entrypoint["python"], f"{location} entrypoint.python")
    if entrypoint.get("command"):
        if protocol != "batch":
            raise ValueError(f"{location} command entrypoints only support protocol batch.")
        _require_string_list(entrypoint["command"], f"{location} entrypoint.command", non_empty=True)
    accepts = _require_mapping(method, "accepts", location)
    formats = accepts.get("formats", [])
    if not isinstance(formats, list) or not formats:
        raise ValueError(f"{location} accepts.formats must be a non-empty list.")
    if any(item not in CANDIDATE_FORMATS for item in formats):
        raise ValueError(f"{location} accepts.formats must use {sorted(CANDIDATE_FORMATS)}.")
    requires = accepts.get("requires", {}) or {}
    if not isinstance(requires, dict):
        raise ValueError(f"{location} accepts.requires must be an object.")
    for key in ("context", "capabilities"):
        if requires.get(key) is not None:
            _require_string_list(requires[key], f"{location} accepts.requires.{key}")
    if method.get("interface") is not None:
        _validate_interface(method["interface"], f"{location} interface")
    _validate_runtime(method.get("runtime", {}) or {}, f"{location} runtime")


def _validate_resource_semantics(resource: Dict[str, Any], path: Path | None) -> None:
    location = str(path or "<inline resource>")
    _require_field(resource, "id", location)
    if resource.get("interface") is not None:
        _validate_interface(resource["interface"], f"{location} interface")


def _validate_study_semantics(study: Dict[str, Any], path: Path) -> None:
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

    execution = study.get("execution", {}) or {}
    if not isinstance(execution, dict):
        raise ValueError(f"{location} execution must be an object.")
    evidence = study.get("evidence", {}) or {}
    if evidence.get("level", "standard") not in EVIDENCE_LEVELS:
        raise ValueError(f"{location} evidence.level must be one of {sorted(EVIDENCE_LEVELS)}.")
    if evidence.get("outputFileStorage", "reference") not in OUTPUT_FILE_STORAGE_MODES:
        raise ValueError(
            f"{location} evidence.outputFileStorage must be one of {sorted(OUTPUT_FILE_STORAGE_MODES)}."
        )


def _validate_method_environment_compatibility(
    method: Dict[str, Any],
    environment: Dict[str, Any],
    candidate: Dict[str, Any],
    method_path: Path | None,
    environment_path: Path | None,
) -> None:
    accepts = method["accepts"]
    method_location = str(method_path or "<inline method>")
    environment_location = str(environment_path or "<inline environment>")

    formats = accepts.get("formats", []) or []
    if candidate["format"] not in formats:
        raise ValueError(
            f"{method_location} is incompatible with {environment_location}: "
            f"candidate.format {candidate['format']!r} is not in accepts.formats {formats!r}."
        )

    context_paths = _candidate_context_paths(_build_candidate_context(candidate, environment, environment_path))
    requires = accepts.get("requires", {}) or {}
    for required in requires.get("context", []) or []:
        if required not in context_paths:
            raise ValueError(
                f"{method_location} is incompatible with {environment_location}: "
                f"accepts.requires.context {required!r} is not provided."
            )

    capabilities = {str(item.get("id")) for item in environment.get("capabilities", []) or [] if isinstance(item, dict)}
    for required in requires.get("capabilities", []) or []:
        if required not in capabilities:
            raise ValueError(
                f"{method_location} is incompatible with {environment_location}: "
                f"required capability {required!r} is not provided."
            )


def candidate_contract_mismatch(environment_candidate: Dict[str, Any], produced_candidate: Dict[str, Any]) -> str | None:
    """Return a structural candidate-contract mismatch message, if any.

    This helper is kept for legacy catalog/UI diagnostics. The public method
    config model no longer includes a ``produces`` field.
    """

    return _candidate_contract_mismatch(
        _normalize_candidate(environment_candidate),
        _normalize_candidate(produced_candidate),
    )


def _normalize_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    candidate_format = candidate.get("format")
    normalized = deepcopy(candidate)
    normalized["format"] = candidate_format
    normalized.setdefault("description", "")

    if candidate_format == "parameters":
        parameters = deepcopy(candidate.get("parameters"))
        if not isinstance(parameters, dict):
            raise ValueError("candidate.parameters must be defined for candidate.format parameters.")
        if not isinstance(parameters.get("schema"), dict) or not parameters.get("schema"):
            raise ValueError("candidate.parameters.schema must be a non-empty object.")
        normalized["parameters"] = parameters
        return normalized

    if candidate_format == "files":
        files = deepcopy(candidate.get("files"))
        if not isinstance(files, dict):
            raise ValueError("candidate.files must be defined for candidate.format files.")
        editable = files.get("editable", []) or []
        if not isinstance(editable, list) or not editable:
            raise ValueError("candidate.files.editable must be a non-empty list.")
        files.setdefault("required", [item["path"] for item in editable if isinstance(item, dict) and item.get("path")])
        files.setdefault("allow", list(files.get("required", []) or []))
        files.setdefault("deny", [])
        materialize = deepcopy(candidate.get("materialize", {}))
        materialize.setdefault("root", ".")
        normalized["files"] = files
        normalized["materialize"] = materialize
        return normalized

    if candidate_format == "opaque":
        opaque = deepcopy(candidate.get("opaque"))
        if not isinstance(opaque, dict):
            raise ValueError("candidate.opaque must be defined for candidate.format opaque.")
        _require_field(opaque, "family", "candidate.opaque")
        normalized["opaque"] = opaque
        return normalized

    raise ValueError(f"candidate.format must be one of {sorted(CANDIDATE_FORMATS)}.")


def _validate_parameter_schema(schema: Any, location: str) -> None:
    if not isinstance(schema, dict) or not schema:
        raise ValueError(f"{location} candidate.parameters.schema must be a non-empty object.")
    for name, definition in schema.items():
        _validate_parameter_definition(definition, f"{location} candidate.parameters.schema.{name}")


def _validate_parameter_definition(definition: Any, location: str) -> None:
    if not isinstance(definition, dict):
        raise ValueError(f"{location} must be an object.")
    value_type = definition.get("valueType")
    if value_type not in PARAMETER_VALUE_TYPES:
        raise ValueError(f"{location}.valueType must be one of {sorted(PARAMETER_VALUE_TYPES)}.")
    if value_type == "categorical" and not isinstance(definition.get("values"), list):
        raise ValueError(f"{location}.values must be a list.")
    if value_type == "array":
        _validate_parameter_definition(_require_mapping(definition, "items", location), f"{location}.items")
    if value_type == "object":
        properties = _require_mapping(definition, "properties", location)
        for child_name, child in properties.items():
            _validate_parameter_definition(child, f"{location}.properties.{child_name}")


def _validate_parameter_constraints(constraints: Any, location: str) -> None:
    if constraints is None:
        return
    if not isinstance(constraints, list):
        raise ValueError(f"{location} candidate.parameters.constraints must be a list.")
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, dict):
            raise ValueError(f"{location} candidate.parameters.constraints[{index}] must be an object.")
        _require_field(constraint, "id", f"{location} candidate.parameters.constraints[{index}]")
        if "expr" not in constraint:
            raise ValueError(f"{location} candidate.parameters.constraints[{index}] must define expr.")
        _validate_constraint_expr(constraint["expr"], location)


def _candidate_contract_mismatch(environment_candidate: Dict[str, Any], produced_candidate: Dict[str, Any]) -> str | None:
    if produced_candidate["format"] != environment_candidate["format"]:
        return (
            f"produces.format {produced_candidate['format']!r} does not match "
            f"environment candidate.format {environment_candidate['format']!r}."
        )
    if produced_candidate["format"] == "parameters":
        return _parameter_schema_mismatch(
            environment_candidate.get("parameters", {}).get("schema", {}),
            produced_candidate.get("parameters", {}).get("schema", {}),
            "candidate.parameters.schema",
        )
    if produced_candidate["format"] == "files":
        return _file_contract_mismatch(
            environment_candidate.get("files", {}),
            produced_candidate.get("files", {}),
        )
    if produced_candidate["format"] == "opaque":
        expected_family = environment_candidate.get("opaque", {}).get("family")
        produced_family = produced_candidate.get("opaque", {}).get("family")
        if expected_family and produced_family and expected_family != produced_family:
            return f"produces.opaque.family {produced_family!r} does not match environment opaque family {expected_family!r}."
    return None


def _parameter_schema_mismatch(environment_schema: Dict[str, Any], produced_schema: Dict[str, Any], location: str) -> str | None:
    if not isinstance(produced_schema, dict) or not produced_schema:
        return f"produces.{location} must be a non-empty object."
    if not isinstance(environment_schema, dict) or not environment_schema:
        return f"environment {location} is not available."
    for name, produced_definition in produced_schema.items():
        environment_definition = environment_schema.get(name)
        if environment_definition is None:
            return f"produced parameter {name!r} is not accepted by environment {location}."
        mismatch = _parameter_definition_mismatch(environment_definition, produced_definition, f"{location}.{name}")
        if mismatch:
            return mismatch
    return None


def _parameter_definition_mismatch(environment_definition: Dict[str, Any], produced_definition: Dict[str, Any], location: str) -> str | None:
    environment_type = environment_definition.get("valueType")
    produced_type = produced_definition.get("valueType")
    if produced_type != environment_type:
        return f"produces.{location}.valueType {produced_type!r} does not match environment valueType {environment_type!r}."
    if produced_type == "categorical":
        environment_values = environment_definition.get("values")
        produced_values = produced_definition.get("values")
        if isinstance(environment_values, list) and isinstance(produced_values, list):
            missing = [value for value in produced_values if value not in environment_values]
            if missing:
                return f"produces.{location}.values contains values not accepted by the environment: {missing!r}."
    if produced_type == "array":
        if "items" in produced_definition and "items" in environment_definition:
            return _parameter_definition_mismatch(environment_definition["items"], produced_definition["items"], f"{location}.items")
    if produced_type == "object":
        environment_properties = environment_definition.get("properties", {})
        produced_properties = produced_definition.get("properties", {})
        for required in environment_definition.get("required", []) or []:
            if isinstance(produced_properties, dict) and required not in produced_properties:
                return f"produces.{location}.properties is missing required environment property {required!r}."
        if isinstance(produced_properties, dict):
            for name, child in produced_properties.items():
                if not isinstance(environment_properties, dict) or name not in environment_properties:
                    return f"produces.{location}.properties.{name} is not accepted by the environment."
                mismatch = _parameter_definition_mismatch(environment_properties[name], child, f"{location}.properties.{name}")
                if mismatch:
                    return mismatch
    return None


def _file_contract_mismatch(environment_files: Dict[str, Any], produced_files: Dict[str, Any]) -> str | None:
    allow = set(environment_files.get("allow", []) or environment_files.get("required", []) or [])
    produced_paths = {
        str(item.get("path"))
        for key in ("editable", "required")
        for item in produced_files.get(key, []) or []
        if isinstance(item, dict) and item.get("path")
    }
    produced_paths.update(
        str(item)
        for key in ("required", "allow")
        for item in produced_files.get(key, []) or []
        if isinstance(item, str)
    )
    if allow and produced_paths:
        missing = sorted(path for path in produced_paths if path not in allow)
        if missing:
            return f"produces.files paths are not allowed by the environment: {missing!r}."
    return None


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


def _validate_file_candidate(candidate: Dict[str, Any], location: str) -> None:
    files = candidate.get("files", {})
    if not isinstance(files, dict):
        raise ValueError(f"{location} candidate.files must be an object.")
    for key in ("editable", "required", "allow", "deny"):
        value = files.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{location} candidate.files.{key} must be a list.")
    for index, item in enumerate(files.get("editable", []) or []):
        if not isinstance(item, dict):
            raise ValueError(f"{location} candidate.files.editable[{index}] must be an object.")
        _ensure_safe_relative(_require_field(item, "path", f"{location} candidate.files.editable[{index}]"), f"{location} candidate.files.editable[{index}].path")
    for key in ("required", "allow", "deny"):
        for item in files.get(key, []) or []:
            if not isinstance(item, str):
                raise ValueError(f"{location} candidate.files.{key} entries must be strings.")
    root = candidate.get("materialize", {}).get("root", ".")
    _ensure_safe_relative(root, f"{location} candidate.materialize.root", allow_dot=True)


def _validate_runtime(runtime: Any, location: str) -> None:
    if runtime in (None, {}):
        return
    if not isinstance(runtime, dict):
        raise ValueError(f"{location} must be an object.")
    sandbox = runtime.get("sandbox", "process")
    if sandbox not in RUNTIME_SANDBOXES:
        raise ValueError(f"{location}.sandbox must be one of {sorted(RUNTIME_SANDBOXES)}.")
    if runtime.get("setup") is not None:
        if sandbox == "container":
            raise ValueError(f"{location}.setup is supported only for sandbox process.")
        _validate_setup(runtime["setup"], f"{location}.setup")
    if sandbox == "container":
        container = runtime.get("container", {}) or {}
        if not isinstance(container, dict):
            raise ValueError(f"{location}.container must be an object.")
        build = container.get("build", {}) if isinstance(container.get("build", {}), dict) else {}
        if not (container.get("image") or build.get("tag")):
            raise ValueError(f"{location}.container requires image or build.tag.")
        network = container.get("network", "disabled")
        if network not in {"enabled", "disabled"}:
            raise ValueError(f"{location}.container.network must be enabled or disabled.")
    elif runtime.get("container") is not None:
        raise ValueError(f"{location}.container requires sandbox container.")
    for key in ("env",):
        value = runtime.get(key, {})
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{location}.{key} must be an object.")
    for key in ("envFromHost",):
        value = runtime.get(key, [])
        if value is not None:
            _require_string_list(value, f"{location}.{key}")


def _validate_interface(interface: Any, location: str) -> None:
    if not isinstance(interface, dict):
        raise ValueError(f"{location} must be an object.")
    if interface.get("setup") is not None:
        _validate_setup(interface["setup"], f"{location}.setup")
    if interface.get("envFromHost") is not None:
        _require_string_list(interface["envFromHost"], f"{location}.envFromHost")
    if interface.get("env") is not None and not isinstance(interface["env"], dict):
        raise ValueError(f"{location}.env must be an object.")


def _validate_setup(setup: Any, location: str) -> None:
    if not isinstance(setup, dict):
        raise ValueError(f"{location} must be an object.")
    steps = setup.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{location}.steps must be a non-empty list.")
    if setup.get("env") is not None and not isinstance(setup["env"], dict):
        raise ValueError(f"{location}.env must be an object.")
    if setup.get("envFromHost") is not None:
        _require_string_list(setup["envFromHost"], f"{location}.envFromHost")
    timeout = setup.get("timeoutSeconds")
    if timeout is not None and int(timeout) < 1:
        raise ValueError(f"{location}.timeoutSeconds must be a positive integer.")
    for index, step in enumerate(steps):
        step_location = f"{location}.steps[{index}]"
        if not isinstance(step, dict):
            raise ValueError(f"{step_location} must be an object.")
        kind = step.get("uses")
        if kind not in SETUP_STEP_TYPES:
            raise ValueError(f"{step_location}.uses must be one of {sorted(SETUP_STEP_TYPES)}.")
        if kind == "command":
            _require_string_list(step.get("command"), f"{step_location}.command", non_empty=True)
        if step.get("env") is not None and not isinstance(step["env"], dict):
            raise ValueError(f"{step_location}.env must be an object.")
        for key in ("extras", "groups", "requirements"):
            if step.get(key) is not None:
                _require_string_list(step[key], f"{step_location}.{key}")


def _build_candidate_context(
    candidate: Dict[str, Any],
    environment: Dict[str, Any],
    environment_path: Path | None,
) -> Dict[str, Any]:
    base_path = environment_path or Path.cwd()
    trial_workspace = _resolve_trial_workspace(environment.get("trialWorkspace", []) or [], base_path)
    method_context = _resolve_method_context(environment.get("methodContext", {}) or {}, base_path)
    context = {
        "format": candidate["format"],
        "description": candidate.get("description", ""),
        "candidate": _public_candidate_context(candidate),
        "methodContext": method_context,
        "workspace": {"copy": trial_workspace},
        "trialWorkspace": trial_workspace,
        "capabilities": deepcopy(environment.get("capabilities", []) or []),
    }
    if candidate["format"] == "parameters":
        context["parameters"] = deepcopy(candidate.get("parameters", {}))
    elif candidate["format"] == "files":
        files = deepcopy(candidate.get("files", {}))
        files["root"] = candidate.get("materialize", {}).get("root", ".")
        context["files"] = files
        context["materialize"] = deepcopy(candidate.get("materialize", {}))
    elif candidate["format"] == "opaque":
        context["opaque"] = deepcopy(candidate.get("opaque", {}))
    return context


def _public_candidate_context(candidate: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"format": candidate["format"]}
    if candidate.get("description"):
        payload["description"] = candidate["description"]
    for key in ("parameters", "files", "materialize", "opaque"):
        if key in candidate:
            payload[key] = deepcopy(candidate[key])
    return payload


def _candidate_context_paths(context: Dict[str, Any]) -> set:
    paths = {"candidate", "candidate.format", "evidence", "evidence.observations"}
    candidate = context.get("candidate", {})
    if isinstance(candidate, dict):
        _add_nested_paths(paths, "candidate", candidate)
    method_context = context.get("methodContext", {})
    if isinstance(method_context, dict) and method_context:
        paths.add("methodContext")
        _add_nested_paths(paths, "methodContext", method_context)
    return paths


def _add_nested_paths(paths: set, prefix: str, value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if child not in (None, [], {}):
                paths.add(path)
            _add_nested_paths(paths, path, child)
    elif isinstance(value, list) and value:
        paths.add(prefix)


def _compile_environment(environment: Dict[str, Any], environment_path: Path | None, candidate: Dict[str, Any]) -> Dict[str, Any]:
    evaluator = deepcopy(environment["evaluator"])
    if evaluator.get("adapter"):
        adapter = {
            "type": "custom",
            "implementation": _component_ref(evaluator["adapter"]),
            "config": dict(evaluator.get("settings", {})),
        }
    else:
        adapter = {
            "type": "configured_environment",
            "implementation": "builtin.configured_environment",
            "config": _configured_environment_adapter_config(environment, environment_path, candidate),
        }
    return {
        "environmentId": str(environment["id"]),
        "adapter": adapter,
        "accessPolicy": "CodeAwareReadOnly" if candidate["format"] == "files" else "SchemaAware",
        "mutationPolicy": "TrialWorkspaceOnly" if candidate["format"] in {"files", "opaque"} else "NoMutation",
        "runtime": _compile_runtime(environment.get("runtime", {}) or {}, environment_path or Path.cwd()),
        "runtimeContract": {
            "timeoutSeconds": int(evaluator.get("timeoutSeconds", 600) or 600),
        },
    }


def _configured_environment_adapter_config(
    environment: Dict[str, Any],
    environment_path: Path | None,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    base_path = environment_path or Path.cwd()
    evaluator = _compile_evaluator_config(environment["evaluator"], base_path)
    workspace = {"copy": _resolve_trial_workspace(environment.get("trialWorkspace", []) or [], base_path)}
    return {
        "evaluate": evaluator,
        "candidate": deepcopy(candidate),
        "context": _build_candidate_context(candidate, environment, environment_path),
        "interfaces": [],
        "metrics": _compile_metrics_config(environment["metrics"]),
        "workspace": workspace,
        "outputFiles": _compile_output_file_rules(environment.get("outputFiles", []) or []),
        "records": _compile_record_rules(environment.get("records", []) or []),
    }


def _compile_evaluator_config(evaluator: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    result = {
        "timeoutSeconds": int(evaluator.get("timeoutSeconds", 600) or 600),
        "pythonPath": [str(_resolve_path(path, base_path)) for path in evaluator.get("pythonPath", []) or []],
        "cwd": evaluator.get("cwd", "."),
        "env": dict(evaluator.get("env", {}) or {}),
        "config": dict(evaluator.get("settings", {}) or {}),
    }
    if evaluator.get("python"):
        result.update({"type": "python", "callable": evaluator["python"]})
    elif evaluator.get("command"):
        result.update({"type": "command", "command": list(evaluator["command"])})
    else:
        raise ValueError("evaluator.adapter is compiled as a custom environment adapter, not configured_environment.")
    return result


def _compile_metrics_config(metrics: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(metrics)
    if result.get("source") == "custom" and result.get("extractor"):
        result["implementation"] = _component_ref(result.pop("extractor"))
    if "settings" in result:
        result["config"] = result.pop("settings")
    return result


def _compile_record_rules(records: Iterable[Dict[str, Any]]) -> list:
    compiled = []
    for record in records:
        item = deepcopy(record)
        if item.get("source") in {"sqlite_table", "sqlite_query"} and item.get("database"):
            item["path"] = item["database"]
        if item.get("source") == "custom" and item.get("extractor"):
            item["implementation"] = _component_ref(item.pop("extractor"))
        if "settings" in item:
            item["config"] = item.pop("settings")
        compiled.append(item)
    return compiled


def _compile_output_file_rules(output_files: Iterable[Any]) -> list:
    compiled = []
    for output_file in output_files:
        if isinstance(output_file, str):
            compiled.append(output_file)
        elif isinstance(output_file, dict):
            compiled.append(deepcopy(output_file))
    return compiled


def _resolve_trial_workspace(entries: Iterable[Dict[str, Any]], base_path: Path) -> list:
    resolved = []
    for entry in entries:
        resolved.append(
            {
                "from": str(_resolve_path(str(entry["from"]), base_path)),
                "to": str(entry["to"]),
            }
        )
    return resolved


def _resolve_method_context(method_context: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    resolved = {
        "instructions": [
            str(_resolve_path(path, base_path)) for path in method_context.get("instructions", []) or []
        ],
        "references": [],
    }
    for reference in method_context.get("references", []) or []:
        item = deepcopy(reference)
        item["path"] = str(_resolve_path(item["path"], base_path))
        resolved["references"].append(item)
    return resolved


def _compile_candidate_contract(
    environment: Dict[str, Any],
    environment_path: Path | None,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    if candidate["format"] == "parameters":
        parameters = candidate.get("parameters", {})
        return {
            "format": "parameters",
            "context": _build_candidate_context(candidate, environment, environment_path),
            "materialization": {"implementation": "builtin.parameter_to_config", "config": {}},
            "validation": {
                "implementation": "builtin.schema_validation",
                "config": {
                    "enforceBounds": bool(parameters.get("schema")),
                    "constraints": deepcopy(parameters.get("constraints", [])),
                },
            },
        }
    if candidate["format"] == "files":
        files = candidate.get("files", {})
        workspace = _resolve_trial_workspace(environment.get("trialWorkspace", []) or [], environment_path or Path.cwd())
        materializer_config = {
            "candidateRoot": candidate.get("materialize", {}).get("root", "."),
            "seedFiles": [
                {"source": item["from"], "destination": item["to"]}
                for item in workspace
            ],
            "readonlyFiles": [],
            "allowAbsoluteContentRefs": True,
        }
        return {
            "format": "files",
            "context": _build_candidate_context(candidate, environment, environment_path),
            "materialization": {
                "implementation": "builtin.workspace_bundle",
                "config": materializer_config,
            },
            "validation": {
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
        "format": "opaque",
        "context": _build_candidate_context(candidate, environment, environment_path),
        "materialization": {"implementation": "builtin.parameter_to_config", "config": {}},
        "validation": {"implementation": "builtin.schema_validation", "config": {"enforceBounds": False}},
    }


def _compile_method(method: Dict[str, Any], method_path: Path | None, candidate: Dict[str, Any]) -> Dict[str, Any]:
    entrypoint = deepcopy(method["entrypoint"])
    protocol = entrypoint.get("protocol", "batch")
    method_base_dir = (method_path.parent if method_path else Path.cwd()).resolve()
    implementation: Dict[str, Any] = {
        "protocol": "optpilot.method.session.v1" if protocol == "session" else "optpilot.method.batch.v1",
    }
    if entrypoint.get("python"):
        implementation.update({"type": "python", "callable": _component_ref(entrypoint["python"])})
    else:
        implementation.update({"type": "command", "command": list(entrypoint["command"])})
    if entrypoint.get("pythonPath"):
        implementation["pythonPath"] = [str(_resolve_path(path, method_path or Path.cwd())) for path in entrypoint.get("pythonPath", [])]

    settings = dict(method.get("settings", {}))
    parameters = candidate.get("parameters", {})
    if candidate["format"] == "parameters" and parameters.get("schema") and "searchSpace" not in settings:
        settings["searchSpace"] = _internal_parameter_schema(parameters["schema"])

    return {
        "id": str(method["id"]),
        "configBaseDir": str(method_base_dir),
        "implementation": implementation,
        "runtime": _compile_method_runtime(method.get("runtime", {}) or {}, method_path or Path.cwd()),
        "config": settings,
        "settings": deepcopy(method.get("settings", {})),
        "compatibility": _compile_accepts(method.get("accepts", {})),
        "sandboxSpec": {},
    }


def _compile_accepts(accepts: Dict[str, Any]) -> Dict[str, Any]:
    requires = accepts.get("requires", {}) or {}
    return {
        "formats": list(accepts.get("formats", []) or []),
        "requiredContext": list(requires.get("context", []) or []),
        "requiredCapabilities": list(requires.get("capabilities", []) or []),
    }


def _compile_method_runtime(runtime: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    compiled = _compile_runtime(runtime, base_path)
    if not compiled:
        return {}
    sandbox = runtime.get("sandbox", "process")
    compiled["type"] = "container" if sandbox == "container" else "process"
    return compiled


def _compile_objective(objective: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "primaryMetric": {
            "name": str(objective["metric"]),
            "direction": str(objective["direction"]),
        },
        "secondaryMetrics": list(objective.get("secondaryMetrics", [])),
        "aggregation": {"mode": objective.get("aggregation", "mean")},
    }


def _compile_execution(
    study: Dict[str, Any],
    study_path: Path,
    environment: Dict[str, Any],
    environment_path: Path | None,
) -> Dict[str, Any]:
    execution = dict(study.get("execution", {}) or {})
    runtime = deepcopy(environment.get("runtime", {}) or {})
    sandbox = runtime.get("sandbox", "process")

    if sandbox == "container":
        backend_type = "container"
        backend_impl = "builtin.container_backend"
        backend_config = _compile_container_backend_config(runtime, environment_path or study_path)
    else:
        backend_type = "local"
        backend_impl = "builtin.local_subprocess_backend"
        backend_config = _compile_runtime(runtime, environment_path or study_path)

    timeout = int(execution.get("timeoutSeconds") or environment.get("evaluator", {}).get("timeoutSeconds") or 600)
    parallelism = int(execution.get("parallelism", 1) or 1)
    retry = dict(execution.get("retry", {}))
    sandbox_spec = _runtime_to_sandbox_spec(runtime)
    if sandbox == "container":
        sandbox_spec["runtimeType"] = "container"
    max_retries = int(retry.get("maxRetries", 0) or 0)
    return {
        "backend": {
            "type": backend_type,
            "implementation": backend_impl,
            "config": backend_config,
        },
        "scheduler": {
            "type": "local",
            "implementation": "builtin.local_scheduler",
            "config": {
                "retryPolicy": {
                    "maxAttempts": max_retries + 1,
                    "retryStatuses": ["failed", "timeout"],
                }
            },
        },
        "defaults": {
            "resourceProfile": {
                "cpu": 1,
                "memoryGiB": 1,
                "gpu": 0,
                "timeoutSeconds": timeout,
            },
            "sandboxSpec": sandbox_spec,
            "retryPolicy": {
                "maxRetries": max_retries,
            },
        },
        "parallelism": {
            "candidateParallelism": parallelism,
        },
    }


def _compile_evidence(study: Dict[str, Any], study_path: Path) -> Dict[str, Any]:
    evidence = dict(study.get("evidence", {}) or {})
    level = evidence.get("level", "standard")
    compiled = {
        "store": {
            "metadataBackend": "local_json",
            "outputFileBackend": "local_fs",
        },
        "outputFileStorage": evidence.get("outputFileStorage", "reference"),
        "retention": _retention_for_level(level),
        "capture": {
            "methodCalls": level in {"standard", "full"},
            "methodEvents": level in {"standard", "full"},
            "runtimeManifests": level in {"standard", "full"},
            "validationOutputs": level in {"standard", "full"},
            "resourceAssignments": level in {"standard", "full"},
        },
    }
    if evidence.get("outputDir"):
        output_dir = _resolve_launch_path(str(evidence["outputDir"]))
        _reject_catalog_run_root(output_dir, f"{study_path} evidence.outputDir")
        compiled["outputDir"] = str(output_dir)
    return compiled


def _retention_for_level(level: str) -> Dict[str, str]:
    if level == "minimal":
        return {"prompts": "none", "logs": "summary", "traces": "none", "checkpoints": "none", "intermediateTables": "none"}
    if level == "full":
        return {"prompts": "full", "logs": "full", "traces": "full", "checkpoints": "full", "intermediateTables": "full"}
    return {"prompts": "refs", "logs": "full", "traces": "selected", "checkpoints": "selected", "intermediateTables": "full"}


def _compile_reproducibility(study: Dict[str, Any]) -> Dict[str, Any]:
    reproducibility = dict(study.get("reproducibility", {}) or {})
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


def _internal_parameter_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    converted = {}
    for name, definition in schema.items():
        item = deepcopy(definition)
        item.setdefault("type", item.get("valueType"))
        converted[name] = item
    return converted


def _compile_runtime(runtime: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    if not runtime:
        return {}
    container = runtime.get("container", {}) or {}
    compiled = {
        "env": dict(runtime.get("env", {}) or {}),
        "environmentVariables": dict(runtime.get("env", {}) or {}),
        "envFromHost": list(runtime.get("envFromHost", []) or []),
        "setup": deepcopy(runtime.get("setup")),
        "networkPolicy": container.get("network", "disabled") if runtime.get("sandbox") == "container" else "disabled",
    }
    if runtime.get("workdir"):
        compiled["workdir"] = str(_resolve_path(runtime["workdir"], base_path))
    if container:
        if container.get("image"):
            compiled["image"] = container["image"]
        if container.get("executable"):
            compiled["containerExecutable"] = container["executable"]
        if container.get("build"):
            compiled["build"] = _resolve_container_build(container["build"], base_path)
    return {key: value for key, value in compiled.items() if value not in ({}, [], None)}


def _compile_container_backend_config(runtime: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    compiled = _compile_runtime(runtime, base_path)
    return compiled


def _runtime_to_sandbox_spec(runtime: Dict[str, Any]) -> Dict[str, Any]:
    container = runtime.get("container", {}) or {}
    return {
        "runtimeType": runtime.get("sandbox", "process"),
        "networkPolicy": container.get("network", "disabled") if runtime.get("sandbox") == "container" else "disabled",
        "environmentVariables": dict(runtime.get("env", {}) or {}),
        "cleanupPolicy": "always",
    }


def _resolve_container_build(build: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    resolved = deepcopy(build)
    context_path = _resolve_path(resolved.get("context", "."), base_path)
    if resolved.get("context"):
        resolved["context"] = str(context_path)
    if resolved.get("dockerfile"):
        dockerfile = Path(str(resolved["dockerfile"])).expanduser()
        if dockerfile.is_absolute():
            resolved["dockerfile"] = str(dockerfile.resolve())
        else:
            resolved["dockerfile"] = str((context_path / dockerfile).resolve())
    return resolved


def _component_ref(value: str) -> str:
    text = str(value)
    if text.startswith("builtin."):
        return text
    return text


def _require_plain_python_import(value: Any, location: str) -> None:
    text = str(value)
    if text.startswith("python:"):
        raise ValueError(f"{location} must use module:object format without the legacy prefix.")
    module, sep, attr = text.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"{location} must use module:object format.")


def _resolve_path(value: Any, base_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    base_dir = base_path.parent if base_path.is_file() else base_path
    return (base_dir / path).resolve()


def _resolve_launch_path(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _reject_catalog_run_root(path: Path, location: str) -> None:
    if _is_catalog_source_path(path):
        raise ValueError(f"{location} must not resolve inside catalog source: {path}")


def _is_catalog_source_path(path: Path) -> bool:
    return any(part == "catalog" for part in path.resolve().parts)


def _require_field(data: Dict[str, Any], field: str, location: str) -> Any:
    if field not in data or data[field] in {None, ""}:
        raise ValueError(f"{location} must define {field}.")
    return data[field]


def _require_mapping(data: Dict[str, Any], field: str, location: str) -> Dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{location} must define {field} as an object.")
    return value


def _require_string_list(value: Any, location: str, *, non_empty: bool = False) -> None:
    if not isinstance(value, list) or (non_empty and not value) or not all(isinstance(item, str) for item in value):
        detail = "a non-empty list of strings" if non_empty else "a list of strings"
        raise ValueError(f"{location} must be {detail}.")


def _ensure_safe_relative(value: Any, location: str, *, allow_dot: bool = False) -> None:
    text = str(value)
    if allow_dot and text == ".":
        return
    path = Path(text)
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        raise ValueError(f"{location} must be a safe relative path.")
