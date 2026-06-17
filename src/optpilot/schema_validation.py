"""JSON Schema validation for public OptPilot configs."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Iterable, List

import json


SCHEMA_PACKAGE = "optpilot.schemas"
SCHEMA_BY_CONFIG = {
    "environment": "environment.schema.json",
    "method": "method.schema.json",
    "study": "study.schema.json",
}


@dataclass
class ValidationIssue:
    path: str
    message: str
    schema_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "message": self.message,
            "schema_path": self.schema_path,
        }


@dataclass
class ValidationResult:
    valid: bool
    errors: List[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [error.to_dict() for error in self.errors],
        }


def validate_public_config_schema(raw: Dict[str, Any], *, config_path: str | Path = "<config>") -> ValidationResult:
    """Validate one public config object against its JSON Schema."""

    config = raw.get("config")
    if config not in SCHEMA_BY_CONFIG:
        return ValidationResult(
            valid=False,
            errors=[
                ValidationIssue(
                    path="$",
                    message="config must be one of: environment, method, study",
                )
            ],
        )

    try:
        validator = _validator_for(config)
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema":
            raise RuntimeError(
                "JSON Schema validation requires the 'jsonschema' package. "
                "Install OptPilot with its runtime dependencies."
            ) from exc
        raise

    errors = [
        ValidationIssue(
            path=_json_path(error.absolute_path),
            message=error.message,
            schema_path=_json_path(error.absolute_schema_path),
        )
        for error in sorted(validator.iter_errors(raw), key=lambda item: list(item.absolute_path))
    ]
    return ValidationResult(valid=not errors, errors=errors)


def require_public_config_schema(raw: Dict[str, Any], *, config_path: str | Path = "<config>") -> None:
    result = validate_public_config_schema(raw, config_path=config_path)
    if result.valid:
        return
    location = str(config_path)
    messages = "; ".join(f"{issue.path}: {issue.message}" for issue in result.errors[:8])
    if len(result.errors) > 8:
        messages += f"; and {len(result.errors) - 8} more"
    raise ValueError(f"{location} failed schema validation: {messages}")


def _validator_for(config: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schema_name = SCHEMA_BY_CONFIG[config]
    schemas = _load_schema_documents()
    registry = Registry().with_resources(
        [
            (f"https://optpilot.io/schemas/{name}", Resource.from_contents(document))
            for name, document in schemas.items()
        ]
    )
    schema = schemas[schema_name]
    return Draft202012Validator(schema, registry=registry)


def _load_schema_documents() -> Dict[str, Dict[str, Any]]:
    root = resources.files(SCHEMA_PACKAGE)
    documents: Dict[str, Dict[str, Any]] = {}
    for relative in _schema_relative_paths():
        path = root.joinpath(*relative.split("/"))
        documents[relative] = json.loads(path.read_text(encoding="utf-8"))
    return documents


def _schema_relative_paths() -> Iterable[str]:
    yield "environment.schema.json"
    yield "method.schema.json"
    yield "study.schema.json"
    yield "defs/common.schema.json"
    yield "defs/runtime.schema.json"
    yield "defs/candidate.schema.json"
    yield "defs/metrics.schema.json"
    yield "defs/instances.schema.json"


def _json_path(parts: Iterable[Any]) -> str:
    parts = list(parts)
    if not parts:
        return "$"
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result
