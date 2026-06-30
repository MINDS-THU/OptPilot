"""Lightweight stdlib web UI server for OptPilot."""

from __future__ import annotations

import argparse
import base64
import calendar
import difflib
import fnmatch
import hashlib
import importlib.util
import json
import mimetypes
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
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
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from optpilot.container_utils import network_args
from optpilot.config import (
    AUTHORING_API_VERSION,
    candidate_contract_mismatch,
    compile_authoring_config,
    validate_authoring_config,
)
from optpilot.registry import BUILTIN_COMPONENTS
from optpilot.run_sources import _choose_source_root
from optpilot.schema_validation import validate_public_config_schema
from optpilot.setup import minimal_host_env, setup_commands_for_step, setup_cwd

from ..agent import OpenHandsAdapter, OpenHandsRuntimeConfig


JsonDict = Dict[str, Any]

RUN_SENTINEL_FILES = {
    "study_spec.json",
    "observations.jsonl",
    "trials.jsonl",
    "candidates.jsonl",
}

# Study YAMLs are indexed for the Studies page, but only environments and
# methods are reusable catalog configs that can be registered through Studio.
INDEXED_CONFIGS = {"environment", "method", "study"}
REGISTERABLE_CONFIGS = {"environment", "method"}
CATALOG_DIR_NAME = "catalog"
EXAMPLE_PACKAGE_NAME = "example_package"
LOCAL_PACKAGE_NAME = "local_package"
CATALOG_PACKAGE_DIRS = {"environments", "methods", "resources", "studies"}
EXCLUDED_SCAN_DIRS = {
    ".git",
    ".mypy_cache",
    ".optpilot",
    ".optpilot-ui",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "resource",
    "runs",
}

DERIVED_WORKSPACE_FIELDS = {
    "delete_action",
    "delete_label",
    "managed_by_studio",
    "runtime",
}

DEFAULT_WORKSPACE_RUNTIME_IMAGE = "optpilot/workspace-dev:latest"
DEFAULT_WORKSPACE_RUNTIME_BASE_IMAGE = "ghcr.io/coder/code-server:latest"
READ_ONLY_WORKSPACE_PRUNE_GRACE_SECONDS = 30
CODE_SERVER_DEFAULT_USER_SETTINGS: JsonDict = {
    "chat.agent.enabled": False,
    "chat.commandCenter.enabled": False,
    "chat.disableAIFeatures": True,
    "extensions.ignoreRecommendations": True,
    "git.openRepositoryInParentFolders": "never",
    "telemetry.telemetryLevel": "off",
    "terminal.integrated.defaultLocation": "view",
    "update.mode": "none",
    "window.commandCenter": False,
    "window.menuBarVisibility": "classic",
    "workbench.activityBar.location": "hidden",
    "workbench.panel.defaultLocation": "bottom",
    "workbench.panel.opensMaximized": "never",
    "workbench.startupEditor": "welcomePage",
    "workbench.statusBar.visible": False,
    "workbench.tips.enabled": False,
    "workbench.welcomePage.walkthroughs.openOnInstall": False,
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
class UiLaunchJob:
    launch_id: str
    kind: str
    uid: str
    label: str
    port: int
    status: str = "queued"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    steps: List[JsonDict] = field(default_factory=list)
    log_paths: JsonDict = field(default_factory=dict)
    result: JsonDict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "launch_id": self.launch_id,
            "kind": self.kind,
            "uid": self.uid,
            "label": self.label,
            "port": self.port,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "steps": list(self.steps),
            "result": dict(self.result),
            "error": self.error,
        }
        logs = _launch_log_tail(self.log_paths)
        if logs:
            payload["logs"] = logs
        return payload


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


@dataclass
class WorkspacePreviewProxy:
    key: str
    host: str
    port: int
    target_base_url: str
    token: str
    allowed_ports: List[int]
    server: ThreadingHTTPServer
    thread: threading.Thread
    started_at: float = field(default_factory=time.time)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def preview_url(self) -> str:
        return f"{self.url}?__optpilot_preview_token={quote(self.token)}"

    @property
    def running(self) -> bool:
        return self.thread.is_alive()


@dataclass
class WorkspaceRuntimeOptions:
    executable: Optional[str] = None
    image: str = DEFAULT_WORKSPACE_RUNTIME_IMAGE
    base_image: str = DEFAULT_WORKSPACE_RUNTIME_BASE_IMAGE
    build_image: bool = True
    dockerfile: Optional[str] = None
    host: str = "127.0.0.1"
    port_start: int = 18766
    container_port: int = 8766
    auth: str = "none"
    password: Optional[str] = None
    network: str = "bridge"
    idle_timeout_seconds: int = 3600
    image_pull_timeout_seconds: int = 600
    image_build_timeout_seconds: int = 1200
    start_timeout_seconds: int = 90
    image_allowlist_patterns: List[str] = field(default_factory=list)
    cpu_limit: str = "2"
    memory_limit: str = "4g"
    pids_limit: int = 1024
    no_new_privileges: bool = True

    @classmethod
    def from_env(cls) -> "WorkspaceRuntimeOptions":
        image = os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_IMAGE") or cls.image
        return cls(
            executable=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_EXECUTABLE") or os.environ.get("OPTPILOT_CONTAINER_EXECUTABLE"),
            image=image,
            base_image=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_BASE_IMAGE") or cls.base_image,
            build_image=_ui_env_flag("OPTPILOT_WORKSPACE_RUNTIME_BUILD", image == DEFAULT_WORKSPACE_RUNTIME_IMAGE),
            dockerfile=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_DOCKERFILE") or None,
            host=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_HOST") or cls.host,
            port_start=_int_env("OPTPILOT_WORKSPACE_RUNTIME_PORT_START", cls.port_start),
            container_port=_int_env("OPTPILOT_WORKSPACE_RUNTIME_CONTAINER_PORT", cls.container_port),
            auth=os.environ.get("OPTPILOT_WORKSPACE_CODE_SERVER_AUTH") or cls.auth,
            password=os.environ.get("OPTPILOT_WORKSPACE_CODE_SERVER_PASSWORD"),
            network=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_NETWORK") or cls.network,
            idle_timeout_seconds=_int_env("OPTPILOT_WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS", cls.idle_timeout_seconds),
            image_pull_timeout_seconds=_int_env("OPTPILOT_WORKSPACE_RUNTIME_IMAGE_PULL_TIMEOUT_SECONDS", cls.image_pull_timeout_seconds),
            image_build_timeout_seconds=_int_env("OPTPILOT_WORKSPACE_RUNTIME_IMAGE_BUILD_TIMEOUT_SECONDS", cls.image_build_timeout_seconds),
            start_timeout_seconds=_int_env("OPTPILOT_WORKSPACE_RUNTIME_START_TIMEOUT_SECONDS", cls.start_timeout_seconds),
            image_allowlist_patterns=_split_env_patterns("OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST"),
            cpu_limit=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_CPUS") or cls.cpu_limit,
            memory_limit=os.environ.get("OPTPILOT_WORKSPACE_RUNTIME_MEMORY") or cls.memory_limit,
            pids_limit=_int_env("OPTPILOT_WORKSPACE_RUNTIME_PIDS_LIMIT", cls.pids_limit),
            no_new_privileges=_ui_env_flag("OPTPILOT_WORKSPACE_RUNTIME_NO_NEW_PRIVILEGES", cls.no_new_privileges),
        )


