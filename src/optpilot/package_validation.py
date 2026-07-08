"""Package-level validation for OptPilot catalog packages."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .config import validate_authoring_config
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
    check_source: bool = False,
    check_setup_files: bool = False,
) -> JsonDict:
    """Validate all recognized public OptPilot configs in a package folder."""

    index = index_package(package_root)
    entries: List[PackageValidationEntry] = []
    for entry in index.entries:
        entries.append(
            _validate_entry(
                entry,
                package_root=index.package_root,
                check_imports=check_imports,
                check_source=check_source,
                check_setup_files=check_setup_files,
            )
        )

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


def _validate_entry(
    entry: PackageEntry,
    *,
    package_root: Path,
    check_imports: bool,
    check_source: bool,
    check_setup_files: bool,
) -> PackageValidationEntry:
    errors: List[str] = []
    warnings: List[str] = []
    if entry.synthesized and entry.config == "resource":
        schema_result = validate_public_config_schema(entry.raw, config_path=entry.path)
        if not schema_result.valid:
            errors.extend(f"{issue.path}: {issue.message}" for issue in schema_result.errors)
    else:
        result = validate_authoring_config(entry.path)
        errors.extend(result.get("errors", []) or [])

    if check_source:
        errors.extend(_check_source_paths(entry, package_root))
    if check_setup_files:
        errors.extend(_check_setup_files(entry, package_root))
    if check_imports:
        errors.extend(_check_imports(entry, package_root))

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


def _check_imports(entry: PackageEntry, package_root: Path) -> List[str]:
    """Best-effort import checks for Python callables referenced by a config."""

    raw = entry.raw
    refs: List[Tuple[str, List[Path]]] = []
    if entry.config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        for key in ("python", "adapter"):
            if isinstance(evaluator.get(key), str):
                refs.append((evaluator[key], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or [])))
    elif entry.config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        if isinstance(entrypoint.get("python"), str):
            refs.append((entrypoint["python"], _python_path_roots(entry.path, entrypoint.get("pythonPath", []) or [])))

    if not refs:
        return []

    errors: List[str] = []
    for ref, python_paths in refs:
        completed = _run_import_check(ref, python_paths, package_root)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            errors.append(f"Could not import {ref!r}: {detail}")
    return errors


def _check_source_paths(entry: PackageEntry, package_root: Path) -> List[str]:
    raw = entry.raw
    errors: List[str] = []
    config_dir = entry.source_root if entry.config == "resource" and entry.source_root is not None else _config_dir(entry.path)

    def check_path(value: Any, location: str, *, must_exist: bool = True) -> None:
        if not isinstance(value, str) or not value:
            return
        path = _resolve_package_path(value, config_dir, package_root, location, errors)
        if path is None:
            return
        if must_exist and not path.exists():
            errors.append(f"{location} does not exist: {value}")

    def check_python_paths(values: Iterable[Any], location: str) -> None:
        for index, value in enumerate(values):
            if isinstance(value, str):
                check_path(value, f"{location}[{index}]")

    if entry.config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        check_python_paths(evaluator.get("pythonPath", []) or [], "evaluator.pythonPath")
        if evaluator.get("pythonPath") and isinstance(evaluator.get("python"), str):
            errors.extend(_check_python_ref_source(evaluator["python"], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or []), package_root, "evaluator.python"))
        if evaluator.get("pythonPath") and isinstance(evaluator.get("adapter"), str):
            errors.extend(_check_python_ref_source(evaluator["adapter"], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or []), package_root, "evaluator.adapter"))
        if isinstance(evaluator.get("cwd"), str):
            check_path(evaluator["cwd"], "evaluator.cwd")
        if isinstance(evaluator.get("command"), list):
            errors.extend(_check_command_file_tokens(evaluator["command"], config_dir, package_root, "evaluator.command"))
        for index, item in enumerate(raw.get("trialWorkspace", []) or []):
            if isinstance(item, dict):
                check_path(item.get("from"), f"trialWorkspace[{index}].from")
        method_context = raw.get("methodContext", {}) if isinstance(raw.get("methodContext"), dict) else {}
        for index, value in enumerate(method_context.get("instructions", []) or []):
            check_path(value, f"methodContext.instructions[{index}]")
        for index, item in enumerate(method_context.get("references", []) or []):
            if isinstance(item, dict):
                check_path(item.get("path"), f"methodContext.references[{index}].path")
    elif entry.config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        check_python_paths(entrypoint.get("pythonPath", []) or [], "entrypoint.pythonPath")
        if entrypoint.get("pythonPath") and isinstance(entrypoint.get("python"), str):
            errors.extend(_check_python_ref_source(entrypoint["python"], _python_path_roots(entry.path, entrypoint.get("pythonPath", []) or []), package_root, "entrypoint.python"))
        if isinstance(entrypoint.get("command"), list):
            errors.extend(_check_command_file_tokens(entrypoint["command"], config_dir, package_root, "entrypoint.command"))
    elif entry.config == "resource":
        interface = raw.get("interface", {}) if isinstance(raw.get("interface"), dict) else {}
        if isinstance(interface.get("cwd"), str):
            check_path(interface["cwd"], "interface.cwd")
        if isinstance(interface.get("command"), list):
            interface_dir = _resolve_package_path(interface.get("cwd", "."), config_dir, package_root, "interface.cwd", errors) if isinstance(interface.get("cwd"), str) else config_dir
            if interface_dir is not None:
                errors.extend(_check_command_file_tokens(interface["command"], interface_dir, package_root, "interface.command"))
    elif entry.config == "study":
        check_path(raw.get("environmentConfig"), "environmentConfig")
        check_path(raw.get("methodConfig"), "methodConfig")
    return errors


def _check_setup_files(entry: PackageEntry, package_root: Path) -> List[str]:
    errors: List[str] = []
    raw = entry.raw
    config_base = entry.source_root if entry.config == "resource" and entry.source_root is not None else _config_dir(entry.path)
    if entry.config in {"environment", "method"}:
        runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
        errors.extend(_check_runtime_setup_files(runtime, config_base, package_root, "runtime"))
    if entry.config in {"environment", "method", "resource"}:
        interface = raw.get("interface", {}) if isinstance(raw.get("interface"), dict) else {}
        if interface:
            errors.extend(_check_setup_block_files(interface.get("setup"), config_base, package_root, "interface.setup"))
    return errors


def _check_runtime_setup_files(runtime: JsonDict, base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    if not runtime:
        return errors
    errors.extend(_check_setup_block_files(runtime.get("setup"), base_dir, package_root, f"{location}.setup"))
    container = runtime.get("container", {}) if isinstance(runtime.get("container"), dict) else {}
    build = container.get("build", {}) if isinstance(container.get("build"), dict) else {}
    if build:
        context_path = _resolve_package_path(build.get("context", "."), base_dir, package_root, f"{location}.container.build.context", errors)
        if context_path is not None and not context_path.exists():
            errors.append(f"{location}.container.build.context does not exist: {build.get('context', '.')}")
        dockerfile = build.get("dockerfile")
        if context_path is not None and isinstance(dockerfile, str) and dockerfile:
            dockerfile_path = Path(dockerfile).expanduser()
            resolved = dockerfile_path.resolve() if dockerfile_path.is_absolute() else (context_path / dockerfile_path).resolve()
            if not _is_relative_to(resolved, package_root):
                errors.append(f"{location}.container.build.dockerfile must stay inside package: {dockerfile}")
                return errors
            if not resolved.exists():
                errors.append(f"{location}.container.build.dockerfile does not exist: {dockerfile}")
    return errors


def _check_setup_block_files(setup: Any, base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(setup, dict):
        return errors
    for index, step in enumerate(setup.get("steps", []) or []):
        if not isinstance(step, dict):
            continue
        step_location = f"{location}.steps[{index}]"
        cwd_value = step.get("cwd", ".")
        cwd = _resolve_package_path(cwd_value, base_dir, package_root, f"{step_location}.cwd", errors)
        if cwd is None:
            continue
        if not cwd.exists():
            errors.append(f"{step_location}.cwd does not exist: {cwd_value}")
            continue
        kind = step.get("uses")
        if kind == "uv":
            if not ((cwd / "pyproject.toml").exists() or (cwd / "uv.lock").exists()):
                errors.append(f"{step_location} uses uv but no pyproject.toml or uv.lock exists in {cwd_value}")
        elif kind == "python-venv":
            for req_index, requirement in enumerate(step.get("requirements", []) or []):
                requirement_path = _resolve_package_path(requirement, cwd, package_root, f"{step_location}.requirements[{req_index}]", errors)
                if requirement_path is None:
                    continue
                if not requirement_path.exists():
                    errors.append(f"{step_location}.requirements[{req_index}] does not exist: {requirement}")
        elif kind == "npm":
            if not ((cwd / "package.json").exists() or (cwd / "package-lock.json").exists()):
                errors.append(f"{step_location} uses npm but no package.json or package-lock.json exists in {cwd_value}")
        elif kind == "command":
            command = step.get("command", []) if isinstance(step.get("command"), list) else []
            errors.extend(_check_command_file_tokens(command, cwd, package_root, f"{step_location}.command"))
    return errors


def _check_command_file_tokens(command: Iterable[Any], base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    tokens = list(command)
    inline_payload_indexes = _command_inline_payload_indexes(tokens)
    for index, token in enumerate(tokens):
        if index in inline_payload_indexes:
            continue
        if not isinstance(token, str) or not _looks_like_file_token(token):
            continue
        path = _resolve_package_path(token, base_dir, package_root, f"{location}[{index}]", errors)
        if path is None:
            continue
        if not path.exists():
            errors.append(f"{location}[{index}] file does not exist: {token}")
    return errors


def _command_inline_payload_indexes(command: List[Any]) -> set[int]:
    if not command or not isinstance(command[0], str):
        return set()
    executable = Path(command[0]).name
    indexes: set[int] = set()
    if executable.startswith("python"):
        for index, token in enumerate(command[1:], start=1):
            if token == "-c" and index + 1 < len(command):
                indexes.add(index + 1)
    if executable in {"sh", "bash", "zsh"}:
        for index, token in enumerate(command[1:], start=1):
            if isinstance(token, str) and token.startswith("-") and "c" in token and index + 1 < len(command):
                indexes.add(index + 1)
    return indexes


def _looks_like_file_token(value: str) -> bool:
    if not value or value.startswith("{") or "://" in value:
        return False
    if value in {"python", "python3", "uv", "npm", "node", "bash", "sh", "cmd", "powershell"}:
        return False
    suffix = Path(value).suffix.lower()
    if suffix in {".py", ".js", ".mjs", ".cjs", ".sh", ".bash", ".json", ".yaml", ".yml", ".toml", ".txt"}:
        return True
    return "/" in value or "\\" in value


def _check_python_ref_source(ref: str, python_paths: List[Path], package_root: Path, location: str) -> List[str]:
    module_name = ref.partition(":")[0]
    if not module_name:
        return []
    module_path = Path(*module_name.split("."))
    for root in python_paths:
        if not _is_relative_to(root.resolve(), package_root.resolve()):
            continue
        if (root / f"{module_path}.py").exists() or (root / module_path / "__init__.py").exists():
            return []
    return [f"{location} module file not found under declared pythonPath: {ref}"]


def _run_import_check(ref: str, python_paths: List[Path], package_root: Path) -> subprocess.CompletedProcess[str]:
    script = """
