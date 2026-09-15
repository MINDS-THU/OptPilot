"""Execute one Pi finalizer probe through the normal simulation boundary."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:  # pragma: no cover - exercised by the Pi CLI
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from devs_display.backend.simulation_execution import (  # type: ignore
        SimulationExecutionService,
    )
else:
    from .simulation_execution import SimulationExecutionService


_MAX_REQUEST_BYTES = 64 * 1024


def _real_directory(value: str, *, label: str) -> Path:
    if not value.strip():
        raise ValueError(f"{label} is not configured.")
    supplied = Path(value)
    resolved = supplied.resolve(strict=True)
    supplied_metadata = supplied.lstat()
    resolved_metadata = resolved.lstat()
    if (
        stat.S_ISLNK(supplied_metadata.st_mode)
        or stat.S_ISLNK(resolved_metadata.st_mode)
        or not stat.S_ISDIR(resolved_metadata.st_mode)
    ):
        raise ValueError(f"{label} must be a real directory.")
    return resolved


def _project_directory(workspace: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("project_path must be a canonical relative path.")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("project_path must be a canonical relative path.")
    candidate = workspace.joinpath(*relative.parts)
    for parent in (candidate, *candidate.parents):
        if parent == workspace.parent:
            break
        if parent.exists() and parent.is_symlink():
            raise ValueError("project_path may not contain symbolic links.")
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(workspace)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("project_path must select a real directory.")
    return resolved


def run_request(request: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(request, Mapping) or set(request) != {"project_path", "parameters"}:
        raise ValueError("The Pi execution request has unsupported fields.")
    parameters = request.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be a JSON object.")
    configured_project = str(environment.get("PI_DEVS_PROJECT_PATH") or "")
    if not configured_project:
        raise ValueError("PI_DEVS_PROJECT_PATH is not configured.")
    if request.get("project_path") != configured_project:
        raise ValueError("project_path does not select the assigned simulation.")

    workspace = _real_directory(
        str(environment.get("PI_DEVS_WORKSPACE_ROOT") or ""),
        label="Pi finalizer workspace",
    )
    project = _project_directory(workspace, request.get("project_path"))
    run_root_value = str(environment.get("PI_DEVS_RUN_ROOT") or "").strip()
    if not run_root_value:
        raise ValueError("PI_DEVS_RUN_ROOT is not configured.")
    run_root = Path(run_root_value).resolve()
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    service = SimulationExecutionService(
        run_root,
        sys.executable,
        max_concurrency=1,
        allowed_bundle_root=workspace,
        max_pending=1,
    )
    record = service.execute(project, dict(parameters), purpose="finalizer")
    execution_id = str(record["execution_id"])
    execution_root = run_root / execution_id
    stdout_path = execution_root / "stdout.txt"
    stderr_path = execution_root / "stderr.txt"
    stdout_path.write_text(str(record.get("stdout") or ""), encoding="utf-8")
    stderr_path.write_text(str(record.get("stderr") or ""), encoding="utf-8")

    result_paths: list[str] = []
    results_root = execution_root / "results"
    for item in record.get("result_files") or []:
        relative = item.get("path") if isinstance(item, Mapping) else None
        if not isinstance(relative, str):
            continue
        result = results_root.joinpath(*PurePosixPath(relative).parts).resolve(strict=True)
        result.relative_to(results_root.resolve(strict=True))
        metadata = result.lstat()
        if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            result_paths.append(str(result))

    return {
        "status": record.get("status"),
        "exit_code": record.get("exit_code"),
        "duration_seconds": record.get("duration_seconds"),
        "stdout": str(stdout_path),
        "stdout_truncated": bool(record.get("stdout_truncated")),
        "stderr": str(stderr_path),
        "stderr_truncated": bool(record.get("stderr_truncated")),
        "result_files": result_paths,
        "failure_kind": record.get("failure_kind"),
        "message": record.get("message"),
    }


def main() -> int:
    encoded = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(encoded) > _MAX_REQUEST_BYTES:
        print(json.dumps({"error": "Pi execution request exceeds 64 KiB."}))
        return 2
    try:
        request = json.loads(encoded.decode("utf-8"))
        result = run_request(request, os.environ)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