def _prepare_code_server_profile(user_data_dir: Path, extensions_dir: Path) -> None:
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
    settings.update(CODE_SERVER_DEFAULT_USER_SETTINGS)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class WorkspaceRuntimeManager:
    """Owns one long-lived container runtime per Studio workspace."""

    def __init__(self, *, studio_root: Path, runtime_root: Path, options: WorkspaceRuntimeOptions):
        self.studio_root = studio_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.options = options
        self._health_cache: tuple[float, JsonDict] = (0.0, {})
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def health(self) -> JsonDict:
        cached_at, cached = self._health_cache
        if cached and time.monotonic() - cached_at < 2:
            return dict(cached)
        executable = self._container_executable()
        configured = bool(self.options.executable)
        if not executable:
            payload = {
                "ok": False,
                "available": False,
                "configured": configured,
                "executable": self.options.executable or "",
                "engine": "",
                "image": self.options.image,
                "base_image": self.options.base_image,
                "build_image": self.options.build_image,
                "dockerfile": str(self._image_dockerfile()),
                "error": "No Docker/Podman-compatible executable found. Install Docker or Podman, or set OPTPILOT_WORKSPACE_RUNTIME_EXECUTABLE.",
            }
            self._health_cache = (time.monotonic(), payload)
            return dict(payload)
        allowlist_error = self._image_policy_error()
        if allowlist_error:
            payload = {
                "ok": False,
                "available": True,
                "configured": configured,
                "executable": executable,
                "engine": Path(executable).name,
                "image": self.options.image,
                "base_image": self.options.base_image,
                "build_image": self.options.build_image,
                "dockerfile": str(self._image_dockerfile()),
                "image_allowlist": list(self.options.image_allowlist_patterns),
                "error": allowlist_error,
            }
            self._health_cache = (time.monotonic(), payload)
            return dict(payload)
        try:
            version_completed = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=3, check=False)
            info_completed = subprocess.run([executable, "info"], capture_output=True, text=True, timeout=3, check=False)
        except Exception as exc:
            payload = {
                "ok": False,
                "available": True,
                "configured": configured,
                "executable": executable,
                "engine": Path(executable).name,
                "image": self.options.image,
                "base_image": self.options.base_image,
                "build_image": self.options.build_image,
                "dockerfile": str(self._image_dockerfile()),
                "error": str(exc),
            }
            self._health_cache = (time.monotonic(), payload)
            return dict(payload)
        text = (version_completed.stdout or version_completed.stderr).strip().splitlines()
        ok = version_completed.returncode == 0 and info_completed.returncode == 0
        error = ""
        if version_completed.returncode:
            error = version_completed.stderr.strip() or version_completed.stdout.strip()
        elif info_completed.returncode:
            error = info_completed.stderr.strip() or info_completed.stdout.strip()
        payload = {
            "ok": ok,
            "available": True,
            "configured": configured,
            "executable": executable,
            "engine": Path(executable).name,
            "image": self.options.image,
            "base_image": self.options.base_image,
            "build_image": self.options.build_image,
            "dockerfile": str(self._image_dockerfile()),
            "image_allowlist": list(self.options.image_allowlist_patterns),
            "version": text[0] if text else "",
            "error": error,
        }
        self._health_cache = (time.monotonic(), payload)
        return dict(payload)

    def status(self, workspace: JsonDict) -> JsonDict:
        workspace_id = str(workspace.get("id") or "")
        root = Path(str(workspace.get("root") or self.studio_root)).resolve()
        record = self._read_record(workspace_id)
        container_name = str(record.get("container_name") or self._container_name(workspace_id))
        health = self.health()
        executable = str(health.get("executable") or "") or self._container_executable()
        engine_available = bool(health.get("ok"))
        has_runtime_record = bool(record.get("container_name") or record.get("status"))
        running = self._container_running(container_name) if engine_available and executable and workspace_id and has_runtime_record else False
        current_image = str(record.get("image") or "")
        image_matches = (not current_image) or current_image == self.options.image
        host_port = int(record.get("host_port") or 0) or None
        code_url = self._code_server_base_url(host_port) if host_port else ""
        code_reachable = bool(code_url and _code_server_reachable(code_url))
        mount_mode = self._mount_mode(workspace)
        active_references = {
            "attached_sessions": len(list(workspace.get("attached_sessions", []) or [])),
            "code_server_open": code_reachable,
            "active_processes": 1 if running else 0,
        }
        status = "running" if running else "stopped"
        message = ""
        if not engine_available:
            status = "unavailable"
            message = str(health.get("error") or "Workspace containers require Docker or Podman.")
        elif running and not image_matches:
            status = "stale"
            message = f"Workspace container uses {current_image}; restart it to use {self.options.image}."
        return {
            "target": "per-workspace-container",
            "status": status,
            "containerized": running,
            "executor": "container" if running else "container-manager",
            "engine": health.get("engine") or Path(executable).name if executable else "",
            "engine_available": engine_available,
            "executable": executable,
            "image": self.options.image,
            "current_image": current_image or self.options.image,
            "image_matches": image_matches,
            "base_image": self.options.base_image,
            "build_image": self.options.build_image,
            "dockerfile": str(self._image_dockerfile()),
            "image_allowlist": list(self.options.image_allowlist_patterns),
            "cpu_limit": self.options.cpu_limit,
            "memory_limit": self.options.memory_limit,
            "pids_limit": self.options.pids_limit,
            "no_new_privileges": self.options.no_new_privileges,
            "container_name": container_name,
            "container_running": running,
            "code_server_running": code_reachable,
            "active": running or code_reachable or any(active_references.values()),
            "active_references": active_references,
            "mount_mode": mount_mode,
            "workspace_root": str(root),
            "runtime_dir": str(self._workspace_runtime_dir(workspace_id)),
            "host": self.options.host,
            "port": host_port,
            "url": code_url,
            "started_at": record.get("started_at"),
            "stdout_log": record.get("stdout_log") or "",
            "stderr_log": record.get("stderr_log") or "",
            "message": message,
        }

    def global_status(self, *, active_workspace: Optional[JsonDict] = None, port: Optional[int] = None) -> JsonDict:
        health = self.health()
        active_status = self.status(active_workspace) if active_workspace else {}
        host_port = int(active_status.get("port") or port or self.options.port_start)
        url = str(active_status.get("url") or self._code_server_base_url(host_port))
        running = bool(active_status.get("code_server_running")) and bool(active_status.get("image_matches", True))
        port_conflict = _port_listening(self.options.host, host_port) and not bool(active_status.get("container_running"))
        return {
            "available": bool(health.get("ok")),
            "installed": bool(health.get("ok")),
            "running": running,
            "managed": bool(active_status.get("container_running")),
            "containerized": bool(active_status.get("container_running")),
            "port_conflict": port_conflict,
            "pid": None,
            "url": url,
            "host": self.options.host,
            "port": host_port,
            "auth": self.options.auth,
            "started_at": active_status.get("started_at"),
            "workspace_root": str(active_status.get("workspace_root") or ""),
            "stdout_log": str(active_status.get("stdout_log") or ""),
            "stderr_log": str(active_status.get("stderr_log") or ""),
            "runtime": active_status or {
                "target": "per-workspace-container",
                "status": "ready" if health.get("ok") else "unavailable",
                "containerized": False,
                "engine_available": bool(health.get("ok")),
                "engine": health.get("engine") or "",
                "image": self.options.image,
                "base_image": self.options.base_image,
                "build_image": self.options.build_image,
                "dockerfile": str(self._image_dockerfile()),
                "image_allowlist": list(self.options.image_allowlist_patterns),
                "cpu_limit": self.options.cpu_limit,
                "memory_limit": self.options.memory_limit,
                "pids_limit": self.options.pids_limit,
                "no_new_privileges": self.options.no_new_privileges,
                "runtime_dir": str(self.runtime_root),
            },
            "engine": health.get("engine") or "",
            "engine_available": bool(health.get("ok")),
            "image": self.options.image,
            "base_image": self.options.base_image,
            "build_image": self.options.build_image,
            "dockerfile": str(self._image_dockerfile()),
            "image_allowlist": list(self.options.image_allowlist_patterns),
            "error": health.get("error") or "",
            "install_hint": "Install Docker or Podman, or configure OPTPILOT_WORKSPACE_RUNTIME_EXECUTABLE. The workspace image must include code-server.",
        }

    def start(self, workspace: JsonDict) -> JsonDict:
        workspace_id = str(workspace.get("id") or "")
        if not workspace_id:
            raise ValueError("Workspace id is required to start a runtime container.")
        root = Path(str(workspace.get("root") or "")).resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Workspace root not found: {root}")
        executable = self._require_container_executable()
        self._enforce_image_policy()
        container_name = self._container_name(workspace_id)
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        record = self._read_record(workspace_id)
        running = self._container_running(container_name)
        if running and record.get("image") and str(record.get("image")) != self.options.image:
            self._remove_container(container_name)
            running = False
        if not running:
            self._remove_container(container_name)
            self._ensure_image_available(executable)
            host_port = self._host_port(workspace_id)
            command = self._container_run_command(executable, workspace, container_name, host_port)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=int(self.options.start_timeout_seconds),
                check=False,
            )
            if completed.returncode != 0:
                self._write_record(
                    workspace_id,
                    {
                        "container_name": container_name,
                        "host_port": host_port,
                        "status": "failed",
                        "updated_at": _now_iso(),
                        "stderr": completed.stderr.strip(),
                    },
                )
                raise RuntimeError(f"Workspace container failed to start: {completed.stderr.strip() or completed.stdout.strip()}")
            self._write_record(
                workspace_id,
                {
                    "container_name": container_name,
                    "host_port": host_port,
                    "status": "running",
                    "started_at": time.time(),
                    "updated_at": _now_iso(),
                    "last_used_at": _now_iso(),
                    "image": self.options.image,
                    "base_image": self.options.base_image,
                    "dockerfile": str(self._image_dockerfile()),
                    "cpu_limit": self.options.cpu_limit,
                    "memory_limit": self.options.memory_limit,
                    "pids_limit": self.options.pids_limit,
                    "no_new_privileges": self.options.no_new_privileges,
                    "workspace_root": str(root),
                },
            )
        else:
            record["last_used_at"] = _now_iso()
            record["updated_at"] = _now_iso()
            self._write_record(workspace_id, record)
        return self.status(workspace)

    def start_code_server(self, workspace: JsonDict) -> JsonDict:
        normalized_network = str(self.options.network or "bridge").lower()
        if normalized_network in {"disabled", "none", "off"}:
            raise RuntimeError("Code Server requires workspace runtime network access; configure the workspace runtime network as bridge/default.")
        runtime_status = self.start(workspace)
        executable = self._require_container_executable()
        workspace_id = str(workspace["id"])
        record = self._read_record(workspace_id)
        container_name = str(record.get("container_name") or self._container_name(workspace_id))
        host_port = int(record.get("host_port") or runtime_status.get("port") or self.options.port_start)
        root = Path(str(workspace["root"])).resolve()
        log_dir = self._workspace_runtime_dir(workspace_id) / "logs"
        user_data_dir = self._workspace_runtime_dir(workspace_id) / "code-server" / "user-data"
        extensions_dir = self._workspace_runtime_dir(workspace_id) / "code-server" / "extensions"
        for path in (log_dir, user_data_dir, extensions_dir):
            path.mkdir(parents=True, exist_ok=True)
        _prepare_code_server_profile(user_data_dir, extensions_dir)
        stdout_path = log_dir / "code-server.stdout.log"
        stderr_path = log_dir / "code-server.stderr.log"
        url = self._code_server_base_url(host_port)
        if _code_server_reachable(url):
            record.update(
                {
                    "container_name": container_name,
                    "host_port": host_port,
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                    "updated_at": _now_iso(),
                    "last_used_at": _now_iso(),
                }
            )
            self._write_record(workspace_id, record)
            status = self.status(workspace)
            return {
                **self.global_status(active_workspace=workspace, port=host_port),
                "open_url": f"{url}?folder={quote(str(root), safe='')}",
                "folder": str(root),
                "workspace_id": workspace_id,
                "workspace_root": str(root),
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "user_data_dir": str(user_data_dir),
                "extensions_dir": str(extensions_dir),
                "layout_persistent": True,
                "runtime": status,
                "managed": True,
                "containerized": True,
            }
        runtime_env = self._runtime_env(workspace)
        command = (
            "set -e; "
            f"mkdir -p {shlex.quote(str(user_data_dir))} {shlex.quote(str(extensions_dir))} {shlex.quote(str(log_dir))}; "
            "command -v code-server >/dev/null 2>&1; "
            "nohup code-server "
            f"--bind-addr 0.0.0.0:{int(self.options.container_port)} "
            f"--auth {shlex.quote(str(self.options.auth or 'none'))} "
            f"--user-data-dir {shlex.quote(str(user_data_dir))} "
            f"--extensions-dir {shlex.quote(str(extensions_dir))} "
            "--disable-telemetry --disable-update-check --disable-workspace-trust "
            "--disable-getting-started-override "
            f"{shlex.quote(str(root))} "
            f"> {shlex.quote(str(stdout_path))} 2> {shlex.quote(str(stderr_path))} &"
        )
        exec_command = [executable, "exec"]
        if self.options.password:
            exec_command.extend(["-e", f"PASSWORD={self.options.password}"])
        for key, value in sorted(runtime_env.items()):
            exec_command.extend(["-e", f"{key}={value}"])
        exec_command.extend([container_name, "sh", "-lc", command])
        completed = subprocess.run(exec_command, capture_output=True, text=True, timeout=15, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"Code Server failed to start in workspace container: {completed.stderr.strip() or completed.stdout.strip()}")
        for _ in range(20):
            if _code_server_reachable(url):
                break
            time.sleep(0.25)
        record.update(
            {
                "container_name": container_name,
                "host_port": host_port,
                "code_server_started_at": time.time(),
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "updated_at": _now_iso(),
                "last_used_at": _now_iso(),
            }
        )
        self._write_record(workspace_id, record)
        status = self.status(workspace)
        open_url = f"{url}?folder={quote(str(root), safe='')}"
        return {
            **self.global_status(active_workspace=workspace, port=host_port),
            "open_url": open_url,
            "folder": str(root),
            "workspace_id": workspace_id,
            "workspace_root": str(root),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "user_data_dir": str(user_data_dir),
            "extensions_dir": str(extensions_dir),
            "layout_persistent": True,
            "runtime": status,
            "managed": True,
            "containerized": True,
        }

    def exec(
        self,
        workspace: JsonDict,
        command: List[str],
        *,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> tuple[subprocess.CompletedProcess[str], JsonDict]:
        runtime_status = self.start(workspace)
        executable = self._require_container_executable()
        container_name = str(runtime_status.get("container_name") or self._container_name(str(workspace["id"])))
        exec_command = [executable, "exec", "-w", str(cwd)]
        runtime_env = self._runtime_env(workspace)
        runtime_env.update(env or {})
        for key, value in sorted(runtime_env.items()):
            exec_command.extend(["-e", f"{key}={value}"])
        exec_command.extend([container_name, *command])
        completed = subprocess.run(
            exec_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        record = self._read_record(str(workspace["id"]))
        record["last_used_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        self._write_record(str(workspace["id"]), record)
        return completed, self.status(workspace)

    def exec_detached(
        self,
        workspace: JsonDict,
        command: List[str],
        *,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        name: str = "process",
        timeout: int = 15,
    ) -> JsonDict:
        if not command:
            raise ValueError("command is required.")
        runtime_status = self.start(workspace)
        executable = self._require_container_executable()
        workspace_id = str(workspace["id"])
        container_name = str(runtime_status.get("container_name") or self._container_name(workspace_id))
        safe_name = _slug_text(name or "process") or "process"
        log_dir = self._workspace_runtime_dir(workspace_id) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{safe_name}.stdout.log"
        stderr_path = log_dir / f"{safe_name}.stderr.log"
        exec_command = [executable, "exec", "-d", "-w", str(cwd)]
        runtime_env = self._runtime_env(workspace)
        runtime_env.update(env or {})
        runtime_env["OPTPILOT_PROCESS_STDOUT"] = str(stdout_path)
        runtime_env["OPTPILOT_PROCESS_STDERR"] = str(stderr_path)
        for key, value in sorted(runtime_env.items()):
            exec_command.extend(["-e", f"{key}={value}"])
        script = 'exec "$@" > "$OPTPILOT_PROCESS_STDOUT" 2> "$OPTPILOT_PROCESS_STDERR"'
        exec_command.extend([container_name, "sh", "-lc", script, safe_name, *command])
        completed = subprocess.run(
            exec_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Workspace command failed to start: {completed.stderr.strip() or completed.stdout.strip()}")
        record = self._read_record(workspace_id)
        launches = [item for item in record.get("launched_processes", []) or [] if isinstance(item, dict)]
        launch = {
            "name": safe_name,
            "command": list(command),
            "cwd": str(cwd),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "started_at": _now_iso(),
        }
        launches.append(launch)
        record["launched_processes"] = launches[-20:]
        record["last_used_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        self._write_record(workspace_id, record)
        return {
            **launch,
            "runtime": self.status(workspace),
            "returncode": completed.returncode,
        }

    def garbage_collect(self, workspaces: Iterable[JsonDict], *, active_workspace_id: str = "") -> JsonDict:
        now = time.time()
        stopped: List[JsonDict] = []
        skipped: List[JsonDict] = []
        for workspace in workspaces:
            workspace_id = str(workspace.get("id") or "")
            if not workspace_id:
                continue
            record = self._read_record(workspace_id)
            container_name = str(record.get("container_name") or self._container_name(workspace_id))
            if not record or not self._container_running(container_name):
                continue
            if self._workspace_has_active_reference(workspace, record, active_workspace_id=active_workspace_id):
                skipped.append({"workspace_id": workspace_id, "reason": "active_reference"})
                continue
            idle_for = now - _parse_time_or_iso(record.get("last_used_at") or record.get("updated_at") or record.get("started_at") or now)
            if idle_for < int(self.options.idle_timeout_seconds):
                skipped.append({"workspace_id": workspace_id, "reason": "idle_timeout", "idle_for_seconds": int(idle_for)})
                continue
            self._remove_container(container_name)
            record.update(
                {
                    "status": "stopped",
                    "stopped_at": _now_iso(),
                    "updated_at": _now_iso(),
                    "idle_stopped": True,
                    "idle_for_seconds": int(idle_for),
                }
            )
            self._write_record(workspace_id, record)
            stopped.append({"workspace_id": workspace_id, "container_name": container_name, "idle_for_seconds": int(idle_for)})
        return {
            "stopped": stopped,
            "skipped": skipped,
            "idle_timeout_seconds": int(self.options.idle_timeout_seconds),
        }

    def stop(self, workspace: JsonDict) -> JsonDict:
        workspace_id = str(workspace.get("id") or "")
        if not workspace_id:
            raise ValueError("Workspace id is required to stop a runtime container.")
        container_name = self._container_name(workspace_id)
        self._remove_container(container_name)
        record = self._read_record(workspace_id)
        record.update({"status": "stopped", "updated_at": _now_iso()})
        self._write_record(workspace_id, record)
        return self.status(workspace)

    def delete(self, workspace_id: str) -> bool:
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        container_name = self._container_name(workspace_id)
        self._remove_container(container_name)
        existed = runtime_dir.exists()
        if existed:
            shutil.rmtree(runtime_dir, ignore_errors=True)
        return existed

    def _container_run_command(self, executable: str, workspace: JsonDict, container_name: str, host_port: int) -> List[str]:
        workspace_id = str(workspace["id"])
        root = Path(str(workspace["root"])).resolve()
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        self._ensure_runtime_dirs(workspace_id)
        command = [
            executable,
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            "optpilot.runtime=workspace",
            "--label",
            f"optpilot.workspace_id={workspace_id}",
            "--entrypoint",
            "sh",
        ]
        command.extend(network_args(self.options.network))
        normalized_network = str(self.options.network or "bridge").lower()
        if normalized_network not in {"disabled", "none", "off", "host"}:
            command.extend(["-p", f"{self.options.host}:{int(host_port)}:{int(self.options.container_port)}"])
        if self.options.cpu_limit:
            command.extend(["--cpus", str(self.options.cpu_limit)])
        if self.options.memory_limit:
            command.extend(["--memory", str(self.options.memory_limit)])
        if int(self.options.pids_limit or 0) > 0:
            command.extend(["--pids-limit", str(int(self.options.pids_limit))])
        if self.options.no_new_privileges:
            command.extend(["--security-opt", "no-new-privileges"])
        command.extend(
            [
                "-v",
                f"{root}:{root}:{self._mount_mode(workspace)}",
                "-v",
                f"{runtime_dir}:{runtime_dir}:rw",
                self.options.image,
                "-lc",
                "trap 'exit 0' TERM INT; while true; do sleep 3600; done",
            ]
        )
        return command

    def _host_port(self, workspace_id: str) -> int:
        record = self._read_record(workspace_id)
        existing = int(record.get("host_port") or 0)
        reserved = self._reserved_host_ports(exclude_workspace_id=workspace_id)
        if existing and existing not in reserved and not _port_listening(self.options.host, existing):
            return existing
        start = max(int(self.options.port_start), int(existing or 0) + 1)
        for port in range(start, start + 200):
            if port in reserved:
                continue
            if not _port_listening(self.options.host, port):
                return port
        raise OSError(f"No available workspace runtime port found near {start}.")

    def _reserved_host_ports(self, *, exclude_workspace_id: str = "") -> set[int]:
        reserved: set[int] = set()
        if not self.runtime_root.exists():
            return reserved
        for path in self.runtime_root.glob("*/runtime.json"):
            workspace_id = path.parent.name
            if exclude_workspace_id and workspace_id == exclude_workspace_id:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                port = int(payload.get("host_port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port:
                reserved.add(port)
        return reserved

    def _ensure_image_available(self, executable: str) -> None:
        self._enforce_image_policy()
        inspect = subprocess.run(
            [executable, "image", "inspect", self.options.image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if inspect.returncode == 0:
            return
        if self._should_build_image():
            dockerfile = self._image_dockerfile()
            if not dockerfile.exists():
                raise RuntimeError(f"Workspace runtime Dockerfile is missing: {dockerfile}")
            build = subprocess.run(
                [
                    executable,
                    "build",
                    "--build-arg",
                    f"BASE_IMAGE={self.options.base_image}",
                    "-t",
                    self.options.image,
                    "-f",
                    str(dockerfile),
                    str(dockerfile.parent),
                ],
                capture_output=True,
                text=True,
                timeout=int(self.options.image_build_timeout_seconds),
                check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"Workspace runtime image build failed: {build.stderr.strip() or build.stdout.strip()}")
            return
        pull = subprocess.run(
            [executable, "pull", self.options.image],
            capture_output=True,
            text=True,
            timeout=int(self.options.image_pull_timeout_seconds),
            check=False,
        )
        if pull.returncode != 0:
            raise RuntimeError(f"Workspace runtime image pull failed: {pull.stderr.strip() or pull.stdout.strip()}")

    def _should_build_image(self) -> bool:
        return bool(self.options.build_image) and self.options.image == DEFAULT_WORKSPACE_RUNTIME_IMAGE

    def _image_policy_error(self) -> str:
        patterns = list(self.options.image_allowlist_patterns or [])
        if not patterns:
            return ""
        images = [self.options.image]
        if self._should_build_image():
            images.append(self.options.base_image)
        for image in images:
            if not any(fnmatch.fnmatchcase(image, pattern) for pattern in patterns):
                return f"Workspace runtime image is not allowed by OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST: {image}"
        return ""

    def _enforce_image_policy(self) -> None:
        error = self._image_policy_error()
        if error:
            raise RuntimeError(error)

    def _workspace_has_active_reference(self, workspace: JsonDict, record: JsonDict, *, active_workspace_id: str) -> bool:
        workspace_id = str(workspace.get("id") or "")
        if active_workspace_id and workspace_id == active_workspace_id:
            return True
        if workspace.get("attached_sessions"):
            return True
        host_port = int(record.get("host_port") or 0)
        if host_port and _code_server_reachable(self._code_server_base_url(host_port)):
            return True
        return False

    def _image_dockerfile(self) -> Path:
        if self.options.dockerfile:
            return Path(self.options.dockerfile).expanduser().resolve()
        return Path(__file__).resolve().parent / "workspace_runtime" / "Dockerfile"

    def _ensure_runtime_dirs(self, workspace_id: str) -> None:
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        for child in (
            runtime_dir / "home",
            runtime_dir / "cache" / "pip",
            runtime_dir / "cache" / "uv",
            runtime_dir / "cache" / "npm",
            runtime_dir / "code-server" / "user-data",
            runtime_dir / "code-server" / "extensions",
            runtime_dir / "logs",
        ):
            child.mkdir(parents=True, exist_ok=True)

    def _runtime_env(self, workspace: JsonDict) -> Dict[str, str]:
        workspace_id = str(workspace.get("id") or "")
        runtime_dir = self._workspace_runtime_dir(workspace_id)
        self._ensure_runtime_dirs(workspace_id)
        home = runtime_dir / "home"
        return {
            "HOME": str(home),
            "PIP_CACHE_DIR": str(runtime_dir / "cache" / "pip"),
            "UV_CACHE_DIR": str(runtime_dir / "cache" / "uv"),
            "NPM_CONFIG_CACHE": str(runtime_dir / "cache" / "npm"),
            "OPTPILOT_WORKSPACE_ROOT": str(Path(str(workspace.get("root") or self.studio_root)).resolve()),
            "OPTPILOT_WORKSPACE_RUNTIME_DIR": str(runtime_dir),
        }

    def _mount_mode(self, workspace: JsonDict) -> str:
        mode = str(workspace.get("mode") or "editable")
        source_type = str(workspace.get("source_type") or "workspace")
        return "ro" if mode in {"read-only", "analysis"} or source_type in {"catalog", "run"} else "rw"

    def _code_server_base_url(self, port: Optional[int]) -> str:
        return f"http://{self.options.host}:{int(port or self.options.port_start)}/"

    def _workspace_runtime_dir(self, workspace_id: str) -> Path:
        return self.runtime_root / workspace_id

    def _record_path(self, workspace_id: str) -> Path:
        return self._workspace_runtime_dir(workspace_id) / "runtime.json"

    def _read_record(self, workspace_id: str) -> JsonDict:
        path = self._record_path(workspace_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_record(self, workspace_id: str, record: JsonDict) -> None:
        path = self._record_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _container_name(self, workspace_id: str) -> str:
        suffix = "".join(ch.lower() if ch.isalnum() else "-" for ch in workspace_id).strip("-")
        if not suffix:
            suffix = uuid.uuid4().hex[:12]
        return f"optpilot-ws-{suffix[:48]}"

    def _container_executable(self) -> Optional[str]:
        if self.options.executable:
            path = Path(self.options.executable).expanduser()
            if path.exists() and path.is_file():
                return str(path.resolve())
            return shutil.which(self.options.executable)
        return shutil.which("docker") or shutil.which("podman")

    def _require_container_executable(self) -> str:
        executable = self._container_executable()
        if not executable:
            raise RuntimeError("No Docker/Podman-compatible executable found for workspace runtime.")
        return executable

    def _container_running(self, container_name: str) -> bool:
        executable = self._container_executable()
        if not executable or not container_name:
            return False
        completed = subprocess.run(
            [executable, "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return completed.returncode == 0 and completed.stdout.strip().lower() == "true"

    def _remove_container(self, container_name: str) -> None:
        executable = self._container_executable()
        if not executable or not container_name:
            return
        subprocess.run([executable, "rm", "-f", container_name], capture_output=True, text=True, timeout=15, check=False)


class UiState:
    def __init__(
        self,
        *,
        cwd: Path,
        catalog_roots: List[Path],
        run_roots: List[Path],
        code_server: Optional[CodeServerOptions] = None,
        workspace_runtime: Optional[WorkspaceRuntimeOptions] = None,
    ):
        self.cwd = cwd.resolve()
        self.catalog_roots = _expand_catalog_roots(catalog_roots) if catalog_roots else _default_catalog_roots(self.cwd)
        self.run_roots = _dedupe_paths(run_roots or _default_run_roots(self.cwd))
        self.jobs: Dict[str, UiJob] = {}
        self.interface_launches: Dict[str, UiLaunchJob] = {}
        self.jobs_dir = self.cwd / ".optpilot-ui" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.cwd / ".optpilot-ui" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir = self.cwd / ".optpilot-ui" / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = self.cwd / ".optpilot-ui" / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
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
        runtime_options = workspace_runtime or WorkspaceRuntimeOptions.from_env()
        runtime_options.host = runtime_options.host or code_server_options.host
        if runtime_options.port_start == WorkspaceRuntimeOptions.port_start and code_server_options.port != CodeServerOptions.port:
            runtime_options.port_start = code_server_options.port
        runtime_options.auth = runtime_options.auth or code_server_options.auth
        runtime_options.password = runtime_options.password or code_server_options.password
        self.workspace_runtime = WorkspaceRuntimeManager(
            studio_root=self.cwd,
            runtime_root=self.runtime_dir,
            options=runtime_options,
        )
        self.preview_proxies: Dict[str, WorkspacePreviewProxy] = {}
        self.active_code_workspace_id = ""
        self.agent_session_locks: Dict[str, threading.Lock] = {}
        self._agent_session_locks_lock = threading.Lock()
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
        command = [
            sys.executable,
            "-m",
            "optpilot",
            "run",
            str(study_path),
            "--output-root",
            str(output_root),
        ]
        env = _study_subprocess_env(self, study_path)
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                env=env,
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
        workspace = self._active_code_workspace()
        return self.workspace_runtime.global_status(active_workspace=workspace)

    def start_code_server(self, folder: Optional[Path] = None) -> JsonDict:
        workspace_root = _safe_code_server_folder(self, folder or self.cwd)
        workspace = self._ensure_code_workspace(workspace_root)
        status = self.workspace_runtime.start_code_server(workspace)
        self.active_code_workspace_id = str(workspace["id"])
        self.code_server.started_at = time.time()
        self.code_server.workspace_root = workspace_root
        self.code_server.stdout_path = Path(str(status.get("stdout_log") or "")) if status.get("stdout_log") else None
        self.code_server.stderr_path = Path(str(status.get("stderr_log") or "")) if status.get("stderr_log") else None
        return status

    def _prepare_code_server_profile(self) -> tuple[Path, Path]:
        user_data_dir = self.code_server_dir / "user-data"
        extensions_dir = self.code_server_dir / "extensions"
        _prepare_code_server_profile(user_data_dir, extensions_dir)
        return user_data_dir, extensions_dir

    def stop_code_server(self) -> JsonDict:
        self.active_code_workspace_id = ""
        self.code_server.workspace_root = None
        return self.code_server_status()

    def code_server_open_url(self, folder: Optional[Path] = None) -> JsonDict:
        return self.start_code_server(folder or self.code_server.workspace_root or self.cwd)

    def workspace_preview_open(self, folder: Optional[Path], port: int, *, extra_ports: Optional[Iterable[int]] = None) -> JsonDict:
        if port < 1 or port > 65535:
            raise ValueError("Preview port must be between 1 and 65535.")
        code_server = self.start_code_server(folder or self.code_server.workspace_root or self.cwd)
        workspace_id = str(code_server.get("workspace_id") or "")
        base_url = str(code_server.get("url") or "")
        if not base_url:
            parsed = urlparse(str(code_server.get("open_url") or ""))
            if parsed.scheme and parsed.netloc:
                base_url = f"{parsed.scheme}://{parsed.netloc}/"
        if not base_url:
            raise RuntimeError("Code Server did not return a base URL for workspace preview.")
        target_base_url = f"{base_url.rstrip('/')}/proxy/{int(port)}"
        allowed_ports = _preview_allowed_ports(int(port), extra_ports or [])
        preview_proxy = self._workspace_preview_proxy(workspace_id, int(port), target_base_url, allowed_ports=allowed_ports)
        return {
            "workspace_id": workspace_id,
            "folder": code_server.get("folder") or str(folder or self.cwd),
            "port": int(port),
            "preview_url": preview_proxy.preview_url,
            "proxy": "studio",
            "proxy_target": target_base_url + "/",
            "allowed_ports": list(preview_proxy.allowed_ports),
            "code_server": code_server,
        }

    def _workspace_preview_proxy(self, workspace_id: str, port: int, target_base_url: str, *, allowed_ports: List[int]) -> WorkspacePreviewProxy:
        key = f"{workspace_id}:{int(port)}"
        existing = self.preview_proxies.get(key)
        if existing and existing.running and existing.target_base_url == target_base_url and existing.allowed_ports == allowed_ports:
            return existing
        if existing:
            self._stop_workspace_preview_proxy(key)
        host = self.workspace_runtime.options.host or "127.0.0.1"
        start_port = max(19000, int(self.workspace_runtime.options.port_start) + 1000)
        reserved = {proxy.port for proxy in self.preview_proxies.values() if proxy.running}
        preview_port = _find_available_port(host, start_port, reserved=reserved)
        token = uuid.uuid4().hex
        handler = _preview_proxy_handler_factory(target_base_url, token=token, allowed_ports=allowed_ports)
        server = ThreadingHTTPServer((host, preview_port), handler)
        thread = threading.Thread(target=server.serve_forever, name=f"optpilot-preview-{workspace_id}-{port}", daemon=True)
        proxy = WorkspacePreviewProxy(
            key=key,
            host=host,
            port=server.server_port,
            target_base_url=target_base_url,
            token=token,
            allowed_ports=allowed_ports,
            server=server,
            thread=thread,
        )
        self.preview_proxies[key] = proxy
        thread.start()
        return proxy

    def _stop_workspace_preview_proxy(self, key: str) -> None:
        proxy = self.preview_proxies.pop(key, None)
        if not proxy:
            return
        try:
            proxy.server.shutdown()
        except Exception:
            pass
        proxy.server.server_close()

    def stop_workspace_preview_proxies(self) -> None:
        for key in list(self.preview_proxies):
            self._stop_workspace_preview_proxy(key)

    def _active_code_workspace(self) -> Optional[JsonDict]:
        if not self.active_code_workspace_id:
            return None
        return _workspace_by_id(self, self.active_code_workspace_id)

    def _ensure_code_workspace(self, workspace_root: Path) -> JsonDict:
        workspace_root = workspace_root.resolve()
        for workspace in _list_ui_workspaces(self):
            if Path(str(workspace.get("root") or "")).resolve() == workspace_root:
                return workspace
        source_type = "external"
        mode = "editable"
        if any(_is_relative_to(workspace_root, root.resolve()) for root in self.catalog_roots):
            source_type = "catalog"
            mode = "read-only"
        elif any(_is_relative_to(workspace_root, root.resolve()) for root in self.run_roots):
            source_type = "run"
            mode = "analysis"
        return _create_ui_workspace(
            self,
            {
                "id": f"ws_{slug_path(workspace_root)}",
                "title": workspace_root.name or "Workspace",
                "root": str(workspace_root),
                "source_type": source_type,
                "mode": mode,
                "description": "Workspace opened in Code Server",
                "registration_enabled": mode == "editable",
            },
        )

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
    workspace_runtime_executable: Optional[str] = None,
    workspace_runtime_image: Optional[str] = None,
    workspace_runtime_network: Optional[str] = None,
    workspace_runtime_port_start: Optional[int] = None,
    open_browser: bool = False,
) -> None:
    cwd = Path.cwd().resolve()
    runtime_options = WorkspaceRuntimeOptions.from_env()
    if workspace_runtime_executable:
        runtime_options.executable = workspace_runtime_executable
    if workspace_runtime_image:
        runtime_options.image = workspace_runtime_image
        runtime_options.build_image = workspace_runtime_image == DEFAULT_WORKSPACE_RUNTIME_IMAGE
    if workspace_runtime_network:
        runtime_options.network = workspace_runtime_network
    if workspace_runtime_port_start:
        runtime_options.port_start = workspace_runtime_port_start
    runtime_options.host = code_server_host
    runtime_options.auth = code_server_auth
    runtime_options.password = code_server_password or runtime_options.password
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
        workspace_runtime=runtime_options,
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
        state.stop_workspace_preview_proxies()
        server.server_close()


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _preview_proxy_handler_factory(target_base_url: str, *, token: str, allowed_ports: Iterable[int]):
    target_base_url = target_base_url.rstrip("/")
    code_server_base_url = target_base_url.split("/proxy/", 1)[0].rstrip("/") if "/proxy/" in target_base_url else ""
    preview_token = str(token)
    allowed_port_set = {int(port) for port in allowed_ports}
    token_param = "__optpilot_preview_token"
    token_cookie = "optpilot_preview_token"

    class WorkspacePreviewProxyHandler(BaseHTTPRequestHandler):
        server_version = "OptPilotPreviewProxy/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_HEAD(self) -> None:  # noqa: N802
            self._proxy_request(head_only=True)

        def do_POST(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy_request()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._proxy_request()

        def _proxy_request(self, *, head_only: bool = False) -> None:
            parsed = urlparse(self.path)
            token_from_query = self._query_token(parsed)
            if not self._token_is_valid(parsed):
                self._send_text(HTTPStatus.FORBIDDEN, "Workspace preview token is missing or invalid.", head_only=head_only)
                return
            upstream_url = self._upstream_url(parsed)
            if not upstream_url:
                self._send_text(HTTPStatus.BAD_REQUEST, "Invalid workspace preview proxy path.", head_only=head_only)
                return
            body = self._read_body()
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
            }
            headers["Accept-Encoding"] = "identity"
            request = Request(upstream_url, data=body, headers=headers, method=self.command)
            try:
                with urlopen(request, timeout=30) as response:
                    data = response.read()
                    self._send_proxy_response(
                        response.status,
                        response.headers,
                        data,
                        head_only=head_only,
                        set_token_cookie=token_from_query == preview_token,
                    )
            except HTTPError as exc:
                data = exc.read()
                self._send_proxy_response(
                    exc.code,
                    exc.headers,
                    data,
                    head_only=head_only,
                    set_token_cookie=token_from_query == preview_token,
                )
            except URLError as exc:
                self._send_text(
                    HTTPStatus.BAD_GATEWAY,
                    f"Workspace preview proxy could not reach {upstream_url}: {exc.reason}",
                    head_only=head_only,
                )

        def _upstream_url(self, parsed: Any) -> str:
            prefix = "/__optpilot_workspace_port/"
            if parsed.path.startswith(prefix):
                if not code_server_base_url:
                    return ""
                remainder = parsed.path.removeprefix(prefix)
                port_text, separator, tail = remainder.partition("/")
                try:
                    port = int(port_text)
                except ValueError:
                    return ""
                if port not in allowed_port_set:
                    return ""
                suffix = f"/{tail}" if separator else "/"
                query = self._proxy_query(parsed)
                return f"{code_server_base_url}/proxy/{port}{suffix}{query}"
            query = self._proxy_query(parsed)
            return f"{target_base_url}/{parsed.path.lstrip('/')}{query}"

        def _query_token(self, parsed: Any) -> str:
            values = parse_qs(parsed.query, keep_blank_values=True).get(token_param, [])
            return str(values[0]) if values else ""

        def _token_is_valid(self, parsed: Any) -> bool:
            if self._query_token(parsed) == preview_token:
                return True
            cookie_header = self.headers.get("Cookie", "")
            for chunk in cookie_header.split(";"):
                name, separator, value = chunk.strip().partition("=")
                if separator and name == token_cookie and value == preview_token:
                    return True
            return False

        def _proxy_query(self, parsed: Any) -> str:
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop(token_param, None)
            query = urlencode(params, doseq=True)
            return f"?{query}" if query else ""

        def _read_body(self) -> Optional[bytes]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return None
            return self.rfile.read(length)

        def _send_proxy_response(self, status: int, headers: Any, data: bytes, *, head_only: bool, set_token_cookie: bool) -> None:
            self.send_response(status)
            for key, value in headers.items():
                lowered = key.lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
                    continue
                self.send_header(key, value)
            if set_token_cookie:
                self.send_header("Set-Cookie", f"{token_cookie}={preview_token}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _send_text(self, status: HTTPStatus, message: str, *, head_only: bool) -> None:
            data = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return WorkspacePreviewProxyHandler


def add_ui_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind")
    parser.add_argument(
        "--catalog",
        action="append",
        default=[],
        help="Catalog root to scan. Defaults to packages under catalog/.",
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
    parser.add_argument(
        "--workspace-runtime-bin",
        default=None,
        help="Docker/Podman-compatible executable for per-workspace containers. Defaults to docker or podman on PATH.",
    )
    parser.add_argument(
        "--workspace-runtime-image",
        default=None,
        help="Container image used for workspace shells and Code Server. It must include code-server.",
    )
    parser.add_argument(
        "--workspace-runtime-network",
        default=None,
        help="Container network policy for workspace containers. Use bridge/default for embedded Code Server.",
    )
    parser.add_argument(
        "--workspace-runtime-port-start",
        type=int,
        default=None,
        help="First host port to try when publishing per-workspace Code Server.",
    )
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
        workspace_runtime_executable=args.workspace_runtime_bin,
        workspace_runtime_image=args.workspace_runtime_image,
        workspace_runtime_network=args.workspace_runtime_network,
        workspace_runtime_port_start=args.workspace_runtime_port_start,
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
                if path == "/api/agent/capabilities":
                    self._send_json(_assistant_capability_list(state))
                    return
                if path.startswith("/api/agent/capabilities/"):
                    parts = path.split("/")
                    if len(parts) >= 6:
                        self._send_json(_assistant_capability_detail(state, parts[4], unquote(parts[5])))
                    else:
                        self._send_json({"error": "Missing capability kind or id"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if path == "/api/agent/runtime/status":
                    self._send_json(state.agent_adapter.status())
                    return
                if path.startswith("/api/agent-sessions/"):
                    self._handle_agent_session_get(path)
                    return
                if path == "/api/runtime/health":
                    self._send_json(_runtime_health(state))
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
                if path == "/api/resources":
                    self._send_json({"resources": _catalog_payload(state)["resources"]})
                    return
                if path.startswith("/api/resources/"):
                    self._send_json(_catalog_detail(state, "resource", path.split("/", 3)[3]))
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
                if path.startswith("/api/interface-launches/"):
                    parts = path.split("/")
                    if len(parts) == 4:
                        try:
                            self._send_json({"launch": _interface_launch_by_id(state, parts[3])})
                        except KeyError:
                            self._send_json({"error": f"Unknown id: {parts[3]}"}, status=HTTPStatus.NOT_FOUND)
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
                if parsed.path == "/api/workspace-preview/open":
                    payload = self._read_json_body()
                    folder = _optional_user_path(payload.get("folder"), state.cwd)
                    port = int(payload.get("port") or 5173)
                    extra_ports = payload.get("extra_ports") if isinstance(payload.get("extra_ports"), list) else []
                    self._send_json(state.workspace_preview_open(folder, port, extra_ports=extra_ports), status=HTTPStatus.CREATED)
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

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/api/workspaces/"):
                    parts = parsed.path.split("/")
                    if len(parts) == 4:
                        self._send_json({"workspace": _delete_ui_workspace(state, parts[3])})
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
            if len(parts) == 5 and parts[4] == "rename":
                payload = self._read_json_body()
                self._send_json({"workspace": _rename_ui_workspace(state, workspace_id, str(payload.get("title") or ""))})
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
            if len(parts) == 5 and parts[4] == "context":
                self._send_json({"context": _agent_context_packet(state, _require_agent_session(state, session_id), {})})
                return
            if len(parts) == 5 and parts[4] == "events":
                self._send_json({"events": _read_agent_events(state, session_id)})
                return
            if len(parts) == 5 and parts[4] == "approvals":
                self._send_json({"approvals": _read_agent_approvals(state, session_id)})
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
                with _agent_session_operation_lock(state, session_id):
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
                self._send_json({"session": _cancel_agent_session(state, session_id)})
                return
            if len(parts) == 5 and parts[4] == "sync":
                self._send_json({"session": _sync_agent_session(state, session_id)})
                return
            if len(parts) == 6 and parts[4] == "tools":
                payload = self._read_json_body()
                self._send_json(_execute_agent_tool(state, session_id, parts[5], payload))
                return
            if len(parts) == 7 and parts[4] == "approvals" and parts[6] in {"approve", "reject"}:
                if parts[6] == "approve":
                    self._send_json(_approve_agent_action(state, session_id, parts[5]))
                else:
                    payload = self._read_json_body()
                    self._send_json(_reject_agent_action(state, session_id, parts[5], str(payload.get("reason") or "")))
                return
            self._send_json({"error": "Unknown agent session action"}, status=HTTPStatus.NOT_FOUND)

        def _handle_catalog_workspace_post(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) != 6 or parts[5] not in {"open-workspace", "save-copy", "edit-copy", "launch-interface", "launch-interface-job"}:
                self._send_json({"error": "Unknown catalog workspace action"}, status=HTTPStatus.NOT_FOUND)
                return
            _, _, _, kind, uid, action = parts
            if kind not in {"environment", "method", "study", "resource"}:
                self._send_json({"error": "Unknown catalog kind"}, status=HTTPStatus.BAD_REQUEST)
                return
            payload = self._read_json_body()
            config_override = payload.get("config") if isinstance(payload.get("config"), dict) else None
            if action == "launch-interface":
                self._send_json(_launch_catalog_interface(state, kind, uid, config_override=config_override), status=HTTPStatus.CREATED)
                return
            if action == "launch-interface-job":
                self._send_json(_start_catalog_interface_launch(state, kind, uid, config_override=config_override), status=HTTPStatus.ACCEPTED)
                return
            workspace = _open_catalog_workspace(
                state,
                kind,
                uid,
                editable=action in {"save-copy", "edit-copy"},
                install=action == "edit-copy",
                config_override=config_override,
            )
            session_id = str(payload.get("session_id") or "")
            if session_id:
                _attach_agent_workspace(state, session_id, str(workspace["id"]), select=True)
                workspace = _require_ui_workspace(state, str(workspace["id"]))
            self._send_json(
                {"workspace": workspace},
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
            payload = self._read_json_body()
            workspace = _open_run_workspace(state, run_dir)
            session_id = str(payload.get("session_id") or "")
            if session_id:
                _attach_agent_workspace(state, session_id, str(workspace["id"]), select=True)
                workspace = _require_ui_workspace(state, str(workspace["id"]))
            self._send_json({"workspace": workspace}, status=HTTPStatus.CREATED)

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
    _refresh_catalog_package_roots(state)
    entries = _scan_catalog(state.catalog_roots)
    grouped = {config: [] for config in INDEXED_CONFIGS}
    for entry in entries:
        grouped.setdefault(entry["config"], []).append(entry)
    return {
        "roots": [str(path) for path in state.catalog_roots],
        "environments": grouped.get("environment", []),
        "methods": grouped.get("method", []),
        "studies": grouped.get("study", []),
        "resources": _scan_catalog_resources(state),
        "builtins": {
            category: sorted(implementations)
            for category, implementations in BUILTIN_COMPONENTS.items()
        },
    }


def _scan_catalog(roots: Iterable[Path]) -> List[JsonDict]:
    entries: List[JsonDict] = []
    seen = set()
    ids_by_package: Dict[tuple[str, str, str], Path] = {}
    for root in roots:
        if not root.exists():
            continue
        package_id = _catalog_package_id(root)
        for path in _iter_yaml_files(root):
            if path in seen:
                continue
            seen.add(path)
            raw = _read_yaml(path)
            config = raw.get("config")
            if config not in INDEXED_CONFIGS or raw.get("apiVersion") != AUTHORING_API_VERSION:
                continue
            entry_id = str(raw.get("id") or raw.get("name") or path.stem)
            if config in REGISTERABLE_CONFIGS:
                _reject_duplicate_catalog_id(ids_by_package, package_id, config, entry_id, path)
            entries.append(_catalog_entry(path, raw, package_id=package_id))
    return sorted(entries, key=lambda item: (item["config"], item["label"], item["path"]))


def _catalog_entry(path: Path, raw: JsonDict, *, package_id: str = "") -> JsonDict:
    config = raw["config"]
    label = raw.get("name") or raw.get("id") or path.stem
    interface = _normalize_interface_config(raw.get("interface"))
    entry_id = str(raw.get("id") or raw.get("name") or path.stem)
    entry: JsonDict = {
        "uid": _encode_id(path),
        "id": entry_id,
        "package": package_id,
        "package_id": package_id,
        "qualified_id": _qualified_catalog_id(package_id, config, entry_id),
        "catalog_key": _qualified_catalog_id(package_id, config, entry_id),
        "label": str(label),
        "kind": config,
        "config": config,
        "path": str(path),
        "description": str(raw.get("description", "")),
        "tags": list(raw.get("tags", []) or []),
        "summary": {},
        "raw_config": deepcopy(raw),
        "yaml": yaml.safe_dump(raw, sort_keys=False),
    }
    if interface:
        entry["interface"] = interface
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
            "interface": _interface_summary(interface),
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
            "interface": _interface_summary(interface),
        }
    elif config == "study":
        environment_ref = raw.get("environmentConfig")
        method_ref = raw.get("methodConfig")
        entry["summary"] = {
            "name": raw.get("name"),
            "description": raw.get("description"),
            "tags": list(raw.get("tags", []) or []),
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


def _scan_catalog_resources(state: UiState) -> List[JsonDict]:
    resources: List[JsonDict] = []
    seen: set[Path] = set()
    ids_by_package: Dict[tuple[str, str, str], Path] = {}
    for catalog_root in state.catalog_roots:
        package_id = _catalog_package_id(catalog_root)
        resources_root = catalog_root / "resources"
        if not resources_root.exists() or not resources_root.is_dir():
            continue
        for resource_dir in sorted(item for item in resources_root.iterdir() if item.is_dir()):
            resolved = resource_dir.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            entry = _resource_catalog_entry(resolved, package_id=package_id)
            _reject_duplicate_catalog_id(ids_by_package, package_id, "resource", entry["id"], resolved)
            resources.append(entry)
    return sorted(resources, key=lambda item: (item["label"], item["path"]))


def _resource_catalog_entry(path: Path, *, package_id: str = "") -> JsonDict:
    manifest_path, manifest = _resource_manifest(path)
    readme = _first_existing_file(path, ["README.md", "readme.md", "README.txt"])
    manifest_label = str(manifest.get("name") or manifest.get("id") or "").strip()
    label = manifest_label or path.name
    description = ""
    tags: List[str] = []
    if readme:
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        label = manifest_label or _readme_title(text) or label
        description = str(manifest.get("description") or "").strip() or _readme_description(text)
        tags = _resource_tags(path, text)
    else:
        description = str(manifest.get("description") or "").strip()
        tags = ["resource"]
    if isinstance(manifest.get("tags"), list):
        tags = ["resource", *[str(tag) for tag in manifest.get("tags", []) if str(tag)]]
    tags = list(dict.fromkeys(tags))[:4]
    interface = _normalize_interface_config(manifest.get("interface"))
    resource_id = str(manifest.get("id") or _slug_text(path.name))
    public_config = _resource_public_config(
        manifest,
        resource_id=resource_id,
        label=label,
        description=description,
        tags=tags,
        interface=interface,
    )
    summary: JsonDict = {
        "readme": _relative_path(readme, path) if readme else "",
        "file_count": _count_resource_files(path),
        "editable": False,
        "interface": _interface_summary(interface),
    }
    if manifest_path:
        summary["manifest"] = _relative_path(manifest_path, path)
    entry: JsonDict = {
        "uid": _encode_id(path),
        "id": resource_id,
        "package": package_id,
        "package_id": package_id,
        "qualified_id": _qualified_catalog_id(package_id, "resource", resource_id),
        "catalog_key": _qualified_catalog_id(package_id, "resource", resource_id),
        "label": label,
        "kind": "resource",
        "config": "resource",
        "path": str(path),
        "description": description,
        "tags": tags,
        "summary": summary,
        "raw_config": deepcopy(public_config),
        "yaml": yaml.safe_dump(public_config, sort_keys=False),
    }
    if manifest_path:
        entry["config_path"] = str(manifest_path)
    if interface:
        entry["interface"] = interface
    return entry


def _resource_public_config(
    manifest: JsonDict,
    *,
    resource_id: str,
    label: str,
    description: str,
    tags: List[str],
    interface: JsonDict,
) -> JsonDict:
    if manifest.get("apiVersion") == AUTHORING_API_VERSION and manifest.get("config") == "resource":
        return dict(manifest)
    config: JsonDict = {
        "apiVersion": AUTHORING_API_VERSION,
        "config": "resource",
        "id": resource_id,
    }
    if label:
        config["name"] = label
    if description:
        config["description"] = description
    if tags:
        config["tags"] = tags
    if interface:
        config["interface"] = interface
    return config


def _catalog_package_id(root: Path) -> str:
    return root.resolve().name


def _qualified_catalog_id(package_id: str, kind: str, entry_id: str) -> str:
    package = package_id or "workspace"
    return f"{package}/{kind}/{entry_id}"


def _reject_duplicate_catalog_id(
    seen: Dict[tuple[str, str, str], Path],
    package_id: str,
    kind: str,
    entry_id: str,
    path: Path,
) -> None:
    key = (package_id, kind, entry_id)
    previous = seen.get(key)
    if previous is not None and previous.resolve() != path.resolve():
        raise ValueError(
            f"Duplicate catalog id {entry_id!r} for {kind!r} in package {package_id!r}: "
            f"{previous} and {path}"
        )
    seen[key] = path


def _resource_manifest(path: Path) -> tuple[Optional[Path], JsonDict]:
    for name in ["optpilot.resource.yaml", "optpilot-resource.yaml", ".optpilot/resource.yaml", ".optpilot/interface.yaml"]:
        manifest_path = path / name
        if not manifest_path.exists() or not manifest_path.is_file():
            continue
        raw = _read_yaml(manifest_path)
        if raw.get("apiVersion") == AUTHORING_API_VERSION and raw.get("config") == "resource":
            return manifest_path, raw
        if isinstance(raw.get("interface"), dict):
            return manifest_path, {"interface": raw.get("interface")}
    return None, {}


def _first_existing_file(root: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        path = root / name
        if path.exists() and path.is_file():
            return path
    return None


def _readme_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _readme_description(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return _cap_text(stripped, 220)
    return ""


def _resource_tags(path: Path, readme_text: str) -> List[str]:
    tags = ["resource"]
    suffixes = {item.suffix.lower() for item in path.rglob("*") if item.is_file()}
    if ".py" in suffixes:
        tags.append("python")
    if ".md" in suffixes:
        tags.append("docs")
    if any(suffix in suffixes for suffix in {".csv", ".json", ".yaml", ".yml"}):
        tags.append("data")
    if "simulation" in readme_text.lower() or "simulator" in readme_text.lower():
        tags.append("simulation")
    return tags[:4]


def _count_resource_files(path: Path) -> int:
    count = 0
    for item in path.rglob("*"):
        if item.is_file() and not any(part in EXCLUDED_SCAN_DIRS for part in item.parts):
            count += 1
            if count >= 999:
                break
    return count


def _normalize_interface_config(raw: Any) -> JsonDict:
    if not isinstance(raw, dict):
        return {}
    try:
        command = _normalize_shell_command(raw.get("command"))
    except Exception:
        return {}
    if not command:
        return {}
    try:
        port = int(raw.get("port") or 0)
    except (TypeError, ValueError):
        return {}
    if port < 1 or port > 65535:
        return {}
    extra_ports: List[int] = []
    for item in raw.get("extraPorts") or raw.get("extra_ports") or []:
        try:
            extra_port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= extra_port <= 65535 and extra_port != port:
            extra_ports.append(extra_port)
    env = {
        str(key): str(value)
        for key, value in (raw.get("env") or {}).items()
        if str(key)
    } if isinstance(raw.get("env"), dict) else {}
    env_from_host = [str(item) for item in (raw.get("envFromHost") or raw.get("env_from_host") or []) if str(item)]
    interface: JsonDict = {
        "command": command,
        "port": port,
        "cwd": str(raw.get("cwd") or "."),
        "env": env,
        "envFromHost": env_from_host,
        "extraPorts": sorted(set(extra_ports)),
        "readyPath": _normalize_interface_ready_path(raw.get("readyPath") or raw.get("ready_path") or "/"),
        "readyTimeoutSeconds": _normalize_interface_ready_timeout(
            raw.get("readyTimeoutSeconds", raw.get("ready_timeout_seconds", 90))
        ),
    }
    if isinstance(raw.get("setup"), dict):
        interface["setup"] = deepcopy(raw["setup"])
    if raw.get("label"):
        interface["label"] = str(raw.get("label"))
    if raw.get("description"):
        interface["description"] = str(raw.get("description"))
    return interface


def _interface_summary(interface: JsonDict) -> JsonDict:
    if not interface:
        return {}
    return {
        "label": interface.get("label") or "Interface",
        "description": interface.get("description") or "",
        "port": interface.get("port"),
        "cwd": interface.get("cwd") or ".",
        "command": list(interface.get("command") or []),
        "envFromHost": list(interface.get("envFromHost") or []),
        "setup": deepcopy(interface.get("setup")) if isinstance(interface.get("setup"), dict) else None,
        "extraPorts": list(interface.get("extraPorts") or []),
        "readyPath": interface.get("readyPath") or "/",
        "readyTimeoutSeconds": int(interface.get("readyTimeoutSeconds") or 0),
    }


def _normalize_interface_ready_path(raw: Any) -> str:
    path = str(raw or "/").strip() or "/"
    if "://" in path or path.startswith("//"):
        return "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _normalize_interface_ready_timeout(raw: Any) -> int:
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        timeout = 90
    return max(0, min(timeout, 600))


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
            capabilities = assistant.get("capabilities")
            if isinstance(capabilities, dict):
                current["capabilities"] = _normalize_assistant_capabilities(capabilities)
            permissions = assistant.get("permissions")
            if isinstance(permissions, dict):
                current["permissions"] = _normalize_assistant_permissions(permissions)
        environment = raw.get("environment")
        if isinstance(environment, dict):
            settings["environment"] = _normalize_environment_settings(environment)
    return settings


DEFAULT_ASSISTANT_PERMISSIONS = {
    "file_write": "attached_editable",
    "shell_run": "approval_required",
    "catalog_registration": "approval_required",
    "study_launch": "approval_required",
    "job_stop": "approval_required",
}


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
            "capabilities": {
                "skills": [],
                "mcp_servers": [],
                "custom_tools": [],
            },
            "permissions": dict(DEFAULT_ASSISTANT_PERMISSIONS),
        },
        "environment": {"variables": {}},
    }


def _normalize_environment_settings(raw: Optional[JsonDict]) -> JsonDict:
    variables: Dict[str, str] = {}
    payload = raw if isinstance(raw, dict) else {}
    source = payload.get("variables", {}) if isinstance(payload.get("variables"), dict) else {}
    for key, value in source.items():
        try:
            name = _clean_env_var_name(key)
        except ValueError:
            continue
        text = str(value or "")
        if text:
            variables[name] = text
    return {"variables": variables}


def _clean_env_var_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("Environment variable name is required.")
    if "=" in name:
        raise ValueError("Environment variable names cannot contain '='.")
    first = name[0]
    if not (first == "_" or "A" <= first <= "Z" or "a" <= first <= "z"):
        raise ValueError(f"Invalid environment variable name: {name}")
    for char in name[1:]:
        if not (char == "_" or "A" <= char <= "Z" or "a" <= char <= "z" or "0" <= char <= "9"):
            raise ValueError(f"Invalid environment variable name: {name}")
    return name


def _environment_variables_from_settings(settings: JsonDict) -> Dict[str, str]:
    environment = settings.get("environment", {}) if isinstance(settings.get("environment"), dict) else {}
    return dict(_normalize_environment_settings(environment).get("variables", {}))


def _safe_environment_settings(settings: JsonDict) -> JsonDict:
    variables = _environment_variables_from_settings(settings)
    records = [
        {"name": name, "configured": bool(value)}
        for name, value in sorted(variables.items())
    ]
    return {"variables": records, "count": len(records)}


def _resolve_declared_env_from_host(state: UiState, names: Iterable[Any]) -> tuple[Dict[str, str], List[str]]:
    settings_variables = _environment_variables_from_settings(_read_ui_settings(state))
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for raw_name in _dedupe_strings(names):
        name = _clean_env_var_name(raw_name)
        if settings_variables.get(name):
            resolved[name] = settings_variables[name]
        elif os.environ.get(name):
            resolved[name] = os.environ[name]
        else:
            missing.append(name)
    return resolved, missing


def _require_declared_env_from_host(state: UiState, names: Iterable[Any], *, action: str) -> Dict[str, str]:
    resolved, missing = _resolve_declared_env_from_host(state, names)
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            f"Missing environment variable{'s' if len(missing) != 1 else ''} for {action}: {joined}. "
            "Add them in Studio Settings or export them before launching. "
            "Only variables declared with envFromHost are injected."
        )
    return resolved


def _dedupe_strings(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _declared_env_from_host(payload: Any) -> List[str]:
    names: List[str] = []
    if isinstance(payload, dict):
        env_from_host = payload.get("envFromHost")
        if isinstance(env_from_host, list):
            names.extend(str(item) for item in env_from_host if str(item).strip())
        for value in payload.values():
            names.extend(_declared_env_from_host(value))
    elif isinstance(payload, list):
        for item in payload:
            names.extend(_declared_env_from_host(item))
    return _dedupe_strings(names)


def _study_env_requirements(study_path: Path) -> List[str]:
    compiled = compile_authoring_config(study_path)
    names: List[str] = []
    for key in ("environment", "method", "execution"):
        names.extend(_declared_env_from_host(compiled.get(key)))
    return _dedupe_strings(names)


def _setup_env_requirements(setup: Any) -> List[str]:
    if not isinstance(setup, dict):
        return []
    env_from_host = setup.get("envFromHost", [])
    return _dedupe_strings(env_from_host if isinstance(env_from_host, list) else [])


def _interface_launch_env_requirements(raw: JsonDict, interface: JsonDict) -> List[str]:
    names: List[str] = []
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    names.extend(_setup_env_requirements(runtime.get("setup")))
    names.extend(_setup_env_requirements(interface.get("setup") if isinstance(interface, dict) else {}))
    if isinstance(interface, dict) and isinstance(interface.get("envFromHost"), list):
        names.extend(str(item) for item in interface.get("envFromHost", []) if str(item).strip())
    return _dedupe_strings(names)


def _study_subprocess_env(state: UiState, study_path: Path) -> Dict[str, str]:
    declared_env = _require_declared_env_from_host(state, _study_env_requirements(study_path), action="study launch")
    env = minimal_host_env()
    pythonpath_entries = [
        str(path)
        for path in [
            _pythonpath_root_for_package("optpilot"),
            state.cwd.resolve(),
        ]
        if path
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(item for item in pythonpath_entries if item))
    env.update(declared_env)
    return env


def _pythonpath_root_for_package(package: str) -> Optional[Path]:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.origin or spec.origin in {"built-in", "namespace"}:
        return None
    path = Path(spec.origin).resolve()
    if path.name == "__init__.py":
        return path.parent.parent
    return path.parent


def _package_dir(package: str) -> Optional[Path]:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.origin or spec.origin in {"built-in", "namespace"}:
        return None
    path = Path(spec.origin).resolve()
    if path.name == "__init__.py":
        return path.parent
    return path.parent


def _normalize_assistant_permissions(raw: Optional[JsonDict]) -> JsonDict:
    permissions = dict(DEFAULT_ASSISTANT_PERMISSIONS)
    if not isinstance(raw, dict):
        return permissions
    for key in permissions:
        value = str(raw.get(key) or permissions[key]).strip()
        permissions[key] = value or permissions[key]
    return permissions


def _normalize_capability_id(value: Any, fallback: str) -> str:
    text = str(value or fallback or "").strip().lower()
    normalized = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in text)
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized or fallback


def _normalize_capability_record(raw: Any, *, kind: str, index: int) -> JsonDict:
    item = raw if isinstance(raw, dict) else {}
    fallback = f"{kind}-{index + 1}"
    name = str(item.get("name") or item.get("id") or fallback).strip() or fallback
    record: JsonDict = {
        "id": _normalize_capability_id(item.get("id") or name, fallback),
        "name": name,
        "description": str(item.get("description") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
        "kind": kind,
    }
    if kind == "skill":
        record["source"] = str(item.get("source") or item.get("path") or "").strip()
        triggers = item.get("triggers", [])
        record["triggers"] = [str(trigger).strip() for trigger in triggers if str(trigger).strip()] if isinstance(triggers, list) else []
    elif kind == "mcp_server":
        record["command"] = str(item.get("command") or "").strip()
        record["args"] = [str(arg) for arg in item.get("args", [])] if isinstance(item.get("args"), list) else []
        record["url"] = str(item.get("url") or "").strip()
        record["auth"] = str(item.get("auth") or "").strip()
        record["transport"] = str(item.get("transport") or "stdio").strip() or "stdio"
    elif kind == "custom_tool":
        record["module"] = str(item.get("module") or "").strip()
        record["factory"] = str(item.get("factory") or item.get("class") or "").strip()
        record["tool_name"] = str(item.get("tool_name") or item.get("registered_name") or record["name"]).strip()
        record["approval_required"] = bool(item.get("approval_required", True))
    return record


def _normalize_assistant_capabilities(raw: Optional[JsonDict]) -> JsonDict:
    raw = raw if isinstance(raw, dict) else {}
    skills = [
        _normalize_capability_record(item, kind="skill", index=index)
        for index, item in enumerate(raw.get("skills", []) if isinstance(raw.get("skills"), list) else [])
    ]
    mcp_servers = [
        _normalize_capability_record(item, kind="mcp_server", index=index)
        for index, item in enumerate(raw.get("mcp_servers", []) if isinstance(raw.get("mcp_servers"), list) else [])
    ]
    local_tools = [
        _normalize_capability_record(item, kind="custom_tool", index=index)
        for index, item in enumerate(raw.get("local_tools", []) if isinstance(raw.get("local_tools"), list) else [])
    ]
    custom_tool_inputs = raw.get("custom_tools", []) if isinstance(raw.get("custom_tools"), list) else []
    custom_tools = [
        _normalize_capability_record(item, kind="custom_tool", index=index)
        for index, item in enumerate(custom_tool_inputs)
    ]
    return {
        "skills": skills,
        "mcp_servers": mcp_servers,
        "mcp_filter_regex": str(raw.get("mcp_filter_regex") or raw.get("filter_tools_regex") or "").strip(),
        # `local_tools` is kept as a migration alias for older settings files.
        "custom_tools": custom_tools + local_tools,
    }


def _assistant_capabilities_from_settings(settings: JsonDict) -> JsonDict:
    assistant = settings.get("assistant", {}) if isinstance(settings.get("assistant"), dict) else {}
    return _normalize_assistant_capabilities(assistant.get("capabilities") if isinstance(assistant.get("capabilities"), dict) else {})


def _assistant_permissions_from_settings(settings: JsonDict) -> JsonDict:
    assistant = settings.get("assistant", {}) if isinstance(settings.get("assistant"), dict) else {}
    return _normalize_assistant_permissions(assistant.get("permissions") if isinstance(assistant.get("permissions"), dict) else {})


def _capability_summary(record: JsonDict) -> JsonDict:
    summary = {
        "id": record.get("id"),
        "name": record.get("name"),
        "kind": record.get("kind"),
        "description": record.get("description"),
        "enabled": bool(record.get("enabled")),
    }
    if record.get("kind") == "custom_tool":
        summary["approval_required"] = bool(record.get("approval_required", True))
    return summary


def _assistant_capability_summary(state: UiState) -> JsonDict:
    settings = _read_ui_settings(state)
    capabilities = _assistant_capabilities_from_settings(settings)
    permissions = _assistant_permissions_from_settings(settings)
    buckets = {
        "skills": capabilities["skills"],
        "mcp_servers": capabilities["mcp_servers"],
        "custom_tools": capabilities["custom_tools"],
    }
    return {
        "counts": {
            key: {"total": len(records), "enabled": sum(1 for record in records if record.get("enabled"))}
            for key, records in buckets.items()
        },
        "enabled": {
            key: [_capability_summary(record) for record in records if record.get("enabled")]
            for key, records in buckets.items()
        },
        "permissions": permissions,
    }


def _capability_bucket(kind: str) -> str:
    value = str(kind or "").strip().lower().replace("-", "_")
    return {
        "skill": "skills",
        "skills": "skills",
        "mcp": "mcp_servers",
        "mcp_server": "mcp_servers",
        "mcp_servers": "mcp_servers",
        "local": "custom_tools",
        "local_tool": "custom_tools",
        "local_tools": "custom_tools",
        "custom": "custom_tools",
        "custom_tool": "custom_tools",
        "custom_tools": "custom_tools",
    }.get(value, value)


def _assistant_capability_list(state: UiState, kind: str = "") -> JsonDict:
    settings = _read_ui_settings(state)
    capabilities = _assistant_capabilities_from_settings(settings)
    permissions = _assistant_permissions_from_settings(settings)
    bucket = _capability_bucket(kind)
    if bucket:
        records = capabilities.get(bucket, [])
        return {"capabilities": {bucket: [_capability_summary(record) for record in records]}, "permissions": permissions}
    capability_buckets = {
        "skills": capabilities.get("skills", []),
        "mcp_servers": capabilities.get("mcp_servers", []),
        "custom_tools": capabilities.get("custom_tools", []),
    }
    return {
        "capabilities": {
            key: [_capability_summary(record) for record in records]
            for key, records in capability_buckets.items()
        },
        "mcp_filter_regex": capabilities.get("mcp_filter_regex", ""),
        "permissions": permissions,
    }


def _assistant_capability_detail(state: UiState, kind: str, capability_id: str) -> JsonDict:
    bucket = _capability_bucket(kind)
    if not bucket:
        raise ValueError("Capability kind is required.")
    settings = _read_ui_settings(state)
    capabilities = _assistant_capabilities_from_settings(settings)
    for record in capabilities.get(bucket, []):
        if record.get("id") == capability_id:
            return {"capability": record, "permissions": _assistant_permissions_from_settings(settings)}
    raise KeyError(capability_id)


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
    capabilities = _assistant_capabilities_from_settings(settings)
    permissions = _assistant_permissions_from_settings(settings)
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
                "capabilities": capabilities,
                "permissions": permissions,
            },
            "environment": _safe_environment_settings(settings),
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
    if isinstance(payload.get("capabilities"), dict):
        assistant["capabilities"] = _normalize_assistant_capabilities(payload.get("capabilities"))
    if isinstance(payload.get("permissions"), dict):
        assistant["permissions"] = _normalize_assistant_permissions(payload.get("permissions"))
    if isinstance(payload.get("environment"), dict):
        environment = settings.setdefault("environment", {"variables": {}})
        variables = _environment_variables_from_settings(settings)
        incoming_environment = payload["environment"]
        for raw_name in incoming_environment.get("clear", []) or []:
            try:
                variables.pop(_clean_env_var_name(raw_name), None)
            except ValueError:
                continue
        for item in incoming_environment.get("set", []) or []:
            if not isinstance(item, dict):
                continue
            name = _clean_env_var_name(item.get("name"))
            value = str(item.get("value") or "")
            if value:
                variables[name] = value
        environment["variables"] = variables
    _write_ui_settings(state, settings)
    _refresh_agent_adapter(state)
    return _agent_settings_payload(state)


def _runtime_health(state: Optional[UiState] = None) -> JsonDict:
    docker = _executable_health("docker", ["docker", "--version"])
    podman = _executable_health("podman", ["podman", "--version"])
    code_server = _executable_health("code-server", ["code-server", "--version"])
    runtime_gc: JsonDict = {}
    if state:
        runtime_gc = state.workspace_runtime.garbage_collect(
            _workspace_records_for_runtime_gc(state),
            active_workspace_id=state.active_code_workspace_id,
        )
    workspace_runtime = state.workspace_runtime.global_status(active_workspace=state._active_code_workspace()) if state else {}
    return {
        "python": {
            "ok": True,
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "docker": docker,
        "podman": podman,
        "code_server": code_server,
        "workspace_runtime": workspace_runtime.get("runtime")
        or {
            "target": "per-workspace-container",
            "status": "unconfigured",
            "containerized": False,
            "engine_available": False,
            "runtime_root": "",
            "message": "Workspace runtime status is available after Studio starts.",
        },
        "workspace_runtime_gc": runtime_gc,
    }


def _workspace_records_for_runtime_gc(state: UiState) -> List[JsonDict]:
    attachment_map = _workspace_attachment_map(state)
    records = []
    for workspace in _read_workspace_index(state):
        item = dict(workspace)
        item["attached_sessions"] = attachment_map.get(str(item.get("id") or ""), [])
        records.append(item)
    return records


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


def _find_available_port(host: str, start_port: int, *, reserved: Optional[set[int]] = None) -> int:
    reserved = reserved or set()
    for port in range(int(start_port), int(start_port) + 100):
        if port in reserved:
            continue
        if not _port_listening(host, port):
            return port
    raise OSError(f"No available port found near {start_port}.")


def _preview_allowed_ports(port: int, extra_ports: Iterable[int]) -> List[int]:
    ports = {int(port)}
    for raw in extra_ports:
        try:
            candidate = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Preview extra port must be an integer: {raw!r}") from None
        if candidate < 1 or candidate > 65535:
            raise ValueError("Preview ports must be between 1 and 65535.")
        ports.add(candidate)
    return sorted(ports)


def _preview_ready_url(proxy_target: str, ready_path: str) -> str:
    path = _normalize_interface_ready_path(ready_path)
    return f"{str(proxy_target).rstrip('/')}{path}"


def _wait_for_preview_ready(proxy_target: str, ready_path: str, timeout_seconds: int) -> JsonDict:
    timeout = max(0, int(timeout_seconds or 0))
    ready_url = _preview_ready_url(proxy_target, ready_path)
    if timeout <= 0:
        return {
            "ready": False,
            "skipped": True,
            "url": ready_url,
            "timeoutSeconds": 0,
        }
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() <= deadline:
        remaining = max(0.1, deadline - time.monotonic())
        request = Request(ready_url, method="GET", headers={"Accept": "text/html,application/json,*/*"})
        try:
            with urlopen(request, timeout=min(1.5, remaining)) as response:
                if response.status < 500:
                    return {
                        "ready": True,
                        "status": response.status,
                        "url": ready_url,
                        "timeoutSeconds": timeout,
                    }
                last_error = f"HTTP {response.status}"
        except HTTPError as exc:
            if exc.code < 500:
                return {
                    "ready": True,
                    "status": exc.code,
                    "url": ready_url,
                    "timeoutSeconds": timeout,
                }
            last_error = f"HTTP {exc.code}"
        except URLError as exc:
            last_error = str(exc.reason)
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    return {
        "ready": False,
        "skipped": False,
        "url": ready_url,
        "timeoutSeconds": timeout,
        "error": last_error or "preview target did not respond",
    }


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _split_env_patterns(name: str) -> List[str]:
    value = os.environ.get(name, "")
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _parse_time_or_iso(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return time.time()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return time.time()


def _ui_env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    expected_config = {
        "environments": "environment",
        "methods": "method",
        "studies": "study",
        "study_plan": "study",
        "study_plans": "study",
        "resources": "resource",
    }.get(str(expected_config or ""), str(expected_config or ""))
    if expected_config == "resource":
        path = _decode_id(uid)
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"resource not found: {path}")
        entry = _resource_catalog_entry(path)
        config_yaml = str(entry.get("yaml") or "")
        config_raw = yaml.safe_load(config_yaml) or {}
        if entry.get("config_path"):
            validation = validate_authoring_config(entry["config_path"])
        else:
            schema_result = validate_public_config_schema(config_raw, config_path=str(path / "optpilot.resource.yaml"))
            validation = {
                "valid": schema_result.valid,
                "path": "",
                "errors": [f"{issue.path}: {issue.message}" for issue in schema_result.errors],
            }
        return {
            "entry": entry,
            "config": config_raw,
            "yaml": config_yaml,
            "validation": validation,
            "compatibility": {"compatible": [], "incompatible": []},
        }
    path = _decode_id(uid)
    raw = _read_yaml(path)
    if raw.get("config") != expected_config or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{expected_config} config not found: {path}")
    entry = _catalog_entry(path, raw)
    if expected_config == "study":
        return {
            "entry": entry,
            "config": raw,
            "yaml": entry.get("yaml") or yaml.safe_dump(raw, sort_keys=False),
            "validation": _validate_study(path),
            "compatibility": {"compatible": [], "incompatible": []},
        }
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
        "yaml": entry.get("yaml") or yaml.safe_dump(raw, sort_keys=False),
        "validation": validate_authoring_config(path),
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
    description = str(payload.get("description") or "").strip()
    tags = payload.get("tags", [])
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    if not isinstance(tags, list):
        tags = []
    metric = str(payload.get("metric") or _first_metric(environment) or "score")
    direction = str(payload.get("direction") or "maximize")
    aggregation = str(payload.get("aggregation") or "mean")
    secondary_metrics = payload.get("secondaryMetrics", payload.get("secondary_metrics", []))
    if not isinstance(secondary_metrics, list):
        secondary_metrics = []
    max_trials = int(payload.get("maxTrials", payload.get("max_trials", 12)) or 12)
    max_wall_clock_raw = payload.get("maxWallClockSeconds", payload.get("max_wall_clock_seconds"))
    max_failures_raw = payload.get("maxFailures", payload.get("max_failures"))
    parallelism = int(payload.get("parallelism", 1) or 1)
    timeout = int(payload.get("timeoutSeconds", payload.get("timeout_seconds", 120)) or 120)
    max_retries_raw = payload.get("maxRetries", payload.get("max_retries"))
    evidence_level = str(payload.get("evidenceLevel", payload.get("evidence_level", "standard")) or "standard")
    evidence_storage = str(payload.get("evidenceStorage", payload.get("evidence_storage", "reference")) or "reference")
    evidence_output_dir = str(payload.get("evidenceOutputDir", payload.get("evidence_output_dir", "")) or "").strip()
    draft = {
        "apiVersion": AUTHORING_API_VERSION,
        "config": "study",
        "name": name,
    }
    if description:
        draft["description"] = description
    if tags:
        draft["tags"] = [str(item) for item in tags if str(item)]
    draft.update(
        {
            "environmentConfig": str(environment_path),
            "methodConfig": str(method_path),
            "objective": {
                "metric": metric,
                "direction": direction,
                "aggregation": aggregation,
                "secondaryMetrics": [str(item) for item in secondary_metrics if item],
            },
            "budget": {"maxTrials": max_trials},
            "execution": {"parallelism": parallelism, "timeoutSeconds": timeout},
            "evidence": {"level": evidence_level, "outputFileStorage": evidence_storage},
        }
    )
    if max_wall_clock_raw not in (None, ""):
        max_wall_clock = int(max_wall_clock_raw)
        if max_wall_clock <= 0:
            raise ValueError("maxWallClockSeconds must be >= 1 when provided.")
        draft["budget"]["maxWallClockSeconds"] = max_wall_clock
    if max_failures_raw not in (None, ""):
        max_failures = int(max_failures_raw)
        if max_failures < 0:
            raise ValueError("maxFailures must be >= 1 when provided, or blank/0 for no limit.")
        if max_failures > 0:
            draft["budget"]["maxFailures"] = max_failures
    if max_retries_raw not in (None, ""):
        max_retries = int(max_retries_raw)
        if max_retries < 0:
            raise ValueError("maxRetries must be >= 0 when provided.")
        draft["execution"]["retry"] = {"maxRetries": max_retries}
    if evidence_output_dir:
        draft["evidence"]["outputDir"] = evidence_output_dir
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
        f"# {title}\n\nThis workspace contains an editable OptPilot study plan. Review `study.yaml`, then launch it from Studies.\n",
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
            "registration_enabled": False,
        },
    )


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
        "sandbox": runtime.get("sandbox", "process"),
    }


def _method_runtime_summary(raw: JsonDict) -> JsonDict:
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    container = runtime.get("container", {}) if isinstance(runtime.get("container", {}), dict) else {}
    return {
        "type": runtime.get("sandbox", "process"),
        "image": container.get("image"),
        "has_build": bool(container.get("build")),
        "networkPolicy": container.get("network", "disabled") if runtime.get("sandbox") == "container" else "disabled",
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


def _assistant_run_detail(run_dir: Path) -> JsonDict:
    summary = _read_json(run_dir / "summary.json")
    study_spec = _read_json(run_dir / "study_spec.json")
    observations = _read_jsonl(run_dir / "observations.jsonl")
    trials = _read_jsonl(run_dir / "trials.jsonl")
    candidates = _read_jsonl(run_dir / "candidates.jsonl")
    run = _run_summary(run_dir)
    evidence_files = _run_evidence_files(run_dir, observations)
    best_candidate_id = run.get("best_candidate_id") or summary.get("best_candidate_id")
    best_trial_id = run.get("best_trial_id") or summary.get("best_trial_id")
    best_observation = next(
        (
            observation
            for observation in observations
            if observation.get("trial_id") == best_trial_id or observation.get("candidate_id") == best_candidate_id
        ),
        observations[0] if observations else {},
    )
    best_candidate = next(
        (candidate for candidate in candidates if candidate.get("candidate_id") == best_candidate_id),
        candidates[0] if candidates else {},
    )
    return {
        "run": run,
        "summary": {
            "status": run.get("status"),
            "completed_trials": run.get("completed_trials"),
            "failure_count": run.get("failure_count"),
            "best_metric": run.get("best_metric"),
            "best_trial_id": best_trial_id,
            "best_candidate_id": best_candidate_id,
            "started_at": summary.get("started_at") or run.get("started_at"),
            "finished_at": summary.get("finished_at") or run.get("finished_at"),
        },
        "study": {
            "name": study_spec.get("metadata", {}).get("name") or run.get("name"),
            "environment_id": run.get("environment_id"),
            "method": run.get("method"),
            "objective": run.get("objective"),
        },
        "observations": {
            "total": len(observations),
            "status_counts": _status_counts(observations),
            "metric_keys": _observation_metric_keys(observations),
            "preview": [_compact_observation(observation, run_dir) for observation in observations[:10]],
        },
        "trials": {
            "total": len(trials),
            "preview": [_compact_trial(trial) for trial in trials[:10]],
        },
        "best": {
            "metric": run.get("best_metric"),
            "trial_id": best_trial_id,
            "candidate_id": best_candidate_id,
            "observation": _compact_observation(best_observation, run_dir) if best_observation else {},
            "candidate": _compact_candidate(best_candidate, run_dir) if best_candidate else {},
        },
        "evidence_files": evidence_files,
        "tool_guidance": [
            "For raw evidence, call optpilot_run_file_read with one of the relative_path values in evidence_files.",
            "Do not use workspace file tools for run evidence unless the user explicitly asks to open the run as a workspace.",
        ],
    }


def _observation_metric_keys(observations: List[JsonDict]) -> List[str]:
    keys: set[str] = set()
    for observation in observations:
        metrics = observation.get("metric_values", {}) if isinstance(observation.get("metric_values"), dict) else {}
        keys.update(str(key) for key in metrics.keys())
    return sorted(keys)


def _compact_observation(observation: JsonDict, run_dir: Path) -> JsonDict:
    if not observation:
        return {}
    output_files = observation.get("output_files", []) if isinstance(observation.get("output_files"), list) else []
    return {
        "trial_id": observation.get("trial_id"),
        "candidate_id": observation.get("candidate_id"),
        "status": observation.get("status"),
        "metric_values": observation.get("metric_values", {}),
        "constraint_results": observation.get("constraint_results", {}),
        "event_summary": observation.get("event_summary", {}),
        "resource_usage": observation.get("resource_usage", {}),
        "output_files": [_compact_run_output_file(item, run_dir) for item in output_files[:20] if isinstance(item, dict)],
        "error": observation.get("error") or observation.get("failure_reason"),
    }


def _compact_trial(trial: JsonDict) -> JsonDict:
    if not trial:
        return {}
    return {
        "trial_id": trial.get("trial_id"),
        "candidate_id": trial.get("candidate_id"),
        "status": trial.get("status"),
        "method_id": trial.get("method_id"),
        "created_at": trial.get("created_at"),
        "error": trial.get("error") or trial.get("failure_reason"),
    }


def _compact_candidate(candidate: JsonDict, run_dir: Path) -> JsonDict:
    if not candidate:
        return {}
    output_files = []
    generator = candidate.get("generator", {}) if isinstance(candidate.get("generator"), dict) else {}
    materialization = candidate.get("materialization", {}) if isinstance(candidate.get("materialization"), dict) else {}
    if isinstance(materialization.get("output_files"), list):
        output_files = [_compact_run_output_file(item, run_dir) for item in materialization["output_files"][:20] if isinstance(item, dict)]
    return {
        "candidate_id": candidate.get("candidate_id"),
        "method_id": candidate.get("method_id") or generator.get("method_id"),
        "format": candidate.get("format"),
        "created_at": candidate.get("created_at"),
        "status": candidate.get("status"),
        "output_files": output_files,
    }


def _compact_run_output_file(item: JsonDict, run_dir: Path) -> JsonDict:
    path = str(item.get("path") or item.get("relative_path") or "")
    relative = _run_relative_path(path, run_dir) if path else ""
    return {
        "name": item.get("name") or Path(relative or path).name,
        "type": item.get("type") or Path(relative or path).suffix.lstrip("."),
        "relative_path": relative,
    }


def _run_evidence_files(run_dir: Path, observations: Optional[List[JsonDict]] = None) -> List[JsonDict]:
    observations = observations if observations is not None else _read_jsonl(run_dir / "observations.jsonl")
    files_by_path = {item["relative_path"]: item for item in _list_run_files(run_dir)}
    evidence: Dict[str, JsonDict] = {}
    canonical = [
        ("summary.json", "run summary: status, trial counts, best metric, and best candidate"),
        ("observations.jsonl", "per-trial evaluation results, metric values, output files, and failures"),
        ("trials.jsonl", "trial scheduler records and candidate assignments"),
        ("candidates.jsonl", "candidate metadata and materialization details"),
        ("method_calls.jsonl", "method invocation trace"),
        ("method_events.jsonl", "method-side events and logs"),
        ("scheduler_events.jsonl", "scheduler and execution events"),
        ("run_policy.json", "execution, evidence, sandbox, and reproducibility policy"),
        ("study_spec.json", "compiled study configuration used for this run"),
        ("environment_snapshot.json", "environment configuration snapshot"),
        ("run_lineage.json", "run lineage and reproducibility metadata"),
    ]
    for relative_path, purpose in canonical:
        file_info = files_by_path.get(relative_path)
        if file_info:
            evidence[relative_path] = {**file_info, "purpose": purpose, "recommended": True}
    for observation in observations:
        output_files = observation.get("output_files", []) if isinstance(observation.get("output_files"), list) else []
        for output in output_files:
            if not isinstance(output, dict):
                continue
            relative_path = _run_relative_path(str(output.get("path") or output.get("relative_path") or ""), run_dir)
            if not relative_path or relative_path not in files_by_path:
                continue
            evidence.setdefault(
                relative_path,
                {
                    **files_by_path[relative_path],
                    "purpose": f"trial output: {output.get('name') or Path(relative_path).name}",
                    "recommended": False,
                },
            )
    return sorted(evidence.values(), key=lambda item: (not item.get("recommended"), item.get("relative_path", "")))[:120]


def _run_relative_path(path_value: str, run_dir: Path) -> str:
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = run_dir / path
    try:
        resolved = path.resolve()
    except OSError:
        return path_value
    if not _is_relative_to(resolved, run_dir.resolve()):
        return path_value
    return str(resolved.relative_to(run_dir.resolve()))


def _run_file_not_found_result(tool: str, run_dir: Path, relative: str) -> JsonDict:
    available = _run_evidence_files(run_dir)
    available_paths = [str(item.get("relative_path") or "") for item in available if item.get("relative_path")]
    matches = difflib.get_close_matches(relative, available_paths, n=5, cutoff=0.25) if relative else []
    return _tool_result(
        tool,
        False,
        f"Run file not found: {relative or '(empty path)'}. Use optpilot_run_detail evidence_files to choose a valid relative path.",
        data={
            "requested_path": relative,
            "suggested_paths": matches,
            "available_files": available[:40],
        },
    )


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
        "content": "I can use the current page, attached workspace roots, catalog, study plans, runs, and Code Server context.",
        "created_at": _now_iso(),
        "source": "studio_system",
        "memory_scope": "ui_history",
    }


def _read_agent_messages(state: UiState, session_id: str) -> List[JsonDict]:
    path = _agent_messages_path(state, session_id)
    if not path.exists():
        return []
    messages: List[JsonDict] = []
    seen: set[tuple[str, str]] = set()
    for message in _read_jsonl(path):
        if (
            _is_legacy_openhands_placeholder(message)
            or _is_malformed_openhands_context_echo(message)
            or _is_non_user_facing_openhands_message(message)
        ):
            continue
        key = (str(message.get("role") or ""), _normalize_agent_text(str(message.get("content") or "")))
        if key[1] and key in seen:
            continue
        seen.add(key)
        messages.append(message)
    return messages


def _is_legacy_openhands_placeholder(message: JsonDict) -> bool:
    return str(message.get("content") or "").strip() == "Message sent to OpenHands. Refresh the assistant session to see later events."


def _is_malformed_openhands_context_echo(message: JsonDict) -> bool:
    content = str(message.get("content") or "").strip()
    return (
        message.get("role") == "assistant"
        and content.startswith("User request:")
        and "Visible OptPilot Studio context packet:" in content
    )


def _is_non_user_facing_openhands_message(message: JsonDict) -> bool:
    if message.get("role") != "assistant":
        return False
    content = str(message.get("content") or "").strip()
    normalized = _normalize_agent_text(content).lower()
    return (
        normalized.startswith(("the user hasn't replied", "the user still hasn't sent a new message"))
        or normalized.startswith("the task is complete on my side:")
        or "(waiting for your next message" in normalized
        or "</think>" in content
    )


def _recover_agent_assistant_messages_from_events(state: UiState, session_id: str) -> None:
    path = _agent_messages_path(state, session_id)
    raw_messages = _read_jsonl(path) if path.exists() else []
    existing_ids = {str(message.get("id") or "") for message in raw_messages}
    existing_texts = {
        _normalize_agent_text(str(message.get("content") or ""))
        for message in raw_messages
        if str(message.get("content") or "").strip()
    }
    for event in _read_agent_events(state, session_id):
        text = _openhands_user_facing_message_from_event(event)
        normalized = _normalize_agent_text(text)
        if not text or not normalized or normalized in existing_texts:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_id = str(event.get("id") or payload.get("event_id") or uuid.uuid4().hex[:10])
        message_id = f"msg_{_slug_text(event_id)[:32]}"
        if message_id in existing_ids:
            continue
        _append_jsonl(
            path,
            {
                "id": message_id,
                "role": "assistant",
                "title": "OpenHands",
                "content": text,
                "created_at": event.get("created_at") or _now_iso(),
                "source": "openhands",
                "memory_scope": "openhands_conversation",
                "dispatch": {"status": "answered", "transport": "openhands_http"},
            },
        )
        existing_ids.add(message_id)
        existing_texts.add(normalized)


def _openhands_user_facing_message_from_event(event: JsonDict) -> str:
    if not isinstance(event, dict) or event.get("type") != "openhands_event":
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if str(payload.get("tool") or "") == "finish":
        text = _openhands_finish_message_from_payload(payload)
        if text:
            return text
    return ""


def _openhands_finish_message_from_payload(payload: JsonDict) -> str:
    arguments_preview = str(payload.get("arguments_preview") or "").strip()
    if arguments_preview:
        try:
            arguments = json.loads(arguments_preview)
        except json.JSONDecodeError:
            arguments = {}
        if isinstance(arguments, dict):
            text = _user_facing_openhands_text(arguments.get("message") or arguments.get("content") or arguments.get("text"))
            if text:
                return text
    raw_preview = str(payload.get("raw_preview") or "").strip()
    if raw_preview:
        try:
            raw = json.loads(raw_preview)
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            action = raw.get("action") if isinstance(raw.get("action"), dict) else {}
            observation = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
            text = _user_facing_openhands_text(
                action.get("message")
                or action.get("content")
                or observation.get("message")
                or observation.get("content")
            )
            if text:
                return text
    return ""


def _user_facing_openhands_text(content: Any) -> str:
    text = _content_text(content)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    text = text.strip()
    if not text:
        return ""
    normalized = _normalize_agent_text(text).lower()
    if not normalized:
        return ""
    if "(waiting for your next message" in normalized or normalized == "waiting for your next message.":
        return ""
    if normalized.startswith((
        "the user hasn't replied",
        "the user still hasn't sent a new message",
        "the task is complete on my side",
    )):
        return ""
    if "delayed tool result" in normalized and "prior" in normalized:
        return ""
    planning_markers = (
        " i should ",
        " i need ",
        " let me ",
        " the user ",
        " now i have ",
        "there's nothing more to do except wait",
    )
    marker_count = sum(1 for marker in planning_markers if marker in f" {normalized} ")
    if marker_count >= 2:
        return ""
    return text


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return _content_text(content.get("text"))
        if "content" in content:
            return _content_text(content.get("content"))
        if "message" in content:
            return _content_text(content.get("message"))
        return ""
    if isinstance(content, list):
        parts = [_content_text(item) for item in content]
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def _normalize_agent_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _read_agent_events(state: UiState, session_id: str) -> List[JsonDict]:
    path = _agent_events_path(state, session_id)
    if not path.exists():
        return []
    return [_sanitize_agent_event(event) for event in _read_jsonl(path)]


def _sanitize_agent_event(event: JsonDict) -> JsonDict:
    if not isinstance(event, dict):
        return event
    sanitized = json.loads(json.dumps(event, default=str))
    payload = sanitized.get("payload")
    if not isinstance(payload, dict):
        return sanitized
    if sanitized.get("type") != "openhands_event":
        return sanitized
    if isinstance(payload.get("summary"), str):
        payload["summary"] = _sanitize_openhands_step_text(payload["summary"])
    if isinstance(payload.get("raw_preview"), str):
        payload["raw_preview"] = _sanitize_openhands_step_text(payload["raw_preview"])
    return sanitized


def _sanitize_openhands_step_text(text: str) -> str:
    marker = "Visible OptPilot Studio context packet:"
    if marker not in text:
        return text
    if "User request:" in text:
        request = text.split("User request:", 1)[1].split(marker, 1)[0].strip()
        request = " ".join(request.split())
        if request:
            return f"User request sent to OpenHands: {request[:220]}"
        return "User request and Studio context sent to OpenHands."
    return "[Studio context packet redacted from step preview]"


def _agent_approvals_path(state: UiState, session_id: str) -> Path:
    return _agent_session_dir(state, session_id) / "approvals.json"


def _read_agent_approvals(state: UiState, session_id: str) -> List[JsonDict]:
    path = _agent_approvals_path(state, session_id)
    if not path.exists():
        return []
    raw = _read_json(path)
    approvals = raw.get("approvals", []) if isinstance(raw, dict) else []
    return [item for item in approvals if isinstance(item, dict) and item.get("id")]


def _write_agent_approvals(state: UiState, session_id: str, approvals: List[JsonDict]) -> None:
    path = _agent_approvals_path(state, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"approvals": approvals}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _upsert_agent_approval(state: UiState, session_id: str, approval: JsonDict) -> JsonDict:
    approvals = [item for item in _read_agent_approvals(state, session_id) if item.get("id") != approval.get("id")]
    approvals.append(approval)
    _write_agent_approvals(state, session_id, approvals)
    return approval


def _tool_result(tool: str, ok: bool, summary: str, *, data: Optional[JsonDict] = None, artifacts: Optional[List[JsonDict]] = None, events: Optional[List[JsonDict]] = None) -> JsonDict:
    return {
        "ok": ok,
        "tool": tool,
        "summary": summary,
        "data": data or {},
        "artifacts": artifacts or [],
        "events": events or [],
    }


def _approval_request_key(tool: str, kind: str, arguments: JsonDict) -> str:
    stable_arguments = {
        str(key): value
        for key, value in (arguments or {}).items()
        if key not in {"_openhands_tool_call_id", "approved"}
    }
    payload = {"tool": tool, "kind": kind, "arguments": stable_arguments}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _approval_record_key(approval: JsonDict) -> str:
    arguments = approval.get("arguments") if isinstance(approval.get("arguments"), dict) else {}
    return str(approval.get("request_key") or _approval_request_key(str(approval.get("tool") or ""), str(approval.get("kind") or ""), arguments))


def _supersede_duplicate_pending_approvals(state: UiState, session_id: str, approval: JsonDict, *, action: str) -> None:
    approval_id = str(approval.get("id") or "")
    request_key = _approval_record_key(approval)
    changed = False
    approvals = []
    for item in _read_agent_approvals(state, session_id):
        item = dict(item)
        if item.get("status") == "pending" and str(item.get("id") or "") != approval_id and _approval_record_key(item) == request_key:
            item["status"] = "superseded"
            item["superseded_by"] = approval_id
            item["superseded_action"] = action
            item["superseded_at"] = _now_iso()
            changed = True
        approvals.append(item)
    if changed:
        _write_agent_approvals(state, session_id, approvals)


def _request_agent_approval(
    state: UiState,
    session_id: str,
    *,
    tool: str,
    arguments: JsonDict,
    kind: str,
    title: str,
    summary: str,
    targets: Optional[List[str]] = None,
) -> JsonDict:
    request_key = _approval_request_key(tool, kind, arguments)
    for existing in _read_agent_approvals(state, session_id):
        if existing.get("status") == "pending" and _approval_record_key(existing) == request_key:
            return _tool_result(
                tool,
                False,
                f"Approval required: {summary}",
                data={"approval": existing, "approval_required": True},
                events=[{"level": "warning", "message": summary}],
            )
    tool_call_id = str(arguments.get("_openhands_tool_call_id") or "")
    approval = {
        "id": f"approval_{uuid.uuid4().hex[:10]}",
        "session_id": session_id,
        "tool": tool,
        "kind": kind,
        "title": title,
        "summary": summary,
        "targets": targets or [],
        "arguments": arguments,
        "status": "pending",
        "request_key": request_key,
        "openhands_tool_call_id": tool_call_id,
        "created_at": _now_iso(),
    }
    _upsert_agent_approval(state, session_id, approval)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "approval_requested",
            "created_at": approval["created_at"],
            "payload": approval,
        },
    )
    return _tool_result(
        tool,
        False,
        f"Approval required: {summary}",
        data={"approval": approval, "approval_required": True},
        events=[{"level": "warning", "message": summary}],
    )


def _approve_agent_action(state: UiState, session_id: str, approval_id: str) -> JsonDict:
    approvals = _read_agent_approvals(state, session_id)
    approval = next((item for item in approvals if item.get("id") == approval_id), None)
    if not approval:
        raise KeyError(approval_id)
    if approval.get("status") != "pending":
        return {"approval": approval, "result": approval.get("result"), "session": _agent_session_by_id(state, session_id)}
    arguments = dict(approval.get("arguments", {}) if isinstance(approval.get("arguments"), dict) else {})
    arguments["approved"] = True
    result = _execute_agent_tool(state, session_id, str(approval["tool"]), arguments)
    feedback = _forward_approved_tool_result_to_openhands(state, session_id, approval, result)
    _supersede_duplicate_pending_approvals(state, session_id, approval, action="approved")
    approval["status"] = "approved"
    approval["approved_at"] = _now_iso()
    approval["result"] = result
    approval["openhands_feedback"] = feedback
    _upsert_agent_approval(state, session_id, approval)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "approval_approved",
            "created_at": approval["approved_at"],
            "payload": {"approval_id": approval_id, "tool": approval.get("tool"), "ok": result.get("ok"), "openhands_feedback": feedback},
        },
    )
    return {"approval": approval, "result": result, "session": _agent_session_by_id(state, session_id)}


def _reject_agent_action(state: UiState, session_id: str, approval_id: str, reason: str = "") -> JsonDict:
    approvals = _read_agent_approvals(state, session_id)
    approval = next((item for item in approvals if item.get("id") == approval_id), None)
    if not approval:
        raise KeyError(approval_id)
    if approval.get("status") != "pending":
        return {"approval": approval, "session": _agent_session_by_id(state, session_id)}
    approval["status"] = "rejected"
    approval["rejected_at"] = _now_iso()
    approval["rejection_reason"] = reason or "Rejected by user."
    result = _tool_result(str(approval.get("tool") or "approval"), False, f"Approval rejected: {approval['rejection_reason']}")
    feedback = _forward_approved_tool_result_to_openhands(state, session_id, approval, result)
    _supersede_duplicate_pending_approvals(state, session_id, approval, action="rejected")
    approval["result"] = result
    approval["openhands_feedback"] = feedback
    _upsert_agent_approval(state, session_id, approval)
    _append_jsonl(
        _agent_events_path(state, session_id),
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "approval_rejected",
            "created_at": approval["rejected_at"],
            "payload": {"approval_id": approval_id, "tool": approval.get("tool"), "reason": approval["rejection_reason"], "openhands_feedback": feedback},
        },
    )
    return {"approval": approval, "session": _agent_session_by_id(state, session_id)}


def _forward_approved_tool_result_to_openhands(
    state: UiState,
    session_id: str,
    approval: JsonDict,
    result: JsonDict,
) -> JsonDict:
    session = _require_agent_session(state, session_id)
    conversation_id = str(session.get("openhands_conversation_id") or "")
    arguments = approval.get("arguments") if isinstance(approval.get("arguments"), dict) else {}
    tool_call_id = str(approval.get("openhands_tool_call_id") or arguments.get("_openhands_tool_call_id") or "")
    tool_name = str(approval.get("tool") or result.get("tool") or "")
    feedback: JsonDict = {"sent": False, "reason": "No active OpenHands tool call to continue."}
    submit_tool_result = getattr(state.agent_adapter, "submit_tool_result", None)
    if conversation_id and tool_call_id and callable(submit_tool_result):
        try:
            feedback = submit_tool_result(conversation_id, tool_name, tool_call_id, result)
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            feedback = {"sent": False, "reason": str(exc), "conversation_id": conversation_id, "tool_call_id": tool_call_id}
    _append_agent_event_record(
        state,
        session_id,
        {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": "openhands_tool_result_forwarded" if feedback.get("sent") else "openhands_tool_result_forward_skipped",
            "created_at": _now_iso(),
            "payload": {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "conversation_id": conversation_id,
                "sent": bool(feedback.get("sent")),
                "reason": feedback.get("reason") or "",
            },
        },
    )
    if feedback.get("sent"):
        session["status"] = "waiting_for_agent"
        _upsert_agent_session(state, session)
    return feedback


def _execute_agent_tool(state: UiState, session_id: str, tool: str, arguments: Optional[JsonDict] = None) -> JsonDict:
    arguments = arguments or {}
    if tool == "optpilot_workspace_list":
        sessions = _read_agent_session_index(state)
        session = _require_agent_session(state, session_id)
        attached = set(session.get("attached_workspace_ids", []) or [])
        workspaces = []
        for workspace in _list_ui_workspaces(state):
            item = dict(workspace)
            item["attached_to_current_session"] = item.get("id") in attached
            workspaces.append(item)
        return _tool_result(tool, True, f"Found {len(workspaces)} workspace(s).", data={"workspaces": workspaces, "sessions": sessions})
    if tool == "optpilot_workspace_create":
        workspace = _create_ui_workspace(state, arguments)
        _attach_agent_workspace(state, session_id, workspace["id"], select=True)
        return _tool_result(tool, True, f"Created workspace {workspace['id']}.", data={"workspace": workspace})
    if tool == "optpilot_workspace_attach":
        session = _attach_agent_workspace(state, session_id, str(arguments.get("workspace_id") or ""), select=True)
        return _tool_result(tool, True, "Workspace attached.", data={"session": session})
    if tool == "optpilot_workspace_detach":
        session = _detach_agent_workspace(state, session_id, str(arguments.get("workspace_id") or ""))
        return _tool_result(tool, True, "Workspace detached.", data={"session": session})
    if tool == "optpilot_workspace_focus":
        session = _select_agent_workspace(state, session_id, str(arguments.get("workspace_id") or ""))
        return _tool_result(tool, True, "Workspace selected.", data={"session": session, "focus_path": str(arguments.get("path") or "")})
    if tool == "optpilot_file_tree":
        workspace, root, target = _resolve_agent_workspace_path(state, session_id, arguments, default_path=".")
        max_files = min(max(int(arguments.get("max_files") or 200), 1), 500)
        files = _workspace_file_tree(root, target, max_files=max_files)
        return _tool_result(tool, True, f"Listed {len(files)} file(s).", data={"workspace": workspace, "root": str(root), "path": _relative_path(target, root), "files": files})
    if tool == "optpilot_file_read":
        workspace, root, path = _resolve_agent_workspace_path(state, session_id, arguments)
        if not path.is_file():
            raise FileNotFoundError(_relative_path(path, root))
        if path.stat().st_size > 1_000_000:
            raise ValueError("File is too large to read through the assistant tool.")
        return _tool_result(tool, True, f"Read {_relative_path(path, root)}.", data={"workspace": workspace, "path": _relative_path(path, root), "content": path.read_text(encoding="utf-8", errors="replace")})
    if tool == "optpilot_file_write":
        workspace, root, path = _resolve_agent_workspace_path(state, session_id, arguments)
        _require_editable_workspace(workspace)
        content = str(arguments.get("content") or "")
        if len(content.encode("utf-8")) > 2_000_000:
            raise ValueError("Content is too large to write through the assistant tool.")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return _tool_result(tool, True, f"Wrote {_relative_path(path, root)}.", data={"workspace": workspace, "path": _relative_path(path, root), "created": not existed, "bytes": len(content.encode("utf-8"))})
    if tool == "optpilot_file_diff":
        workspace, root, path = _resolve_agent_workspace_path(state, session_id, arguments)
        new_content = str(arguments.get("content") or "")
        old_content = path.read_text(encoding="utf-8", errors="replace") if path.exists() and path.is_file() else ""
        diff = "".join(difflib.unified_diff(old_content.splitlines(True), new_content.splitlines(True), fromfile=f"a/{_relative_path(path, root)}", tofile=f"b/{_relative_path(path, root)}"))
        return _tool_result(tool, True, f"Prepared diff for {_relative_path(path, root)}.", data={"workspace": workspace, "path": _relative_path(path, root), "diff": diff})
    if tool == "optpilot_shell_run":
        return _agent_tool_shell_run(state, session_id, tool, arguments)
    if tool == "optpilot_workspace_preview_open":
        return _agent_tool_workspace_preview_open(state, session_id, tool, arguments)
    if tool == "optpilot_catalog_list":
        catalog = _catalog_payload(state)
        kind = str(arguments.get("config_kind") or arguments.get("kind") or "")
        kind_keys = {
            "environment": "environments",
            "method": "methods",
            "study": "studies",
            "resource": "resources",
            "environments": "environments",
            "methods": "methods",
            "studies": "studies",
            "resources": "resources",
        }
        data = catalog if not kind else {kind_keys.get(kind, kind): catalog.get(kind_keys.get(kind, kind), [])}
        return _tool_result(tool, True, "Catalog entries and saved study plans listed.", data=data)
    if tool == "optpilot_catalog_detail":
        kind = str(arguments.get("config_kind") or arguments.get("kind") or "")
        uid = str(arguments.get("uid") or "")
        path = str(arguments.get("path") or "")
        if path and not uid:
            uid = _encode_id(_resolve_user_path(path, state.cwd))
        detail = _catalog_detail(state, kind, uid)
        detail_label = "saved study plan" if kind in {"study", "studies"} else "catalog entry"
        return _tool_result(tool, True, f"Loaded {kind} {detail_label}.", data=detail)
    if tool == "optpilot_compatibility_check":
        env_path = arguments.get("environment_path")
        method_path = arguments.get("method_path")
        if env_path and method_path:
            environment_path = _resolve_user_path(env_path, state.cwd)
            method_path_resolved = _resolve_user_path(method_path, state.cwd)
            data = _compatibility_result(_catalog_entry(environment_path, _read_yaml(environment_path)), _read_yaml(environment_path), _catalog_entry(method_path_resolved, _read_yaml(method_path_resolved)), _read_yaml(method_path_resolved))
        else:
            data = _compatibility_payload(state)
        return _tool_result(tool, True, "Compatibility checked.", data=data)
    if tool == "optpilot_config_discover":
        workspace_id = str(arguments.get("workspace_id") or _selected_agent_workspace_id(state, session_id))
        data = _discover_workspace_configs(state, workspace_id)
        return _tool_result(tool, True, f"Discovered {len(data.get('configs', []))} config(s).", data=data)
    if tool == "optpilot_config_validate":
        path = _resolve_agent_or_allowed_path(state, session_id, arguments)
        validation = validate_authoring_config(path)
        return _tool_result(tool, bool(validation.get("valid")), "Config validation passed." if validation.get("valid") else "Config validation failed.", data={"validation": validation, "path": str(path)})
    if tool == "optpilot_registration_prepare":
        workspace_id = str(arguments.get("workspace_id") or _selected_agent_workspace_id(state, session_id))
        data = _create_registration_manifest(state, workspace_id, arguments)
        return _tool_result(tool, True, "Registration manifest prepared.", data=data)
    if tool == "optpilot_registration_validate":
        data = _validate_registration_manifest(state, str(arguments.get("workspace_id") or ""), str(arguments.get("registration_id") or ""))
        return _tool_result(tool, data.get("registration", {}).get("status") == "validated", "Registration validated.", data=data)
    if tool == "optpilot_registration_apply":
        if not arguments.get("approved"):
            return _request_agent_approval(state, session_id, tool=tool, arguments=arguments, kind="registration_apply", title="Apply catalog registration", summary="Apply selected workspace files into catalog/local_package.", targets=[str(arguments.get("registration_id") or "")])
        data = _apply_registration_manifest(state, str(arguments.get("workspace_id") or ""), str(arguments.get("registration_id") or ""))
        return _tool_result(tool, bool(data.get("applied")), "Registration applied." if data.get("applied") else "Registration was not applied.", data=data)
    if tool == "optpilot_study_draft":
        data = _draft_study(state, arguments)
        return _tool_result(tool, bool(data.get("validation", {}).get("valid")), "Study draft prepared.", data=data)
    if tool == "optpilot_study_save":
        workspace, root, path = _resolve_agent_workspace_path(state, session_id, arguments)
        _require_editable_workspace(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments.get("yaml") or ""), encoding="utf-8")
        return _tool_result(tool, True, f"Saved study YAML to {_relative_path(path, root)}.", data={"workspace": workspace, "path": _relative_path(path, root), "validation": _validate_study(path)})
    if tool == "optpilot_study_launch":
        study_path = _resolve_agent_or_allowed_path(state, session_id, {"path": arguments.get("study_path"), "workspace_id": arguments.get("workspace_id")})
        validation = _validate_study(study_path)
        if not validation.get("valid"):
            return _tool_result(tool, False, "Study validation failed; launch blocked.", data={"validation": validation})
        if not arguments.get("approved"):
            return _request_agent_approval(state, session_id, tool=tool, arguments={**arguments, "study_path": str(study_path)}, kind="study_launch", title="Launch OptPilot study", summary=f"Launch {study_path.name} into the configured output root.", targets=[str(study_path)])
        output_root = _optional_user_path(arguments.get("output_root"), state.cwd)
        job = state.launch_study(study_path, output_root, study_name=validation.get("name"), environment_id=validation.get("environment_id"))
        return _tool_result(tool, True, "Study launched.", data={"job": job.to_dict(), "validation": validation})
    if tool == "optpilot_job_stop":
        if not arguments.get("approved"):
            return _request_agent_approval(state, session_id, tool=tool, arguments=arguments, kind="job_stop", title="Stop OptPilot job", summary=f"Stop job {arguments.get('job_id')}.", targets=[str(arguments.get("job_id") or "")])
        return _tool_result(tool, True, "Job stopped.", data={"job": state.stop_job(str(arguments.get("job_id") or ""))})
    if tool == "optpilot_run_list":
        runs = _list_runs(state)
        return _tool_result(tool, True, f"Found {len(runs)} run(s).", data={"runs": runs})
    if tool == "optpilot_run_detail":
        run_dir = _resolve_run_tool_path(arguments)
        return _tool_result(tool, True, "Run detail loaded.", data=_assistant_run_detail(run_dir))
    if tool == "optpilot_run_file_read":
        run_dir = _resolve_run_tool_path(arguments)
        relative = str(arguments.get("path") or "")
        path = (run_dir / relative).resolve()
        if not _is_relative_to(path, run_dir.resolve()) or not path.is_file():
            return _run_file_not_found_result(tool, run_dir, relative)
        if path.stat().st_size > 1_000_000:
            raise ValueError("Run file is too large to read through the assistant tool.")
        return _tool_result(
            tool,
            True,
            f"Read run file {relative}.",
            data={
                "path": relative,
                "content": path.read_text(encoding="utf-8", errors="replace"),
                "available_files": _run_evidence_files(run_dir)[:40],
            },
        )
    if tool == "optpilot_run_open_workspace":
        run_dir = _resolve_run_tool_path(arguments)
        workspace = _open_run_workspace(state, run_dir)
        session = _attach_agent_workspace(state, session_id, workspace["id"], select=True)
        return _tool_result(tool, True, "Run opened as analysis workspace and attached to this assistant session.", data={"workspace": workspace, "session": session})
    if tool == "optpilot_run_compare":
        runs = [_run_detail(_resolve_run_tool_path({"run_id": item})) for item in arguments.get("runs", []) or []]
        return _tool_result(tool, True, f"Compared {len(runs)} run(s).", data={"runs": [_run_compare_summary(run) for run in runs], "comparable": _runs_comparable(runs)})
    if tool == "optpilot_smoke_test_study":
        return _agent_tool_smoke_test_study(state, session_id, tool, arguments)
    if tool == "optpilot_docs_search":
        results = _docs_search(state, str(arguments.get("query") or ""), limit=int(arguments.get("limit") or 5))
        return _tool_result(tool, True, f"Found {len(results)} doc result(s).", data={"results": results})
    if tool == "optpilot_capability_list":
        data = _assistant_capability_list(state, str(arguments.get("capability_kind") or arguments.get("kind") or ""))
        total = sum(len(records) for records in data.get("capabilities", {}).values())
        return _tool_result(tool, True, f"Found {total} configured assistant capability record(s).", data=data)
    if tool == "optpilot_capability_detail":
        data = _assistant_capability_detail(state, str(arguments.get("capability_kind") or arguments.get("kind") or ""), str(arguments.get("id") or ""))
        return _tool_result(tool, True, "Assistant capability loaded.", data=data)
    return _tool_result(tool, False, f"Unknown OptPilot assistant tool: {tool}", data={"known_tools": state.agent_adapter.status().get("available_tools", [])})


def _selected_agent_workspace_id(state: UiState, session_id: str) -> str:
    session = _require_agent_session(state, session_id)
    selected = str(session.get("selected_workspace_id") or "")
    attached = [str(item) for item in session.get("attached_workspace_ids", []) or []]
    if selected and selected in attached:
        return selected
    if attached:
        raise ValueError("No workspace is selected for this assistant session. Pass workspace_id explicitly or focus a workspace first.")
    raise ValueError("No workspace is attached to this assistant session.")


def _resolve_agent_workspace_path(
    state: UiState,
    session_id: str,
    arguments: JsonDict,
    *,
    default_path: Optional[str] = None,
) -> tuple[JsonDict, Path, Path]:
    workspace_id = str(arguments.get("workspace_id") or _selected_agent_workspace_id(state, session_id))
    session = _require_agent_session(state, session_id)
    if workspace_id not in (session.get("attached_workspace_ids", []) or []):
        raise PermissionError("Workspace is not attached to this assistant session.")
    workspace = _require_ui_workspace(state, workspace_id)
    root = _safe_workspace_root(state, Path(str(workspace["root"]))).resolve()
    raw_path = arguments.get("path")
    if raw_path in (None, "") and default_path is not None:
        raw_path = default_path
    if raw_path in (None, ""):
        raise ValueError("path is required.")
    requested = Path(str(raw_path)).expanduser()
    target = requested if requested.is_absolute() else root / requested
    target = target.resolve()
    if not _is_relative_to(target, root):
        raise PermissionError("Path is outside the attached workspace root.")
    return workspace, root, target


def _require_editable_workspace(workspace: JsonDict) -> None:
    if str(workspace.get("mode") or "editable") != "editable":
        raise PermissionError("This workspace is read-only; attach an editable copy before writing.")


def _workspace_file_tree(root: Path, target: Path, *, max_files: int) -> List[JsonDict]:
    if target.is_file():
        stat = target.stat()
        return [{"path": _relative_path(target, root), "type": "file", "size": stat.st_size}]
    if not target.exists() or not target.is_dir():
        raise FileNotFoundError(_relative_path(target, root))
    files: List[JsonDict] = []
    stack = [target]
    while stack and len(files) < max_files:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            continue
        for child in entries:
            if child.name in EXCLUDED_SCAN_DIRS:
                continue
            resolved = child.resolve()
            if not _is_relative_to(resolved, root):
                continue
            if child.is_dir():
                files.append({"path": _relative_path(resolved, root), "type": "directory"})
                stack.append(resolved)
            elif child.is_file():
                stat = child.stat()
                files.append({"path": _relative_path(resolved, root), "type": "file", "size": stat.st_size})
            if len(files) >= max_files:
                break
    return files


def _resolve_agent_or_allowed_path(state: UiState, session_id: str, arguments: JsonDict) -> Path:
    raw_path = arguments.get("path") or arguments.get("study_path")
    if not raw_path:
        raise ValueError("path is required.")
    workspace_id = str(arguments.get("workspace_id") or "")
    if workspace_id:
        return _resolve_agent_workspace_path(state, session_id, {**arguments, "path": raw_path})[2]
    candidate = _resolve_user_path(raw_path, state.cwd)
    allowed_roots = [
        state.cwd,
        *state.catalog_roots,
        *state.run_roots,
        state.sessions_dir,
        state.workspaces_dir,
    ]
    if any(_is_relative_to(candidate, root.resolve()) for root in allowed_roots):
        return candidate
    raise PermissionError(f"Path is outside OptPilot-controlled roots: {candidate}")


def _resolve_run_tool_path(arguments: JsonDict) -> Path:
    raw = str(arguments.get("run_id") or arguments.get("path") or "")
    if not raw:
        raise ValueError("run_id or path is required.")
    candidates: List[Path] = []
    try:
        candidates.append(_decode_id(raw))
    except Exception:
        pass
    candidates.append(Path(raw).expanduser().resolve())
    for candidate in candidates:
        if _is_run_dir(candidate):
            return candidate
    raise FileNotFoundError(f"Run not found: {raw}")


def _agent_tool_workspace_preview_open(state: UiState, session_id: str, tool: str, arguments: JsonDict) -> JsonDict:
    workspace, root, _ = _resolve_agent_workspace_path(
        state,
        session_id,
        {**arguments, "path": "."},
        default_path=".",
    )
    port = int(arguments.get("port") or 0)
    extra_ports = arguments.get("extra_ports") if isinstance(arguments.get("extra_ports"), list) else []
    result = state.workspace_preview_open(root, port, extra_ports=extra_ports)
    return _tool_result(
        tool,
        True,
        f"Workspace preview opened on port {port}.",
        data={
            **result,
            "workspace": workspace,
        },
    )


def _agent_tool_shell_run(state: UiState, session_id: str, tool: str, arguments: JsonDict) -> JsonDict:
    workspace, root, cwd = _resolve_agent_workspace_path(
        state,
        session_id,
        {**arguments, "path": arguments.get("cwd") or arguments.get("path") or "."},
        default_path=".",
    )
    _require_editable_workspace(workspace)
    if cwd.is_file():
        cwd = cwd.parent
    if not cwd.exists() or not cwd.is_dir():
        raise FileNotFoundError(_relative_path(cwd, root))
    command = _normalize_shell_command(arguments.get("command"))
    if not command:
        raise ValueError("command is required.")
    timeout_seconds = min(max(int(arguments.get("timeout_seconds") or 30), 1), 120)
    if _shell_needs_approval(command) and not arguments.get("approved"):
        return _request_agent_approval(
            state,
            session_id,
            tool=tool,
            arguments={**arguments, "command": command, "cwd": _relative_path(cwd, root), "timeout_seconds": timeout_seconds},
            kind="shell_run",
            title="Run workspace command",
            summary=f"Run {' '.join(shlex.quote(part) for part in command)} in {workspace.get('title') or workspace.get('id')}.",
            targets=[str(root), _relative_path(cwd, root)],
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "UV_CACHE_DIR"}
    }
    env["OPTPILOT_WORKSPACE_ROOT"] = str(root)
    env["OPTPILOT_STUDIO_ROOT"] = str(state.cwd)
    completed, runtime_status = state.workspace_runtime.exec(
        workspace,
        command,
        cwd=cwd,
        env=env,
        timeout=timeout_seconds,
    )
    return _tool_result(
        tool,
        completed.returncode == 0,
        f"Command exited with {completed.returncode}.",
        data={
            "workspace": workspace,
            "runtime": runtime_status,
            "cwd": _relative_path(cwd, root),
            "command": command,
            "returncode": completed.returncode,
            "stdout": _cap_text(completed.stdout, 12000),
            "stderr": _cap_text(completed.stderr, 12000),
        },
    )


def _normalize_shell_command(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    raise ValueError("command must be a string or list of strings.")


def _shell_needs_approval(command: List[str]) -> bool:
    if not command:
        return False
    first = Path(command[0]).name
    tokens = {item.lower() for item in command[1:]}
    if first in {"curl", "wget", "brew", "docker", "podman", "pip", "pip3", "rm", "mv", "cp", "chmod", "chown"}:
        return True
    if first in {"npm", "pnpm", "yarn"}:
        return True
    if first == "git":
        return len(command) > 1 and command[1] in {"clone", "push", "pull", "fetch", "reset", "checkout", "clean", "merge", "rebase"}
    if first == "uv":
        risky = {"add", "remove", "sync", "lock", "tool", "pip", "build", "publish"}
        return bool(tokens.intersection(risky) or "install" in tokens or "--with" in tokens)
    return False


def _agent_tool_smoke_test_study(state: UiState, session_id: str, tool: str, arguments: JsonDict) -> JsonDict:
    study_path = _resolve_agent_or_allowed_path(state, session_id, {"path": arguments.get("study_path"), "workspace_id": arguments.get("workspace_id")})
    validation = _validate_study(study_path)
    if not validation.get("valid"):
        return _tool_result(tool, False, "Study validation failed; smoke test blocked.", data={"validation": validation})
    if not arguments.get("approved"):
        return _request_agent_approval(
            state,
            session_id,
            tool=tool,
            arguments={**arguments, "study_path": str(study_path)},
            kind="smoke_test_study",
            title="Run study smoke test",
            summary=f"Execute {study_path.name} into a temporary output directory.",
            targets=[str(study_path)],
        )
    with tempfile.TemporaryDirectory(prefix="optpilot-assistant-smoke-") as tmp_dir:
        tmp = Path(tmp_dir)
        smoke_study = _smoke_study_file(study_path, tmp, int(arguments.get("max_trials") or 0))
        output_root = tmp / "runs"
        completed = subprocess.run(
            [sys.executable, "-m", "optpilot", "run", str(smoke_study), "--output-root", str(output_root)],
            cwd=str(state.cwd),
            env={**os.environ, "PYTHONPATH": f"{state.cwd}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep)},
            capture_output=True,
            text=True,
            timeout=min(max(int(arguments.get("timeout_seconds") or 120), 10), 300),
            check=False,
        )
        run_dirs = _find_run_dirs([output_root])
        detail = _run_detail(run_dirs[0]) if run_dirs else {}
        return _tool_result(
            tool,
            completed.returncode == 0,
            "Smoke test completed." if completed.returncode == 0 else "Smoke test failed.",
            data={
                "validation": validation,
                "returncode": completed.returncode,
                "stdout": _cap_text(completed.stdout, 12000),
                "stderr": _cap_text(completed.stderr, 12000),
                "summary": detail.get("summary", {}),
                "run": detail.get("run", {}),
            },
        )


def _smoke_study_file(study_path: Path, tmp: Path, max_trials: int) -> Path:
    raw = _read_yaml(study_path)
    if max_trials > 0:
        raw.setdefault("budget", {})["maxTrials"] = max_trials
    for key in ("environmentConfig", "methodConfig"):
        if raw.get(key):
            raw[key] = str(_resolve_config_path(raw[key], study_path))
    output = tmp / study_path.name
    output.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return output


def _run_compare_summary(run: JsonDict) -> JsonDict:
    summary = run.get("summary", {}) if isinstance(run.get("summary"), dict) else {}
    info = run.get("run", {}) if isinstance(run.get("run"), dict) else {}
    return {
        "id": info.get("id"),
        "path": info.get("path"),
        "name": info.get("name"),
        "status": info.get("status"),
        "environment_id": info.get("environment_id"),
        "method": info.get("method"),
        "objective": info.get("objective"),
        "completed_trials": info.get("completed_trials"),
        "best_metric": summary.get("best_metric", info.get("best_metric")),
        "best_trial_id": summary.get("best_trial_id", info.get("best_trial_id")),
        "failure_count": summary.get("failure_count", info.get("failure_count")),
    }


def _runs_comparable(runs: List[JsonDict]) -> JsonDict:
    if len(runs) < 2:
        return {"compatible": True, "caveats": ["Only one run was provided."]}
    summaries = [_run_compare_summary(run) for run in runs]
    envs = {str(item.get("environment_id") or "") for item in summaries}
    objectives = {json.dumps(item.get("objective") or {}, sort_keys=True) for item in summaries}
    caveats = []
    if len(envs) > 1:
        caveats.append("Runs use different environment ids.")
    if len(objectives) > 1:
        caveats.append("Runs use different objective settings.")
    return {"compatible": not caveats, "caveats": caveats}


def _docs_search(state: UiState, query: str, *, limit: int = 5) -> List[JsonDict]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    studio_package_root = _package_dir("optpilot_studio")
    core_package_root = _package_dir("optpilot")
    roots = [
        state.cwd / "docs",
        state.cwd / ".agents" / "optpilot-assistant",
        state.cwd / "src" / "optpilot" / "schemas",
    ]
    if studio_package_root is not None:
        roots.extend([studio_package_root / "docs_assets", studio_package_root / "assistant_assets"])
    if core_package_root is not None:
        roots.append(core_package_root / "schemas")
    matches: List[JsonDict] = []
    suffixes = {".md", ".yaml", ".yml", ".json", ".py"}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if any(part in EXCLUDED_SCAN_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower = text.lower()
            score = sum(lower.count(term) for term in terms)
            if score <= 0:
                continue
            line_number, snippet = _snippet_for_terms(text, terms)
            matches.append(
                {
                    "path": str(path.relative_to(state.cwd) if _is_relative_to(path, state.cwd) else path),
                    "line": line_number,
                    "score": score,
                    "snippet": snippet,
                }
            )
    return sorted(matches, key=lambda item: (-int(item["score"]), str(item["path"])))[: max(1, min(limit, 20))]


def _snippet_for_terms(text: str, terms: List[str]) -> tuple[int, str]:
    lines = text.splitlines()
    for index, line in enumerate(lines, start=1):
        lower = line.lower()
        if any(term in lower for term in terms):
            start = max(index - 2, 1)
            end = min(index + 2, len(lines))
            snippet = "\n".join(lines[start - 1 : end])
            return index, _cap_text(snippet, 1200)
    return 1, _cap_text(text, 1200)


def _cap_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... truncated ..."


def _append_jsonl(path: Path, record: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _agent_session_operation_lock(state: UiState, session_id: str) -> threading.Lock:
    with state._agent_session_locks_lock:
        lock = state.agent_session_locks.get(session_id)
        if lock is None:
            lock = threading.RLock()
            state.agent_session_locks[session_id] = lock
        return lock


def _append_agent_event_record(state: UiState, session_id: str, event: JsonDict) -> None:
    event_id = str(event.get("id") or "")
    path = _agent_events_path(state, session_id)
    if event_id and any(str(existing.get("id") or "") == event_id for existing in _read_agent_events(state, session_id)):
        return
    _append_jsonl(
        path,
        {
            "id": event_id or f"evt_{uuid.uuid4().hex[:10]}",
            "type": str(event.get("type") or "openhands_event"),
            "created_at": event.get("created_at") or _now_iso(),
            "payload": event.get("payload", {}),
        },
    )


def _agent_session_payload(state: UiState, session: JsonDict) -> JsonDict:
    payload = dict(session)
    session_id = str(session["id"])
    lock = _agent_session_operation_lock(state, session_id)
    lock_acquired = lock.acquire(blocking=False)
    try:
        if lock_acquired:
            _recover_agent_assistant_messages_from_events(state, session_id)
        messages = _read_agent_messages(state, session_id)
    finally:
        if lock_acquired:
            lock.release()
    if not messages:
        messages = [_default_agent_message()]
        _append_jsonl(_agent_messages_path(state, session_id), messages[0])
    payload["messages"] = messages
    payload["events"] = _read_agent_events(state, session_id)
    payload["approvals"] = _read_agent_approvals(state, session_id)
    return payload


def _list_agent_sessions(state: UiState) -> List[JsonDict]:
    sessions = _read_agent_session_index(state)
    if not sessions:
        sessions = [_create_agent_session(state, {"title": "Main Session", "description": "General OptPilot work"})]
    known_workspaces = {str(workspace["id"]) for workspace in _read_workspace_index(state)}
    changed = False
    normalized = []
    for session in sessions:
        session = dict(session)
        attached = [item for item in session.get("attached_workspace_ids", []) if item in known_workspaces]
        if attached != session.get("attached_workspace_ids", []):
            session["attached_workspace_ids"] = attached
            if session.get("selected_workspace_id") not in attached:
                session["selected_workspace_id"] = ""
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
        "selected_workspace_id": str(payload.get("selected_workspace_id") or ""),
        "openhands_conversation_id": str(payload.get("openhands_conversation_id") or ""),
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
    if _workspace_should_remove_after_last_detach(state, workspace):
        _remove_ui_workspace_reference(state, workspace_id)
        return
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
        session["selected_workspace_id"] = ""
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
    session.pop("active_turn_id", None)
    session.pop("active_turn_started_at", None)
    return _upsert_agent_session(state, session)


def _cancel_agent_session(state: UiState, session_id: str) -> JsonDict:
    remote_cancel_scheduled = False
    with _agent_session_operation_lock(state, session_id):
        session = _require_agent_session(state, session_id)
        conversation_id = str(session.get("openhands_conversation_id") or "")
        turn_id = str(session.get("active_turn_id") or "")
        cancelled_at = _now_iso()
        cancel_conversation = getattr(state.agent_adapter, "cancel_conversation", None)
        remote_cancel_scheduled = bool(conversation_id and callable(cancel_conversation))
        session["status"] = "idle"
        session["cancelled_at"] = cancelled_at
        if turn_id:
            session["cancelled_turn_id"] = turn_id
        if conversation_id:
            session["cancelled_openhands_conversation_id"] = conversation_id
        session.pop("active_turn_id", None)
        session.pop("active_turn_started_at", None)
        session.pop("openhands_pending_sync", None)
        _append_agent_event_record(
            state,
            session_id,
            {
                "id": f"evt_{uuid.uuid4().hex[:10]}",
                "type": "openhands_dispatch_cancelled",
                "created_at": cancelled_at,
                "payload": {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "remote_cancel_scheduled": remote_cancel_scheduled,
                    "remote_cancelled": False,
                    "remote_action": "",
                    "remote_error": "" if remote_cancel_scheduled else "No active OpenHands conversation.",
                },
            },
        )
        updated = _upsert_agent_session(state, session)
    if remote_cancel_scheduled:
        _schedule_openhands_cancel(state, session_id, conversation_id, turn_id)
    return updated


def _schedule_openhands_cancel(state: UiState, session_id: str, conversation_id: str, turn_id: str) -> None:
    def worker() -> None:
        cancel_conversation = getattr(state.agent_adapter, "cancel_conversation", None)
        if not callable(cancel_conversation):
            return
        event_type = "openhands_cancel_acknowledged"
        try:
            cancel_result = cancel_conversation(conversation_id)
            if not cancel_result.get("cancelled"):
                event_type = "openhands_cancel_failed"
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            cancel_result = {"cancelled": False, "conversation_id": conversation_id, "error": str(exc)}
            event_type = "openhands_cancel_failed"
        with _agent_session_operation_lock(state, session_id):
            _append_agent_event_record(
                state,
                session_id,
                {
                    "id": f"evt_{uuid.uuid4().hex[:10]}",
                    "type": event_type,
                    "created_at": _now_iso(),
                    "payload": {
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                        "remote_cancelled": bool(cancel_result.get("cancelled")),
                        "remote_action": cancel_result.get("action") or "",
                        "remote_error": cancel_result.get("error") or cancel_result.get("reason") or "",
                    },
                },
            )

    threading.Thread(
        target=worker,
        name=f"optpilot-openhands-cancel-{session_id}",
        daemon=True,
    ).start()


def _cancelled_agent_turn_session(
    state: UiState,
    session_id: str,
    *,
    turn_id: str,
    conversation_id: str,
    turn_started_at: str,
) -> Optional[JsonDict]:
    latest = _require_agent_session(state, session_id)
    cancelled_turn_id = str(latest.get("cancelled_turn_id") or "")
    if turn_id and cancelled_turn_id == turn_id:
        return latest
    cancelled_conversation_id = str(latest.get("cancelled_openhands_conversation_id") or "")
    cancelled_at = str(latest.get("cancelled_at") or "")
    if (
        conversation_id
        and cancelled_conversation_id == conversation_id
        and cancelled_at
        and turn_started_at
        and cancelled_at >= turn_started_at
    ):
        return latest
    return None


def _append_agent_assistant_message_if_new(
    state: UiState,
    session_id: str,
    *,
    content: Any,
    title: str = "Assistant",
    context: Optional[JsonDict] = None,
    dispatch: Optional[JsonDict] = None,
) -> bool:
    assistant_content = _user_facing_openhands_text(content)
    normalized = _normalize_agent_text(assistant_content)
    if not assistant_content or not normalized:
        return False
    existing_texts = {
        _normalize_agent_text(str(message.get("content") or ""))
        for message in _read_agent_messages(state, session_id)
        if message.get("role") == "assistant" and str(message.get("content") or "").strip()
    }
    if normalized in existing_texts:
        return False
    source, memory_scope = _agent_assistant_message_origin(dispatch)
    message: JsonDict = {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "role": "assistant",
        "title": title or "Assistant",
        "content": assistant_content,
        "created_at": _now_iso(),
        "source": source,
        "memory_scope": memory_scope,
    }
    if context is not None:
        message["context"] = context
    if dispatch is not None:
        message["dispatch"] = dispatch
    _append_jsonl(_agent_messages_path(state, session_id), message)
    return True


def _agent_assistant_message_origin(dispatch: Optional[JsonDict]) -> tuple[str, str]:
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    status = str(dispatch.get("status") or "")
    transport = str(dispatch.get("transport") or dispatch.get("dispatch") or "")
    conversation_id = str(dispatch.get("conversation_id") or "")
    if status in {"queued", "failed"}:
        return "studio_system", "ui_history"
    if conversation_id and "openhands" in transport:
        return "openhands", "openhands_conversation"
    if transport:
        return "model_chat", "stateless_model"
    return "assistant", "ui_history"


def _append_agent_message(state: UiState, session_id: str, payload: JsonDict) -> JsonDict:
    session = _require_agent_session(state, session_id)
    role = str(payload.get("role") or "user")
    content = str(payload.get("content") or payload.get("message") or "")
    title = str(payload.get("title") or ("User" if role == "user" else "Assistant"))
    source = str(payload.get("source") or ("user" if role == "user" else "studio_ui"))
    memory_scope = str(
        payload.get("memory_scope")
        or ("openhands_conversation" if role == "user" or source == "openhands" else "ui_history")
    )
    ui_context = payload.get("ui_context") if isinstance(payload.get("ui_context"), dict) else {}
    if not content:
        raise ValueError("Message content is required.")
    context = _agent_context_packet(state, session, ui_context)
    message = {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "role": role,
        "title": title,
        "content": content,
        "created_at": _now_iso(),
        "source": source,
        "memory_scope": memory_scope,
        "context": context,
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
        turn_id = f"turn_{uuid.uuid4().hex[:10]}"
        turn_started_at = str(message["created_at"])
        session["status"] = "running"
        session["active_turn_id"] = turn_id
        session["active_turn_started_at"] = turn_started_at
        session.pop("openhands_pending_sync", None)
        _upsert_agent_session(state, session)
        _append_agent_event_record(
            state,
            session_id,
            {
                "id": f"evt_{uuid.uuid4().hex[:10]}",
                "type": "openhands_dispatch_started",
                "created_at": _now_iso(),
                "payload": {
                    "dispatch": state.agent_adapter.status().get("dispatch"),
                    "conversation_id": str(session.get("openhands_conversation_id") or ""),
                    "turn_id": turn_id,
                },
            },
        )
        dispatch = state.agent_adapter.dispatch_message(
            message=content,
            context=context,
            conversation_id=str(session.get("openhands_conversation_id") or "") or None,
            tool_executor=lambda tool_name, arguments: _execute_agent_tool(state, session_id, tool_name, arguments),
            ignored_response_texts=_assistant_response_texts(state, session_id),
        )
        conversation_id = str(dispatch.get("conversation_id") or "")
        if conversation_id:
            session["openhands_conversation_id"] = conversation_id
        cancelled_session = _cancelled_agent_turn_session(
            state,
            session_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            turn_started_at=turn_started_at,
        )
        if cancelled_session:
            return {"session": _agent_session_payload(state, cancelled_session), "message": message}
        sync_state = dispatch.get("sync_state") if isinstance(dispatch.get("sync_state"), dict) else {}
        if sync_state:
            session["openhands_pending_sync"] = sync_state
        for event in dispatch.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            _append_agent_event_record(state, session_id, event)
        raw_assistant = dispatch.get("assistant_message") if isinstance(dispatch.get("assistant_message"), dict) else {}
        _append_agent_assistant_message_if_new(
            state,
            session_id,
            content=raw_assistant.get("content"),
            title=str(raw_assistant.get("title") or "Assistant"),
            context=context,
            dispatch={
                "status": dispatch.get("status"),
                "mode": dispatch.get("mode"),
                "transport": dispatch.get("dispatch"),
                "conversation_id": conversation_id,
            },
        )
        dispatch_status = str(dispatch.get("status") or "")
        if dispatch_status in {"answered", "dispatched"}:
            session["status"] = "idle"
            session.pop("active_turn_id", None)
            session.pop("active_turn_started_at", None)
            session.pop("openhands_pending_sync", None)
        elif dispatch_status == "failed":
            session["status"] = "error"
            session.pop("active_turn_id", None)
            session.pop("active_turn_started_at", None)
            session.pop("openhands_pending_sync", None)
        else:
            session["status"] = "waiting_for_agent"
    else:
        session["status"] = session.get("status", "idle")
    updated = _upsert_agent_session(state, session)
    return {"session": updated, "message": message}


def _sync_agent_session(state: UiState, session_id: str) -> JsonDict:
    sync_started_at = _now_iso()
    session = _require_agent_session(state, session_id)
    conversation_id = str(session.get("openhands_conversation_id") or "")
    turn_id = str(session.get("active_turn_id") or "")
    turn_started_at = str(session.get("active_turn_started_at") or sync_started_at)
    if not conversation_id or session.get("status") not in {"waiting_for_agent", "running"}:
        return _agent_session_payload(state, session)
    handled_tool_calls = _handled_optpilot_tool_call_ids(state, session_id)
    pending_sync = session.get("openhands_pending_sync") if isinstance(session.get("openhands_pending_sync"), dict) else {}
    ignored_event_ids = {
        str(event_id)
        for event_id in pending_sync.get("ignored_event_ids", [])
        if event_id
    }
    ignored_response_texts = {
        str(text)
        for text in pending_sync.get("ignored_response_texts", [])
        if text
    }
    ignored_response_texts.update(_assistant_response_texts(state, session_id))
    dispatch = state.agent_adapter.sync_conversation(
        conversation_id,
        tool_executor=lambda tool_name, arguments: _execute_agent_tool(state, session_id, tool_name, arguments),
        ignored_tool_calls=handled_tool_calls,
        ignored_event_ids=ignored_event_ids,
        ignored_response_texts=ignored_response_texts,
        allow_final_response_fallback=bool(pending_sync.get("allow_final_response_fallback")),
        poll_seconds=3.0,
    )
    with _agent_session_operation_lock(state, session_id):
        cancelled_session = _cancelled_agent_turn_session(
            state,
            session_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            turn_started_at=turn_started_at,
        )
        if cancelled_session:
            return _agent_session_payload(state, cancelled_session)
        latest_session = _require_agent_session(state, session_id)
        latest_turn_id = str(latest_session.get("active_turn_id") or "")
        latest_conversation_id = str(latest_session.get("openhands_conversation_id") or "")
        if (
            latest_session.get("status") not in {"waiting_for_agent", "running"}
            or latest_turn_id != turn_id
            or latest_conversation_id != conversation_id
        ):
            return _agent_session_payload(state, latest_session)
        session = latest_session
        for event in dispatch.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            _append_agent_event_record(state, session_id, event)
        raw_assistant = dispatch.get("assistant_message") if isinstance(dispatch.get("assistant_message"), dict) else {}
        if _append_agent_assistant_message_if_new(
            state,
            session_id,
            content=raw_assistant.get("content"),
            title=str(raw_assistant.get("title") or "Assistant"),
            dispatch={
                "status": dispatch.get("status"),
                "transport": "openhands_http",
                "conversation_id": conversation_id,
            },
        ):
            session["status"] = "idle"
            session.pop("active_turn_id", None)
            session.pop("active_turn_started_at", None)
            session.pop("openhands_pending_sync", None)
        elif dispatch.get("status") == "failed":
            session["status"] = "error"
            session.pop("active_turn_id", None)
            session.pop("active_turn_started_at", None)
            session.pop("openhands_pending_sync", None)
        else:
            session["status"] = "waiting_for_agent"
            sync_state = dispatch.get("sync_state") if isinstance(dispatch.get("sync_state"), dict) else {}
            if sync_state:
                session["openhands_pending_sync"] = sync_state
        return _upsert_agent_session(state, session)


def _assistant_response_texts(state: UiState, session_id: str) -> set[str]:
    return {
        str(message.get("content") or "").strip()
        for message in _read_agent_messages(state, session_id)
        if (
            message.get("role") == "assistant"
            and _agent_message_source(message) == "openhands"
            and str(message.get("content") or "").strip()
        )
    }


def _agent_message_source(message: JsonDict) -> str:
    source = str(message.get("source") or "")
    if source:
        return source
    role = str(message.get("role") or "user")
    if role == "user":
        return "user"
    title = str(message.get("title") or "")
    dispatch = message.get("dispatch") if isinstance(message.get("dispatch"), dict) else {}
    if role == "assistant" and (title == "OpenHands" or dispatch.get("conversation_id")):
        return "openhands"
    if role == "assistant" and title == "Assistant" and dispatch.get("transport"):
        return "model_chat"
    return "studio_ui"


def _handled_optpilot_tool_call_ids(state: UiState, session_id: str) -> set[str]:
    handled = set()
    for event in _read_agent_events(state, session_id):
        if event.get("type") != "optpilot_tool_result":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        call_id = str(payload.get("tool_call_id") or "")
        if call_id:
            handled.add(call_id)
    return handled


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
    catalog = _catalog_payload(state)
    current_page = _assistant_page_name(ui_context.get("current_page"))
    assistant_mode = str(ui_context.get("assistant_mode") or "chat")
    selected_workspace = None
    ui_selected_workspace = ui_context.get("selected_workspace") if isinstance(ui_context.get("selected_workspace"), dict) else None
    ui_selected_workspace_id = str((ui_selected_workspace or {}).get("id") or "")
    if current_page == "editor" and ui_selected_workspace_id:
        selected_workspace = next((item for item in attached if item["id"] == ui_selected_workspace_id), None)
    selected_catalog_entry = ui_context.get("selected_catalog_entry") if current_page == "catalog" and isinstance(ui_context.get("selected_catalog_entry"), dict) else None
    selected_study_plan = ui_context.get("selected_study_plan") if current_page == "studies" and isinstance(ui_context.get("selected_study_plan"), dict) else None
    selected_run = ui_context.get("selected_run") if current_page == "runs" and isinstance(ui_context.get("selected_run"), dict) else None
    registration_menu = ui_context.get("registration_menu") if assistant_mode == "registration" and isinstance(ui_context.get("registration_menu"), dict) else None
    code_editor = ui_context.get("code_editor") if current_page == "editor" and isinstance(ui_context.get("code_editor"), dict) else None
    workspace_preview = ui_context.get("workspace_preview") if current_page == "editor" and isinstance(ui_context.get("workspace_preview"), dict) else None
    return state.agent_adapter.context_packet(
        session_id=str(session.get("id") or ""),
        selected_workspace=selected_workspace,
        attached_workspaces=attached,
        catalog_counts={
            "environments": len(catalog["environments"]),
            "methods": len(catalog["methods"]),
            "resources": len(catalog.get("resources", [])),
        },
        study_plan_count=len(catalog["studies"]),
        run_count=len(_list_runs(state)),
        current_page=current_page,
        registration_menu=registration_menu,
        selected_catalog_entry=selected_catalog_entry,
        selected_study_plan=selected_study_plan,
        selected_run=selected_run,
        code_editor=code_editor,
        workspace_preview=workspace_preview,
        visible_state={
            key: value
            for key, value in ui_context.items()
            if key not in {"current_page", "registration_menu", "selected_workspace", "selected_catalog_entry", "selected_study_plan", "selected_run", "code_editor", "workspace_preview"}
        },
        assistant_capabilities=_assistant_capability_summary(state),
    )


def _assistant_page_name(value: Any) -> str:
    page = str(value or "editor")
    return {
        "workspace": "editor",
        "experiments": "studies",
    }.get(page, page)


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
    cleaned = [_stored_workspace_record(item) for item in workspaces]
    payload = {"workspaces": sorted(cleaned, key=lambda item: item.get("updated_at", ""), reverse=True)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _stored_workspace_record(workspace: JsonDict) -> JsonDict:
    return {key: value for key, value in dict(workspace).items() if key not in DERIVED_WORKSPACE_FIELDS}


def _workspace_attachment_map(state: UiState) -> Dict[str, List[str]]:
    attachments: Dict[str, List[str]] = {}
    for session in _read_agent_session_index(state):
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        for workspace_id in session.get("attached_workspace_ids", []) or []:
            workspace_key = str(workspace_id)
            if workspace_key:
                attachments.setdefault(workspace_key, []).append(session_id)
    return {key: sorted(set(value)) for key, value in attachments.items()}


def _decorate_ui_workspace(state: UiState, workspace: JsonDict) -> JsonDict:
    item = dict(workspace)
    item["ownership"] = str(item.get("ownership") or _workspace_ownership(state, item))
    item["managed_by_studio"] = item["ownership"] == "studio-owned"
    item["delete_action"] = _workspace_delete_action(state, item)
    item["delete_label"] = _workspace_delete_label(item, item["delete_action"])
    item["runtime"] = _workspace_runtime_status(state, item)
    return item


def _workspace_ownership(state: UiState, workspace: JsonDict) -> str:
    root = Path(str(workspace.get("root") or "")).resolve()
    workspace_id = str(workspace.get("id") or "")
    source_type = str(workspace.get("source_type") or "")
    mode = str(workspace.get("mode") or "editable")
    if workspace_id and _is_managed_draft_root(state, workspace_id, root):
        return "studio-owned"
    if source_type == "run" or mode == "analysis":
        return "run-artifact"
    if source_type == "catalog" or mode == "read-only":
        return "catalog-asset"
    return "external-reference"


def _workspace_delete_action(state: UiState, workspace: JsonDict) -> str:
    if _workspace_ownership(state, workspace) == "studio-owned":
        return "delete_draft"
    return "remove_reference"


def _workspace_delete_label(workspace: JsonDict, action: str) -> str:
    if action != "delete_draft":
        return "Remove From Studio"
    if str(workspace.get("source_type") or "") == "catalog-copy":
        return "Delete Copy"
    return "Delete Draft"


def _workspace_runtime_status(state: UiState, workspace: JsonDict) -> JsonDict:
    return state.workspace_runtime.status(workspace)


def _list_ui_workspaces(state: UiState) -> List[JsonDict]:
    workspaces = []
    returned = []
    changed = False
    attachment_map = _workspace_attachment_map(state)
    for workspace in _read_workspace_index(state):
        workspace = dict(workspace)
        root = Path(str(workspace["root"]))
        if not root.exists():
            workspace["status"] = "missing"
            changed = True
        attached_sessions = attachment_map.get(str(workspace.get("id") or ""), [])
        if workspace.get("attached_sessions", []) != attached_sessions:
            workspace["attached_sessions"] = attached_sessions
            changed = True
        if _workspace_should_remove_after_last_detach(state, workspace, keep_recent=True):
            state.workspace_runtime.delete(str(workspace.get("id") or ""))
            changed = True
            continue
        workspaces.append(workspace)
        returned.append(_decorate_ui_workspace(state, workspace))
    if changed:
        _write_workspace_index(state, workspaces)
    return returned


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
    workspace = _stored_workspace_record(workspace)
    workspace["updated_at"] = _now_iso()
    workspaces.append(workspace)
    _write_workspace_index(state, workspaces)
    return _decorate_ui_workspace(state, workspace)


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
        "setup": dict(payload.get("setup", {}) or {}),
        "validation": dict(payload.get("validation", {}) or {}),
        "source_path": str(payload.get("source_path") or ""),
        "source_root": str(payload.get("source_root") or payload.get("root") or ""),
        "created_at": str(payload.get("created_at") or _now_iso()),
    }
    workspace["ownership"] = str(payload.get("ownership") or _workspace_ownership(state, workspace))
    return _upsert_ui_workspace(state, workspace)


def _detach_workspace(state: UiState, workspace_id: str, session_id: str) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    if session_id:
        sessions = []
        for session in _read_agent_session_index(state):
            if session.get("id") == session_id:
                session = dict(session)
                attached = [item for item in session.get("attached_workspace_ids", []) if item != workspace_id]
                session["attached_workspace_ids"] = attached
                if session.get("selected_workspace_id") == workspace_id:
                    session["selected_workspace_id"] = ""
                session["updated_at"] = _now_iso()
            sessions.append(session)
        _write_agent_session_index(state, sessions)
    workspace["attached_sessions"] = _workspace_attachment_map(state).get(workspace_id, [])
    if _workspace_should_remove_after_last_detach(state, workspace):
        return _remove_ui_workspace_reference(state, workspace_id)
    return _upsert_ui_workspace(state, workspace)


def _workspace_should_remove_after_last_detach(state: UiState, workspace: JsonDict, *, keep_recent: bool = False) -> bool:
    if workspace.get("attached_sessions"):
        return False
    if str(workspace.get("mode") or "") not in {"read-only", "analysis"}:
        return False
    if keep_recent and _workspace_created_within(workspace, READ_ONLY_WORKSPACE_PRUNE_GRACE_SECONDS):
        return False
    return _workspace_ownership(state, workspace) in {"catalog-asset", "run-artifact"}


def _workspace_created_within(workspace: JsonDict, seconds: int) -> bool:
    created_at = str(workspace.get("created_at") or "")
    if not created_at:
        return False
    try:
        created_epoch = calendar.timegm(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return False
    return time.time() - created_epoch < seconds


def _remove_ui_workspace_reference(state: UiState, workspace_id: str) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    sessions = _read_agent_session_index(state)
    changed_sessions = []
    for session in sessions:
        attached = [item for item in session.get("attached_workspace_ids", []) if item != workspace_id]
        if attached != session.get("attached_workspace_ids", []):
            session = dict(session)
            session["attached_workspace_ids"] = attached
            if session.get("selected_workspace_id") == workspace_id:
                session["selected_workspace_id"] = ""
            session["updated_at"] = _now_iso()
        changed_sessions.append(session)
    _write_agent_session_index(state, changed_sessions)
    _write_workspace_index(state, [item for item in _read_workspace_index(state) if item.get("id") != workspace_id])
    runtime_deleted = state.workspace_runtime.delete(workspace_id)
    workspace = dict(workspace)
    workspace["deleted"] = True
    workspace["files_deleted"] = False
    workspace["runtime_deleted"] = runtime_deleted
    workspace["delete_action"] = "remove_reference"
    workspace["delete_label"] = _workspace_delete_label(workspace, workspace["delete_action"])
    return workspace


def _rename_ui_workspace(state: UiState, workspace_id: str, title: str) -> JsonDict:
    cleaned = " ".join(str(title or "").strip().split())
    if not cleaned:
        raise ValueError("Workspace name cannot be empty.")
    if len(cleaned) > 120:
        raise ValueError("Workspace name must be 120 characters or fewer.")
    workspace = _require_ui_workspace(state, workspace_id)
    workspace["title"] = cleaned
    return _upsert_ui_workspace(state, workspace)


def _delete_ui_workspace(state: UiState, workspace_id: str) -> JsonDict:
    workspace = _require_ui_workspace(state, workspace_id)
    if str(workspace.get("mode") or "editable") not in {"editable"}:
        raise ValueError("Only editable workspaces can be removed from Studio.")
    sessions = _read_agent_session_index(state)
    changed_sessions = []
    for session in sessions:
        attached = [item for item in session.get("attached_workspace_ids", []) if item != workspace_id]
        if attached != session.get("attached_workspace_ids", []):
            session = dict(session)
            session["attached_workspace_ids"] = attached
            if session.get("selected_workspace_id") == workspace_id:
                session["selected_workspace_id"] = ""
            session["updated_at"] = _now_iso()
        changed_sessions.append(session)
    _write_agent_session_index(state, changed_sessions)
    workspaces = [item for item in _read_workspace_index(state) if item.get("id") != workspace_id]
    _write_workspace_index(state, workspaces)
    root = Path(str(workspace["root"])).resolve()
    files_deleted = False
    delete_error = ""
    if _is_managed_draft_root(state, workspace_id, root):
        try:
            shutil.rmtree(root.parent)
        except Exception as exc:
            delete_error = str(exc)
        files_deleted = not root.parent.exists()
    runtime_deleted = state.workspace_runtime.delete(workspace_id)
    workspace = dict(workspace)
    workspace["deleted"] = True
    workspace["files_deleted"] = files_deleted
    workspace["runtime_deleted"] = runtime_deleted
    if delete_error:
        workspace["delete_error"] = delete_error
    workspace["delete_action"] = "delete_draft" if files_deleted else "remove_reference"
    workspace["delete_label"] = _workspace_delete_label(workspace, workspace["delete_action"])
    return workspace


def _is_managed_draft_root(state: UiState, workspace_id: str, root: Path) -> bool:
    expected = (state.workspaces_dir / workspace_id / "workspace").resolve()
    return root == expected and _is_relative_to(root, state.workspaces_dir.resolve())


def _component_setup_specs(raw: JsonDict, *, interface: Optional[JsonDict] = None) -> List[tuple[str, JsonDict]]:
    specs: List[tuple[str, JsonDict]] = []
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    runtime_setup = runtime.get("setup") if isinstance(runtime.get("setup"), dict) else None
    if runtime_setup:
        specs.append(("Runtime setup", runtime_setup))
    interface_setup = interface.get("setup") if isinstance(interface, dict) and isinstance(interface.get("setup"), dict) else None
    if interface_setup:
        specs.append(("Interface setup", interface_setup))
    return specs


def _run_component_setup_in_workspace_runtime(
    state: UiState,
    workspace: JsonDict,
    raw: JsonDict,
    root: Path,
    interface: JsonDict,
    report: Any,
) -> JsonDict:
    results = []
    for label, setup in _component_setup_specs(raw, interface=interface):
        timeout = int(setup.get("timeoutSeconds", 600) or 600)
        base_env = _workspace_runtime_setup_env(state, setup)
        step_results = []
        for index, step in enumerate(setup.get("steps") or []):
            for command in setup_commands_for_step(step, root):
                cwd = setup_cwd(step, root)
                env = dict(base_env)
                env.update({str(key): str(value) for key, value in (step.get("env") or {}).items()})
                report(
                    f"{label} {index + 1}",
                    " ".join(command),
                )
                completed, _runtime = state.workspace_runtime.exec(
                    workspace,
                    command,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                )
                step_result = {
                    "command": command,
                    "cwd": str(cwd),
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:] if completed.stdout else "",
                    "stderr": completed.stderr[-4000:] if completed.stderr else "",
                }
                step_results.append(step_result)
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"{label} step {index + 1} failed with exit code {completed.returncode}: "
                        f"{completed.stderr.strip() or completed.stdout.strip()}"
                    )
        results.append({"label": label, "ran": True, "steps": step_results})
    return {"ran": bool(results), "results": results}


def _normalize_component_config_override(kind: str, config_override: Optional[JsonDict]) -> Optional[JsonDict]:
    if not config_override:
        return None
    if not isinstance(config_override, dict):
        raise ValueError("config must be an object.")
    raw = deepcopy(config_override)
    if raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise ValueError(f"config.apiVersion must be {AUTHORING_API_VERSION}.")
    if raw.get("config") != kind:
        raise ValueError(f"config.config must be {kind}.")
    return raw


def _write_component_config_copy(kind: str, target: Path, raw: JsonDict) -> JsonDict:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    if kind == "study":
        return _validate_study(target)
    return validate_authoring_config(target)


def _resource_config_target(entry: JsonDict, original_root: Path, copied_root: Path) -> Path:
    source_value = entry.get("config_path")
    if source_value:
        source_path = Path(str(source_value)).resolve()
        try:
            return copied_root / source_path.relative_to(original_root.resolve())
        except ValueError:
            return copied_root / source_path.name
    return copied_root / "optpilot.resource.yaml"


def _workspace_runtime_setup_env(state: UiState, setup: JsonDict) -> Dict[str, str]:
    env = _require_declared_env_from_host(
        state,
        setup.get("envFromHost", []) or [],
        action="component setup",
    )
    env.update({str(key): str(value) for key, value in (setup.get("env") or {}).items()})
    return env


def _workspace_source_root(workspace: JsonDict) -> Path:
    return Path(str(workspace.get("source_root") or workspace.get("root") or ".")).resolve()


def _catalog_component_source_root(kind: str, config_path: Path, raw: JsonDict) -> Path:
    if kind == "study":
        return config_path.parent.resolve()
    refs: List[str] = []
    hints: List[Path] = []
    if kind == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        for key in ("python", "adapter"):
            if evaluator.get(key):
                refs.append(str(evaluator[key]))
        hints.extend(_resolve_public_hint_path(path, config_path) for path in evaluator.get("pythonPath", []) or [])
    elif kind == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        if entrypoint.get("python"):
            refs.append(str(entrypoint["python"]))
        hints.extend(_resolve_public_hint_path(path, config_path) for path in entrypoint.get("pythonPath", []) or [])
    return _choose_source_root(config_path, refs, hints)


def _resolve_public_hint_path(value: Any, config_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config_path.parent / path).resolve()


def _copy_catalog_source_to_workspace(source_root: Path, workspace_root: Path) -> Path:
    source_root = source_root.resolve()
    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    if (source_root / "__init__.py").exists():
        workspace_root.mkdir(parents=True, exist_ok=True)
        copied_source_root = workspace_root / source_root.name
        shutil.copytree(source_root, copied_source_root, ignore=_copy_ignore)
        return copied_source_root.resolve()
    shutil.copytree(source_root, workspace_root, ignore=_copy_ignore)
    return workspace_root.resolve()


def _open_catalog_workspace(
    state: UiState,
    kind: str,
    uid: str,
    *,
    editable: bool,
    title_prefix: str = "Edit",
    install: bool = False,
    config_override: Optional[JsonDict] = None,
) -> JsonDict:
    override_raw = _normalize_component_config_override(kind, config_override)
    if kind == "resource":
        resource_root = _decode_id(uid).resolve()
        if not resource_root.exists() or not resource_root.is_dir():
            raise FileNotFoundError(f"resource not found: {resource_root}")
        entry = _resource_catalog_entry(resource_root)
        raw_for_workspace = override_raw or deepcopy(entry.get("raw_config") or {})
        label = str(entry.get("label") or resource_root.name)
        if editable:
            workspace_id = f"ws_{uuid.uuid4().hex[:10]}"
            root = state.workspaces_dir / workspace_id / "workspace"
            shutil.copytree(resource_root, root, ignore=_copy_ignore)
            source_root = root
            mode = "editable"
            source_type = "catalog-copy"
            title = f"{title_prefix} {label}"
            copied_config_path = _resource_config_target(entry, resource_root, source_root)
        else:
            workspace_id = f"ws_{slug_path(resource_root)}_resource"
            root = resource_root
            source_root = resource_root
            mode = "read-only"
            source_type = "catalog"
            title = f"Inspect {label}"
            copied_config_path = _resource_config_target(entry, resource_root, source_root)
        validation: JsonDict = {}
        if editable and raw_for_workspace:
            validation = _write_component_config_copy("resource", copied_config_path, raw_for_workspace)
        readme = entry.get("summary", {}).get("readme") or "README.md"
        workspace = _create_ui_workspace(
            state,
            {
                "id": workspace_id,
                "title": title,
                "root": str(root),
                "source_root": str(source_root),
                "source_type": source_type,
                "mode": mode,
                "description": "resource catalog entry",
                "source_path": str(resource_root),
                "registered_entries": [{
                    "kind": "resource",
                    "id": str(entry.get("id") or resource_root.name),
                    "config_path": _relative_path(copied_config_path, root) if editable else "",
                    "source_config_path": str(resource_root),
                }],
                "focus_paths": [readme] if (root / readme).exists() else ["README.md"],
                "registration_enabled": editable,
                "validation": validation,
            },
        )
        if editable and install:
            interface = _normalize_interface_config(raw_for_workspace.get("interface"))
            if validation and not validation.get("valid"):
                setup_result = {"ran": False, "skipped": True, "reason": "Config validation failed.", "validation": validation}
            else:
                setup_result = _run_component_setup_in_workspace_runtime(state, workspace, raw_for_workspace, source_root, interface, lambda *_args, **_kwargs: None)
            workspace = dict(_require_ui_workspace(state, str(workspace["id"])))
            workspace["setup"] = setup_result
            workspace = _upsert_ui_workspace(state, workspace)
        return workspace
    config_path = _decode_id(uid)
    raw = _read_yaml(config_path)
    if raw.get("config") != kind or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{kind} config not found: {config_path}")
    raw_for_workspace = override_raw or deepcopy(raw)
    label = str(raw.get("name") or raw.get("id") or config_path.stem)
    original_source_root = _catalog_component_source_root(kind, config_path, raw)
    if editable:
        workspace_id = f"ws_{uuid.uuid4().hex[:10]}"
        root = state.workspaces_dir / workspace_id / "workspace"
        source_root = _copy_catalog_source_to_workspace(original_source_root, root)
        mode = "editable"
        source_type = "catalog-copy"
        title = f"{title_prefix} {label}"
    else:
        root = original_source_root
        source_root = original_source_root
        mode = "read-only"
        source_type = "catalog"
        title = f"Inspect {label}"
    copied_config_path = source_root / config_path.relative_to(original_source_root)
    validation: JsonDict = {}
    if editable and override_raw is not None:
        validation = _write_component_config_copy(kind, copied_config_path, raw_for_workspace)
    focus_paths = _focus_paths_for_config(root, copied_config_path if editable else config_path, raw)
    registered_entry = {
        "kind": kind,
        "id": str(raw_for_workspace.get("id") or raw_for_workspace.get("name") or config_path.stem),
        "config_path": _relative_path(copied_config_path if editable else config_path, root),
        "source_config_path": str(config_path),
    }
    workspace = _create_ui_workspace(
        state,
        {
            "id": workspace_id if editable else f"ws_{slug_path(original_source_root)}_{kind}_{slug_path(config_path)}",
            "title": title,
            "root": str(root),
            "source_root": str(source_root),
            "source_type": source_type,
            "mode": mode,
            "description": f"{kind} catalog entry",
            "source_path": str(config_path),
            "registered_entries": [registered_entry],
            "focus_paths": focus_paths,
            "registration_enabled": editable or mode != "read-only",
            "validation": validation,
        },
    )
    if editable and install:
        if validation and not validation.get("valid"):
            setup_result = {"ran": False, "skipped": True, "reason": "Config validation failed.", "validation": validation}
        else:
            setup_result = _run_component_setup_in_workspace_runtime(state, workspace, raw_for_workspace, source_root, {}, lambda *_args, **_kwargs: None)
        workspace = dict(_require_ui_workspace(state, str(workspace["id"])))
        workspace["setup"] = setup_result
        workspace = _upsert_ui_workspace(state, workspace)
    return workspace


def _interface_launch_by_id(state: UiState, launch_id: str) -> JsonDict:
    with state._lock:
        job = state.interface_launches.get(launch_id)
    if job is None:
        raise KeyError(launch_id)
    return job.to_dict()


def _start_catalog_interface_launch(state: UiState, kind: str, uid: str, *, config_override: Optional[JsonDict] = None) -> JsonDict:
    if kind == "study":
        raise ValueError("Study configs do not declare launchable interfaces.")
    override_raw = _normalize_component_config_override(kind, config_override)
    interface = _component_interface_for_uid(kind, uid, config_override=override_raw)
    if not interface:
        raise ValueError("This catalog entry does not declare an interface.")
    raw = override_raw or (_read_yaml(_decode_id(uid)) if kind != "resource" else _resource_catalog_entry(_decode_id(uid).resolve()).get("raw_config", {}))
    _require_declared_env_from_host(
        state,
        _interface_launch_env_requirements(raw, interface),
        action="interface launch",
    )
    launch_id = f"launch-{uuid.uuid4().hex[:12]}"
    job = UiLaunchJob(
        launch_id=launch_id,
        kind=kind,
        uid=uid,
        label=str(interface.get("label") or "interface"),
        port=int(interface.get("port") or 0),
    )
    job.steps.append(
        {
            "time": _now_iso(),
            "status": "queued",
            "title": "Queued launch",
            "detail": "Preparing to create an editable workspace and start the declared interface.",
        }
    )
    with state._lock:
        state.interface_launches[launch_id] = job
    threading.Thread(target=_run_catalog_interface_launch, args=(state, launch_id, kind, uid, override_raw), daemon=True).start()
    return {"launch": job.to_dict()}


def _run_catalog_interface_launch(state: UiState, launch_id: str, kind: str, uid: str, config_override: Optional[JsonDict] = None) -> None:
    try:
        result = _launch_catalog_interface(
            state,
            kind,
            uid,
            config_override=config_override,
            progress=lambda title, detail="", status="running", data=None: _record_interface_launch_step(
                state,
                launch_id,
                title,
                detail,
                status=status,
                data=data,
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive background boundary
        with state._lock:
            job = state.interface_launches.get(launch_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = time.time()
                job.finished_at = time.time()
                job.steps.append(
                    {
                        "time": _now_iso(),
                        "status": "failed",
                        "title": "Launch failed",
                        "detail": str(exc),
                    }
                )
                job.steps = job.steps[-80:]
        return
    with state._lock:
        job = state.interface_launches.get(launch_id)
        if job is not None:
            job.status = "ready"
            job.result = result
            job.updated_at = time.time()
            job.finished_at = time.time()
            job.steps.append(
                {
                    "time": _now_iso(),
                    "status": "ready",
                    "title": "Preview ready",
                    "detail": f"Interface is reachable on port {job.port}.",
                }
            )
            job.steps = job.steps[-80:]


def _record_interface_launch_step(
    state: UiState,
    launch_id: str,
    title: str,
    detail: str = "",
    *,
    status: str = "running",
    data: Optional[JsonDict] = None,
) -> None:
    with state._lock:
        job = state.interface_launches.get(launch_id)
        if job is None:
            return
        if job.status in {"queued", "running"}:
            job.status = "running" if status != "failed" else "failed"
        if data:
            stdout_log = str(data.get("stdout_log") or "")
            stderr_log = str(data.get("stderr_log") or "")
            if stdout_log:
                job.log_paths["stdout"] = stdout_log
            if stderr_log:
                job.log_paths["stderr"] = stderr_log
        job.updated_at = time.time()
        step: JsonDict = {
            "time": _now_iso(),
            "status": status,
            "title": title,
        }
        if detail:
            step["detail"] = detail
        job.steps.append(step)
        job.steps = job.steps[-80:]


def _launch_log_tail(log_paths: JsonDict, *, max_chars: int = 4000) -> JsonDict:
    logs: JsonDict = {}
    for name in ("stdout", "stderr"):
        path = str(log_paths.get(name) or "")
        if not path:
            continue
        log_path = Path(path)
        if not log_path.exists() or not log_path.is_file():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text:
            logs[name] = text[-max_chars:]
    return logs


def _launch_catalog_interface(state: UiState, kind: str, uid: str, progress: Optional[Any] = None, config_override: Optional[JsonDict] = None) -> JsonDict:
    def report(title: str, detail: str = "", status: str = "running", data: Optional[JsonDict] = None) -> None:
        if progress:
            progress(title, detail, status, data)

    if kind == "study":
        raise ValueError("Study configs do not declare launchable interfaces.")
    override_raw = _normalize_component_config_override(kind, config_override)
    report("Reading interface config", f"Loading {kind} interface declaration.")
    interface = _component_interface_for_uid(kind, uid, config_override=override_raw)
    if not interface:
        raise ValueError("This catalog entry does not declare an interface.")
    report("Creating editable workspace", "Copying the catalog entry into a draft workspace.")
    workspace = _open_catalog_workspace(state, kind, uid, editable=True, title_prefix="Launch", config_override=override_raw)
    validation = workspace.get("validation") if isinstance(workspace.get("validation"), dict) else {}
    if validation and not validation.get("valid"):
        raise ValueError("Edited config validation failed: " + "; ".join(str(error) for error in validation.get("errors", []) or ["invalid config"]))
    root = Path(str(workspace["root"])).resolve()
    source_root = _workspace_source_root(workspace)
    raw = override_raw or (_read_yaml(_decode_id(uid)) if kind != "resource" else _resource_catalog_entry(_decode_id(uid).resolve()).get("raw_config", {}))
    cwd = (source_root / str(interface.get("cwd") or ".")).resolve()
    if not _is_relative_to(cwd, source_root):
        raise ValueError("Interface cwd must stay inside the launched workspace copy.")
    port = int(interface.get("port") or 0)
    command = list(interface.get("command") or [])
    env = {
        str(key): str(value)
        for key, value in (interface.get("env") or {}).items()
        if str(key)
    }
    env.setdefault("HOST", "0.0.0.0")
    env.setdefault("PORT", str(port))
    env["OPTPILOT_INTERFACE_PORT"] = str(port)
    env["OPTPILOT_WORKSPACE_ROOT"] = str(root)
    env.update(
        _require_declared_env_from_host(
            state,
            interface.get("envFromHost") or [],
            action="interface launch",
        )
    )
    report(
        "Starting workspace runtime",
        "Ensuring the per-workspace container is running, then starting the interface command.",
    )
    setup_result = _run_component_setup_in_workspace_runtime(state, workspace, raw, source_root, interface, report)
    launch = state.workspace_runtime.exec_detached(
        workspace,
        command,
        cwd=cwd,
        env=env,
        name="interface",
    )
    report("Launch command started", "Interface stdout and stderr are being captured.", data=launch)
    preview = state.workspace_preview_open(root, port, extra_ports=interface.get("extraPorts") or [])
    report("Opening preview proxy", f"Routing workspace port {port} through Studio Preview.")
    report(
        "Waiting for preview port",
        f"Checking {interface.get('readyPath') or '/'} until the interface becomes reachable.",
    )
    readiness = _wait_for_preview_ready(
        str(preview.get("proxy_target") or ""),
        str(interface.get("readyPath") or "/"),
        int(interface.get("readyTimeoutSeconds") or 0),
    )
    preview["readiness"] = readiness
    if not readiness.get("ready") and not readiness.get("skipped"):
        raise ValueError(
            "Interface started but did not become reachable "
            f"on port {port} within {readiness.get('timeoutSeconds')}s: "
            f"{readiness.get('error') or readiness.get('url')}"
        )
    return {
        "workspace": workspace,
        "interface": _interface_summary(interface),
        "setup": setup_result,
        "launch": launch,
        "preview": preview,
    }


def _catalog_interface_for_uid(kind: str, uid: str) -> JsonDict:
    return _component_interface_for_uid(kind, uid)


def _component_interface_for_uid(kind: str, uid: str, *, config_override: Optional[JsonDict] = None) -> JsonDict:
    if config_override is not None:
        return _normalize_interface_config(config_override.get("interface"))
    if kind == "resource":
        resource_root = _decode_id(uid).resolve()
        if not resource_root.exists() or not resource_root.is_dir():
            raise FileNotFoundError(f"resource not found: {resource_root}")
        return dict(_resource_catalog_entry(resource_root).get("interface") or {})
    config_path = _decode_id(uid)
    raw = _read_yaml(config_path)
    if raw.get("config") != kind or raw.get("apiVersion") != AUTHORING_API_VERSION:
        raise FileNotFoundError(f"{kind} config not found: {config_path}")
    return _normalize_interface_config(raw.get("interface"))


def _open_run_workspace(state: UiState, run_dir: Path) -> JsonDict:
    summary = _run_summary(run_dir, state)
    workspace_id = f"ws_run_{slug_path(run_dir)}_{_path_hash(run_dir)}"
    return _create_ui_workspace(
        state,
        {
            "id": workspace_id,
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
        if config not in REGISTERABLE_CONFIGS or raw.get("apiVersion") != AUTHORING_API_VERSION:
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
    if str(payload.get("kind") or payload.get("registration_kind") or "") == "resource" or payload.get("resource_id"):
        return _create_resource_registration_manifest(state, workspace, root, payload)
    discovered = _discover_workspace_configs(state, workspace_id)["configs"]
    requested_paths = {str(item) for item in payload.get("config_paths", []) or []}
    selected = [item for item in discovered if not requested_paths or item["relative_path"] in requested_paths or item["path"] in requested_paths]
    if not selected:
        raise ValueError("No environment or method config files selected for registration.")
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


def _create_resource_registration_manifest(state: UiState, workspace: JsonDict, root: Path, payload: JsonDict) -> JsonDict:
    registration_id = str(payload.get("id") or f"reg_{uuid.uuid4().hex[:10]}")
    resource_id = _slug_text(str(payload.get("resource_id") or workspace.get("title") or workspace["id"]))
    include = [str(item) for item in payload.get("include", []) or [] if str(item).strip()]
    if not include:
        include = _default_resource_registration_include(root)
    target = {
        "target_id": f"target_{uuid.uuid4().hex[:8]}",
        "kind": "resource",
        "config_path": "",
        "catalog_id": resource_id,
        "destination": str(_local_catalog_package_root(state) / "resources" / resource_id),
        "focus_paths": [item for item in ("README.md", "readme.md") if (root / item).exists()],
        "include": include,
        "exclude": [".git/**", ".venv/**", "runs/**", "__pycache__/**", "node_modules/**"],
        "validation": {
            "valid": True,
            "warnings": [],
            "errors": [],
            "description": str(payload.get("description") or workspace.get("description") or ""),
        },
    }
    manifest = {
        "id": registration_id,
        "workspace_id": workspace["id"],
        "status": "draft",
        "root": str(root),
        "targets": [target],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _write_registration_manifest(state, str(workspace["id"]), manifest)
    return {"registration": manifest}


def _default_resource_registration_include(root: Path) -> List[str]:
    include: List[str] = []
    for item in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if item.name in EXCLUDED_SCAN_DIRS or item.name in {".DS_Store"}:
            continue
        include.append(f"{item.name}/**" if item.is_dir() else item.name)
    return include or ["README.md"]


def _validate_registration_manifest(state: UiState, workspace_id: str, registration_id: str) -> JsonDict:
    manifest = _read_registration_manifest(state, workspace_id, registration_id)
    root = Path(str(manifest["root"])).resolve()
    all_valid = True
    for target in manifest.get("targets", []):
        if target.get("kind") == "resource":
            matched = []
            for pattern in target.get("include", []):
                matched.extend(_match_workspace_pattern(root, pattern))
            validation = {
                "valid": bool(matched),
                "errors": [] if matched else ["No files matched the resource registration manifest."],
                "warnings": [],
                "file_count": len({str(path) for path in matched}),
            }
        else:
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
                "config_path": str(destination / Path(target["config_path"]).name) if target.get("config_path") else "",
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


def _path_hash(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8", errors="replace")).hexdigest()[:10]


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


def _is_local_catalog_path(state: UiState, path: Path) -> bool:
    return _is_relative_to(path.resolve(), _local_catalog_package_root(state).resolve())


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
        return _local_catalog_package_root(state) / "environments" / safe_id
    if kind == "method":
        return _local_catalog_package_root(state) / "methods" / safe_id
    raise ValueError(f"{kind} configs are not catalog registration targets.")


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
    local_catalog = _local_catalog_package_root(state).resolve()
    if not _is_relative_to(destination.resolve(), local_catalog):
        raise PermissionError("Registration can only write into catalog/local_package.")


def _local_catalog_package_root(state: UiState) -> Path:
    return state.cwd / CATALOG_DIR_NAME / LOCAL_PACKAGE_NAME


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
    return _dedupe_paths([path for path in roots if path.exists()])


def _default_catalog_roots(cwd: Path) -> List[Path]:
    catalog_root = cwd / CATALOG_DIR_NAME
    if catalog_root.exists():
        package_roots = _expand_catalog_roots([catalog_root])
        if package_roots:
            return package_roots
    return [cwd]


def _refresh_catalog_package_roots(state: UiState) -> None:
    catalog_root = state.cwd / CATALOG_DIR_NAME
    if not catalog_root.exists():
        return
    state.catalog_roots = _dedupe_paths([*state.catalog_roots, *_expand_catalog_roots([catalog_root])])


def _expand_catalog_roots(roots: Iterable[Path]) -> List[Path]:
    expanded: List[Path] = []
    for root in roots:
        root = root.resolve()
        packages = _catalog_package_roots(root)
        if packages and not _looks_like_catalog_package(root):
            expanded.extend(packages)
        else:
            expanded.append(root)
    return _dedupe_paths(expanded)


def _catalog_package_roots(catalog_root: Path) -> List[Path]:
    if _looks_like_catalog_package(catalog_root):
        return [catalog_root]
    if not catalog_root.exists() or not catalog_root.is_dir():
        return []
    return sorted(
        path
        for path in catalog_root.iterdir()
        if path.is_dir() and _looks_like_catalog_package(path)
    )


def _looks_like_catalog_package(path: Path) -> bool:
    return any((path / name).exists() for name in CATALOG_PACKAGE_DIRS)


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
