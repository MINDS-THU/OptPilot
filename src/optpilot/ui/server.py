"""Lightweight stdlib web UI server for OptPilot."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from ..config import AUTHORING_API_VERSION, compile_authoring_config
from ..registry import BUILTIN_COMPONENTS


JsonDict = Dict[str, Any]

RUN_SENTINEL_FILES = {
    "study_spec.json",
    "observations.jsonl",
    "trials.jsonl",
    "artifacts.jsonl",
}

CATALOG_KINDS = {"EnvironmentConfig", "MethodConfig", "StudyConfig"}
EXCLUDED_SCAN_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "resource",
    "runs",
}


@dataclass
class UiJob:
    job_id: str
    study_path: Path
    output_root: Path
    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    run_dir: Optional[Path] = None
    summary: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        status = self.status
        return {
            "job_id": self.job_id,
            "study_path": str(self.study_path),
            "output_root": str(self.output_root),
            "process_id": self.process.pid,
            "status": status,
            "exit_code": self.process.poll(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "stdout_log": str(self.stdout_path),
            "stderr_log": str(self.stderr_path),
            "summary": dict(self.summary),
        }

    @property
    def status(self) -> str:
        code = self.process.poll()
        if code is None:
            return "running"
        if self.finished_at is None:
            self.finished_at = time.time()
        return "completed" if code == 0 else "failed"


class UiState:
    def __init__(
        self,
        *,
        cwd: Path,
        catalog_roots: List[Path],
        run_roots: List[Path],
    ):
        self.cwd = cwd.resolve()
        self.catalog_roots = _dedupe_paths(catalog_roots or [self.cwd])
        self.run_roots = _dedupe_paths(run_roots or _default_run_roots(self.cwd))
        self.jobs: Dict[str, UiJob] = {}
        self.jobs_dir = self.cwd / ".optpilot-ui" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def launch_study(self, study_path: Path, output_root: Optional[Path]) -> UiJob:
        study_path = study_path.resolve()
        output_root = (output_root or self.cwd / "runs").resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        known_before = {path.resolve() for path in _find_run_dirs([output_root])}
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "optpilot",
            "run",
            str(study_path),
            "--output-root",
            str(output_root),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        job = UiJob(
            job_id=job_id,
            study_path=study_path,
            output_root=output_root,
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._watch_job, args=(job, known_before), daemon=True).start()
        return job

    def stop_job(self, job_id: str) -> JsonDict:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.process.poll() is None:
            job.process.terminate()
            try:
                job.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                job.process.kill()
                job.process.wait(timeout=5)
        job.finished_at = time.time()
        return job.to_dict()

    def _watch_job(self, job: UiJob, known_before: set[Path]) -> None:
        while job.process.poll() is None:
            job.run_dir = job.run_dir or _newest_run_dir(job.output_root, exclude=known_before)
            time.sleep(0.5)
        job.finished_at = time.time()
        job.run_dir = job.run_dir or _newest_run_dir(job.output_root, exclude=known_before)
        if job.run_dir:
            job.summary = _read_json(job.run_dir / "summary.json")
        if not job.summary:
            job.summary = _parse_summary_from_stdout(job.stdout_path)


def run_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    catalog_roots: Optional[List[str]] = None,
    run_roots: Optional[List[str]] = None,
    open_browser: bool = False,
) -> None:
    cwd = Path.cwd().resolve()
    state = UiState(
        cwd=cwd,
        catalog_roots=[Path(path).resolve() for path in catalog_roots or []],
        run_roots=[Path(path).resolve() for path in run_roots or []],
    )
    handler_cls = _handler_factory(state)
    server = ThreadingHTTPServer((host, port), handler_cls)
    url = f"http://{host}:{server.server_port}/"
    print(f"OptPilot UI running at {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot ui")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind")
    parser.add_argument("--catalog", action="append", default=[], help="Catalog root to scan")
    parser.add_argument("--runs", action="append", default=[], help="Run root to scan")
    parser.add_argument("--open-browser", action="store_true", help="Open the UI in a browser")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    run_ui(
        host=args.host,
        port=args.port,
        catalog_roots=args.catalog,
        run_roots=args.runs,
        open_browser=args.open_browser,
    )
    return 0


def _handler_factory(state: UiState):
    static_dir = Path(__file__).parent / "static"

    class OptPilotUiHandler(BaseHTTPRequestHandler):
        server_version = "OptPilotUI/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/" or path == "/index.html":
                    self._send_file(static_dir / "index.html")
                    return
                if path.startswith("/static/"):
                    self._send_file(static_dir / path.removeprefix("/static/"))
                    return
                if path == "/api/health":
                    self._send_json({"ok": True, "cwd": str(state.cwd)})
                    return
                if path == "/api/catalog":
                    self._send_json(_catalog_payload(state))
                    return
                if path == "/api/runs":
                    self._send_json({"runs": _list_runs(state)})
                    return
                if path.startswith("/api/runs/"):
                    self._handle_run_get(path, query)
                    return
                if path == "/api/jobs":
                    with state._lock:
                        jobs = [job.to_dict() for job in state.jobs.values()]
                    self._send_json({"jobs": sorted(jobs, key=lambda item: item["started_at"], reverse=True)})
                    return
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json({"error": str(exc), "type": type(exc).__name__}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/studies/validate":
                    payload = self._read_json_body()
                    study_path = _resolve_user_path(payload.get("study_path"), state.cwd)
                    self._send_json(_validate_study(study_path))
                    return
                if parsed.path == "/api/studies/launch":
                    payload = self._read_json_body()
                    study_path = _resolve_user_path(payload.get("study_path"), state.cwd)
                    output_root = _optional_user_path(payload.get("output_root"), state.cwd)
                    validation = _validate_study(study_path)
                    if not validation["valid"]:
                        self._send_json(validation, status=HTTPStatus.BAD_REQUEST)
                        return
                    job = state.launch_study(study_path, output_root)
                    self._send_json({"job": job.to_dict()}, status=HTTPStatus.CREATED)
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/stop"):
                    job_id = parsed.path.split("/")[3]
                    self._send_json({"job": state.stop_job(job_id)})
                    return
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            except KeyError as exc:
                self._send_json({"error": f"Unknown id: {exc.args[0]}"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json({"error": str(exc), "type": type(exc).__name__}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_run_get(self, path: str, query: Dict[str, List[str]]) -> None:
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"error": "Missing run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            run_id = parts[3]
            run_dir = _decode_id(run_id)
            if not _is_run_dir(run_dir):
                self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 4:
                self._send_json(_run_detail(run_dir))
                return
            resource = parts[4]
            if resource == "observations":
                self._send_json({"observations": _read_jsonl(run_dir / "observations.jsonl")})
                return
            if resource == "trials":
                self._send_json({"trials": _read_jsonl(run_dir / "trials.jsonl")})
                return
            if resource == "artifacts":
                self._send_json({"artifacts": _read_jsonl(run_dir / "artifacts.jsonl")})
                return
            if resource == "file":
                relative = query.get("path", [""])[0]
                self._send_run_file(run_dir, relative)
                return
            self._send_json({"error": "Unknown run resource"}, status=HTTPStatus.NOT_FOUND)

        def _send_run_file(self, run_dir: Path, relative: str) -> None:
            if not relative:
                self._send_json({"error": "Missing path"}, status=HTTPStatus.BAD_REQUEST)
                return
            requested = (run_dir / unquote(relative)).resolve()
            if not _is_relative_to(requested, run_dir.resolve()) or not requested.is_file():
                self._send_json({"error": "File not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if requested.stat().st_size > 1_000_000:
                self._send_json({"error": "File is too large to preview"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            self._send_json(
                {
                    "path": str(requested),
                    "relative_path": str(requested.relative_to(run_dir)),
                    "content": requested.read_text(encoding="utf-8", errors="replace"),
                }
            )

        def _read_json_body(self) -> JsonDict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: JsonDict, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return OptPilotUiHandler


def _catalog_payload(state: UiState) -> JsonDict:
    entries = _scan_catalog(state.catalog_roots)
    grouped = {kind: [] for kind in CATALOG_KINDS}
    for entry in entries:
        grouped.setdefault(entry["kind"], []).append(entry)
    return {
        "roots": [str(path) for path in state.catalog_roots],
        "environments": grouped.get("EnvironmentConfig", []),
        "methods": grouped.get("MethodConfig", []),
        "studies": grouped.get("StudyConfig", []),
        "builtins": {
            category: sorted(implementations)
            for category, implementations in BUILTIN_COMPONENTS.items()
        },
    }


def _scan_catalog(roots: Iterable[Path]) -> List[JsonDict]:
    entries: List[JsonDict] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for path in _iter_yaml_files(root):
            if path in seen:
                continue
            seen.add(path)
            raw = _read_yaml(path)
            kind = raw.get("kind")
            if kind not in CATALOG_KINDS or raw.get("apiVersion") != AUTHORING_API_VERSION:
                continue
            entries.append(_catalog_entry(path, raw))
    return sorted(entries, key=lambda item: (item["kind"], item["label"], item["path"]))


def _catalog_entry(path: Path, raw: JsonDict) -> JsonDict:
    kind = raw["kind"]
    label = raw.get("name") or raw.get("id") or path.stem
    entry: JsonDict = {
        "id": str(raw.get("id") or raw.get("name") or path.stem),
        "label": str(label),
        "kind": kind,
        "path": str(path),
        "description": str(raw.get("description", "")),
        "tags": list(raw.get("tags", []) or []),
        "summary": {},
    }
    if kind == "EnvironmentConfig":
        entry["summary"] = {
            "evaluate_type": raw.get("evaluate", {}).get("type"),
            "candidate_type": raw.get("candidate", {}).get("type"),
            "metrics": list(raw.get("metrics", {}).get("keys", []) or []),
        }
    elif kind == "MethodConfig":
        entry["summary"] = {
            "controller": raw.get("controller", {}).get("implementation", "builtin.single_engine_controller"),
            "engine": raw.get("engine", {}).get("implementation"),
            "batch_size": raw.get("engine", {}).get("config", {}).get("batchSize"),
        }
    elif kind == "StudyConfig":
        entry["summary"] = {
            "environment": raw.get("environment"),
            "method": raw.get("method"),
            "objective": raw.get("objective", {}),
            "budget": raw.get("budget", {}),
        }
    return entry


def _list_runs(state: UiState) -> List[JsonDict]:
    run_dirs = _find_run_dirs(state.run_roots)
    job_run_dirs = []
    with state._lock:
        for job in state.jobs.values():
            if job.run_dir:
                job_run_dirs.append(job.run_dir)
    run_dirs = _dedupe_paths([*run_dirs, *job_run_dirs])
    return [_run_summary(path, state) for path in sorted(run_dirs, key=_path_mtime, reverse=True)]


def _run_summary(run_dir: Path, state: Optional[UiState] = None) -> JsonDict:
    summary = _read_json(run_dir / "summary.json")
    study_spec = _read_json(run_dir / "study_spec.json")
    observations = _read_jsonl(run_dir / "observations.jsonl")
    status_counts = _status_counts(observations)
    matching_job = None
    if state is not None:
        with state._lock:
            for job in state.jobs.values():
                if job.run_dir and job.run_dir.resolve() == run_dir.resolve():
                    matching_job = job.to_dict()
                    break
    status = _run_status(summary, matching_job)
    objective = study_spec.get("objective", {}).get("primaryMetric", {})
    return {
        "id": _encode_id(run_dir),
        "path": str(run_dir),
        "name": study_spec.get("metadata", {}).get("name") or run_dir.name,
        "status": status,
        "completed_trials": summary.get("completed_trials", len(observations)),
        "best_metric": summary.get("best_metric"),
        "best_trial_id": summary.get("best_trial_id"),
        "best_artifact_id": summary.get("best_artifact_id"),
        "failure_count": summary.get("failure_count", _failure_count(status_counts)),
        "objective": objective,
        "target_id": study_spec.get("target", {}).get("targetId"),
        "method": _method_summary(study_spec),
        "started_at": summary.get("started_at"),
        "finished_at": summary.get("finished_at"),
        "updated_at": _path_mtime(run_dir),
        "status_counts": status_counts,
        "job": matching_job,
    }


def _run_detail(run_dir: Path) -> JsonDict:
    observations = _read_jsonl(run_dir / "observations.jsonl")
    trials = _read_jsonl(run_dir / "trials.jsonl")
    artifacts = _read_jsonl(run_dir / "artifacts.jsonl")
    return {
        "run": _run_summary(run_dir),
        "summary": _read_json(run_dir / "summary.json"),
        "study_spec": _read_json(run_dir / "study_spec.json"),
        "run_policy": _read_json(run_dir / "run_policy.json"),
        "run_lineage": _read_json(run_dir / "run_lineage.json"),
        "environment_snapshot": _read_json(run_dir / "environment_snapshot.json"),
        "observations": observations,
        "trials": trials,
        "artifacts": artifacts,
        "controller_decisions": _read_jsonl(run_dir / "controller_decisions.jsonl"),
        "engine_snapshots": _read_jsonl(run_dir / "engine_snapshots.jsonl"),
        "scheduler_events": _read_jsonl(run_dir / "scheduler_events.jsonl"),
        "files": _list_run_files(run_dir),
    }


def _validate_study(study_path: Path) -> JsonDict:
    try:
        raw = _read_yaml(study_path)
        if raw.get("kind") != "StudyConfig":
            return {"valid": False, "errors": ["Config must be kind StudyConfig."], "path": str(study_path)}
        compiled = compile_authoring_config(study_path)
        return {
            "valid": True,
            "errors": [],
            "path": str(study_path),
            "name": compiled.get("metadata", {}).get("name"),
            "target_id": compiled.get("target", {}).get("targetId"),
            "objective": compiled.get("objective", {}).get("primaryMetric", {}),
            "max_trials": compiled.get("stopping", {}).get("maxTrials"),
        }
    except Exception as exc:
        return {"valid": False, "errors": [str(exc)], "path": str(study_path)}


def _find_run_dirs(roots: Iterable[Path]) -> List[Path]:
    run_dirs: List[Path] = []
    for root in roots:
        root = root.resolve()
        if _is_run_dir(root):
            run_dirs.append(root)
            continue
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if _is_run_dir(path):
                run_dirs.append(path.resolve())
    return _dedupe_paths(run_dirs)


def _default_run_roots(cwd: Path) -> List[Path]:
    roots = [cwd / "runs"]
    examples = cwd / "examples"
    if examples.exists():
        roots.extend(path for path in examples.rglob("runs") if path.is_dir())
    return _dedupe_paths([path for path in roots if path.exists()])


def _newest_run_dir(root: Path, *, exclude: set[Path]) -> Optional[Path]:
    candidates = [path for path in _find_run_dirs([root]) if path.resolve() not in exclude]
    if not candidates:
        return None
    return max(candidates, key=_path_mtime)


def _is_run_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return any((path / file_name).exists() for file_name in RUN_SENTINEL_FILES)


def _list_run_files(run_dir: Path) -> List[JsonDict]:
    files = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(run_dir)
            stat = path.stat()
            files.append(
                {
                    "relative_path": str(relative),
                    "path": str(path),
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
    return files[:500]


def _iter_yaml_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.lower() in {".yaml", ".yml"}:
            yield root.resolve()
        return
    for path in root.rglob("*"):
        if any(part in EXCLUDED_SCAN_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}:
            yield path.resolve()


def _run_status(summary: JsonDict, job: Optional[JsonDict]) -> str:
    if job and job.get("status") == "running":
        return "running"
    if not summary:
        return "incomplete"
    completed = int(summary.get("completed_trials", 0) or 0)
    failures = int(summary.get("failure_count", 0) or 0)
    if completed > 0 and failures >= completed:
        return "failed"
    return "completed"


def _method_summary(study_spec: JsonDict) -> JsonDict:
    controllers = study_spec.get("controllers", [])
    engines = study_spec.get("engines", [])
    return {
        "controller": controllers[0].get("implementation") if controllers else None,
        "engine": engines[0].get("implementation") if engines else None,
    }


def _status_counts(observations: List[JsonDict]) -> JsonDict:
    counts: JsonDict = {}
    for observation in observations:
        status = observation.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _failure_count(status_counts: JsonDict) -> int:
    return sum(int(status_counts.get(status, 0) or 0) for status in {"failed", "invalid", "timeout", "partial"})


def _parse_summary_from_stdout(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _read_json(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_jsonl(path: Path) -> List[JsonDict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_parse_error": True, "raw": line})
    return rows


def _read_yaml(path: Path) -> JsonDict:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _resolve_user_path(value: Any, cwd: Path) -> Path:
    if not value:
        raise ValueError("Path is required.")
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _optional_user_path(value: Any, cwd: Path) -> Optional[Path]:
    if not value:
        return None
    return _resolve_user_path(value, cwd)


def _encode_id(path: Path) -> str:
    return base64.urlsafe_b64encode(str(path.resolve()).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_id(value: str) -> Path:
    padding = "=" * (-len(value) % 4)
    return Path(base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")).resolve()


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    result = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
