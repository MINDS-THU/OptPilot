"""Import helpers for external benchmark layouts."""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


def build_frontier_unified_study_config(
    benchmark_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    study_name: str | None = None,
    engine_implementation: str = "python:my_lab.engines:FrontierCodeEngine",
    max_trials: int = 20,
    candidate_parallelism: int = 1,
) -> JsonDict:
    """Build an OptPilot StudyConfig draft from Frontier unified metadata.

    The returned config is intentionally a draft: users still provide the engine
    that proposes file artifact manifests. OptPilot owns validation,
    materialization, evaluator execution, and official observation records.
    """

    benchmark_dir, metadata_dir = _resolve_frontier_dirs(Path(benchmark_path))
    repo_root_path = Path(repo_root).resolve() if repo_root else _infer_frontier_repo_root(benchmark_dir)
    benchmark_id = _frontier_benchmark_id(benchmark_dir, repo_root_path)

    initial_program = _read_scalar(metadata_dir / "initial_program.txt", required=True)
    candidate_destination = _read_scalar(metadata_dir / "candidate_destination.txt", required=True)
    eval_command = _read_scalar(metadata_dir / "eval_command.txt", required=True)
    eval_cwd = _read_scalar(metadata_dir / "eval_cwd.txt", default=".")
    copy_files = _read_list(metadata_dir / "copy_files.txt") or ["."]
    readonly_files = _read_list(metadata_dir / "readonly_files.txt")
    artifact_files = _read_list(metadata_dir / "artifact_files.txt")
    agent_files = _read_list(metadata_dir / "agent_files.txt")
    constraints_text = _read_text(metadata_dir / "constraints.txt")
    timeout_seconds = _read_int(metadata_dir / "timeout_s.txt", default=600)
    metrics_json = _read_scalar(metadata_dir / "metrics_json.txt", default="metrics.json")
    artifacts_json = _read_scalar(metadata_dir / "artifacts_json.txt", default="artifacts.json")

    return {
        "apiVersion": "optpilot.io/v3alpha1",
        "kind": "StudyConfig",
        "name": study_name or f"frontier-{_slugify(benchmark_id)}",
        "description": f"Imported Frontier unified benchmark: {benchmark_id}",
        "tags": ["frontier", "code-evolution"],
        "environment": {
            "apiVersion": "optpilot.io/v3alpha1",
            "kind": "EnvironmentConfig",
            "id": f"frontier-{_slugify(benchmark_id)}",
            "evaluate": {
                "type": "command",
                "command": shlex.split(eval_command),
                "cwd": eval_cwd,
                "env": {
                    "FRONTIER_ENGINEERING_ROOT": str(repo_root_path),
                    "FRONTIER_EVAL_UNIFIED_SOURCE_BENCHMARK_DIR": str(benchmark_dir),
                },
                "timeoutSeconds": timeout_seconds,
            },
            "candidate": {
                "type": "files",
                "artifactKind": "code_bundle",
                "description": "Frontier benchmark candidate files.",
                "files": {
                    "root": ".",
                    "source": {
                        "type": "workspace_copy",
                        "root": ".",
                    },
                    "editable": [
                        {
                            "path": candidate_destination,
                            "language": _language_for_path(candidate_destination),
                            "role": "candidate_program",
                        }
                    ],
                    "required": [candidate_destination],
                    "allow": ["**/*"],
                },
                "exposure": {
                    "contextFiles": [str((benchmark_dir / path).resolve()) for path in agent_files],
                },
            },
            "workspace": {
                "copy": [
                    {
                        "from": str((benchmark_dir / source).resolve()),
                        "to": source,
                    }
                    for source in copy_files
                ],
                "readonly": readonly_files,
            },
            "metrics": {
                "source": "file",
                "path": metrics_json,
                "keys": ["combined_score"],
            },
            "filesToSave": [artifacts_json, *artifact_files],
        },
        "method": {
            "apiVersion": "optpilot.io/v3alpha1",
            "kind": "MethodConfig",
            "id": "frontier-code-engine",
            "engine": {
                "implementation": engine_implementation,
                "config": {
                    "artifactKind": "code_bundle",
                    "candidateDestination": candidate_destination,
                    "initialProgram": str((benchmark_dir / initial_program).resolve()),
                    "agentFiles": [str((benchmark_dir / path).resolve()) for path in agent_files],
                    "constraints": constraints_text,
                },
            },
            "compatibility": {
                "candidateTypes": ["files"],
                "artifactKinds": ["code_bundle"],
                "requiredContext": ["files.source", "files.editable"],
                "optionalContext": ["exposure.contextFiles"],
            },
        },
        "objective": {
            "metric": "combined_score",
            "direction": "maximize",
        },
        "instances": {
            "source": "inline",
            "value": {
                "benchmark_id": benchmark_id,
                "benchmark_source": str(benchmark_dir),
            },
        },
        "budget": {
            "maxTrials": max_trials,
        },
        "execution": {
            "backend": "local",
            "parallelism": candidate_parallelism,
            "timeoutSeconds": timeout_seconds,
        },
    }