import importlib
import json
import sys

ref = sys.argv[1]
try:
    module_name, sep, attr_path = ref.partition(":")
    if not sep:
        raise ValueError("Python reference must use module:attribute")
    module = importlib.import_module(module_name)
    value = module
    for part in attr_path.split("."):
        value = getattr(value, part)
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
"""
    env = dict(os.environ)
    path_values = [str(path) for path in python_paths]
    if path_values:
        env["PYTHONPATH"] = os.pathsep.join(path_values)
    else:
        env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", script, ref],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(package_root),
        timeout=30,
        check=False,
    )


def _python_path_roots(config_path: Path, values: Iterable[Any]) -> List[Path]:
    roots = [_config_dir(config_path)]
    for value in values:
        if isinstance(value, str) and value:
            roots.append(_resolve_config_path(value, config_path))
    return _dedupe_paths(roots)


def _resolve_config_path(value: Any, config_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_config_dir(config_path) / path).resolve()


def _resolve_relative_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _resolve_package_path(value: Any, base_dir: Path, package_root: Path, location: str, errors: List[str]) -> Path | None:
    path = Path(str(value)).expanduser()
    resolved = path.resolve() if path.is_absolute() else (base_dir / path).resolve()
    if not _is_relative_to(resolved, package_root.resolve()):
        errors.append(f"{location} must stay inside package: {value}")
        return None
    return resolved


def _config_dir(path: Path) -> Path:
    return path.parent if path.is_file() else path


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
