"""Lightweight stdlib web UI server for OptPilot."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import mimetypes
import os
import shutil
import socket
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
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import yaml

from ..agent import OpenHandsAdapter, OpenHandsRuntimeConfig
from ..config import (
    AUTHORING_API_VERSION,
    candidate_contract_mismatch,
    compile_authoring_config,
    validate_authoring_config,
)
from ..registry import BUILTIN_COMPONENTS


JsonDict = Dict[str, Any]

RUN_SENTINEL_FILES = {
    "study_spec.json",
    "observations.jsonl",
    "trials.jsonl",
    "candidates.jsonl",
}

CATALOG_CONFIGS = {"environment", "method", "study"}
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
    environment_id: Optional[str] = None
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
            "environment_id": self.environment_id,
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


@dataclass
class CodeServerOptions:
    executable: Optional[str] = None
    host: str = "127.0.0.1"
    port: int = 8766
    auth: str = "none"
    password: Optional[str] = None


@dataclass
class CodeServerState:
    options: CodeServerOptions
    process: Optional[subprocess.Popen] = None
    started_at: Optional[float] = None
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    workspace_root: Optional[Path] = None

    @property
    def url(self) -> str:
        return f"http://{self.options.host}:{self.options.port}/"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class UiState:
    def __init__(
        self,
        *,
        cwd: Path,
        catalog_roots: List[Path],
        run_roots: List[Path],
        code_server: Optional[CodeServerOptions] = None,
    ):
        self.cwd = cwd.resolve()
        self.catalog_roots = _dedupe_paths(catalog_roots or _default_catalog_roots(self.cwd))
        self.run_roots = _dedupe_paths(run_roots or _default_run_roots(self.cwd))
        self.jobs: Dict[str, UiJob] = {}
        self.jobs_dir = self.cwd / ".optpilot-ui" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.cwd / ".optpilot-ui" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir = self.cwd / ".optpilot-ui" / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.agent_sessions_dir = self.cwd / ".optpilot-ui" / "agent_sessions"
        self.agent_sessions_dir.mkdir(parents=True, exist_ok=True)
        self.code_server_dir = self.cwd / ".optpilot-ui" / "code-server"
        self.code_server_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.cwd / ".optpilot-ui" / "settings.json"
        self.agent_adapter = OpenHandsAdapter(_openhands_config_from_settings(_read_ui_settings(self)))
        code_server_options = code_server or CodeServerOptions()
        if not code_server_options.executable:
            local_code_server = _local_code_server_executable(self.cwd)
            if local_code_server.exists():
                code_server_options.executable = str(local_code_server)
        self.code_server = CodeServerState(code_server_options)
        self._lock = threading.Lock()

    def launch_study(
        self,
        study_path: Path,
        output_root: Optional[Path],
        *,
        study_name: Optional[str] = None,
        environment_id: Optional[str] = None,
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
            environment_id=environment_id,
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

    def code_server_status(self) -> JsonDict:
        executable = _code_server_executable(self.code_server.options)
        process_running = self.code_server.running
        reachable = _code_server_reachable(self.code_server.url)
        port_conflict = _port_listening(self.code_server.options.host, self.code_server.options.port) and not reachable and not process_running
        return {
            "available": bool(executable),
            "executable": executable,
            "installed": bool(executable),
            "running": process_running or reachable,
            "managed": process_running,
            "port_conflict": port_conflict,
            "pid": self.code_server.process.pid if process_running and self.code_server.process else None,
            "url": self.code_server.url,
            "host": self.code_server.options.host,
            "port": self.code_server.options.port,
            "auth": self.code_server.options.auth,
            "started_at": self.code_server.started_at,
            "workspace_root": str(self.code_server.workspace_root or self.cwd),
            "stdout_log": str(self.code_server.stdout_path) if self.code_server.stdout_path else None,
            "stderr_log": str(self.code_server.stderr_path) if self.code_server.stderr_path else None,
            "install_hint": "Install coder/code-server and ensure the code-server binary is on PATH, or pass --code-server-bin.",
        }

    def start_code_server(self, folder: Optional[Path] = None) -> JsonDict:
        executable = _code_server_executable(self.code_server.options)
        if not executable:
            raise FileNotFoundError("code-server executable not found. Install coder/code-server or pass --code-server-bin.")
        workspace_root = _safe_code_server_folder(self, folder or self.cwd)
        if self.code_server.running:
            self.code_server.workspace_root = workspace_root
            return self.code_server_open_url(workspace_root)
        if _code_server_reachable(self.code_server.url):
            self.code_server.workspace_root = workspace_root
            return self.code_server_open_url(workspace_root)
        if _port_listening(self.code_server.options.host, self.code_server.options.port):
            self.code_server.options.port = _find_available_port(self.code_server.options.host, self.code_server.options.port + 1)
        stdout_path = self.code_server_dir / "stdout.log"
        stderr_path = self.code_server_dir / "stderr.log"
        user_data_dir, extensions_dir = self._prepare_code_server_profile()
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        command = [
            executable,
            "--bind-addr",
            f"{self.code_server.options.host}:{self.code_server.options.port}",
            "--auth",
            self.code_server.options.auth,
            "--user-data-dir",
            str(user_data_dir),
            "--extensions-dir",
            str(extensions_dir),
            "--disable-telemetry",
            "--disable-update-check",
            "--disable-workspace-trust",
            "--disable-getting-started-override",
            str(workspace_root),
        ]
        env = os.environ.copy()
        if self.code_server.options.password:
            env["PASSWORD"] = self.code_server.options.password
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                env=env,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        self.code_server.process = process
        self.code_server.started_at = time.time()
        self.code_server.stdout_path = stdout_path
        self.code_server.stderr_path = stderr_path
        self.code_server.workspace_root = workspace_root
        return self.code_server_open_url(workspace_root)

    def _prepare_code_server_profile(self) -> tuple[Path, Path]:
        user_data_dir = self.code_server_dir / "user-data"
        extensions_dir = self.code_server_dir / "extensions"
        settings_path = user_data_dir / "User" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        extensions_dir.mkdir(parents=True, exist_ok=True)
        settings: JsonDict = {}
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    settings.update(existing)
            except json.JSONDecodeError:
                settings = {}
        settings.update(
            {
                "chat.agent.enabled": False,
                "chat.commandCenter.enabled": False,
                "chat.disableAIFeatures": True,
                "extensions.ignoreRecommendations": True,
                "git.openRepositoryInParentFolders": "never",
                "telemetry.telemetryLevel": "off",
                "update.mode": "none",
                "window.commandCenter": False,
                "workbench.startupEditor": "none",
                "workbench.tips.enabled": False,
                "workbench.welcomePage.walkthroughs.openOnInstall": False,
            }
        )
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return user_data_dir, extensions_dir

    def stop_code_server(self) -> JsonDict:
        if self.code_server.process and self.code_server.process.poll() is None:
            self.code_server.process.terminate()
            try:
                self.code_server.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.code_server.process.kill()
                self.code_server.process.wait(timeout=5)
        return self.code_server_status()

    def code_server_open_url(self, folder: Optional[Path] = None) -> JsonDict:
        workspace_root = _safe_code_server_folder(self, folder or self.code_server.workspace_root or self.cwd)
        workspace_root.mkdir(parents=True, exist_ok=True)
        self.code_server.workspace_root = workspace_root
        url = f"{self.code_server.url}?folder={quote(str(workspace_root), safe='')}"
        status = self.code_server_status()
        status.update({"open_url": url, "folder": str(workspace_root)})
        return status

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
    code_server_bin: Optional[str] = None,
    code_server_host: str = "127.0.0.1",
    code_server_port: int = 8766,
    code_server_auth: str = "none",
    code_server_password: Optional[str] = None,
    open_browser: bool = False,
) -> None:
    cwd = Path.cwd().resolve()
    state = UiState(
        cwd=cwd,
        catalog_roots=[Path(path).resolve() for path in catalog_roots or []],
        run_roots=[Path(path).resolve() for path in run_roots or []],
        code_server=CodeServerOptions(
            executable=code_server_bin,
            host=code_server_host,
            port=code_server_port,
            auth=code_server_auth,
            password=code_server_password,
        ),
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
        state.stop_code_server()
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
    parser.add_argument("--code-server-bin", default=None, help="Path to the coder/code-server executable")
    parser.add_argument("--code-server-host", default="127.0.0.1", help="Host interface for coder/code-server")
    parser.add_argument("--code-server-port", type=int, default=8766, help="Port for coder/code-server")
    parser.add_argument(
        "--code-server-auth",
        choices=["none", "password"],
        default="none",
        help="Authentication mode passed to coder/code-server",
    )
    parser.add_argument("--code-server-password", default=None, help="PASSWORD value when --code-server-auth=password")
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
        code_server_bin=args.code_server_bin,
        code_server_host=args.code_server_host,
        code_server_port=args.code_server_port,
        code_server_auth=args.code_server_auth,
        code_server_password=args.code_server_password,
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
                if path == "/api/workspaces":
                    self._send_json({"workspaces": _list_ui_workspaces(state)})
                    return
                if path.startswith("/api/workspaces/"):
                    self._handle_workspace_get(path)
                    return
                if path == "/api/agent-sessions":
                    self._send_json({"sessions": _list_agent_sessions(state)})
                    return
                if path == "/api/agent/settings":
                    self._send_json(_agent_settings_payload(state))
                    return
                if path == "/api/agent/runtime/status":
                    self._send_json(state.agent_adapter.status())
                    return
                if path.startswith("/api/agent-sessions/"):
                    self._handle_agent_session_get(path)
                    return
                if path == "/api/runtime/health":
                    self._send_json(_runtime_health())
                    return
                if path == "/api/code-server/status":
                    self._send_json(state.code_server_status())
                    return
                if path == "/api/catalog":
                    self._send_json(_catalog_payload(state))
                    return
                if path == "/api/environments":
                    self._send_json({"environments": _catalog_payload(state)["environments"]})
                    return
                if path.startswith("/api/environments/"):
                    self._send_json(_catalog_detail(state, "environment", path.split("/", 3)[3]))
                    return
                if path == "/api/methods":
                    self._send_json({"methods": _catalog_payload(state)["methods"]})
                    return
                if path.startswith("/api/methods/"):
                    self._send_json(_catalog_detail(state, "method", path.split("/", 3)[3]))
                    return
                if path == "/api/compatibility":
                    self._send_json(_compatibility_payload(state))
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
                        environment_id=validation.get("environment_id"),
                    )
                    self._send_json({"job": job.to_dict()}, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/studies/workspace":
                    payload = self._read_json_body()
                    self._send_json({"workspace": _open_study_workspace(state, payload)}, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/workspaces":
                    payload = self._read_json_body()
                    self._send_json({"workspace": _create_ui_workspace(state, payload)}, status=HTTPStatus.CREATED)
                    return
                if parsed.path.startswith("/api/workspaces/"):
                    self._handle_workspace_post(parsed.path)
                    return
                if parsed.path == "/api/agent-sessions":
                    payload = self._read_json_body()
                    self._send_json({"session": _create_agent_session(state, payload)}, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/agent/settings":
                    payload = self._read_json_body()
                    self._send_json(_update_agent_settings(state, payload))
                    return
                if parsed.path.startswith("/api/agent-sessions/"):
                    self._handle_agent_session_post(parsed.path)
                    return
                if parsed.path.startswith("/api/catalog/"):
                    self._handle_catalog_workspace_post(parsed.path)
                    return
                if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/open-workspace"):
                    self._handle_run_workspace_post(parsed.path)
                    return
                if parsed.path == "/api/code-server/start":
                    payload = self._read_json_body()
                    folder = _optional_user_path(payload.get("folder"), state.cwd)
                    self._send_json(state.start_code_server(folder), status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/code-server/open":
                    payload = self._read_json_body()
                    folder = _optional_user_path(payload.get("folder"), state.cwd)
                    self._send_json(state.code_server_open_url(folder))
                    return
                if parsed.path == "/api/code-server/stop":
                    self._send_json(state.stop_code_server())
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/stop"):
                    job_id = parsed.path.split("/")[3]
                    self._send_json({"job": state.stop_job(job_id)})
                    return
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            except KeyError as exc:
                self._send_json({"error": f"Unknown id: {exc.args[0]}"}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json({"error": str(exc), "type": type(exc).__name__}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_workspace_get(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"error": "Missing workspace id"}, status=HTTPStatus.BAD_REQUEST)
                return
            workspace = _workspace_by_id(state, parts[3])
            if not workspace:
                self._send_json({"error": "Workspace not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"workspace": workspace})

        def _handle_workspace_post(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"error": "Missing workspace id"}, status=HTTPStatus.BAD_REQUEST)
                return
            workspace_id = parts[3]
            if len(parts) == 5 and parts[4] == "discover-configs":
                self._send_json(_discover_workspace_configs(state, workspace_id))
                return
            if len(parts) == 5 and parts[4] == "open-code":
                workspace = _require_ui_workspace(state, workspace_id)
                self._send_json(state.code_server_open_url(Path(workspace["root"])))
                return
            if len(parts) == 5 and parts[4] == "open-code-window":
                workspace = _require_ui_workspace(state, workspace_id)
                self._send_json(state.code_server_open_url(Path(workspace["root"])))
                return
            if len(parts) == 5 and parts[4] == "detach":
                payload = self._read_json_body()
                self._send_json({"workspace": _detach_workspace(state, workspace_id, str(payload.get("session_id", "")))})
                return
            if len(parts) == 5 and parts[4] == "registrations":
                payload = self._read_json_body()
                self._send_json(_create_registration_manifest(state, workspace_id, payload), status=HTTPStatus.CREATED)
                return
            if len(parts) == 7 and parts[4] == "registrations" and parts[6] == "validate":
                self._send_json(_validate_registration_manifest(state, workspace_id, parts[5]))
                return
            if len(parts) == 7 and parts[4] == "registrations" and parts[6] == "apply":
                self._send_json(_apply_registration_manifest(state, workspace_id, parts[5]))
                return
            self._send_json({"error": "Unknown workspace action"}, status=HTTPStatus.NOT_FOUND)

        def _handle_agent_session_get(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"error": "Missing agent session id"}, status=HTTPStatus.BAD_REQUEST)
                return
            session_id = parts[3]
            session = _agent_session_by_id(state, session_id)
            if not session:
                self._send_json({"error": "Agent session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 5 and parts[4] == "events":
                self._send_json({"events": _read_agent_events(state, session_id)})
                return
            self._send_json({"session": session})

        def _handle_agent_session_post(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 4:
                self._send_json({"error": "Missing agent session id"}, status=HTTPStatus.BAD_REQUEST)
                return
            session_id = parts[3]
            if len(parts) == 5 and parts[4] == "message":
                payload = self._read_json_body()
                self._send_json(_append_agent_message(state, session_id, payload))
                return
            if len(parts) == 5 and parts[4] == "attach-workspace":
                payload = self._read_json_body()
                workspace_id = str(payload.get("workspace_id") or "")
                self._send_json({"session": _attach_agent_workspace(state, session_id, workspace_id, select=True)})
                return
            if len(parts) == 5 and parts[4] == "detach-workspace":
                payload = self._read_json_body()
                workspace_id = str(payload.get("workspace_id") or "")
                self._send_json({"session": _detach_agent_workspace(state, session_id, workspace_id)})
                return
            if len(parts) == 5 and parts[4] == "select-workspace":
                payload = self._read_json_body()
                self._send_json({"session": _select_agent_workspace(state, session_id, str(payload.get("workspace_id") or ""))})
                return
            if len(parts) == 5 and parts[4] == "cancel":
                self._send_json({"session": _mark_agent_session_idle(state, session_id)})
                return
            self._send_json({"error": "Unknown agent session action"}, status=HTTPStatus.NOT_FOUND)

        def _handle_catalog_workspace_post(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) != 6 or parts[5] not in {"open-workspace", "edit-copy"}:
                self._send_json({"error": "Unknown catalog workspace action"}, status=HTTPStatus.NOT_FOUND)
                return
            _, _, _, kind, uid, action = parts
            if kind not in {"environment", "method", "study"}:
                self._send_json({"error": "Unknown catalog kind"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                {"workspace": _open_catalog_workspace(state, kind, uid, editable=action == "edit-copy")},
                status=HTTPStatus.CREATED,
            )

        def _handle_run_workspace_post(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) != 5:
                self._send_json({"error": "Missing run id"}, status=HTTPStatus.BAD_REQUEST)
                return
            run_dir = _decode_id(parts[3])
            if not _is_run_dir(run_dir):
                self._send_json({"error": "Run not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"workspace": _open_run_workspace(state, run_dir)}, status=HTTPStatus.CREATED)

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
            if resource == "candidates":
                self._send_json({"candidates": _read_jsonl(run_dir / "candidates.jsonl")})
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
    grouped = {config: [] for config in CATALOG_CONFIGS}
    for entry in entries:
        grouped.setdefault(entry["config"], []).append(entry)
    return {
        "roots": [str(path) for path in state.catalog_roots],
        "environments": grouped.get("environment", []),
        "methods": grouped.get("method", []),
        "studies": grouped.get("study", []),
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
            config = raw.get("config")
            if config not in CATALOG_CONFIGS or raw.get("apiVersion") != AUTHORING_API_VERSION:
                continue
            entries.append(_catalog_entry(path, raw))
    return sorted(entries, key=lambda item: (item["config"], item["label"], item["path"]))


def _catalog_entry(path: Path, raw: JsonDict) -> JsonDict:
    config = raw["config"]
    label = raw.get("name") or raw.get("id") or path.stem
    entry: JsonDict = {
        "uid": _encode_id(path),
        "id": str(raw.get("id") or raw.get("name") or path.stem),
        "label": str(label),
        "kind": config,
        "config": config,
        "path": str(path),
        "description": str(raw.get("description", "")),
        "tags": list(raw.get("tags", []) or []),
        "summary": {},
    }
    if config == "environment":
        candidate = raw.get("candidate", {})
        candidate_format = candidate.get("format")
        editable = []
        files = candidate.get("files", {}) if isinstance(candidate.get("files"), dict) else candidate
        for item in files.get("editable", []) or []:
            if isinstance(item, dict) and item.get("path"):
                editable.append(str(item["path"]))
        entry["summary"] = {
            "evaluate_type": _evaluator_mode(raw.get("evaluator", {})),
            "candidate_format": candidate_format,
            "runtime": _environment_runtime_summary(raw),
            "editable_files": editable,
            "capabilities": [
                capability.get("id")
                for capability in raw.get("capabilities", []) or []
                if isinstance(capability, dict) and capability.get("id")
            ],
            "metrics": list(raw.get("metrics", {}).get("keys", []) or []),
        }
    elif config == "method":
        accepts = raw.get("accepts", {}) if isinstance(raw.get("accepts"), dict) else {}
        requires = accepts.get("requires", {}) if isinstance(accepts.get("requires"), dict) else {}
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        settings = raw.get("settings", {}) if isinstance(raw.get("settings"), dict) else {}
        entry["summary"] = {
            "implementation_type": _entrypoint_mode(entrypoint),
            "implementation": entrypoint.get("python") or entrypoint.get("command"),
            "protocol": entrypoint.get("protocol", "batch"),
            "runtime": _method_runtime_summary(raw),
            "batch_size": settings.get("batchSize"),
            "candidate_formats": list(accepts.get("formats", []) or []),
            "required_context": list(requires.get("context", []) or []),
            "required_capabilities": list(requires.get("capabilities", []) or []),
        }
    elif config == "study":
        environment_ref = raw.get("environmentConfig")
        method_ref = raw.get("methodConfig")
        entry["yaml"] = yaml.safe_dump(raw, sort_keys=False)
        entry["summary"] = {
            "environment": environment_ref,
            "environmentPath": str(_resolve_config_path(environment_ref, path)) if environment_ref else "",
            "method": method_ref,
            "methodPath": str(_resolve_config_path(method_ref, path)) if method_ref else "",
            "objective": raw.get("objective", {}),
            "budget": raw.get("budget", {}),
            "execution": raw.get("execution", {}),
            "evidence": raw.get("evidence", {}),
            "reproducibility": raw.get("reproducibility", {}),
        }
    return entry


def _workspace_payload(state: UiState) -> JsonDict:
    return {
        "cwd": str(state.cwd),
        "catalog_roots": [str(path) for path in state.catalog_roots],
        "run_roots": [str(path) for path in state.run_roots],
        "jobs_dir": str(state.jobs_dir),
        "sessions_dir": str(state.sessions_dir),
        "workspaces_dir": str(state.workspaces_dir),
        "code_server": state.code_server_status(),
    }


def _read_ui_settings(state: UiState) -> JsonDict:
    settings = _default_ui_settings()
    raw = _read_json(state.settings_path)
    if isinstance(raw, dict):
        assistant = raw.get("assistant")
        if isinstance(assistant, dict):
            current = settings.setdefault("assistant", {})
            current.update({key: value for key, value in assistant.items() if key != "openhands"})
            openhands = assistant.get("openhands")
            if isinstance(openhands, dict):
                current.setdefault("openhands", {}).update(openhands)
    return settings


def _default_ui_settings() -> JsonDict:
    env_config = OpenHandsRuntimeConfig.from_env()
    return {
        "assistant": {
            "runtime": "openhands",
            "openhands": {
                "enabled": env_config.enabled,
                "base_url": env_config.base_url,
                "session_endpoint": env_config.session_endpoint,
                "model": env_config.model,
                "api_key": "",
            },
        }
    }


def _write_ui_settings(state: UiState, settings: JsonDict) -> None:
    state.settings_path.parent.mkdir(parents=True, exist_ok=True)
    state.settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        state.settings_path.chmod(0o600)
    except OSError:
        pass


def _openhands_config_from_settings(settings: JsonDict) -> OpenHandsRuntimeConfig:
    assistant = settings.get("assistant", {}) if isinstance(settings.get("assistant"), dict) else {}
    openhands = assistant.get("openhands", {}) if isinstance(assistant.get("openhands"), dict) else {}
    config = OpenHandsRuntimeConfig.from_mapping(openhands)
    if config.api_key:
        return config
    env_config = OpenHandsRuntimeConfig.from_env()
    return OpenHandsRuntimeConfig(
        base_url=config.base_url,
        session_endpoint=config.session_endpoint,
        model=config.model,
        api_key=env_config.api_key,
        enabled=config.enabled,
    )


def _refresh_agent_adapter(state: UiState) -> None:
    state.agent_adapter = OpenHandsAdapter(_openhands_config_from_settings(_read_ui_settings(state)))


def _agent_settings_payload(state: UiState) -> JsonDict:
    _refresh_agent_adapter(state)
    settings = _read_ui_settings(state)
    assistant = settings.get("assistant", {}) if isinstance(settings.get("assistant"), dict) else {}
    openhands = assistant.get("openhands", {}) if isinstance(assistant.get("openhands"), dict) else {}
    safe_openhands = {
        "enabled": bool(openhands.get("enabled")),
        "base_url": str(openhands.get("base_url") or ""),
        "session_endpoint": str(openhands.get("session_endpoint") or ""),
        "model": str(openhands.get("model") or ""),
        "api_key_configured": bool(openhands.get("api_key") or OpenHandsRuntimeConfig.from_env().api_key),
    }
    return {
        "settings": {
            "assistant": {
                "runtime": "openhands",
                "openhands": safe_openhands,
            }
        },
        "status": state.agent_adapter.status(),
        "settings_path": str(state.settings_path),
    }


def _update_agent_settings(state: UiState, payload: JsonDict) -> JsonDict:
    settings = _read_ui_settings(state)
    assistant = settings.setdefault("assistant", {})
    assistant["runtime"] = "openhands"
    openhands = assistant.setdefault("openhands", {})
    incoming = payload.get("openhands") if isinstance(payload.get("openhands"), dict) else payload
    openhands["enabled"] = bool(incoming.get("enabled"))
    openhands["base_url"] = str(incoming.get("base_url") or "").strip().rstrip("/")
    openhands["session_endpoint"] = str(incoming.get("session_endpoint") or "").strip()
    openhands["model"] = str(incoming.get("model") or "").strip()
    if incoming.get("clear_api_key"):
        openhands["api_key"] = ""
    elif "api_key" in incoming and str(incoming.get("api_key") or "").strip():
        openhands["api_key"] = str(incoming.get("api_key") or "").strip()
    _write_ui_settings(state, settings)
    _refresh_agent_adapter(state)
    return _agent_settings_payload(state)


def _runtime_health() -> JsonDict:
    docker = _executable_health("docker", ["docker", "--version"])
    podman = _executable_health("podman", ["podman", "--version"])
    code_server = _executable_health("code-server", ["code-server", "--version"])
    return {
        "python": {
            "ok": True,
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "docker": docker,
        "podman": podman,
        "code_server": code_server,
    }


def _code_server_executable(options: CodeServerOptions) -> Optional[str]:
    if options.executable:
        path = Path(options.executable).expanduser()
        if path.exists() and path.is_file():
            return str(path.resolve())
        resolved = shutil.which(options.executable)
        return resolved
    return shutil.which("code-server")


def _local_code_server_executable(cwd: Path) -> Path:
    standalone_root = cwd / ".optpilot-ui" / "code-server-standalone"
    direct = standalone_root / "bin" / "code-server"
    if direct.exists():
        return direct
    candidates = sorted(
        standalone_root.glob("lib/code-server-*/bin/code-server"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return candidates[0] if candidates else direct


def _code_server_reachable(url: str) -> bool:
    try:
        request = Request(url, method="HEAD")
        with urlopen(request, timeout=0.4) as response:
            server = response.headers.get("Server", "")
            return 200 <= response.status < 400 and "OptPilotUI" not in server
    except Exception:
        return False


def _port_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, int(port))) == 0


def _find_available_port(host: str, start_port: int) -> int:
    for port in range(int(start_port), int(start_port) + 100):
        if not _port_listening(host, port):
            return port
    raise OSError(f"No available port found near {start_port}.")


def _safe_code_server_folder(state: UiState, folder: Path) -> Path:
    folder = folder.resolve()
    allowed_roots = [state.cwd, *state.catalog_roots, *state.run_roots, state.sessions_dir, state.workspaces_dir]
    if any(_is_relative_to(folder, root) for root in allowed_roots):
        return folder
    raise ValueError(f"Folder is outside the OptPilot workspace: {folder}")


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


def _catalog_detail(state: UiState, expected_config: str, uid: str) -> JsonDict:
    path = _decode_id(uid)
    raw = _read_yaml(path)
    if raw.get("config") != expected_config or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{expected_config} config not found: {path}")
    entry = _catalog_entry(path, raw)
    compatibility = _compatibility_payload(state)
    if expected_config == "environment":
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
    accepts = method_raw.get("accepts", {}) if isinstance(method_raw.get("accepts"), dict) else {}
    requires = accepts.get("requires", {}) if isinstance(accepts.get("requires"), dict) else {}
    env_candidate_format = candidate.get("format")
    method_candidate_formats = list(accepts.get("formats", []) or [])
    required_context = list(requires.get("context", []) or [])
    required_capabilities = list(requires.get("capabilities", []) or [])
    env_context = _environment_context_paths(env_raw)
    env_capabilities = {
        str(item.get("id"))
        for item in env_raw.get("capabilities", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    checks = []
    checks.append(_compat_check(
        not method_candidate_formats or env_candidate_format in method_candidate_formats,
        f"candidate format {env_candidate_format!r} is supported",
        f"method accepts.formats {method_candidate_formats!r}, environment uses {env_candidate_format!r}",
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
    produces = method_raw.get("produces")
    if isinstance(produces, dict):
        try:
            mismatch = candidate_contract_mismatch(candidate, produces)
        except Exception as exc:
            mismatch = f"produces contract cannot be compared: {exc}"
        checks.append(_compat_check(
            mismatch is None,
            "produced candidate contract matches environment candidate contract",
            mismatch or "produced candidate contract does not match environment candidate contract",
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


def _environment_context_paths(environment: JsonDict) -> set:
    paths = {"candidate", "candidate.format", "evidence", "evidence.observations"}
    candidate = environment.get("candidate", {}) if isinstance(environment.get("candidate"), dict) else {}
    _add_context_paths(paths, "candidate", candidate)
    method_context = environment.get("methodContext", {}) if isinstance(environment.get("methodContext"), dict) else {}
    if method_context:
        paths.add("methodContext")
        _add_context_paths(paths, "methodContext", method_context)
    return paths


def _add_context_paths(paths: set, prefix: str, value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if child not in (None, [], {}):
                paths.add(path)
            _add_context_paths(paths, path, child)
    elif isinstance(value, list) and value:
        paths.add(prefix)


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
    aggregation = str(payload.get("aggregation") or "mean")
    secondary_metrics = payload.get("secondaryMetrics", payload.get("secondary_metrics", []))
    if not isinstance(secondary_metrics, list):
        secondary_metrics = []
    max_trials = int(payload.get("maxTrials", payload.get("max_trials", 12)) or 12)
    max_failures_raw = payload.get("maxFailures", payload.get("max_failures"))
    requested_backend = str(payload.get("backend") or "local")
    backend = "local" if requested_backend == "container" else requested_backend
    parallelism = int(payload.get("parallelism", 1) or 1)
    timeout = int(payload.get("timeoutSeconds", payload.get("timeout_seconds", 120)) or 120)
    evidence_level = str(payload.get("evidenceLevel", payload.get("evidence_level", "standard")) or "standard")
    evidence_storage = str(payload.get("evidenceStorage", payload.get("evidence_storage", "reference")) or "reference")
    draft = {
        "apiVersion": AUTHORING_API_VERSION,
        "config": "study",
        "name": name,
        "environmentConfig": str(environment_path),
        "methodConfig": str(method_path),
        "objective": {
            "metric": metric,
            "direction": direction,
            "aggregation": aggregation,
            "secondaryMetrics": [str(item) for item in secondary_metrics if item],
        },
        "budget": {"maxTrials": max_trials},
        "execution": {"backend": backend, "parallelism": parallelism, "timeoutSeconds": timeout},
        "evidence": {"level": evidence_level, "outputFileStorage": evidence_storage},
    }
    if max_failures_raw not in (None, ""):
        draft["budget"]["maxFailures"] = int(max_failures_raw)
    execution_config = _draft_execution_config({**payload, "backend": requested_backend})
    if requested_backend == "container" and execution_config:
        container = {}
        if execution_config.get("image"):
            container["image"] = execution_config["image"]
        if execution_config.get("containerExecutable"):
            container["executable"] = execution_config["containerExecutable"]
        if execution_config.get("build"):
            container["build"] = execution_config["build"]
        draft["execution"]["runtime"] = {"sandbox": "container", "container": container}
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


def _open_study_workspace(state: UiState, payload: JsonDict) -> JsonDict:
    source_path: Optional[Path] = None
    if payload.get("study_path"):
        source_path = _resolve_user_path(payload.get("study_path"), state.cwd)
        study_yaml = source_path.read_text(encoding="utf-8")
        validation = _validate_study(source_path)
        raw = _read_yaml(source_path)
        title = str(raw.get("name") or raw.get("id") or source_path.stem)
    else:
        draft = _draft_study(state, payload)
        study_yaml = str(draft.get("yaml") or "")
        validation = draft.get("validation", {})
        source_path = Path(str(draft.get("path", ""))).resolve() if draft.get("path") else None
        title = str(draft.get("draft", {}).get("name") or payload.get("name") or "Study plan")

    workspace_id = f"ws_study_{uuid.uuid4().hex[:10]}"
    root = state.workspaces_dir / workspace_id / "workspace"
    root.mkdir(parents=True, exist_ok=False)
    (root / "study.yaml").write_text(study_yaml, encoding="utf-8")
    (root / "README.md").write_text(
        f"# {title}\n\nThis workspace contains an editable OptPilot study plan. Review `study.yaml`, then launch it from Studies or register it to the catalog.\n",
        encoding="utf-8",
    )
    return _create_ui_workspace(
        state,
        {
            "id": workspace_id,
            "title": f"Study plan: {title}",
            "root": str(root),
            "source_type": "study-plan",
            "mode": "editable",
            "status": "ready" if validation.get("valid") else "review",
            "description": "Editable study plan workspace",
            "source_path": str(source_path) if source_path else "",
            "focus_paths": ["study.yaml", "README.md"],
            "registration_enabled": True,
        },
    )


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
    return result


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


def _evaluator_mode(evaluator: JsonDict) -> Optional[str]:
    for key in ("python", "command", "adapter"):
        if evaluator.get(key):
            return key
    return None


def _entrypoint_mode(entrypoint: JsonDict) -> Optional[str]:
    for key in ("python", "command"):
        if entrypoint.get(key):
            return key
    return None


def _environment_runtime_summary(raw: JsonDict) -> JsonDict:
    evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    return {
        "evaluate_type": _evaluator_mode(evaluator),
        "timeoutSeconds": evaluator.get("timeoutSeconds"),
        "has_python_path": bool(evaluator.get("pythonPath")),
        "sandbox": runtime.get("sandbox", "host"),
    }


def _method_runtime_summary(raw: JsonDict) -> JsonDict:
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    return {
        "type": runtime.get("sandbox", "host"),
        "image": (runtime.get("container", {}) or {}).get("image") if isinstance(runtime.get("container", {}), dict) else None,
        "has_build": bool((runtime.get("container", {}) or {}).get("build")) if isinstance(runtime.get("container", {}), dict) else False,
        "networkPolicy": runtime.get("network", "disabled"),
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
        "best_candidate_id": summary.get("best_candidate_id"),
        "failure_count": summary.get("failure_count", _failure_count(status_counts)),
        "objective": objective,
        "environment_id": study_spec.get("environment", {}).get("environmentId"),
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
    candidates = _read_jsonl(run_dir / "candidates.jsonl")
    return {
        "run": _run_summary(run_dir),
        "summary": _read_json(run_dir / "summary.json"),
        "study_spec": _read_json(run_dir / "study_spec.json"),
        "run_policy": _read_json(run_dir / "run_policy.json"),
        "run_lineage": _read_json(run_dir / "run_lineage.json"),
        "environment_snapshot": _read_json(run_dir / "environment_snapshot.json"),
        "observations": observations,
        "trials": trials,
        "candidates": candidates,
        "method_calls": _read_jsonl(run_dir / "method_calls.jsonl"),
        "method_events": _read_jsonl(run_dir / "method_events.jsonl"),
        "scheduler_events": _read_jsonl(run_dir / "scheduler_events.jsonl"),
        "files": _list_run_files(run_dir),
    }


def _validate_study(study_path: Path) -> JsonDict:
    try:
        validation = validate_authoring_config(study_path)
        if not validation["valid"]:
            return validation
        compiled = compile_authoring_config(study_path)
        return {
            "valid": True,
            "errors": [],
            "path": str(study_path),
            "name": compiled.get("metadata", {}).get("name"),
            "environment_id": compiled.get("environment", {}).get("environmentId"),
            "objective": compiled.get("objective", {}).get("primaryMetric", {}),
            "max_trials": compiled.get("stopping", {}).get("maxTrials"),
        }
    except Exception as exc:
        return {"valid": False, "errors": [str(exc)], "path": str(study_path)}


def _workspace_index_path(state: UiState) -> Path:
    return state.workspaces_dir / "index.json"


def _agent_session_index_path(state: UiState) -> Path:
    return state.agent_sessions_dir / "index.json"


def _agent_session_dir(state: UiState, session_id: str) -> Path:
    return state.agent_sessions_dir / session_id


def _agent_messages_path(state: UiState, session_id: str) -> Path:
    return _agent_session_dir(state, session_id) / "messages.jsonl"


def _agent_events_path(state: UiState, session_id: str) -> Path:
    return _agent_session_dir(state, session_id) / "events.jsonl"


def _read_agent_session_index(state: UiState) -> List[JsonDict]:
    path = _agent_session_index_path(state)
    if not path.exists():
        return []
    raw = _read_json(path)
    sessions = raw.get("sessions", []) if isinstance(raw, dict) else []
    if not isinstance(sessions, list):
        return []
    return [item for item in sessions if isinstance(item, dict) and item.get("id")]


def _write_agent_session_index(state: UiState, sessions: List[JsonDict]) -> None:
    path = _agent_session_index_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sessions": sorted(sessions, key=lambda item: item.get("updated_at", ""), reverse=True)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_agent_message() -> JsonDict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "role": "assistant",
        "title": "Ready",
        "content": "I can use the selected session, attached workspace roots, catalog, study plans, runs, and code editor context.",
        "created_at": _now_iso(),
    }


def _read_agent_messages(state: UiState, session_id: str) -> List[JsonDict]:
    path = _agent_messages_path(state, session_id)
    if not path.exists():
        return []
    return _read_jsonl(path)


def _read_agent_events(state: UiState, session_id: str) -> List[JsonDict]:
    path = _agent_events_path(state, session_id)
    if not path.exists():
        return []
    return _read_jsonl(path)


def _append_jsonl(path: Path, record: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _agent_session_payload(state: UiState, session: JsonDict) -> JsonDict:
    payload = dict(session)
    messages = _read_agent_messages(state, str(session["id"]))
    if not messages:
        messages = [_default_agent_message()]
        _append_jsonl(_agent_messages_path(state, str(session["id"])), messages[0])
    payload["messages"] = messages
    payload["events"] = _read_agent_events(state, str(session["id"]))[-50:]
    return payload


def _list_agent_sessions(state: UiState) -> List[JsonDict]:
    sessions = _read_agent_session_index(state)
    if not sessions:
        sessions = [_create_agent_session(state, {"title": "Main Session", "description": "General OptPilot work"})]
    known_workspaces = {workspace["id"] for workspace in _list_ui_workspaces(state)}
    changed = False
    normalized = []
    for session in sessions:
        session = dict(session)
        attached = [item for item in session.get("attached_workspace_ids", []) if item in known_workspaces]
        if attached != session.get("attached_workspace_ids", []):
            session["attached_workspace_ids"] = attached
            if session.get("selected_workspace_id") not in attached:
                session["selected_workspace_id"] = attached[0] if attached else ""
            session["updated_at"] = _now_iso()
            changed = True
        normalized.append(session)
    if changed:
        _write_agent_session_index(state, normalized)
    return [_agent_session_payload(state, session) for session in normalized]


def _agent_session_by_id(state: UiState, session_id: str) -> Optional[JsonDict]:
    for session in _list_agent_sessions(state):
        if session.get("id") == session_id:
            return session
    return None


def _require_agent_session(state: UiState, session_id: str) -> JsonDict:
    for session in _read_agent_session_index(state):
        if session.get("id") == session_id:
            return dict(session)
    raise KeyError(session_id)


def _upsert_agent_session(state: UiState, session: JsonDict) -> JsonDict:
    sessions = [item for item in _read_agent_session_index(state) if item.get("id") != session.get("id")]
    session = dict(session)
    session["updated_at"] = _now_iso()
    sessions.append(session)
    _write_agent_session_index(state, sessions)
    return _agent_session_payload(state, session)


def _create_agent_session(state: UiState, payload: JsonDict) -> JsonDict:
    session_id = str(payload.get("id") or f"as_{uuid.uuid4().hex[:10]}")
    now = _now_iso()
    attached = [str(item) for item in payload.get("attached_workspace_ids", []) or []]
    session = {
        "id": session_id,
        "title": str(payload.get("title") or "New Session"),
        "description": str(payload.get("description") or "New conversation"),
        "status": "idle",
        "created_at": now,
        "updated_at": now,
        "attached_workspace_ids": attached,
        "selected_workspace_id": str(payload.get("selected_workspace_id") or (attached[0] if attached else "")),
    }
    _agent_session_dir(state, session_id).mkdir(parents=True, exist_ok=True)
    _append_jsonl(_agent_messages_path(state, session_id), _default_agent_message())
    event = {
        "id": f"evt_{uuid.uuid4().hex[:10]}",
        "type": "session_created",
        "created_at": now,
        "payload": {"title": session["title"]},
    }
    _append_jsonl(_agent_events_path(state, session_id), event)
    for workspace_id in attached:
        if _workspace_by_id(state, workspace_id):
            _attach_workspace_to_session_record(state, workspace_id, session_id)
    return _upsert_agent_session(state, session)


def _attach_workspace_to_session_record(state: UiState, workspace_id: str, session_id: str) -> None:
    workspace = _require_ui_workspace(state, workspace_id)
    attached = list(workspace.get("attached_sessions", []) or [])
    if session_id not in attached:
        attached.append(session_id)
        workspace["attached_sessions"] = attached
        _upsert_ui_workspace(state, workspace)


def _detach_workspace_from_session_record(state: UiState, workspace_id: str, session_id: str) -> None:
    if not _workspace_by_id(state, workspace_id):
        return
    workspace = _require_ui_workspace(state, workspace_id)
    workspace["attached_sessions"] = [item for item in workspace.get("attached_sessions", []) if item != session_id]
    _upsert_ui_workspace(state, workspace)


def _attach_agent_workspace(state: UiState, session_id: str, workspace_id: str, *, select: bool = False) -> JsonDict:
    if not workspace_id:
        raise ValueError("workspace_id is required.")
    _require_ui_workspace(state, workspace_id)
    session = _require_agent_session(state, session_id)
    attached = list(session.get("attached_workspace_ids", []) or [])
    if workspace_id not in attached:
        attached.append(workspace_id)
    session["attached_workspace_ids"] = attached
    if select or not session.get("selected_workspace_id"):
        session["selected_workspace_id"] = workspace_id
    _attach_workspace_to_session_record(state, workspace_id, session_id)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "workspace_attached",
            "created_at": _now_iso(),
            "payload": {"workspace_id": workspace_id},
        },
    )
    return _upsert_agent_session(state, session)


def _detach_agent_workspace(state: UiState, session_id: str, workspace_id: str) -> JsonDict:
    if not workspace_id:
        raise ValueError("workspace_id is required.")
    session = _require_agent_session(state, session_id)
    attached = [item for item in session.get("attached_workspace_ids", []) if item != workspace_id]
    session["attached_workspace_ids"] = attached
    if session.get("selected_workspace_id") == workspace_id:
        session["selected_workspace_id"] = attached[0] if attached else ""
    _detach_workspace_from_session_record(state, workspace_id, session_id)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "workspace_detached",
            "created_at": _now_iso(),
            "payload": {"workspace_id": workspace_id},
        },
    )
    return _upsert_agent_session(state, session)


def _select_agent_workspace(state: UiState, session_id: str, workspace_id: str) -> JsonDict:
    session = _require_agent_session(state, session_id)
    if workspace_id and workspace_id not in session.get("attached_workspace_ids", []):
        _require_ui_workspace(state, workspace_id)
        session = _attach_agent_workspace(state, session_id, workspace_id, select=True)
        return session
    session["selected_workspace_id"] = workspace_id
    return _upsert_agent_session(state, session)


def _mark_agent_session_idle(state: UiState, session_id: str) -> JsonDict:
    session = _require_agent_session(state, session_id)
    session["status"] = "idle"
    return _upsert_agent_session(state, session)


def _append_agent_message(state: UiState, session_id: str, payload: JsonDict) -> JsonDict:
    session = _require_agent_session(state, session_id)
    role = str(payload.get("role") or "user")
    content = str(payload.get("content") or payload.get("message") or "")
    title = str(payload.get("title") or ("User" if role == "user" else "Assistant"))
    ui_context = payload.get("ui_context") if isinstance(payload.get("ui_context"), dict) else {}
    if not content:
        raise ValueError("Message content is required.")
    message = {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "role": role,
        "title": title,
        "content": content,
        "created_at": _now_iso(),
        "context": _agent_context_packet(state, session, ui_context),
    }
    _append_jsonl(_agent_messages_path(state, session_id), message)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "message",
            "created_at": message["created_at"],
            "payload": {"message_id": message["id"], "role": role},
        },
    )
    if role == "user":
        assistant_message = {
            "id": f"msg_{uuid.uuid4().hex[:10]}",
            "role": "assistant",
            "title": "Queued for agent",
            "content": "This message is stored with the current OptPilot context. The OpenHands runtime adapter will process it when connected.",
            "created_at": _now_iso(),
            "context": _agent_context_packet(state, session, ui_context),
        }
        _append_jsonl(_agent_messages_path(state, session_id), assistant_message)
    session["status"] = "waiting_for_agent" if role == "user" else session.get("status", "idle")
    updated = _upsert_agent_session(state, session)
    return {"session": updated, "message": message}


def _agent_context_packet(state: UiState, session: JsonDict, ui_context: Optional[JsonDict] = None) -> JsonDict:
    ui_context = ui_context or {}
    attached = []
    for workspace_id in session.get("attached_workspace_ids", []) or []:
        workspace = _workspace_by_id(state, workspace_id)
        if workspace:
            attached.append(
                {
                    "id": workspace["id"],
                    "title": workspace.get("title"),
                    "root": workspace.get("root"),
                    "mode": workspace.get("mode"),
                    "source_type": workspace.get("source_type"),
                    "registered_entries": workspace.get("registered_entries", []),
                    "focus_paths": workspace.get("focus_paths", []),
                }
            )
    selected_workspace = None
    if session.get("selected_workspace_id"):
        selected_workspace = next((item for item in attached if item["id"] == session.get("selected_workspace_id")), None)
    catalog = _catalog_payload(state)
    return state.agent_adapter.context_packet(
        session_id=str(session.get("id") or ""),
        selected_workspace=selected_workspace,
        attached_workspaces=attached,
        catalog_counts={
            "environments": len(catalog["environments"]),
            "methods": len(catalog["methods"]),
            "studies": len(catalog["studies"]),
        },
        run_count=len(_list_runs(state)),
        current_page=str(ui_context.get("current_page") or "workspace"),
        registration_menu=ui_context.get("registration_menu") if isinstance(ui_context.get("registration_menu"), dict) else None,
        selected_catalog_entry=ui_context.get("selected_catalog_entry") if isinstance(ui_context.get("selected_catalog_entry"), dict) else None,
        selected_study_plan=ui_context.get("selected_study_plan") if isinstance(ui_context.get("selected_study_plan"), dict) else None,
        selected_run=ui_context.get("selected_run") if isinstance(ui_context.get("selected_run"), dict) else None,
        code_editor=ui_context.get("code_editor") if isinstance(ui_context.get("code_editor"), dict) else None,
        visible_state={
            key: value
            for key, value in ui_context.items()
            if key not in {"registration_menu", "selected_catalog_entry", "selected_study_plan", "selected_run", "code_editor"}
        },
    )


def _read_workspace_index(state: UiState) -> List[JsonDict]:
    path = _workspace_index_path(state)
    if not path.exists():
        return []
    raw = _read_json(path)
    items = raw.get("workspaces", []) if isinstance(raw, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("id") and item.get("root")]


def _write_workspace_index(state: UiState, workspaces: List[JsonDict]) -> None:
    path = _workspace_index_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"workspaces": sorted(workspaces, key=lambda item: item.get("updated_at", ""), reverse=True)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _list_ui_workspaces(state: UiState) -> List[JsonDict]:
    workspaces = []
    changed = False
    for workspace in _read_workspace_index(state):
        root = Path(str(workspace["root"]))
        if not root.exists():
            workspace = dict(workspace)
            workspace["status"] = "missing"
            changed = True
        workspaces.append(workspace)
    if changed:
        _write_workspace_index(state, workspaces)
    return workspaces


def _workspace_by_id(state: UiState, workspace_id: str) -> Optional[JsonDict]:
    for workspace in _list_ui_workspaces(state):
        if workspace.get("id") == workspace_id:
            return workspace
    return None


def _require_ui_workspace(state: UiState, workspace_id: str) -> JsonDict:
    workspace = _workspace_by_id(state, workspace_id)
    if not workspace:
        raise KeyError(workspace_id)
    return workspace


def _upsert_ui_workspace(state: UiState, workspace: JsonDict) -> JsonDict:
    workspaces = [item for item in _read_workspace_index(state) if item.get("id") != workspace.get("id")]
    workspace = dict(workspace)
    workspace["updated_at"] = _now_iso()
    workspaces.append(workspace)
    _write_workspace_index(state, workspaces)
    return workspace


def _create_ui_workspace(state: UiState, payload: JsonDict) -> JsonDict:
    workspace_id = str(payload.get("id") or f"ws_{uuid.uuid4().hex[:10]}")
    root_value = payload.get("root")
    if root_value:
        root = _resolve_user_path(root_value, state.cwd)
        _safe_workspace_root(state, root)
    else:
        root = state.workspaces_dir / workspace_id / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    title = str(payload.get("title") or "Untitled workspace")
    if not any(root.iterdir()):
        readme = root / "README.md"
        readme.write_text(
            f"# {title}\n\nThis is a generic OptPilot workspace. Add code, configs, data, or notes here, then use Register to Catalog when it is ready.\n",
            encoding="utf-8",
        )
    workspace = {
        "id": workspace_id,
        "title": title,
        "root": str(root.resolve()),
        "source_type": str(payload.get("source_type") or "blank"),
        "mode": str(payload.get("mode") or "editable"),
        "status": str(payload.get("status") or "ready"),
        "description": str(payload.get("description") or "Generic project workspace"),
        "attached_sessions": list(payload.get("attached_sessions", []) or []),
        "registered_entries": list(payload.get("registered_entries", []) or []),
        "focus_paths": list(payload.get("focus_paths", []) or []),
        "registration_enabled": bool(payload.get("registration_enabled", True)),
        "source_path": str(payload.get("source_path") or ""),
        "created_at": _now_iso(),
    }
    return _upsert_ui_workspace(state, workspace)


def _detach_workspace(state: UiState, workspace_id: str, session_id: str) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    attached = [item for item in workspace.get("attached_sessions", []) if item != session_id]
    workspace["attached_sessions"] = attached
    return _upsert_ui_workspace(state, workspace)


def _open_catalog_workspace(state: UiState, kind: str, uid: str, *, editable: bool) -> JsonDict:
    config_path = _decode_id(uid)
    raw = _read_yaml(config_path)
    if raw.get("config") != kind or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{kind} config not found: {config_path}")
    label = str(raw.get("name") or raw.get("id") or config_path.stem)
    source_root = config_path.parent.resolve()
    if editable:
        workspace_id = f"ws_{uuid.uuid4().hex[:10]}"
        root = state.workspaces_dir / workspace_id / "workspace"
        shutil.copytree(source_root, root, ignore=_copy_ignore)
        mode = "editable"
        source_type = "catalog-copy"
        title = f"Edit {label}"
    else:
        root = source_root
        mode = "editable" if _is_user_catalog_path(state, config_path) else "read-only"
        source_type = "catalog"
        title = f"Inspect {label}" if mode == "read-only" else label
    focus_paths = _focus_paths_for_config(root, root / config_path.name if editable else config_path, raw)
    registered_entry = {
        "kind": kind,
        "id": str(raw.get("id") or raw.get("name") or config_path.stem),
        "config_path": _relative_path(config_path if not editable else root / config_path.name, root),
        "source_config_path": str(config_path),
    }
    return _create_ui_workspace(
        state,
        {
            "id": workspace_id if editable else f"ws_{slug_path(source_root)}_{kind}_{slug_path(config_path)}",
            "title": title,
            "root": str(root),
            "source_type": source_type,
            "mode": mode,
            "description": f"{kind} catalog entry",
            "source_path": str(config_path),
            "registered_entries": [registered_entry],
            "focus_paths": focus_paths,
            "registration_enabled": editable or mode != "read-only",
        },
    )


def _open_run_workspace(state: UiState, run_dir: Path) -> JsonDict:
    summary = _run_summary(run_dir, state)
    return _create_ui_workspace(
        state,
        {
            "id": f"ws_run_{slug_path(run_dir)}",
            "title": f"Run: {summary.get('name') or run_dir.name}",
            "root": str(run_dir),
            "source_type": "run",
            "mode": "analysis",
            "description": "Read-only run evidence workspace",
            "source_path": str(run_dir),
            "registration_enabled": False,
            "focus_paths": [
                item for item in ("summary.json", "observations.jsonl", "candidates.jsonl", "trials.jsonl") if (run_dir / item).exists()
            ],
        },
    )


def _discover_workspace_configs(state: UiState, workspace_id: str) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    root = Path(str(workspace["root"])).resolve()
    _safe_workspace_root(state, root)
    configs = []
    for path in _iter_yaml_files(root):
        raw = _read_yaml(path)
        config = raw.get("config")
        if config not in CATALOG_CONFIGS or raw.get("apiVersion") != AUTHORING_API_VERSION:
            continue
        validation = validate_authoring_config(path)
        configs.append(
            {
                "path": str(path),
                "relative_path": _relative_path(path, root),
                "kind": config,
                "id": str(raw.get("id") or raw.get("name") or path.stem),
                "label": str(raw.get("name") or raw.get("id") or path.stem),
                "valid": bool(validation.get("valid")),
                "validation": validation,
                "focus_paths": _focus_paths_for_config(root, path, raw),
            }
        )
    return {"workspace": workspace, "configs": sorted(configs, key=lambda item: (item["kind"], item["relative_path"]))}


def _create_registration_manifest(state: UiState, workspace_id: str, payload: JsonDict) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    if not workspace.get("registration_enabled", True):
        raise ValueError("This workspace cannot be registered to the catalog.")
    root = Path(str(workspace["root"])).resolve()
    discovered = _discover_workspace_configs(state, workspace_id)["configs"]
    requested_paths = {str(item) for item in payload.get("config_paths", []) or []}
    selected = [item for item in discovered if not requested_paths or item["relative_path"] in requested_paths or item["path"] in requested_paths]
    if not selected:
        raise ValueError("No OptPilot config files selected for registration.")
    registration_id = str(payload.get("id") or f"reg_{uuid.uuid4().hex[:10]}")
    targets = []
    for item in selected:
        config_path = Path(item["path"])
        raw = _read_yaml(config_path)
        kind = str(item["kind"])
        destination = _default_registration_destination(state, kind, item["id"])
        include = _default_registration_include(root, config_path, raw)
        targets.append(
            {
                "target_id": f"target_{uuid.uuid4().hex[:8]}",
                "kind": kind,
                "config_path": item["relative_path"],
                "catalog_id": item["id"],
                "destination": str(destination),
                "focus_paths": item["focus_paths"],
                "include": include,
                "exclude": [".git/**", ".venv/**", "runs/**", "__pycache__/**", "node_modules/**"],
                "validation": item["validation"],
            }
        )
    manifest = {
        "id": registration_id,
        "workspace_id": workspace_id,
        "status": "draft",
        "root": str(root),
        "targets": targets,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    path = _registration_manifest_path(state, workspace_id, registration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"registration": manifest}


def _validate_registration_manifest(state: UiState, workspace_id: str, registration_id: str) -> JsonDict:
    manifest = _read_registration_manifest(state, workspace_id, registration_id)
    root = Path(str(manifest["root"])).resolve()
    all_valid = True
    for target in manifest.get("targets", []):
        config_path = (root / target["config_path"]).resolve()
        validation = validate_authoring_config(config_path)
        target["validation"] = validation
        if not validation.get("valid"):
            all_valid = False
    manifest["status"] = "validated" if all_valid else "invalid"
    manifest["updated_at"] = _now_iso()
    _write_registration_manifest(state, workspace_id, manifest)
    return {"registration": manifest}


def _apply_registration_manifest(state: UiState, workspace_id: str, registration_id: str) -> JsonDict:
    manifest = _validate_registration_manifest(state, workspace_id, registration_id)["registration"]
    if manifest.get("status") != "validated":
        return {"registration": manifest, "applied": False}
    root = Path(str(manifest["root"])).resolve()
    applied_entries = []
    for target in manifest.get("targets", []):
        destination = Path(str(target["destination"])).resolve()
        _require_catalog_destination(state, destination)
        destination.mkdir(parents=True, exist_ok=True)
        for pattern in target.get("include", []):
            for source in _match_workspace_pattern(root, pattern):
                relative = source.relative_to(root)
                if _excluded_by_patterns(str(relative), target.get("exclude", [])):
                    continue
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)
        applied_entries.append(
            {
                "kind": target["kind"],
                "id": target["catalog_id"],
                "config_path": str(destination / Path(target["config_path"]).name),
                "registered_at": _now_iso(),
            }
        )
    workspace = _require_ui_workspace(state, workspace_id)
    workspace["registered_entries"] = applied_entries
    _upsert_ui_workspace(state, workspace)
    manifest["status"] = "applied"
    manifest["applied_at"] = _now_iso()
    _write_registration_manifest(state, workspace_id, manifest)
    return {"registration": manifest, "workspace": workspace, "applied": True}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug_path(path: Path) -> str:
    encoded = base64.urlsafe_b64encode(str(path.resolve()).encode("utf-8")).decode("ascii").rstrip("=")
    return encoded[:24]


def _safe_workspace_root(state: UiState, root: Path) -> Path:
    root = root.resolve()
    allowed_roots = [state.cwd, *state.catalog_roots, *state.run_roots, state.sessions_dir, state.workspaces_dir]
    if any(_is_relative_to(root, allowed) for allowed in allowed_roots):
        return root
    raise PermissionError(f"Workspace root is outside allowed OptPilot paths: {root}")


def _copy_ignore(directory: str, names: List[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in EXCLUDED_SCAN_DIRS or name in {".DS_Store"}:
            ignored.add(name)
    return ignored


def _is_user_catalog_path(state: UiState, path: Path) -> bool:
    user_catalog = (state.cwd / "user_catalog").resolve()
    return _is_relative_to(path.resolve(), user_catalog)


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def _focus_paths_for_config(root: Path, config_path: Path, raw: JsonDict) -> List[str]:
    focus = [_relative_path(config_path, root)]
    config = raw.get("config")
    if config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        focus.extend(_likely_python_focus_paths(root, config_path, evaluator.get("python") or evaluator.get("adapter")))
        command = evaluator.get("command")
        if isinstance(command, list):
            focus.extend(_command_focus_paths(root, config_path, command))
        for item in raw.get("methodContext", {}).get("references", []) or []:
            if isinstance(item, dict) and item.get("path"):
                focus.append(str(item["path"]))
    elif config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        focus.extend(_likely_python_focus_paths(root, config_path, entrypoint.get("python")))
        command = entrypoint.get("command")
        if isinstance(command, list):
            focus.extend(_command_focus_paths(root, config_path, command))
    elif config == "study":
        for key in ("environmentConfig", "methodConfig"):
            if raw.get(key):
                focus.append(str(raw[key]))
    seen = set()
    result = []
    for item in focus:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _likely_python_focus_paths(root: Path, config_path: Path, import_ref: Any) -> List[str]:
    candidates = []
    if isinstance(import_ref, str) and ":" in import_ref:
        module_name = import_ref.split(":", 1)[0].split(".")[-1]
        candidates.append(f"{module_name}.py")
    candidates.extend(["evaluator.py", "method.py", "adapter.py", "main.py"])
    result = []
    for name in candidates:
        path = config_path.parent / name
        if path.exists() and path.is_file():
            result.append(_relative_path(path, root))
    return result


def _command_focus_paths(root: Path, config_path: Path, command: List[Any]) -> List[str]:
    result = []
    for item in command:
        if not isinstance(item, str) or item.startswith("{"):
            continue
        path = (config_path.parent / item).resolve()
        if path.exists() and path.is_file():
            result.append(_relative_path(path, root))
    return result


def _default_registration_destination(state: UiState, kind: str, catalog_id: str) -> Path:
    safe_id = _slug_text(catalog_id)
    if kind == "environment":
        return state.cwd / "user_catalog" / "environments" / safe_id
    if kind == "method":
        return state.cwd / "user_catalog" / "methods" / safe_id
    return state.cwd / "user_catalog" / "studies" / safe_id


def _default_registration_include(root: Path, config_path: Path, raw: JsonDict) -> List[str]:
    include = set(_focus_paths_for_config(root, config_path, raw))
    for name in ("assets", "prompts", "data", "cases"):
        if (config_path.parent / name).exists():
            include.add(_relative_path(config_path.parent / name, root) + "/**")
    include.add(_relative_path(config_path, root))
    return sorted(include)


def _registration_manifest_path(state: UiState, workspace_id: str, registration_id: str) -> Path:
    return state.workspaces_dir / workspace_id / "registrations" / f"{registration_id}.json"


def _read_registration_manifest(state: UiState, workspace_id: str, registration_id: str) -> JsonDict:
    path = _registration_manifest_path(state, workspace_id, registration_id)
    if not path.exists():
        raise FileNotFoundError(f"Registration not found: {registration_id}")
    return _read_json(path)


def _write_registration_manifest(state: UiState, workspace_id: str, manifest: JsonDict) -> None:
    registration_id = str(manifest.get("id") or "")
    if not registration_id:
        raise ValueError("Registration id is required.")
    path = _registration_manifest_path(state, workspace_id, registration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _require_catalog_destination(state: UiState, destination: Path) -> None:
    user_catalog = (state.cwd / "user_catalog").resolve()
    if not _is_relative_to(destination.resolve(), user_catalog):
        raise PermissionError("Registration can only write into user_catalog.")


def _match_workspace_pattern(root: Path, pattern: str) -> List[Path]:
    root = root.resolve()
    has_glob = any(char in pattern for char in "*?[]")
    path = (root / pattern).resolve()
    if not has_glob:
        if path.is_file():
            return [path]
        if path.is_dir():
            return [item for item in path.rglob("*") if item.is_file()]
        return []
    return [item.resolve() for item in root.glob(pattern) if item.is_file()]


def _excluded_by_patterns(relative: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _slug_text(value: str) -> str:
    text = "".join(char.lower() if char.isalnum() else "-" for char in str(value)).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    return text or "component"


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
