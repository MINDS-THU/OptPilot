"""Prepare writable component source copies for a study run."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import utc_now_iso
from .setup import prepared_runtime_from_setup, run_process_setup
from .spec import StudySpec


JsonDict = Dict[str, Any]

IGNORED_SOURCE_NAMES = {
    ".DS_Store",
    ".git",
    ".mypy_cache",
    ".optpilot",
    ".optpilot-ui",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "runs",
}


@dataclass
class ComponentSourceCopy:
    kind: str
    original_config_path: Path
    original_source_root: Path
    copied_source_root: Path
    copied_config_path: Path
    python_path_root: Path


def prepare_run_sources(study_spec: StudySpec, run_dir: Path) -> StudySpec:
    """Copy public environment/method sources into ``run_dir/source``.

    Expanded internal specs that do not carry public authoring paths are left
    unchanged. Public specs are rebased so component-relative paths and Python
    imports prefer the writable run source copy.
    """

    raw = deepcopy(study_spec.raw)
    authoring = raw.get("extensions", {}).get("authoringConfig", {})
    if not isinstance(authoring, dict):
        return study_spec

    components: List[ComponentSourceCopy] = []
    for kind, field in (("environment", "environmentConfigPath"), ("method", "methodConfigPath")):
        config_value = authoring.get(field)
        if not config_value:
            continue
        config_path = Path(str(config_value)).expanduser().resolve()
        if not config_path.exists():
            continue
        component_raw = raw.get(kind if kind == "method" else "environment", {})
        refs = list(_component_python_refs(kind, component_raw))
        source_hints = list(_component_source_hints(kind, component_raw, config_path))
        original_source_root = _choose_source_root(config_path, refs, source_hints)
        copied_root, python_path_root = _copy_component_source(original_source_root, run_dir / "source" / kind)
        components.append(
            ComponentSourceCopy(
                kind=kind,
                original_config_path=config_path,
                original_source_root=original_source_root,
                copied_source_root=copied_root,
                copied_config_path=copied_root / config_path.relative_to(original_source_root),
                python_path_root=python_path_root,
            )
        )

    if not components:
        return study_spec

    raw = _rebase_absolute_paths(
        raw,
        [(item.original_source_root, item.copied_source_root) for item in components],
    )
    for item in components:
        _add_component_python_path(raw, item)
        _record_component_source(raw, item)
        _run_component_setup(raw, item)
    _sync_environment_runtime_to_execution(raw)

    return StudySpec(path=run_dir / "study_spec.json", raw=raw)


def _component_python_refs(kind: str, component: JsonDict) -> Iterable[str]:
    if kind == "environment":
        adapter = component.get("adapter", {}) if isinstance(component.get("adapter"), dict) else {}
        implementation = adapter.get("implementation")
        if isinstance(implementation, str) and not implementation.startswith("builtin."):
            yield implementation
        config = adapter.get("config", {}) if isinstance(adapter.get("config"), dict) else {}
        evaluate = config.get("evaluate", {}) if isinstance(config.get("evaluate"), dict) else {}
        callable_ref = evaluate.get("callable")
        if isinstance(callable_ref, str):
            yield callable_ref
        metrics = config.get("metrics", {}) if isinstance(config.get("metrics"), dict) else {}
        metric_impl = metrics.get("implementation")
        if isinstance(metric_impl, str):
            yield metric_impl
        for record in config.get("records", []) or []:
            if isinstance(record, dict) and isinstance(record.get("implementation"), str):
                yield record["implementation"]
        return

    implementation = component.get("implementation", {}) if isinstance(component.get("implementation"), dict) else {}
    callable_ref = implementation.get("callable") or implementation.get("implementation")
    if isinstance(callable_ref, str):
        yield callable_ref


def _choose_source_root(config_path: Path, refs: List[str], source_hints: List[Path] | None = None) -> Path:
    default_root = config_path.parent.resolve()
    candidates: List[Path] = []
    for hint in source_hints or []:
        resolved = hint.resolve()
        if _is_relative_to(config_path, resolved):
            candidates.append(resolved)
    for ref in refs:
        top_package_dir = _top_package_dir_for_ref(ref, config_path)
        if top_package_dir is None:
            continue
        if _is_relative_to(config_path, top_package_dir):
            candidates.append(top_package_dir)
    if not candidates:
        return default_root
    return sorted({path.resolve() for path in candidates}, key=lambda path: len(path.parts))[0]


def _component_source_hints(kind: str, component: JsonDict, config_path: Path) -> Iterable[Path]:
    if kind == "environment":
        adapter = component.get("adapter", {}) if isinstance(component.get("adapter"), dict) else {}
        for path in adapter.get("pythonPath", []) or []:
            yield _resolve_hint_path(path, config_path)
        config = adapter.get("config", {}) if isinstance(adapter.get("config"), dict) else {}
        evaluate = config.get("evaluate", {}) if isinstance(config.get("evaluate"), dict) else {}
        for path in evaluate.get("pythonPath", []) or []:
            yield _resolve_hint_path(path, config_path)
        return

    implementation = component.get("implementation", {}) if isinstance(component.get("implementation"), dict) else {}
    for path in implementation.get("pythonPath", []) or []:
        yield _resolve_hint_path(path, config_path)


def _resolve_hint_path(value: Any, config_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config_path.parent / path).resolve()


def _top_package_dir_for_ref(ref: str, config_path: Path) -> Optional[Path]:
    if ref.startswith("builtin.") or ":" not in ref:
        return None
    module_name, _, _attr = ref.partition(":")
    if not module_name or module_name.startswith("python:"):
        return None
    parts = module_name.split(".")
    near_config = _top_package_dir_near_config(parts, config_path)
    if near_config is not None:
        return near_config
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    if spec is None or not spec.origin or spec.origin in {"built-in", "namespace"}:
        return None
    origin = Path(spec.origin).resolve()
    if len(parts) == 1:
        return origin.parent
    top_package_dir = origin.parents[len(parts) - 2]
    if not (top_package_dir / "__init__.py").exists():
        return None
    return top_package_dir


def _top_package_dir_near_config(parts: List[str], config_path: Path) -> Optional[Path]:
    if not parts:
        return None
    for ancestor in [config_path.parent, *config_path.parents]:
        if ancestor.name != parts[0] or not (ancestor / "__init__.py").exists():
            continue
        if len(parts) == 1:
            return ancestor.resolve()
        module_file = ancestor / Path(*parts[1:]).with_suffix(".py")
        package_init = ancestor / Path(*parts[1:]) / "__init__.py"
        if module_file.exists() or package_init.exists():
            return ancestor.resolve()
    return None


def _copy_component_source(original_source_root: Path, component_source_dir: Path) -> tuple[Path, Path]:
    original_source_root = original_source_root.resolve()
    if (original_source_root / "__init__.py").exists():
        component_source_dir.mkdir(parents=True, exist_ok=True)
        copied_root = component_source_dir / original_source_root.name
        python_path_root = component_source_dir
    else:
        component_source_dir.parent.mkdir(parents=True, exist_ok=True)
        copied_root = component_source_dir
        python_path_root = copied_root
    if not copied_root.exists():
        shutil.copytree(original_source_root, copied_root, ignore=_copy_ignore_for_destination(original_source_root, copied_root))
    return copied_root.resolve(), python_path_root.resolve()


def _copy_ignore_for_destination(original_source_root: Path, copied_root: Path):
    ignored_root_child = ""
    try:
        ignored_root_child = copied_root.resolve().relative_to(original_source_root.resolve()).parts[0]
    except (IndexError, ValueError):
        ignored_root_child = ""

    def ignore(directory: str, names: List[str]) -> set[str]:
        ignored = {name for name in names if name in IGNORED_SOURCE_NAMES}
        if ignored_root_child and Path(directory).resolve() == original_source_root.resolve():
            ignored.add(ignored_root_child)
        return ignored

    return ignore


def _rebase_absolute_paths(value: Any, replacements: List[tuple[Path, Path]]) -> Any:
    if isinstance(value, dict):
        return {key: _rebase_absolute_paths(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_rebase_absolute_paths(child, replacements) for child in value]
    if isinstance(value, str):
        return _rebase_path_string(value, replacements)
    return value


def _rebase_path_string(value: str, replacements: List[tuple[Path, Path]]) -> str:
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError):
        return value
    if not path.is_absolute():
        return value
    resolved = path.resolve()
    for old_root, new_root in replacements:
        if _is_relative_to(resolved, old_root):
            return str((new_root / resolved.relative_to(old_root)).resolve())
    return value


def _add_component_python_path(raw: JsonDict, source: ComponentSourceCopy) -> None:
    python_path = str(source.python_path_root)
    if source.kind == "environment":
        environment = raw.get("environment", {})
        adapter = environment.get("adapter", {}) if isinstance(environment.get("adapter"), dict) else {}
        _prepend_path(adapter, "pythonPath", python_path)
        config = adapter.get("config", {}) if isinstance(adapter.get("config"), dict) else {}
        evaluate = config.get("evaluate", {}) if isinstance(config.get("evaluate"), dict) else {}
        if evaluate.get("type") == "python":
            _prepend_path(evaluate, "pythonPath", python_path)
        return

    method = raw.get("method", {})
    implementation = method.get("implementation", {}) if isinstance(method.get("implementation"), dict) else {}
    _prepend_path(implementation, "pythonPath", python_path)


def _prepend_path(data: JsonDict, key: str, path: str) -> None:
    values = [str(item) for item in data.get(key, []) or [] if item]
    values = [item for item in values if item != path]
    data[key] = [path, *values]


def _record_component_source(raw: JsonDict, source: ComponentSourceCopy) -> None:
    extensions = raw.setdefault("extensions", {})
    run_source = extensions.setdefault("runSource", {})
    run_source[source.kind] = {
        "originalConfigPath": str(source.original_config_path),
        "originalSourceRoot": str(source.original_source_root),
        "copiedConfigPath": str(source.copied_config_path),
        "copiedSourceRoot": str(source.copied_source_root),
        "pythonPathRoot": str(source.python_path_root),
    }


def _run_component_setup(raw: JsonDict, source: ComponentSourceCopy) -> None:
    component = raw.get(source.kind, {})
    runtime = component.get("runtime", {}) if isinstance(component.get("runtime"), dict) else {}
    setup = runtime.get("setup") if isinstance(runtime.get("setup"), dict) else None
    if not setup:
        return
    setup_root = source.copied_source_root
    status_dir = setup_root / ".optpilot"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / "setup-status.json"
    setup_hash = _setup_hash(setup)
    prepared_runtime = prepared_runtime_from_setup(setup, setup_root)
    _apply_prepared_runtime(runtime, prepared_runtime)
    existing = _read_json(status_path)
    if existing.get("status") == "ready" and existing.get("setup_hash") == setup_hash:
        raw.setdefault("extensions", {}).setdefault("runSource", {}).setdefault(source.kind, {})["setupStatusPath"] = str(status_path)
        raw["extensions"]["runSource"][source.kind]["setupReused"] = True
        return
    status: JsonDict = {
        "component": source.kind,
        "status": "running",
        "started_at": utc_now_iso(),
        "setup_hash": setup_hash,
        "source_root": str(setup_root),
        "prepared_runtime": prepared_runtime,
    }
    _write_json(status_path, status)
    try:
        result = run_process_setup(setup, setup_root)
    except Exception as exc:
        status.update(
            {
                "status": "failed",
                "finished_at": utc_now_iso(),
                "error": str(exc),
            }
        )
        _write_json(status_path, status)
        raise
    prepared_runtime = prepared_runtime_from_setup(setup, setup_root)
    _apply_prepared_runtime(runtime, prepared_runtime)
    status.update(
        {
            "status": "ready",
            "finished_at": utc_now_iso(),
            "prepared_runtime": prepared_runtime,
            "result": result,
        }
    )
    _write_json(status_path, status)
    raw.setdefault("extensions", {}).setdefault("runSource", {}).setdefault(source.kind, {})["setupStatusPath"] = str(status_path)
    raw["extensions"]["runSource"][source.kind]["setupReused"] = False


def _apply_prepared_runtime(runtime: JsonDict, prepared_runtime: JsonDict) -> None:
    if not prepared_runtime:
        return
    prepared_env = dict(runtime.get("preparedEnv", {}) or {})
    path_entries = [str(item) for item in prepared_runtime.get("pathPrepend", []) or [] if item]
    if path_entries:
        prepared_env["PATH"] = _prepend_env_paths(path_entries, prepared_env.get("PATH"))
    python_path_entries = [str(item) for item in prepared_runtime.get("pythonPathPrepend", []) or [] if item]
    if python_path_entries:
        prepared_env["PYTHONPATH"] = _prepend_env_paths(python_path_entries, prepared_env.get("PYTHONPATH"))
    if prepared_env:
        runtime["preparedEnv"] = prepared_env
    if prepared_runtime.get("pythonExecutable"):
        runtime["pythonExecutable"] = str(prepared_runtime["pythonExecutable"])


def _prepend_env_paths(entries: List[str], existing: Any) -> str:
    values = [str(item) for item in entries if item]
    if existing:
        values.extend(str(existing).split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(item for item in values if item))


def _sync_environment_runtime_to_execution(raw: JsonDict) -> None:
    environment_runtime = raw.get("environment", {}).get("runtime")
    if not isinstance(environment_runtime, dict):
        return
    execution = raw.get("execution")
    if not isinstance(execution, dict):
        return
    backend = execution.get("backend")
    if not isinstance(backend, dict):
        return
    config = backend.setdefault("config", {})
    if not isinstance(config, dict):
        config = {}
        backend["config"] = config
    for key in ("preparedEnv", "pythonExecutable", "env", "environmentVariables", "envFromHost", "networkPolicy", "image", "containerExecutable", "build"):
        if key in environment_runtime:
            config[key] = deepcopy(environment_runtime[key])


def _setup_hash(setup: JsonDict) -> str:
    payload = json.dumps(setup, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: JsonDict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