def build_frontier_initial_artifact(
    benchmark_path: str | Path,
    *,
    artifact_id: str = "frontier-initial-program",
) -> JsonDict:
    """Return a code artifact manifest for Frontier's initial program."""

    benchmark_dir, metadata_dir = _resolve_frontier_dirs(Path(benchmark_path))
    initial_program = _read_scalar(metadata_dir / "initial_program.txt", required=True)
    candidate_destination = _read_scalar(metadata_dir / "candidate_destination.txt", required=True)
    initial_path = (benchmark_dir / initial_program).resolve()
    if not initial_path.exists() or initial_path.is_dir():
        raise FileNotFoundError(f"Frontier initial program does not exist: {initial_program}")
    return {
        "artifact_id": artifact_id,
        "artifact_kind": "code_bundle",
        "spec": {
            "bundleRef": str(initial_path.parent),
            "entrypoint": candidate_destination,
            "files": [
                {
                    "path": candidate_destination,
                    "contentRef": str(initial_path),
                    "sha256": _sha256_file(initial_path),
                }
            ],
        },
        "lineage": {
            "parents": [],
        },
        "generator_record": {
            "engine_id": "frontier_importer",
            "strategy": "initial_program",
        },
    }


def _resolve_frontier_dirs(path: Path) -> tuple[Path, Path]:
    resolved = path.resolve()
    if resolved.name == "frontier_eval":
        metadata_dir = resolved
        benchmark_dir = resolved.parent
    else:
        benchmark_dir = resolved
        metadata_dir = resolved / "frontier_eval"
    if not metadata_dir.is_dir():
        raise FileNotFoundError(f"Frontier metadata directory not found: {metadata_dir}")
    return benchmark_dir, metadata_dir


def _infer_frontier_repo_root(benchmark_dir: Path) -> Path:
    benchmark_dir = benchmark_dir.resolve()
    for parent in [benchmark_dir, *benchmark_dir.parents]:
        benchmarks_root = parent / "benchmarks"
        if benchmarks_root.is_dir() and _is_relative_to(benchmark_dir, benchmarks_root):
            return parent.resolve()
        if (parent / "frontier_eval" / "tasks" / "unified").is_dir():
            return parent.resolve()
    return benchmark_dir.resolve()


def _frontier_benchmark_id(benchmark_dir: Path, repo_root: Path) -> str:
    benchmarks_root = repo_root / "benchmarks"
    if benchmarks_root.is_dir() and _is_relative_to(benchmark_dir, benchmarks_root):
        return benchmark_dir.resolve().relative_to(benchmarks_root.resolve()).as_posix()
    return benchmark_dir.name


def _read_scalar(path: Path, *, default: Optional[str] = None, required: bool = False) -> str:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required Frontier metadata file is missing: {path}")
        return "" if default is None else default
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    if required:
        raise ValueError(f"Required Frontier metadata file is empty: {path}")
    return "" if default is None else default


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_list(path: Path) -> List[str]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            items.append(stripped)
    return items


def _read_int(path: Path, *, default: int) -> int:
    value = _read_scalar(path, default=str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Frontier metadata value must be an integer: {path}") from exc


def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower() or "benchmark"


def _language_for_path(value: str) -> str:
    suffix = Path(value).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".sh": "shell",
        ".java": "java",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".c": "c",
        ".rs": "rust",
    }.get(suffix, "")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
