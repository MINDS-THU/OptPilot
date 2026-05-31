"""Run environment snapshot helpers."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import utc_now_iso


JsonDict = Dict[str, Any]

DEFAULT_ENV_KEYS = [
    "PATH",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CUDA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
]
DEPENDENCY_FILE_NAMES = [
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "environment.yml",
    "environment.yaml",
    "conda.yml",
    "conda.yaml",
]


def build_environment_snapshot(
    *,
    study_spec_path: Optional[Path] = None,
    run_dir: Optional[Path] = None,
    env_keys: Optional[Iterable[str]] = None,
    dependency_roots: Optional[Iterable[Path]] = None,
) -> JsonDict:
    """Capture reproducibility-relevant runtime metadata for a study run."""

    env_keys = list(env_keys or DEFAULT_ENV_KEYS)
    snapshot: JsonDict = {
        "created_at": utc_now_iso(),
        "python": {
            "executable": sys.executable,
            "version": sys.version,
            "version_info": list(sys.version_info[:5]),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "platform": platform.platform(),
        },
        "process": {
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
        },
        "environment_variables": _selected_environment(env_keys),
        "packages": _installed_packages(),
    }
    if run_dir is not None:
        snapshot["run_dir"] = str(run_dir.resolve())
    if study_spec_path is not None:
        snapshot["study_spec"] = _file_snapshot(study_spec_path)
    snapshot["dependency_files"] = _dependency_file_snapshots(study_spec_path, dependency_roots)
    return snapshot


def _selected_environment(keys: Iterable[str]) -> JsonDict:
    selected = {}
    for key in keys:
        if key in os.environ:
            selected[key] = os.environ[key]
    return selected


def _installed_packages() -> List[JsonDict]:
    packages = []
    for distribution in importlib_metadata.distributions():
        name = distribution.metadata.get("Name") or distribution.metadata.get("Summary") or "unknown"
        packages.append(
            {
                "name": str(name),
                "version": distribution.version,
            }
        )
    packages.sort(key=lambda item: item["name"].lower())
    return packages


def _dependency_file_snapshots(
    study_spec_path: Optional[Path],
    dependency_roots: Optional[Iterable[Path]],
) -> List[JsonDict]:
    roots = []
    if dependency_roots is not None:
        roots.extend(Path(root).resolve() for root in dependency_roots)
    elif study_spec_path is not None:
        roots.extend(_ancestor_roots(Path(study_spec_path).resolve().parent))
    else:
        roots.append(Path.cwd().resolve())

    snapshots = []
    seen = set()
    for root in roots:
        for name in DEPENDENCY_FILE_NAMES:
            path = root / name
            resolved = path.resolve()
            if resolved in seen or not resolved.exists() or not resolved.is_file():
                continue
            seen.add(resolved)
            record = _file_snapshot(resolved)
            record["name"] = name
            record["root"] = str(root)
            record["kind"] = _dependency_kind(name)
            snapshots.append(record)
    snapshots.sort(key=lambda item: item["path"])
    return snapshots


def _ancestor_roots(start: Path) -> List[Path]:
    roots = []
    current = start.resolve()
    for root in [current, *current.parents]:
        roots.append(root)
        if (root / ".git").exists() or (root / "pyproject.toml").exists():
            break
    return roots


def _dependency_kind(name: str) -> str:
    if name == "pyproject.toml":
        return "python_project"
    if name in {"uv.lock", "poetry.lock", "Pipfile.lock"}:
        return "lockfile"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "pip_requirements"
    if name.endswith((".yml", ".yaml")):
        return "conda_environment"
    return "dependency_file"


def _file_snapshot(path: Path) -> JsonDict:
    resolved = path.resolve()
    payload: JsonDict = {
        "path": str(resolved),
        "exists": resolved.exists(),
    }
    if resolved.exists() and resolved.is_file():
        payload.update(
            {
                "sha256": _sha256_file(resolved),
                "sizeBytes": resolved.stat().st_size,
            }
        )
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
