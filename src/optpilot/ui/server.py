"""Lightweight stdlib web UI server for OptPilot."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
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
    study_name: Optional[str] = None
    target_id: Optional[str] = None
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
            "study_name": self.study_name,
            "target_id": self.target_id,
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
        self.catalog_roots = _dedupe_paths(catalog_roots or _default_catalog_roots(self.cwd))
        self.run_roots = _dedupe_paths(run_roots or _default_run_roots(self.cwd))
        self.jobs: Dict[str, UiJob] = {}
        self.jobs_dir = self.cwd / ".optpilot-ui" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def launch_study(
        self,
        study_path: Path,
        output_root: Optional[Path],
        *,
        study_name: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> UiJob:
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
            study_name=study_name,
            target_id=target_id,
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


def add_ui_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind")
    parser.add_argument(
        "--catalog",
        action="append",
        default=[],
        help="Catalog root to scan. Defaults to examples and user_catalog when present.",
    )
    parser.add_argument("--runs", action="append", default=[], help="Run root to scan")
    parser.add_argument("--open-browser", action="store_true", help="Open the UI in a browser")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot ui")
    return add_ui_arguments(parser)


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
                if path == "/api/workspace":
                    self._send_json(_workspace_payload(state))
                    return
                if path == "/api/runtime/health":
                    self._send_json(_runtime_health())
                    return
                if path == "/api/catalog":
                    self._send_json(_catalog_payload(state))
                    return
                if path == "/api/environments":
                    self._send_json({"environments": _catalog_payload(state)["environments"]})
                    return
                if path.startswith("/api/environments/"):
                    self._send_json(_catalog_detail(state, "EnvironmentConfig", path.split("/", 3)[3]))
                    return
                if path == "/api/methods":
                    self._send_json({"methods": _catalog_payload(state)["methods"]})
                    return
                if path.startswith("/api/methods/"):
                    self._send_json(_catalog_detail(state, "MethodConfig", path.split("/", 3)[3]))
                    return
                if path == "/api/compatibility":
                    self._send_json(_compatibility_payload(state))
                    return
                if path == "/api/config/file":
                    requested = _resolve_user_path(query.get("path", [""])[0], state.cwd)
                    self._send_json(_read_editable_config_file(state, requested))
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
                if parsed.path == "/api/studies/draft":
                    payload = self._read_json_body()
                    self._send_json(_draft_study(state, payload))
                    return
                if parsed.path == "/api/studies/launch":
                    payload = self._read_json_body()
                    study_path = _resolve_user_path(payload.get("study_path"), state.cwd)
                    output_root = _optional_user_path(payload.get("output_root"), state.cwd)
                    validation = _validate_study(study_path)
                    if not validation["valid"]:
                        self._send_json(validation, status=HTTPStatus.BAD_REQUEST)
                        return
                    job = state.launch_study(
                        study_path,
                        output_root,
                        study_name=validation.get("name"),
                        target_id=validation.get("target_id"),
                    )
                    self._send_json({"job": job.to_dict()}, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/config/file":
                    payload = self._read_json_body()
                    requested = _resolve_user_path(payload.get("path"), state.cwd)
                    self._send_json(_write_editable_config_file(state, requested, str(payload.get("content", ""))))
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
        "uid": _encode_id(path),
        "id": str(raw.get("id") or raw.get("name") or path.stem),
        "label": str(label),
        "kind": kind,
        "path": str(path),
        "description": str(raw.get("description", "")),
        "tags": list(raw.get("tags", []) or []),
        "summary": {},
    }
    if kind == "EnvironmentConfig":
        candidate = raw.get("candidate", {})
        candidate_type = candidate.get("type")
        artifact_kind = candidate.get("artifactKind")
        editable = []
        files = candidate.get("files", {}) if isinstance(candidate.get("files"), dict) else candidate
        for item in files.get("editable", []) or []:
            if isinstance(item, dict) and item.get("path"):
                editable.append(str(item["path"]))
        entry["summary"] = {
            "evaluate_type": raw.get("evaluate", {}).get("type"),
            "candidate_type": candidate_type,
            "artifact_kind": artifact_kind,
            "runtime": _environment_runtime_summary(raw),
            "editable_files": editable,
            "capabilities": [
                interface.get("capability")
                for interface in raw.get("interfaces", []) or []
                if isinstance(interface, dict) and interface.get("capability")
            ],
            "metrics": list(raw.get("metrics", {}).get("keys", []) or []),
        }
    elif kind == "MethodConfig":
        compatibility = raw.get("compatibility", {}) if isinstance(raw.get("compatibility"), dict) else {}
        implementation = raw.get("implementation", {}) if isinstance(raw.get("implementation"), dict) else {}
        config = raw.get("config", {}) if isinstance(raw.get("config"), dict) else {}
        entry["summary"] = {
            "implementation_type": implementation.get("type"),
            "implementation": implementation.get("callable") or implementation.get("command") or implementation.get("endpoint"),
            "protocol": implementation.get("protocol", "optpilot.method.batch.v1"),
            "runtime": _method_runtime_summary(raw),
            "batch_size": config.get("batchSize"),
            "candidate_types": list(compatibility.get("candidateTypes", []) or []),
            "artifact_kinds": list(compatibility.get("artifactKinds", []) or []),
            "required_capabilities": list(compatibility.get("requiredCapabilities", []) or []),
        }
    elif kind == "StudyConfig":
        entry["summary"] = {
            "environment": raw.get("environment"),
            "method": raw.get("method"),
            "objective": raw.get("objective", {}),
            "budget": raw.get("budget", {}),
        }
    return entry


def _workspace_payload(state: UiState) -> JsonDict:
    return {
        "cwd": str(state.cwd),
        "catalog_roots": [str(path) for path in state.catalog_roots],
        "run_roots": [str(path) for path in state.run_roots],
        "jobs_dir": str(state.jobs_dir),
    }


def _runtime_health() -> JsonDict:
    docker = _executable_health("docker", ["docker", "--version"])
    podman = _executable_health("podman", ["podman", "--version"])
    return {
        "python": {
            "ok": True,
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "docker": docker,
        "podman": podman,
    }


def _executable_health(name: str, command: List[str]) -> JsonDict:
    path = shutil.which(name)
    if not path:
        return {"ok": False, "available": False, "path": None, "version": None}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except Exception as exc:
        return {"ok": False, "available": True, "path": path, "version": None, "error": str(exc)}
    text = (completed.stdout or completed.stderr).strip().splitlines()
    return {
        "ok": completed.returncode == 0,
        "available": True,
        "path": path,
        "version": text[0] if text else None,
        "error": completed.stderr.strip() if completed.returncode else "",
    }


def _catalog_detail(state: UiState, expected_kind: str, uid: str) -> JsonDict:
    path = _decode_id(uid)
    raw = _read_yaml(path)
    if raw.get("kind") != expected_kind or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{expected_kind} not found: {path}")
    entry = _catalog_entry(path, raw)
    compatibility = _compatibility_payload(state)
    if expected_kind == "EnvironmentConfig":
        related = [
            item for item in compatibility["pairs"]
            if item["environment"]["uid"] == entry["uid"]
        ]
    else:
        related = [
            item for item in compatibility["pairs"]
            if item["method"]["uid"] == entry["uid"]
        ]
    return {
        "entry": entry,
        "config": raw,
        "compatibility": {
            "compatible": [item for item in related if item["compatible"]],
            "incompatible": [item for item in related if not item["compatible"]],
        },
    }


def _compatibility_payload(state: UiState) -> JsonDict:
    catalog = _catalog_payload(state)
    pairs = []
    for environment in catalog["environments"]:
        env_raw = _read_yaml(Path(environment["path"]))
        for method in catalog["methods"]:
            method_raw = _read_yaml(Path(method["path"]))
            result = _compatibility_result(environment, env_raw, method, method_raw)
            pairs.append(result)
    return {
        "environments": catalog["environments"],
        "methods": catalog["methods"],
        "pairs": pairs,
    }


def _compatibility_result(environment: JsonDict, env_raw: JsonDict, method: JsonDict, method_raw: JsonDict) -> JsonDict:
    candidate = env_raw.get("candidate", {}) if isinstance(env_raw.get("candidate"), dict) else {}
    compatibility = method_raw.get("compatibility", {}) if isinstance(method_raw.get("compatibility"), dict) else {}
    env_candidate_type = candidate.get("type")
    env_artifact_kind = candidate.get("artifactKind")
    method_candidate_types = list(compatibility.get("candidateTypes", []) or [])
    method_artifact_kinds = list(compatibility.get("artifactKinds", []) or [])
    required_context = list(compatibility.get("requiredContext", []) or [])
    required_capabilities = list(compatibility.get("requiredCapabilities", []) or [])
    env_context = _environment_context_paths(candidate)
    env_capabilities = {
        str(item.get("capability"))
        for item in env_raw.get("interfaces", []) or []
        if isinstance(item, dict) and item.get("capability")
    }
    checks = []
    checks.append(_compat_check(
        not method_candidate_types or env_candidate_type in method_candidate_types,
        f"candidate type {env_candidate_type!r} is supported",
        f"method supports candidateTypes {method_candidate_types!r}, environment uses {env_candidate_type!r}",
    ))
    checks.append(_compat_check(
        not method_artifact_kinds or env_artifact_kind in method_artifact_kinds,
        f"artifact kind {env_artifact_kind!r} is supported",
        f"method supports artifactKinds {method_artifact_kinds!r}, environment uses {env_artifact_kind!r}",
    ))
    for required in required_context:
        checks.append(_compat_check(
            required in env_context,
            f"required context {required!r} is available",
            f"required context {required!r} is missing",
        ))
    for required in required_capabilities:
        checks.append(_compat_check(
            required in env_capabilities,
            f"required capability {required!r} is available",
            f"required capability {required!r} is missing",
        ))
    compatible = all(check["ok"] for check in checks)
    return {
        "compatible": compatible,
        "environment": _compat_entity(environment),
        "method": _compat_entity(method),
        "checks": checks,
        "reasons": [check["message"] for check in checks if not check["ok"]] or [check["message"] for check in checks],
    }


def _compat_entity(entry: JsonDict) -> JsonDict:
    return {
        "uid": entry["uid"],
        "id": entry["id"],
        "label": entry["label"],
        "path": entry["path"],
        "summary": entry.get("summary", {}),
    }


def _compat_check(ok: bool, success: str, failure: str) -> JsonDict:
    return {"ok": ok, "message": success if ok else failure}


def _environment_context_paths(candidate: JsonDict) -> set:
    paths = {"type", "artifactKind"}
    candidate_type = candidate.get("type")
    if candidate_type:
        paths.add(candidate_type)
    for top_level in ("parameters", "files", "opaque", "exposure", "workspace", "interfaces"):
        value = candidate.get(top_level)
        if isinstance(value, dict) and value:
            paths.add(top_level)
            for key, nested in value.items():
                if nested not in (None, [], {}):
                    paths.add(f"{top_level}.{key}")
        elif isinstance(value, list) and value:
            paths.add(top_level)
    return paths


def _draft_study(state: UiState, payload: JsonDict) -> JsonDict:
    environment_path = _resolve_user_path(payload.get("environment_path"), state.cwd)
    method_path = _resolve_user_path(payload.get("method_path"), state.cwd)
    environment = _read_yaml(environment_path)
    method = _read_yaml(method_path)
    compatibility = _compatibility_result(
        _catalog_entry(environment_path, environment),
        environment,
        _catalog_entry(method_path, method),
        method,
    )
    name = str(payload.get("name") or f"{environment.get('id', environment_path.stem)}-{method.get('id', method_path.stem)}")
    metric = str(payload.get("metric") or _first_metric(environment) or "score")
    direction = str(payload.get("direction") or "maximize")
    max_trials = int(payload.get("maxTrials", payload.get("max_trials", 12)) or 12)
    backend = str(payload.get("backend") or "local")
    parallelism = int(payload.get("parallelism", 1) or 1)
    timeout = int(payload.get("timeoutSeconds", payload.get("timeout_seconds", 120)) or 120)
    instances = _draft_instances(payload, state.cwd)
    if instances.get("source") == "none":
        instances = _matching_study_instances(state, environment_path, method_path) or instances
    draft = {
        "apiVersion": AUTHORING_API_VERSION,
        "kind": "StudyConfig",
        "name": name,
        "environment": str(environment_path),
        "method": str(method_path),
        "objective": {"metric": metric, "direction": direction},
        "instances": instances,
        "budget": {"maxTrials": max_trials},
        "execution": {"backend": backend, "parallelism": parallelism, "timeoutSeconds": timeout},
    }
    execution_config = _draft_execution_config(payload)
    if execution_config:
        draft["execution"]["config"] = execution_config
    if backend == "custom" and payload.get("customBackendImplementation"):
        draft["execution"]["implementation"] = str(payload["customBackendImplementation"])
    if payload.get("seed") not in (None, ""):
        draft["reproducibility"] = {"seed": int(payload.get("seed"))}
    draft_yaml = yaml.safe_dump(draft, sort_keys=False)
    draft_path = state.jobs_dir / f"draft-{uuid.uuid4().hex[:12]}.yaml"
    draft_path.write_text(draft_yaml, encoding="utf-8")
    validation = _validate_study(draft_path)
    return {
        "draft": draft,
        "yaml": draft_yaml,
        "path": str(draft_path),
        "compatibility": compatibility,
        "validation": validation,
    }


def _draft_instances(payload: JsonDict, cwd: Path) -> JsonDict:
    instance_paths = payload.get("instance_paths") or payload.get("instances")
    if isinstance(instance_paths, str) and instance_paths.strip():
        paths = [item.strip() for item in instance_paths.splitlines() if item.strip()]
    elif isinstance(instance_paths, list):
        paths = [str(item) for item in instance_paths if str(item).strip()]
    else:
        paths = []
    if paths:
        return {
            "source": "files",
            "paths": [str(_resolve_user_path(path, cwd)) for path in paths],
        }
    return {"source": "none"}


def _draft_execution_config(payload: JsonDict) -> JsonDict:
    backend = str(payload.get("backend") or "local")
    config = payload.get("executionConfig")
    if isinstance(config, dict):
        result = deepcopy(config)
    else:
        result = {}
    if backend == "container":
        for source, target in (
            ("containerImage", "image"),
            ("containerExecutable", "containerExecutable"),
            ("containerNetworkPolicy", "networkPolicy"),
        ):
            value = payload.get(source)
            if value not in (None, ""):
                result[target] = str(value)
        build = result.get("build") if isinstance(result.get("build"), dict) else {}
        for source, target in (
            ("containerBuildContext", "context"),
            ("containerBuildDockerfile", "dockerfile"),
            ("containerBuildTag", "tag"),
        ):
            value = payload.get(source)
            if value not in (None, ""):
                build[target] = str(value)
        if build:
            result["build"] = build
    if backend == "custom" and payload.get("customBackendConfig") not in (None, ""):
        custom_config = _parse_json_object(str(payload.get("customBackendConfig")), "custom backend config")
        result.update(custom_config)
    return result


def _matching_study_instances(state: UiState, environment_path: Path, method_path: Path) -> Optional[JsonDict]:
    for entry in _scan_catalog(state.catalog_roots):
        if entry["kind"] != "StudyConfig":
            continue
        study_path = Path(entry["path"])
        study = _read_yaml(study_path)
        if _study_ref_matches(study.get("environment"), study_path, environment_path) and _study_ref_matches(
            study.get("method"), study_path, method_path
        ):
            instances = study.get("instances")
            if isinstance(instances, dict) and instances.get("source", "none") != "none":
                return _resolve_study_instances(instances, study_path)
    return None


def _resolve_study_instances(instances: JsonDict, study_path: Path) -> JsonDict:
    resolved = deepcopy(instances)
    if resolved.get("source") == "files":
        resolved["paths"] = [
            str(_resolve_config_path(path, study_path))
            for path in resolved.get("paths", []) or []
        ]
    return resolved


def _study_ref_matches(value: Any, study_path: Path, expected_path: Path) -> bool:
    if not value:
        return False
    return _resolve_config_path(value, study_path) == expected_path.resolve()


def _resolve_config_path(value: Any, config_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _first_metric(environment: JsonDict) -> Optional[str]:
    metrics = environment.get("metrics", {}) if isinstance(environment.get("metrics"), dict) else {}
    keys = metrics.get("keys", [])
    if isinstance(keys, list) and keys:
        return str(keys[0])
    return None


def _relative_or_absolute(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except ValueError:
        return str(path.resolve())


def _environment_runtime_summary(raw: JsonDict) -> JsonDict:
    evaluate = raw.get("evaluate", {}) if isinstance(raw.get("evaluate"), dict) else {}
    return {
        "evaluate_type": evaluate.get("type"),
        "timeoutSeconds": evaluate.get("timeoutSeconds"),
        "has_python_path": bool(evaluate.get("pythonPath")),
    }


def _method_runtime_summary(raw: JsonDict) -> JsonDict:
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    implementation = raw.get("implementation", {}) if isinstance(raw.get("implementation"), dict) else {}
    return {
        "type": runtime.get("type", "host" if implementation.get("type") == "command" else "process"),
        "image": runtime.get("image") or runtime.get("runtimeImage"),
        "has_build": bool(runtime.get("build")),
        "networkPolicy": runtime.get("networkPolicy"),
    }


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
        "method_calls": _read_jsonl(run_dir / "method_calls.jsonl"),
        "method_events": _read_jsonl(run_dir / "method_events.jsonl"),
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


def _default_catalog_roots(cwd: Path) -> List[Path]:
    roots = [
        cwd / "examples",
        cwd / "user_catalog",
    ]
    existing = [path for path in roots if path.exists()]
    return existing or [cwd]


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
    method = study_spec.get("method", {}) if isinstance(study_spec.get("method"), dict) else {}
    implementation = method.get("implementation", {}) if isinstance(method.get("implementation"), dict) else {}
    return {
        "id": method.get("id"),
        "implementation_type": implementation.get("type"),
        "implementation": implementation.get("callable") or implementation.get("command") or implementation.get("endpoint"),
        "protocol": implementation.get("protocol", "optpilot.method.batch.v1"),
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


EDITABLE_FILE_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _read_editable_config_file(state: UiState, path: Path) -> JsonDict:
    _require_editable_file(state, path, must_exist=True)
    content = path.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(path),
        "relative_path": _relative_or_absolute(path, state.cwd),
        "content": content,
        "validation": _validate_editable_content(path, content),
    }


def _write_editable_config_file(state: UiState, path: Path, content: str) -> JsonDict:
    _require_editable_file(state, path, must_exist=False)
    validation = _validate_editable_content(path, content)
    if not validation["valid"]:
        return {
            "path": str(path),
            "relative_path": _relative_or_absolute(path, state.cwd),
            "saved": False,
            "validation": validation,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "relative_path": _relative_or_absolute(path, state.cwd),
        "saved": True,
        "validation": validation,
    }


def _require_editable_file(state: UiState, path: Path, *, must_exist: bool) -> None:
    path = path.resolve()
    if not _is_relative_to(path, state.cwd):
        raise PermissionError("Config editor can only access files inside the workspace.")
    if any(part in EXCLUDED_SCAN_DIRS for part in path.relative_to(state.cwd).parts):
        raise PermissionError("Config editor cannot edit ignored workspace directories.")
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    name = path.name.lower()
    if path.suffix.lower() not in EDITABLE_FILE_SUFFIXES and not name.startswith("dockerfile"):
        raise ValueError("Config editor only supports text/config files.")
    if path.exists() and path.stat().st_size > 300_000:
        raise ValueError("File is too large for the lightweight editor.")


def _validate_editable_content(path: Path, content: str) -> JsonDict:
    if len(content.encode("utf-8")) > 300_000:
        return {"valid": False, "errors": ["File content is too large for the lightweight editor."]}
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            yaml.safe_load(content) if content.strip() else None
        except yaml.YAMLError as exc:
            return {"valid": False, "errors": [str(exc)]}
    if path.suffix.lower() == ".json":
        try:
            json.loads(content) if content.strip() else None
        except json.JSONDecodeError as exc:
            return {"valid": False, "errors": [str(exc)]}
    return {"valid": True, "errors": []}


def _parse_json_object(value: str, label: str) -> JsonDict:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return parsed


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
