"""Package-level validation for OptPilot catalog packages."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .config import compile_authoring_config, validate_authoring_config
from .package_index import PackageEntry, index_package
from .schema_validation import validate_public_config_schema


JsonDict = Dict[str, Any]


@dataclass
class PackageValidationEntry:
    path: str
    config: str
    id: str
    qualified_id: str
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    synthesized: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path,
            "config": self.config,
            "id": self.id,
            "qualified_id": self.qualified_id,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "synthesized": self.synthesized,
        }


def validate_package(
    package_root: str | Path,
    *,
    check_imports: bool = False,
) -> JsonDict:
    """Validate all recognized public OptPilot configs in a package folder."""

    index = index_package(package_root)
    entries: List[PackageValidationEntry] = []
    for entry in index.entries:
        entries.append(_validate_entry(entry, check_imports=check_imports))

    valid = not index.errors and all(entry.valid for entry in entries)
    return {
        "valid": valid,
        "package": str(index.package_root),
        "package_id": index.package_id,
        "counts": index.counts(),
        "errors": list(index.errors),
        "ignored_yaml": [str(path) for path in index.ignored_yaml],
        "entries": [entry.to_dict() for entry in entries],
    }


def _validate_entry(entry: PackageEntry, *, check_imports: bool) -> PackageValidationEntry:
    errors: List[str] = []
    warnings: List[str] = []
    if entry.synthesized and entry.config == "resource":
        schema_result = validate_public_config_schema(entry.raw, config_path=entry.path)
        if not schema_result.valid:
            errors.extend(f"{issue.path}: {issue.message}" for issue in schema_result.errors)
    else:
        result = validate_authoring_config(entry.path)
        errors.extend(result.get("errors", []) or [])

    if not errors and check_imports:
        warnings.extend(_check_imports(entry))

    return PackageValidationEntry(
        path=str(entry.path),
        config=entry.config,
        id=entry.id,
        qualified_id=entry.qualified_id,
        valid=not errors,
        errors=errors,
        warnings=warnings,
        synthesized=entry.synthesized,
    )


def _check_imports(entry: PackageEntry) -> List[str]:
    """Best-effort import checks for Python callables referenced by a config."""

    raw = entry.raw
    refs: List[str] = []
    if entry.config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        for key in ("python", "adapter"):
            if isinstance(evaluator.get(key), str):
                refs.append(evaluator[key])
    elif entry.config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        if isinstance(entrypoint.get("python"), str):
            refs.append(entrypoint["python"])

    if not refs:
        return []

    warnings: List[str] = []
    original_path = list(sys.path)
    config_dir = entry.path.parent if entry.path.is_file() else entry.path
    try:
        for path in [str(config_dir), str(Path.cwd())]:
            if path not in sys.path:
                sys.path.insert(0, path)
        for ref in refs:
            try:
                _import_ref(ref)
            except Exception as exc:
                warnings.append(f"Could not import {ref!r}: {exc}")
    finally:
        sys.path[:] = original_path
    return warnings


def _import_ref(ref: str) -> Any:
    if ":" not in ref:
        raise ValueError("Python reference must use module:attribute")
    module_name, _, attr_path = ref.partition(":")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in attr_path.split("."):
        value = getattr(value, part)
    return value
