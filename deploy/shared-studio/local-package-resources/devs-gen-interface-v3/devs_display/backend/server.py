import base64
import json
import mimetypes
import os
import re
import inspect
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import hashlib
import io
import zipfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from queue import Queue
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from smolagents import CodeAgent, ToolCallingAgent
import uvicorn

from .routes import create_app
from .schemas import CloneProjectSpec
from .graph_parser import (
    FRONTEND_MODEL_PRESETS,
    build_project_graph,
    has_devs_project_marker,
    local_parse_xdevs_structure,
    parse_model_for_visualizer as parse_model_for_visualizer_impl,
)
from .interface_outputs import InterfaceOutputPublisher, stable_tree_digest
from .collector_reporting import CollectorReporter
from .remote_finalizer import RemoteCodexFinalizerClient
from .simulation_execution import (
    assess_behavior_smoke,
    ExecutionStateError,
    SimulationBundleError,
    SimulationCapacityError,
    SimulationExecutionError,
    SimulationExecutionService,
    SimulationManifestError,
    ensure_simulation_manifest,
    simulation_command_arguments,
    simulation_metadata,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.generated_interface import (
    refresh_generated_interface_registry,
)
from devs_tools.devs_construct_recon.json_retry_guidance import (
    append_retry_guidance,
    json_retry_guidance,
)
from devs_settings import (
    first_preset_model,
    max_active_generations,
    openrouter_api_key,
    visualizer_model_id,
)

load_dotenv(override=False)

META_DIR_NAME = ".devs_display_sessions"
DEVS_INTERFACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_RUNTIME_ROOT = os.path.abspath(
    os.getenv(
        "OPTPILOT_INTERFACE_RUNTIME_ROOT",
        os.path.join(DEVS_INTERFACE_ROOT, ".runtime"),
    )
)
DEFAULT_REGISTRY_PATH = os.getenv(
    "DEVS_DISPLAY_REGISTRY_PATH",
    os.path.join(DEFAULT_RUNTIME_ROOT, "session-registry.json"),
)
DEFAULT_WORKING_DIRS_ROOT = os.getenv(
    "DEVS_DISPLAY_WORKING_DIRS_ROOT",
    os.path.join(DEFAULT_RUNTIME_ROOT, "working-dirs"),
)
DEFAULT_GRAPH_PARSE_MODEL = visualizer_model_id()


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


DEFAULT_DATA_MAX_BYTES = max(0, _env_int("DEVS_INTERFACE_DATA_MAX_BYTES", 0))
MAX_UPLOAD_FILES = 2_000
MAX_UPLOAD_FILE_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 128 * 1024 * 1024
MAX_UPLOAD_PATH_BYTES = 1_024
MAX_PROJECT_DOWNLOAD_FILES = 20_000
MAX_PROJECT_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_PROJECT_DOWNLOAD_FILE_BYTES = 128 * 1024 * 1024
MAX_PROJECT_DIRECTORY_NAME_BYTES = 160
MAX_SIMULATION_RESULT_PREVIEW_BYTES = 512 * 1024
MAX_SIMULATION_RESULT_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_ACTIVITY_FILE_CHANGES = 24
MAX_ACTIVITY_FILE_PREVIEW_BYTES = 512 * 1024
MAX_ACTIVITY_PROJECT_FILES = 512
MAX_ACTIVITY_PROJECT_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_INTERACTION_TEXT_BYTES = 32 * 1024
MAX_HISTORY_USER_MESSAGE_BYTES = 20 * 1024
MAX_HISTORY_ASSISTANT_MESSAGE_BYTES = 3 * 1024
MAX_ANALYTICS_EVENT_TYPE_BYTES = 128
MAX_ANALYTICS_PAYLOAD_BYTES = 32 * 1024
MAX_EVALUATION_COMMENTS_BYTES = 8 * 1024
GENERATION_MODES = frozenset({"automatic", "guided"})
REQUEST_PHASES = frozenset({"interpret_intent", "plan_structure", "build"})
ACTIVE_SESSION_STATUSES = frozenset(
    {"queued", "running", "waiting_for_user", "cancelling"}
)
TERMINAL_REQUEST_STATUSES = frozenset(
    {"completed", "failed", "cancelled"}
)
_ACTIVITY_FILE_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".text",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_ACTIVITY_INTERNAL_PARTS = {
    META_DIR_NAME,
    "__pycache__",
    "_analysis_logs",
    "logs",
}
_SIMULATION_RESULT_TEXT_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".text",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

_AUTOMATIC_REPAIR_FAILURE_KINDS = frozenset(
    {
        "acceptance_failed",
        "behavior_stalled",
        "invalid_bundle",
        "missing_result",
        "nonzero_exit",
        "output_limit",
        "result_limit",
        "timeout",
    }
)
_EXECUTION_INFRASTRUCTURE_FAILURE_KINDS = frozenset(
    {
        "capacity_timeout",
        "execution_boundary",
        "execution_error",
        "launch_error",
    }
)

_RECOVERY_BASELINE_SCHEMA = "devs.request-workspace-baseline.v1"
_MAX_RECOVERY_BASELINE_ROOTS = 2_048
_SENSITIVE_ENV_NAME_PARTS = (
    "api_key",
    "apikey",
    "auth",
    "credential",
    "password",
    "secret",
    "token",
)
_REPAIR_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|secret|token)"
    r"(\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_REPAIR_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

# Public agent activity is backend-owned copy. Reporter callsites choose only
# the semantic key and state; arbitrary model/tool text never crosses into the
# student-facing event stream.
_PUBLIC_AGENT_ACTIVITY: Dict[str, Dict[str, tuple[str, str]]] = {
    "understand_request": {
        "started": (
            "Understanding your request",
            "Interpreting the simulation goals and constraints.",
        ),
        "progress": (
            "Reviewing simulation requirements",
            "Identifying the model scope and expected behavior.",
        ),
        "completed": (
            "Simulation requirements understood",
            "The requested model scope is ready for planning.",
        ),
        "failed": (
            "Simulation requirements need attention",
            "The generator could not finish interpreting the request.",
        ),
    },
    "plan_structure": {
        "started": (
            "Planning the model structure",
            "Defining the component hierarchy and responsibilities.",
        ),
        "progress": (
            "Detailing the component hierarchy",
            "Organizing components and their responsibilities for review.",
        ),
        "completed": (
            "Model structure ready to review",
            "The component hierarchy is ready for confirmation.",
        ),
        "failed": (
            "Model planning stopped",
            "The component hierarchy could not be completed.",
        ),
    },
    "detail_architecture": {
        "started": (
            "Detailing the approved architecture",
            "Defining internal ports, protocols, and couplings before code generation.",
        ),
        "progress": (
            "Detailing the approved architecture",
            "Expanding the confirmed hierarchy into an implementation plan.",
        ),
        "completed": (
            "Approved architecture detailed",
            "The confirmed hierarchy is ready for code generation.",
        ),
        "failed": (
            "Architecture detailing stopped",
            "Implementation details could not be derived from the approved hierarchy.",
        ),
    },
    "generate_components": {
        "started": (
            "Generating component code",
            "Creating the planned DEVS components.",
        ),
        "progress": (
            "Generating component code",
            "Another planned component is ready.",
        ),
        "completed": (
            "Component code generated",
            "The planned DEVS components have been implemented.",
        ),
        "failed": (
            "Component generation stopped",
            "Not all planned components could be generated.",
        ),
    },
    "verify_model": {
        "started": (
            "Checking model behavior",
            "Reviewing the generated component hierarchy and behavior.",
        ),
        "progress": (
            "Checking model behavior",
            "The internal model check is still running.",
        ),
        "completed": (
            "Model behavior checked",
            "The generated hierarchy passed its internal behavior check.",
        ),
        "failed": (
            "Model check needs attention",
            "The generated behavior did not pass the internal check.",
        ),
    },
    "create_runner": {
        "started": (
            "Creating a runnable simulation",
            "Generating the entry point and scenario runner.",
        ),
        "progress": (
            "Creating a runnable simulation",
            "The simulation entry point is being assembled.",
        ),
        "completed": (
            "Runnable simulation created",
            "The entry point and scenario runner are ready for testing.",
        ),
        "failed": (
            "Simulation runner could not be created",
            "The generated model files remain available for inspection.",
        ),
    },
    "package_simulation": {
        "started": (
            "Preparing simulation files",
            "Adding documentation and organizing the generated result.",
        ),
        "progress": (
            "Preparing simulation files",
            "The generated files are being organized.",
        ),
        "completed": (
            "Simulation files prepared",
            "Source, runner, and documentation have been assembled.",
        ),
        "failed": (
            "Simulation files could not be prepared",
            "The files already created remain available for inspection.",
        ),
    },
    "build_simulation": {
        "started": (
            "Building the simulation",
            "Creating the model structure and implementation.",
        ),
        "progress": (
            "Building the simulation",
            "The generator is working through the planned build stages.",
        ),
        "completed": (
            "Simulation build completed",
            "The model, runner, and supporting files have been generated.",
        ),
        "failed": (
            "Simulation generation encountered a problem",
            "The completed activity history and generated files were retained.",
        ),
    },
    "agent_test_simulation": {
        "started": (
            "Testing the generated simulation",
            "Running a bounded copy in the prepared runtime.",
        ),
        "progress": (
            "Testing the generated simulation",
            "The generated simulator is executing in the prepared runtime.",
        ),
        "completed": (
            "Generated simulation ran successfully",
            "The agent's bounded execution check completed successfully.",
        ),
        "failed": (
            "Automatic repair is in progress",
            "The execution check found a problem and the agent is continuing with a repair.",
        ),
    },
    "agent_update_files": {
        "progress": (
            "Updating simulation files",
            "The agent is revising the generated implementation.",
        ),
    },
    "agent_inspect_files": {
        "progress": (
            "Inspecting simulation files",
            "The agent is reviewing the current implementation.",
        ),
    },
    "codex_finalizer": {
        "started": (
            "Automatic check is reviewing the simulation",
            "The generated bundle is being checked before it is marked ready.",
        ),
        "progress": (
            "Automatic check is repairing the simulation",
            "A bounded final repair is running against the exact generated bundle.",
        ),
        "completed": (
            "Automatic check passed the simulation",
            "The final check marked this exact generated bundle ready.",
        ),
        "failed": (
            "Automatic check did not pass the simulation",
            "The final check found remaining issues in this generated bundle.",
        ),
    },
}

_PUBLIC_AGENT_TECHNICAL_NAMES = {
    "understand_request": "devs_construct_tree",
    "plan_structure": "devs_construct_tree",
    "detail_architecture": "devs_construct_tree",
    "generate_components": "devs_construct_tree",
    "verify_model": "devs_construct_tree",
    "create_runner": "devs_construct_tree",
    "package_simulation": "devs_construct_tree",
    "build_simulation": "devs_construct_tree",
    "agent_test_simulation": "devs_execute",
    "agent_update_files": "file editing",
    "agent_inspect_files": "file inspection",
    "codex_finalizer": "automatic check",
}

_COMPONENT_GENERATION_ACTIVITY_RE = re.compile(
    r"^component_generation:([A-Za-z0-9_-]{1,72})$"
)
_CODEX_FINALIZER_ACTIVITY_RE = re.compile(
    r"^codex_finalizer:([A-Za-z0-9_-]{1,96})$"
)


def _public_agent_activity_copy(
    activity_key: str,
) -> tuple[Optional[Dict[str, tuple[str, str]]], str]:
    """Resolve one reporter key without opening the public activity boundary.

    Static lifecycle keys remain exact allowlist entries.  Component retries
    are the sole dynamic family: the suffix is a short, already-sanitized
    component name and all displayed copy is still created by this backend.
    """

    activity_copy = _PUBLIC_AGENT_ACTIVITY.get(activity_key)
    if activity_copy is not None:
        return activity_copy, _PUBLIC_AGENT_TECHNICAL_NAMES.get(activity_key, "")

    if _CODEX_FINALIZER_ACTIVITY_RE.fullmatch(activity_key):
        return (
            _PUBLIC_AGENT_ACTIVITY["codex_finalizer"],
            _PUBLIC_AGENT_TECHNICAL_NAMES["codex_finalizer"],
        )

    match = _COMPONENT_GENERATION_ACTIVITY_RE.fullmatch(activity_key)
    if match is None:
        return None, ""
    component_name = match.group(1)
    return {
        "progress": (
            f"Generating {component_name}",
            "Creating or correcting this component before continuing the model build.",
        )
    }, "devs_construct_tree"


def _simulation_result_media_type(relative_path: str) -> str:
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or "application/octet-stream"


def _simulation_result_is_previewable(relative_path: str, media_type: str) -> bool:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return (
        suffix in _SIMULATION_RESULT_TEXT_SUFFIXES
        or media_type.startswith("text/")
        or media_type
        in {
            "application/json",
            "application/ld+json",
            "application/xml",
            "application/yaml",
        }
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _canonical_simulation_result_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or len(value.encode("utf-8")) > 1024
    ):
        raise ValueError("Result file path is invalid.")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Result file path is invalid.")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or str(relative) != value
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Result file path must be a canonical relative path.")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_client_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = re.sub(r"[^A-Za-z0-9_-]", "_", str(value).strip())[:120]
    return candidate or None


def safe_project_id(display_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in display_name).strip("_")
    return f"proj_{cleaned or uuid.uuid4().hex[:8]}"


def safe_project_directory_name(display_name: str) -> str:
    """Return a portable directory name derived from a user-facing label."""

    cleaned = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in str(display_name)
    ).strip("._")
    if not cleaned:
        cleaned = "simulation"
    encoded = cleaned.encode("utf-8")
    if len(encoded) > MAX_PROJECT_DIRECTORY_NAME_BYTES:
        encoded = encoded[:MAX_PROJECT_DIRECTORY_NAME_BYTES]
        while encoded:
            try:
                cleaned = encoded.decode("utf-8").rstrip("._")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        if not cleaned:
            cleaned = "simulation"
    return cleaned


def canonical_relative_file_path(value: str) -> PurePosixPath:
    """Validate one portable, non-traversing relative file path."""

    raw = str(value or "")
    if not raw or len(raw.encode("utf-8")) > MAX_UPLOAD_PATH_BYTES:
        raise ValueError("Uploaded file path is empty or too long.")
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized.startswith("/") or ":" in path.parts[0]:
        raise ValueError(f"Uploaded file path must be relative: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Uploaded file path is not canonical: {raw!r}")
    if path.parts[0] == META_DIR_NAME or any(part.startswith(".") for part in path.parts):
        raise ValueError(f"Hidden and backend-owned upload paths are not allowed: {raw!r}")
    return path


def contained_path(root: Path, relative: str | PurePosixPath) -> Path:
    """Resolve a path below ``root`` without accepting links or prefix tricks."""

    resolved_root = root.resolve()
    parts = (
        relative.parts
        if isinstance(relative, PurePosixPath)
        else canonical_relative_file_path(str(relative)).parts
    )
    candidate = resolved_root
    for part in parts:
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Path contains a symbolic-link component.")
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path escapes the session workspace.") from exc
    return candidate


def _assert_regular_source_tree(root: Path) -> None:
    """Reject links and special files before copying a student-provided tree."""

    resolved_root = root.resolve()
    if not resolved_root.is_dir() or root.is_symlink():
        raise ValueError("Simulation source must be a real directory.")
    for current_root, dirs, files in os.walk(resolved_root, followlinks=False):
        current = Path(current_root)
        for name in [*dirs, *files]:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("Simulation source cannot contain symbolic links.")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ValueError("Simulation source cannot contain special files.")


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


class DEVSBackendService:
    def __init__(
        self,
        agent: CodeAgent | ToolCallingAgent,
        working_directory: str,
        start_worker: bool = True,
        registry_path: Optional[str] = None,
        discover_existing: bool = False,
        agent_factory: Optional[Callable[[str], CodeAgent | ToolCallingAgent]] = None,
        worker_count: int = 4,
        interface_output_publisher: Optional[InterfaceOutputPublisher] = None,
        simulation_execution_service: Optional[SimulationExecutionService] = None,
        intent_interpreter: Optional[Callable[..., Any]] = None,
        plan_preparer: Optional[Callable[..., Any]] = None,
        plan_builder: Optional[Callable[..., Any]] = None,
    ):
        self.agent = agent
        self.agent_factory = agent_factory
        self.workspace_agents: Dict[str, CodeAgent | ToolCallingAgent] = {}
        # These optional seams keep the interaction protocol independent from
        # one particular agent or constructor implementation. Production uses
        # the matching capabilities on the workspace agent/tool when present;
        # focused tests and alternative generators can inject the same three
        # side-effect boundaries directly.
        self.intent_interpreter = intent_interpreter
        self.plan_preparer = plan_preparer
        self.plan_builder = plan_builder
        self.working_dir = os.path.abspath(working_directory)
        self.meta_dir = os.path.join(self.working_dir, META_DIR_NAME)
        self.sessions_dir = os.path.join(self.meta_dir, "sessions")
        if registry_path is None and self.working_dir.startswith(os.path.abspath(tempfile.gettempdir()) + os.sep):
            registry_path = os.path.join(self.meta_dir, "session_registry.json")
        self.registry_path = os.path.abspath(registry_path or DEFAULT_REGISTRY_PATH)
        self.data_max_bytes = DEFAULT_DATA_MAX_BYTES
        self.participant_data_max_bytes = max(
            0,
            _env_int("DEVS_DISPLAY_PARTICIPANT_DATA_MAX_BYTES", 0),
        )
        self.session_locations: Dict[str, Dict[str, str]] = {}
        self.lock = Lock()
        # Constructor progress can arrive from parallel component workers.
        # Serialize event id allocation independently from the service lock so
        # activity reporting cannot race or block unrelated state changes.
        self.event_lock = Lock()
        self.analytics_lock = Lock()
        self.event_next_ids: Dict[str, int] = {}
        self.worker_queue: Queue[str] = Queue()
        self.worker_count = max(1, worker_count)
        self.workspace_agents[self.working_dir] = agent
        self.interface_output_publisher = (
            interface_output_publisher
            if interface_output_publisher is not None
            else InterfaceOutputPublisher.from_environment()
        )
        execution_root = Path(self.registry_path).parent / "simulation_executions"
        self.simulation_execution_service = (
            simulation_execution_service
            if simulation_execution_service is not None
            else SimulationExecutionService(execution_root, sys.executable)
        )
        self.simulation_execution_context: Dict[str, Dict[str, Any]] = {}
        self.simulation_execution_threads: Dict[str, Thread] = {}
        self.collector_reporter = CollectorReporter.from_environment(
            self._collector_bundle
        )

        self._ensure_storage()
        if discover_existing:
            self._register_discovered_sessions()
        self._register_workspace_session(self.working_dir)
        self._rebuild_session_locations()
        self._recover_incomplete_requests(requeue=start_worker)
        self._recover_incomplete_validations()

        self.worker_threads: List[Thread] = []
        if start_worker:
            for _ in range(self.worker_count):
                worker_thread = Thread(target=self._worker_loop, daemon=True)
                worker_thread.start()
                self.worker_threads.append(worker_thread)

    def _ensure_storage(self):
        os.makedirs(self.working_dir, exist_ok=True)
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)

    def storage_status(self) -> Dict[str, Any]:
        root = Path(self.registry_path).parent
        used_bytes = 0
        file_count = 0
        directory_count = 0
        if root.exists():
            for current_root, dirs, files in os.walk(root, followlinks=False):
                directory_count += 1
                dirs[:] = [
                    name
                    for name in dirs
                    if not (Path(current_root) / name).is_symlink()
                ]
                for name in files:
                    path = Path(current_root) / name
                    try:
                        metadata = path.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        used_bytes += int(metadata.st_size)
                        file_count += 1
        disk = shutil.disk_usage(root if root.exists() else root.parent)
        return {
            "root": str(root),
            "used_bytes": used_bytes,
            "file_count": file_count,
            "directory_count": directory_count,
            "disk_free_bytes": int(disk.free),
            "disk_total_bytes": int(disk.total),
            "max_bytes": self.data_max_bytes or None,
            "capacity_enforced": self.data_max_bytes > 0,
            "over_capacity": (
                self.data_max_bytes > 0 and used_bytes >= self.data_max_bytes
            ),
        }

    def _ensure_storage_capacity(self, operation: str, estimated_new_bytes: int = 0):
        if self.data_max_bytes <= 0:
            return
        status = self.storage_status()
        projected = int(status["used_bytes"]) + max(0, int(estimated_new_bytes))
        if projected > self.data_max_bytes:
            raise RuntimeError(
                f"DEVS Generator storage limit would be exceeded while {operation}. "
                f"Current usage is {status['used_bytes']} bytes; limit is "
                f"{self.data_max_bytes} bytes."
            )

    def _participant_storage_bytes_unlocked(self, client_id: str) -> int:
        owner_client_id = normalize_client_id(client_id)
        if owner_client_id is None:
            return 0
        workspaces: set[Path] = set()
        for session_id, location in self.session_locations.items():
            try:
                session = self._load_session(session_id)
            except KeyError:
                continue
            if normalize_client_id(session.get("owner_client_id")) == owner_client_id:
                workspaces.add(Path(location["workspace"]))
        return sum(self._tree_regular_file_size(path) for path in workspaces)

    def _ensure_participant_storage_capacity_unlocked(
        self,
        client_id: Optional[str],
        operation: str,
        *,
        estimated_new_bytes: int = 0,
    ) -> None:
        owner_client_id = normalize_client_id(client_id)
        if self.participant_data_max_bytes <= 0 or owner_client_id is None:
            return
        used_bytes = self._participant_storage_bytes_unlocked(owner_client_id)
        projected = used_bytes + max(0, int(estimated_new_bytes))
        if projected > self.participant_data_max_bytes:
            raise RuntimeError(
                f"Your DEVS Generator storage limit would be exceeded while "
                f"{operation}. Current usage is {used_bytes} bytes; limit is "
                f"{self.participant_data_max_bytes} bytes. Download or delete "
                "an older design before continuing."
            )

    def participant_storage_status(self, client_id: str) -> Dict[str, Any]:
        with self.lock:
            used_bytes = self._participant_storage_bytes_unlocked(client_id)
        return {
            "participant_used_bytes": used_bytes,
            "participant_max_bytes": self.participant_data_max_bytes or None,
            "participant_capacity_enforced": self.participant_data_max_bytes > 0,
            "participant_over_capacity": (
                self.participant_data_max_bytes > 0
                and used_bytes >= self.participant_data_max_bytes
            ),
        }

    @staticmethod
    def _tree_regular_file_size(root: Path) -> int:
        total = 0
        for current_root, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not (Path(current_root) / name).is_symlink()
            ]
            for name in files:
                path = Path(current_root) / name
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    total += int(metadata.st_size)
        return total

    def _recover_incomplete_validations(self) -> None:
        """Release projects left blocked by a previous backend process.

        Simulator subprocesses and their in-memory ownership contexts do not
        survive a backend restart.  A persisted ``validating`` label therefore
        cannot become ready on its own and must become an explicit retryable
        state instead of disabling Run forever.
        """

        for session_id in tuple(self.session_locations):
            projects = self._load_projects(session_id)
            changed = False
            for project in projects:
                validation = project.get("validation")
                if not isinstance(validation, dict):
                    continue
                if validation.get("status") != "validating":
                    continue
                project["validation"] = {
                    "status": "stale",
                    "message": (
                        "The previous simulation run was interrupted when the "
                        "interface restarted. Run this version again."
                    ),
                }
                changed = True
            if changed:
                self._save_projects(session_id, projects)

    def _ensure_workspace_storage(self, workspace: str):
        os.makedirs(os.path.join(workspace, META_DIR_NAME, "sessions"), exist_ok=True)

    def _new_session_workspace(self) -> str:
        parent = os.path.dirname(self.working_dir) or DEFAULT_WORKING_DIRS_ROOT
        os.makedirs(parent, exist_ok=True)
        curr_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        return tempfile.mkdtemp(dir=parent, prefix=f"session_workspace_{curr_time}_")

    def _read_registry(self) -> Dict[str, Any]:
        registry = self._read_json(self.registry_path, {"sessions": []})
        registry.setdefault("sessions", [])
        return registry

    def _write_registry(self, registry: Dict[str, Any]):
        self._write_json(self.registry_path, {"sessions": registry.get("sessions", [])})

    def _unique_public_session_id(self, preferred_id: str, workspace: str, storage_id: str, registry: Dict[str, Any]) -> str:
        existing = {
            entry.get("session_id")
            for entry in registry.get("sessions", [])
            if not (
                os.path.abspath(entry.get("workspace_path", entry.get("path", ""))) == os.path.abspath(workspace)
                and entry.get("storage_session_id", entry.get("storage_id")) == storage_id
            )
        }
        if preferred_id not in existing:
            return preferred_id
        candidate = f"sess_{short_hash(os.path.abspath(workspace) + ':' + storage_id)}"
        suffix = 2
        base = candidate
        while candidate in existing:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _registry_entry_for_workspace(self, workspace: str) -> Optional[Dict[str, Any]]:
        workspace = os.path.abspath(workspace)
        registry = self._read_registry()
        return next(
            (
                entry for entry in registry.get("sessions", [])
                if os.path.abspath(entry.get("workspace_path", entry.get("path", ""))) == workspace
            ),
            None,
        )

    def _register_existing_storage_session(self, workspace: str, storage_id: str, registry: Optional[Dict[str, Any]] = None) -> str:
        workspace = os.path.abspath(workspace)
        registry = registry or self._read_registry()
        session_path = os.path.join(workspace, META_DIR_NAME, "sessions", storage_id, "session.json")
        session = self._read_json(session_path, None)
        if not session:
            raise KeyError(storage_id)
        existing = next(
            (
                entry for entry in registry.get("sessions", [])
                if os.path.abspath(entry.get("workspace_path", entry.get("path", ""))) == workspace
                and entry.get("storage_session_id", entry.get("storage_id")) == storage_id
            ),
            None,
        )
        public_id = existing["session_id"] if existing else self._unique_public_session_id(session.get("session_id", storage_id), workspace, storage_id, registry)
        entry = {
            "session_id": public_id,
            "storage_session_id": storage_id,
            "workspace_path": workspace,
            "title": session.get("title") or os.path.basename(workspace),
            "owner_client_id": normalize_client_id(session.get("owner_client_id")),
            "created_at": session.get("created_at") or utc_now(),
            "updated_at": session.get("updated_at") or utc_now(),
            "last_seen_at": utc_now(),
        }
        if existing:
            existing.update(entry)
        else:
            registry.setdefault("sessions", []).append(entry)
        self._write_registry(registry)
        return public_id

    def _register_workspace_session(self, workspace: str) -> str:
        workspace = os.path.abspath(workspace)
        self._ensure_workspace_storage(workspace)
        registry = self._read_registry()
        existing = next(
            (
                entry for entry in registry.get("sessions", [])
                if os.path.abspath(entry.get("workspace_path", "")) == workspace
            ),
            None,
        )
        if existing:
            existing["last_seen_at"] = utc_now()
            self._write_registry(registry)
            self._rebuild_session_locations()
            return existing["session_id"]

        session_id = new_id("sess")
        session_dir = os.path.join(workspace, META_DIR_NAME, "sessions", session_id)
        os.makedirs(session_dir, exist_ok=True)
        session = {
            "session_id": session_id,
            "title": os.path.basename(workspace) or "Session",
            "owner_client_id": None,
            "status": "idle",
            "active_request_id": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "project_count": 0,
        }
        self._write_json(os.path.join(session_dir, "session.json"), session)
        self._write_json(os.path.join(session_dir, "projects.json"), [])
        for path in (
            os.path.join(session_dir, "messages.jsonl"),
            os.path.join(session_dir, "requests.jsonl"),
            os.path.join(session_dir, "events.jsonl"),
            os.path.join(session_dir, "trace.jsonl"),
            os.path.join(session_dir, "evaluations.jsonl"),
        ):
            open(path, "a", encoding="utf-8").close()
        self._register_existing_storage_session(workspace, session_id, registry)
        self._rebuild_session_locations()
        public_id = self._registry_entry_for_workspace(workspace)["session_id"]
        self._sync_session_projects(public_id)
        return public_id

    def _register_discovered_sessions(self):
        return

    def _rebuild_session_locations(self):
        locations: Dict[str, Dict[str, str]] = {}
        registry = self._read_registry()
        for entry in registry.get("sessions", []):
            public_id = entry.get("session_id")
            workspace = os.path.abspath(entry.get("workspace_path", entry.get("path", "")))
            storage_id = entry.get("storage_session_id", entry.get("storage_id"))
            if not public_id or not workspace or not storage_id:
                continue
            session_path = os.path.join(workspace, META_DIR_NAME, "sessions", storage_id, "session.json")
            if os.path.exists(session_path):
                locations[public_id] = {"workspace": workspace, "storage_id": storage_id}
        self.session_locations = locations

    def _session_location(self, session_id: str) -> Dict[str, str]:
        location = self.session_locations.get(session_id)
        if location:
            return location
        raise KeyError(session_id)

    def _session_workspace(self, session_id: str) -> str:
        return self._session_location(session_id)["workspace"]

    def _agent_for_workspace(self, workspace: str) -> CodeAgent | ToolCallingAgent:
        workspace = os.path.abspath(workspace)
        agent = self.workspace_agents.get(workspace)
        if agent:
            return agent
        if not self.agent_factory:
            if workspace == self.working_dir:
                return self.agent
            raise RuntimeError(
                "No agent factory is configured for this session workspace. "
                "Restart the backend through devs_app.run so it can create per-session agents."
            )
        os.makedirs(workspace, exist_ok=True)
        agent = self.agent_factory(workspace)
        self.workspace_agents[workspace] = agent
        return agent

    def _release_session_agent_unlocked(self, session_id: str) -> None:
        """Release an idle per-session agent while retaining durable context."""

        try:
            workspace = self._session_workspace(session_id)
        except KeyError:
            return
        if workspace != self.working_dir:
            self.workspace_agents.pop(workspace, None)

    def _session_dir(self, session_id: str) -> str:
        location = self._session_location(session_id)
        return os.path.join(location["workspace"], META_DIR_NAME, "sessions", location["storage_id"])

    def _session_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "session.json")

    def _projects_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "projects.json")

    def _messages_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "messages.jsonl")

    def _requests_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "requests.jsonl")

    def _events_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "events.jsonl")

    def _trace_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "trace.jsonl")

    def _evaluations_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "evaluations.jsonl")

    def _request_artifacts_dir(self, session_id: str, request_id: str) -> str:
        if not re.fullmatch(r"req_[A-Za-z0-9_-]+", str(request_id)):
            raise KeyError(request_id)
        return os.path.join(
            self._session_dir(session_id), "request_artifacts", request_id
        )

    def _request_artifact_path(
        self, session_id: str, request_id: str, artifact_id: str
    ) -> str:
        if not re.fullmatch(r"artifact_[A-Za-z0-9_-]+", str(artifact_id)):
            raise KeyError(artifact_id)
        return os.path.join(
            self._request_artifacts_dir(session_id, request_id),
            f"{artifact_id}.json",
        )

    def _graph_cache_dir(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "graph_cache")

    def _graph_cache_path(self, session_id: str, project_id: str) -> str:
        return os.path.join(self._graph_cache_dir(session_id), f"{project_id}.json")

    def _delete_graph_cache(self, session_id: str, project_id: str):
        try:
            os.remove(self._graph_cache_path(session_id, project_id))
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[Backend] Failed to delete graph cache for {session_id}/{project_id}: {exc}")

    def _read_json(self, path: str, default: Any):
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: str, data: Any):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _read_jsonl(self, path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _append_jsonl(self, path: str, row: Dict[str, Any]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @classmethod
    def _bounded_json_payload(
        cls,
        value: Any,
        *,
        limit: int = MAX_ANALYTICS_PAYLOAD_BYTES,
    ) -> Any:
        payload = cls._jsonable(value if value is not None else {})
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= limit:
            return payload
        return {
            "_truncated": True,
            "summary": str(payload)[: max(0, min(4000, limit // 4))],
        }

    def _rewrite_jsonl(self, path: str, rows: List[Dict[str, Any]]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        """Convert a constructor artifact to a durable JSON value.

        Constructor implementations commonly return Pydantic models or nested
        dataclasses. The interaction boundary persists values before asking the
        student, so no in-memory Python object is required to resume after a
        backend restart.
        """

        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return value.as_posix()
        if isinstance(value, dict):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._jsonable(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return cls._jsonable(model_dump(mode="json"))
        as_dict = getattr(value, "__dict__", None)
        if isinstance(as_dict, dict):
            return cls._jsonable(
                {
                    key: item
                    for key, item in as_dict.items()
                    if not str(key).startswith("_")
                }
            )
        return str(value)

    @staticmethod
    def _artifact_digest(data: Any) -> str:
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _save_request_artifact(
        self,
        session_id: str,
        request_id: str,
        *,
        kind: str,
        revision: int,
        data: Any,
        public: Dict[str, Any],
    ) -> Dict[str, Any]:
        artifact_id = new_id("artifact")
        durable_data = self._jsonable(data)
        durable_public = self._jsonable(public)
        record = {
            "artifact_id": artifact_id,
            "request_id": request_id,
            "kind": kind,
            "revision": max(1, int(revision)),
            "created_at": utc_now(),
            "digest": self._artifact_digest(durable_data),
            # The build digest pins the exact private constructor artifact.
            # The review digest separately attests to the bounded projection
            # that was actually shown to the student.
            "review_digest": self._artifact_digest(
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "revision": max(1, int(revision)),
                    "public": durable_public,
                }
            ),
            "public": durable_public,
            "data": durable_data,
        }
        self._write_json(
            self._request_artifact_path(session_id, request_id, artifact_id),
            record,
        )
        return record

    def _load_request_artifact(
        self, session_id: str, request_id: str, artifact_id: str
    ) -> Dict[str, Any]:
        path = self._request_artifact_path(session_id, request_id, artifact_id)
        artifact = self._read_json(path, None)
        if not isinstance(artifact, dict):
            raise KeyError(artifact_id)
        if artifact.get("request_id") != request_id:
            raise KeyError(artifact_id)
        return artifact

    def get_request_artifact(
        self, session_id: str, request_id: str, artifact_id: str
    ) -> Dict[str, Any]:
        with self.lock:
            self._load_session(session_id)
            self._get_request(session_id, request_id)
            artifact = self._load_request_artifact(
                session_id, request_id, artifact_id
            )
            return {
                "artifact_id": artifact["artifact_id"],
                "request_id": request_id,
                "kind": artifact["kind"],
                "revision": artifact["revision"],
                "created_at": artifact["created_at"],
                "digest": artifact.get("review_digest") or artifact["digest"],
                "payload": artifact.get("public") or {},
            }

    def _workspace_recovery_baseline(
        self,
        workspace: str,
        snapshot: Dict[str, float],
    ) -> Dict[str, Any]:
        """Return a bounded durable marker for crash-time project ownership.

        The normal worker discovers project ids after the model call returns.
        A process restart can happen before then, so persist fixed-size hashes
        for the pre-call top-level trees.  Recovery can compare these hashes to
        the retained workspace and claim only roots changed by that request.
        """

        workspace_root = Path(workspace).resolve()
        root_hashers: Dict[str, Any] = {}
        overflow = False
        for path, modified_ns in sorted(snapshot.items()):
            try:
                relative = Path(path).resolve().relative_to(workspace_root)
            except (OSError, ValueError):
                continue
            if not relative.parts or relative.parts[0] == META_DIR_NAME:
                continue
            root_name = relative.parts[0]
            hasher = root_hashers.get(root_name)
            if hasher is None:
                if len(root_hashers) >= _MAX_RECOVERY_BASELINE_ROOTS:
                    overflow = True
                    continue
                hasher = hashlib.sha256()
                root_hashers[root_name] = hasher
            hasher.update(relative.as_posix().encode("utf-8", errors="replace"))
            hasher.update(b"\0")
            hasher.update(str(int(modified_ns)).encode("ascii"))
            hasher.update(b"\n")
        return {
            "schema": _RECOVERY_BASELINE_SCHEMA,
            "root_signatures": {
                name: hasher.hexdigest()
                for name, hasher in sorted(root_hashers.items())
            },
            "overflow": overflow,
        }

    def _recover_interrupted_request_projects_unlocked(
        self,
        session_id: str,
        request: Dict[str, Any],
    ) -> None:
        """Discover files changed before a running worker was interrupted."""

        baseline = request.get("workspace_baseline")
        if not isinstance(baseline, dict):
            return
        if baseline.get("schema") != _RECOVERY_BASELINE_SCHEMA:
            return
        previous = baseline.get("root_signatures")
        if not isinstance(previous, dict):
            return
        current = self._workspace_recovery_baseline(
            self._session_workspace(session_id),
            self._get_snapshot(self._session_workspace(session_id)),
        )
        current_signatures = current.get("root_signatures")
        if not isinstance(current_signatures, dict):
            return
        if baseline.get("overflow") or current.get("overflow"):
            changed_names = set(current_signatures)
            changed_names.update(
                str(project.get("path") or "").replace("\\", "/").split("/")[0]
                for project in self._load_projects(session_id)
                if project.get("path")
            )
        else:
            changed_names = {
                name
                for name in set(previous) | set(current_signatures)
                if previous.get(name) != current_signatures.get(name)
            }
        changed_names.discard("")
        changed_names.discard(META_DIR_NAME)
        if not changed_names:
            return
        recovered_ids = self._sync_changed_projects_unlocked(
            session_id, sorted(changed_names)
        )
        request["updated_project_ids"] = list(
            dict.fromkeys(
                [*(request.get("updated_project_ids") or []), *recovered_ids]
            )
        )
        request["updated_project_names"] = list(
            dict.fromkeys(
                [*(request.get("updated_project_names") or []), *sorted(changed_names)]
            )
        )

    def _recover_incomplete_requests(self, requeue: bool):
        for session_id in list(self.session_locations):
            try:
                session = self._load_session(session_id)
            except KeyError:
                continue
            changed = False
            for request in self._load_requests(session_id):
                if request.get("status") == "waiting_for_user":
                    # A review checkpoint is durable state, not a suspended
                    # worker. Restarts must leave it actionable and must not
                    # replay either planning or code generation.
                    session["status"] = "waiting_for_user"
                    session["active_request_id"] = request["request_id"]
                    changed = True
                    continue
                if request.get("status") == "queued":
                    if requeue and (self._session_workspace(session_id) == self.working_dir or self.agent_factory):
                        self.worker_queue.put(request["request_id"])
                        session["status"] = "queued"
                        session["active_request_id"] = request["request_id"]
                        self._add_event(session_id, request["request_id"], "request_recovered", "Queued request recovered after backend restart.")
                        changed = True
                    elif self._session_workspace(session_id) != self.working_dir:
                        request["status"] = "failed"
                        request["completed_at"] = utc_now()
                        request["error"] = "Backend restarted with a different workspace; this queued request cannot be resumed by the current agent."
                        self._save_request(session_id, request)
                        self._add_event(session_id, request["request_id"], "request_failed", request["error"])
                        if session.get("active_request_id") == request["request_id"]:
                            session["status"] = "failed"
                            session["active_request_id"] = None
                            changed = True
                    continue
                if request.get("status") == "running":
                    phase = request.get("phase", "build")
                    if (
                        phase in {"interpret_intent", "plan_structure"}
                        and requeue
                        and (
                            self._session_workspace(session_id)
                            == self.working_dir
                            or self.agent_factory
                        )
                    ):
                        # Guided preparation has no generated-source side
                        # effects. Replaying only this bounded phase is safe.
                        request["status"] = "queued"
                        request["phase_started_at"] = None
                        self._save_request(session_id, request)
                        self.worker_queue.put(request["request_id"])
                        session["status"] = "queued"
                        session["active_request_id"] = request["request_id"]
                        self._add_event(
                            session_id,
                            request["request_id"],
                            "request_recovered",
                            "Guided review preparation resumed after backend restart.",
                        )
                        changed = True
                        continue
                    self._recover_interrupted_request_projects_unlocked(
                        session_id, request
                    )
                    request["status"] = "failed"
                    request["completed_at"] = utc_now()
                    request["error"] = "Backend restarted while this request was running; the prior worker process cannot be resumed."
                    self._terminalize_unverified_request_projects_unlocked(
                        session_id,
                        request,
                        message=(
                            "Generation was interrupted when the interface restarted. "
                            "The retained files need to be tested again."
                        ),
                        failure_kind="generation_interrupted",
                    )
                    self._save_request(session_id, request)
                    self._add_event(session_id, request["request_id"], "request_failed", request["error"])
                    if session.get("active_request_id") == request["request_id"]:
                        session["status"] = "failed"
                        session["active_request_id"] = None
                        changed = True
            if changed:
                if session.get("status") == "failed":
                    session["status"] = "idle"
                self._save_session(session)

    def _sync_session_projects(self, session_id: str):
        workspace = self._session_workspace(session_id)
        session = self._load_session(session_id)
        generation_active = session.get("status") in ACTIVE_SESSION_STATUSES
        projects, changed = self._canonicalize_project_records(
            self._load_projects(session_id), workspace
        )
        by_path = {p.get("path"): p for p in projects}
        existing_ids = {p["project_id"] for p in projects}
        for rel_path in self._discover_project_rel_paths(workspace=workspace):
            display_name = self._project_display_name(rel_path, workspace)
            project = by_path.get(rel_path)
            if project:
                if project.get("display_name") != display_name:
                    project["display_name"] = display_name
                    project["updated_at"] = utc_now()
                    changed = True
                continue
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            existing_ids.add(project_id)
            projects.append(
                self._make_project_record(
                    project_id,
                    display_name,
                    rel_path,
                    "legacy_working_directory",
                    status="updating" if generation_active else "ready",
                )
            )
            by_path[rel_path] = projects[-1]
            changed = True
        if changed:
            self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)

    def _discover_project_rel_paths(self, search_rel: str = "", workspace: Optional[str] = None) -> List[str]:
        workspace_root = Path(workspace or self.working_dir).resolve()
        try:
            search_abs = (
                str(workspace_root)
                if not search_rel
                else str(contained_path(workspace_root, search_rel))
            )
        except ValueError:
            return []
        discovered = set()
        for root, dirs, _files in os.walk(search_abs):
            dirs[:] = [
                d
                for d in dirs
                if d != META_DIR_NAME
                and not d.startswith(".")
                and d != "__pycache__"
                and not (Path(root) / d).is_symlink()
            ]
            if has_devs_project_marker(root):
                marker_rel = os.path.relpath(
                    Path(root).resolve(), workspace_root
                ).replace("\\", "/")
                discovered.add(
                    self._canonical_project_rel_path(marker_rel, str(workspace_root))
                )
                dirs[:] = []
        if not discovered and search_rel and has_devs_project_marker(search_abs):
            discovered.add(
                self._canonical_project_rel_path(
                    search_rel.replace("\\", "/"), str(workspace_root)
                )
            )
        return sorted(discovered)

    def _canonical_project_rel_path(
        self, rel_path: str, workspace: Optional[str] = None
    ) -> str:
        """Return the stable generated-simulation bundle root.

        The constructor writes analysis metadata under ``bundle/devs_project``.
        That marker identifies the bundle, not a second simulation.  Older
        records stored the marker directory itself, which also made their
        display names depend on whichever component happened to be generated
        first.  Normalize that layout to ``bundle`` while retaining direct
        marker-root projects.
        """

        normalized = str(PurePosixPath(str(rel_path).replace("\\", "/")))
        if normalized in {"", "."}:
            return normalized
        marker_path = PurePosixPath(normalized)
        if marker_path.name != "devs_project" or marker_path.parent == PurePosixPath("."):
            return normalized

        workspace_root = Path(workspace or self.working_dir)
        try:
            marker_abs = contained_path(workspace_root, normalized)
        except ValueError:
            return normalized
        if marker_abs.is_symlink() or not has_devs_project_marker(str(marker_abs)):
            return normalized
        return str(marker_path.parent)

    def _canonicalize_project_records(
        self,
        projects: List[Dict[str, Any]],
        workspace: Optional[str] = None,
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Migrate nested marker records and remove duplicate bundle entries."""

        canonical_projects: List[Dict[str, Any]] = []
        seen_paths = set()
        changed = False
        for original in projects:
            project = dict(original)
            canonical_path = self._canonical_project_rel_path(
                str(project.get("path") or ""), workspace
            )
            if canonical_path in seen_paths:
                changed = True
                continue
            seen_paths.add(canonical_path)

            display_name = self._project_display_name(canonical_path, workspace)
            if project.get("path") != canonical_path:
                project["path"] = canonical_path
                changed = True
            if project.get("display_name") != display_name:
                project["display_name"] = display_name
                changed = True
            canonical_projects.append(project)
        return canonical_projects, changed

    def _read_text_files_from_abs_path(self, project_path: str) -> Dict[str, str]:
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [
                d
                for d in dirs
                if d != META_DIR_NAME
                and not d.startswith(".")
                and d != "__pycache__"
                and not (Path(root) / d).is_symlink()
            ]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                try:
                    metadata = os.lstat(abs_path)
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        files_data[rel_path] = f.read()
                except Exception:
                    continue
        return files_data

    def _project_display_name(self, rel_path: str, workspace: Optional[str] = None) -> str:
        # A simulation's menu identity is its stable bundle root.  Model classes
        # are evolving contents of that bundle and belong in Structure/Files,
        # not in the simulation selector.
        canonical_path = self._canonical_project_rel_path(rel_path, workspace)
        bundle_name = PurePosixPath(canonical_path).name
        if bundle_name:
            return bundle_name
        return Path(workspace or self.working_dir).name or "simulation"

    def _make_project_record(
        self,
        project_id: str,
        display_name: str,
        rel_path: str,
        source_type: str,
        *,
        status: str = "ready",
    ) -> Dict[str, Any]:
        return {
            "project_id": project_id,
            "display_name": display_name,
            "status": status,
            "version": 1,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "path": rel_path,
            "source": {"type": source_type},
            "validation": {
                "status": "unverified",
                "message": "This simulation has not been test-run yet.",
            },
        }

    def _load_session(self, session_id: str) -> Dict[str, Any]:
        session = self._read_json(self._session_path(session_id), None)
        if not session:
            raise KeyError(session_id)
        location = self._session_location(session_id)
        session = dict(session)
        session["session_id"] = session_id
        session["storage_session_id"] = location["storage_id"]
        session["workspace_path"] = location["workspace"]
        session["is_current_workspace"] = location["workspace"] == self.working_dir
        return session

    def _save_session(self, session: Dict[str, Any]):
        session["updated_at"] = utc_now()
        session_id = session["session_id"]
        location = self._session_location(session_id)
        persisted = dict(session)
        persisted["session_id"] = location["storage_id"]
        persisted.pop("storage_session_id", None)
        persisted.pop("workspace_path", None)
        persisted.pop("is_current_workspace", None)
        self._write_json(self._session_path(session_id), persisted)
        self._update_registry_session_metadata(session_id, persisted)

    def _update_registry_session_metadata(self, session_id: str, session: Dict[str, Any]):
        registry = self._read_registry()
        for entry in registry.get("sessions", []):
            if entry.get("session_id") == session_id:
                entry["title"] = session.get("title", entry.get("title"))
                entry["owner_client_id"] = normalize_client_id(
                    session.get("owner_client_id")
                    or entry.get("owner_client_id")
                )
                entry["updated_at"] = session.get("updated_at", entry.get("updated_at"))
                entry["last_seen_at"] = utc_now()
                break
        self._write_registry(registry)

    def _load_projects(self, session_id: str) -> List[Dict[str, Any]]:
        return self._read_json(self._projects_path(session_id), [])

    def _save_projects(self, session_id: str, projects: List[Dict[str, Any]]):
        self._write_json(self._projects_path(session_id), projects)

    def _update_session_project_count(self, session_id: str):
        session = self._load_session(session_id)
        project_count = len(self._load_projects(session_id))
        if int(session.get("project_count", 0)) == project_count:
            return
        session["project_count"] = project_count
        self._save_session(session)

    def _project_by_id(self, session_id: str, project_id: str) -> Dict[str, Any]:
        for project in self._load_projects(session_id):
            if project["project_id"] == project_id:
                return project
        raise KeyError(project_id)

    def _project_abs_path(self, project: Dict[str, Any], session_id: Optional[str] = None) -> str:
        workspace = Path(
            self._session_workspace(session_id) if session_id else self.working_dir
        )
        return str(contained_path(workspace, str(project["path"])))

    def _next_event_id(self, session_id: str) -> int:
        next_id = self.event_next_ids.get(session_id)
        if next_id is None:
            events = self._read_jsonl(self._events_path(session_id))
            next_id = events[-1]["event_id"] + 1 if events else 1
        self.event_next_ids[session_id] = next_id + 1
        return next_id

    def _add_event(
        self,
        session_id: str,
        request_id: str,
        event_type: str,
        content: str,
        **public_fields: Any,
    ):
        with self.event_lock:
            event = {
                "event_id": self._next_event_id(session_id),
                "session_id": session_id,
                "request_id": request_id,
                "type": event_type,
                "content": content,
                "created_at": utc_now(),
            }
            event.update(public_fields)
            self._append_jsonl(self._events_path(session_id), event)
        return event

    def _record_trace_unlocked(
        self,
        session_id: str,
        event_type: str,
        *,
        actor: str = "system",
        request_id: Optional[str] = None,
        project_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        event_type = self._bounded_interaction_text(
            re.sub(r"[^A-Za-z0-9_.:-]", "_", str(event_type or "")),
            MAX_ANALYTICS_EVENT_TYPE_BYTES,
        )
        if not event_type:
            raise ValueError("Trace event type cannot be empty.")
        actor = actor if actor in {"user", "system", "frontend"} else "system"
        event = {
            "trace_id": new_id("trace"),
            "created_at": utc_now(),
            "session_id": session_id,
            "actor": actor,
            "type": event_type,
            "request_id": request_id,
            "project_id": project_id,
            "payload": self._bounded_json_payload(payload or {}),
        }
        with self.analytics_lock:
            self._append_jsonl(self._trace_path(session_id), event)
        return event

    def record_trace(
        self,
        session_id: str,
        event_type: str,
        *,
        actor: str = "user",
        request_id: Optional[str] = None,
        project_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._load_session(session_id)
        trace = self._record_trace_unlocked(
            session_id,
            event_type,
            actor=actor,
            request_id=request_id,
            project_id=project_id,
            payload=payload,
        )
        self._schedule_collector_sync(session_id)
        return trace

    def list_trace(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> Dict[str, Any]:
        self._load_session(session_id)
        limit = max(1, min(int(limit or 200), 1000))
        with self.analytics_lock:
            rows = self._read_jsonl(self._trace_path(session_id))
        return {"trace": rows[-limit:]}

    def submit_evaluation(
        self,
        session_id: str,
        *,
        stage: str,
        request_id: Optional[str] = None,
        project_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
        scores: Optional[Dict[str, Any]] = None,
        answers: Optional[Dict[str, Any]] = None,
        comments: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._load_session(session_id)
        if stage not in {"intent", "structure", "final", "simulation", "other"}:
            raise ValueError("Unsupported evaluation stage.")
        if stage == "final" and project_id:
            request_id = self._latest_project_request_id(session_id, project_id)
        evaluation_payload = dict(payload or {})
        if stage == "final" and project_id:
            project = self._project_by_id(session_id, project_id)
            validation = project.get("validation")
            if not isinstance(validation, dict):
                validation = {}
            evaluation_payload.update(
                {
                    "project_name": project.get("display_name"),
                    "project_status": project.get("status"),
                    "project_version": project.get("version"),
                    "validation_execution_id": validation.get("execution_id"),
                    "bundle_digest": validation.get("bundle_digest"),
                }
            )
        comments = self._bounded_interaction_text(
            comments,
            MAX_EVALUATION_COMMENTS_BYTES,
        )
        with self.analytics_lock:
            if idempotency_key:
                for existing in self._read_jsonl(self._evaluations_path(session_id)):
                    if existing.get("idempotency_key") == idempotency_key:
                        return existing
            evaluation = {
                "evaluation_id": new_id("eval"),
                "created_at": utc_now(),
                "session_id": session_id,
                "stage": stage,
                "request_id": request_id,
                "project_id": project_id,
                "interaction_id": interaction_id,
                "scores": self._bounded_json_payload(scores or {}),
                "answers": self._bounded_json_payload(answers or {}),
                "comments": comments or None,
                "payload": self._bounded_json_payload(evaluation_payload),
                "idempotency_key": idempotency_key,
            }
            self._append_jsonl(self._evaluations_path(session_id), evaluation)
        self._record_trace_unlocked(
            session_id,
            "evaluation_submitted",
            actor="user",
            request_id=request_id,
            project_id=project_id,
            payload={
                "stage": stage,
                "interaction_id": interaction_id,
                "scores": scores or {},
            },
        )
        self._schedule_collector_sync(
            session_id,
            include_snapshots=bool(project_id),
        )
        return evaluation

    def _latest_project_request_id(
        self,
        session_id: str,
        project_id: str,
    ) -> Optional[str]:
        for request in reversed(self._load_requests(session_id)):
            if project_id in (request.get("updated_project_ids") or []):
                request_id = request.get("request_id")
                if request_id:
                    return request_id
        return None

    @staticmethod
    def _format_assistant_result_message(
        result: Any,
        *,
        error: Optional[str],
        result_title: Optional[str],
        result_detail: Optional[str],
        updated_project_names: List[str],
        interface_output_count: int,
    ) -> str:
        raw_result = str(result or "").strip()
        if error:
            lines = [
                "Generation stopped before a verified simulation was ready.",
                "",
                f"Reason: {error}",
            ]
            if raw_result:
                lines.extend(["", "Raw diagnostic:", raw_result[:4_000]])
            return "\n".join(lines).strip()

        if not updated_project_names and not raw_result.startswith("Build Success!"):
            return raw_result

        if not result_title:
            return raw_result

        lines = [result_title.rstrip(".") + "."]
        if result_detail:
            lines.extend(["", result_detail])
        if updated_project_names:
            project_list = ", ".join(updated_project_names[:5])
            if len(updated_project_names) > 5:
                project_list += f", and {len(updated_project_names) - 5} more"
            lines.extend(["", f"Project: {project_list}"])
        if interface_output_count:
            lines.append(
                f"Prepared output card{'s' if interface_output_count != 1 else ''}: "
                f"{interface_output_count}"
            )
        if updated_project_names:
            lines.append("Next: open the Run panel to test or replay the simulation.")
        if raw_result and raw_result.startswith("Build Success!"):
            lines.append(
                "Raw build details remain available in the generated files and logs."
            )
        elif raw_result and raw_result not in "\n".join(lines):
            lines.extend(["", raw_result[:4_000]])
        return "\n".join(lines).strip()

    def list_evaluations(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> Dict[str, Any]:
        self._load_session(session_id)
        limit = max(1, min(int(limit or 200), 1000))
        with self.analytics_lock:
            rows = self._read_jsonl(self._evaluations_path(session_id))
        return {"evaluations": rows[-limit:]}

    @staticmethod
    def _bounded_activity_text(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    def _add_activity(
        self,
        session_id: str,
        request_id: str,
        *,
        activity_key: str,
        state: str,
        title: str,
        detail: str = "",
        current: Optional[int] = None,
        total: Optional[int] = None,
        technical_name: str = "",
        file_changes: Any = None,
    ) -> Dict[str, Any]:
        safe_key = "".join(
            char if char.isalnum() or char in {"_", "-", ":"} else "_"
            for char in str(activity_key or "activity")
        )[:96]
        safe_state = (
            state
            if state in {"started", "progress", "completed", "failed"}
            else "progress"
        )
        safe_title = self._bounded_activity_text(title, 160) or "Working"
        safe_detail = self._bounded_activity_text(detail, 360)
        allowed_technical_names = {
            "devs_construct_tree",
            "devs_execute",
            "file editing",
            "file inspection",
            "backend smoke test",
            "output publication",
        }
        safe_technical_name = (
            technical_name if technical_name in allowed_technical_names else ""
        )

        def bounded_count(value: Optional[int]) -> Optional[int]:
            if value is None or isinstance(value, bool):
                return None
            try:
                return max(0, min(int(value), 100_000))
            except (TypeError, ValueError):
                return None

        public_fields: Dict[str, Any] = {
            "activity_key": safe_key,
            "activity_state": safe_state,
            "title": safe_title,
        }
        if safe_detail:
            public_fields["detail"] = safe_detail
        safe_current = bounded_count(current)
        safe_total = bounded_count(total)
        if safe_current is not None:
            public_fields["current"] = safe_current
        if safe_total is not None:
            public_fields["total"] = safe_total
        if safe_technical_name:
            public_fields["technical_name"] = safe_technical_name
        safe_file_changes = self._sanitize_activity_file_changes(
            session_id,
            file_changes,
        )
        if safe_file_changes:
            public_fields["file_changes"] = safe_file_changes

        return self._add_event(
            session_id,
            request_id,
            "activity",
            safe_title,
            **public_fields,
        )

    def _sanitize_activity_file_changes(
        self,
        session_id: str,
        file_changes: Any,
    ) -> List[Dict[str, str]]:
        """Return bounded, existing, student-visible workspace file records."""

        if not isinstance(file_changes, (list, tuple)):
            return []
        workspace = Path(self._session_workspace(session_id)).resolve()
        sanitized: List[Dict[str, str]] = []
        seen: set[str] = set()
        for raw_change in file_changes:
            if len(sanitized) >= MAX_ACTIVITY_FILE_CHANGES:
                break
            if not isinstance(raw_change, dict):
                continue
            change_kind = str(raw_change.get("change") or "")
            if change_kind not in {"added", "modified"}:
                continue
            try:
                relative = canonical_relative_file_path(
                    str(raw_change.get("path") or "")
                )
                if any(
                    part.startswith(".") or part in _ACTIVITY_INTERNAL_PARTS
                    for part in relative.parts
                ):
                    continue
                if relative.suffix.lower() not in _ACTIVITY_FILE_SUFFIXES:
                    continue
                file_path = contained_path(workspace, relative)
                metadata = file_path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_size > MAX_ACTIVITY_FILE_PREVIEW_BYTES
                ):
                    continue
            except (OSError, ValueError):
                continue
            canonical = relative.as_posix()
            if canonical in seen:
                continue
            seen.add(canonical)
            sanitized.append({"path": canonical, "change": change_kind})
        return sanitized

    def _agent_progress_context(
        self,
        agent: Any,
        session_id: str,
        request_id: str,
    ):
        reporter = getattr(agent, "progress_reporter", None)
        bind = getattr(reporter, "bind", None)
        if not callable(bind):
            return nullcontext()

        def record(activity: Any) -> None:
            if not isinstance(activity, dict):
                return
            activity_key = str(activity.get("activity_key") or "")
            activity_copy, technical_name = _public_agent_activity_copy(activity_key)
            if activity_copy is None:
                return
            activity_state = str(activity.get("activity_state") or "progress")
            if activity_state not in {"started", "progress", "completed", "failed"}:
                activity_state = "progress"
            title, detail = activity_copy.get(
                activity_state,
                activity_copy.get("progress", next(iter(activity_copy.values()))),
            )
            self._add_activity(
                session_id,
                request_id,
                activity_key=activity_key,
                state=activity_state,
                title=title,
                detail=detail,
                current=activity.get("current"),
                total=activity.get("total"),
                technical_name=technical_name,
                file_changes=activity.get("file_changes"),
            )

        return bind(record)

    def _load_requests(self, session_id: str) -> List[Dict[str, Any]]:
        return self._read_jsonl(self._requests_path(session_id))

    def _save_request(self, session_id: str, request: Dict[str, Any]):
        rows = self._load_requests(session_id)
        for idx, row in enumerate(rows):
            if row["request_id"] == request["request_id"]:
                rows[idx] = request
                self._rewrite_jsonl(self._requests_path(session_id), rows)
                return
        rows.append(request)
        self._rewrite_jsonl(self._requests_path(session_id), rows)

    def _get_request(self, session_id: str, request_id: str) -> Dict[str, Any]:
        for request in self._load_requests(session_id):
            if request["request_id"] == request_id:
                normalized = dict(request)
                normalized["session_id"] = session_id
                # Additive defaults keep requests written by the pre-guided
                # backend readable and preserve their one-pass behavior.
                normalized.setdefault("generation_mode", "automatic")
                normalized.setdefault("auto_check", True)
                normalized.setdefault("phase", "build")
                normalized.setdefault("phase_started_at", None)
                normalized.setdefault("pending_interaction", None)
                normalized.setdefault("interactions", [])
                normalized.setdefault("approved_intent", None)
                normalized.setdefault("approved_structure", None)
                normalized.setdefault("review_feedback", None)
                return normalized
        raise KeyError(request_id)

    def _save_message(self, session_id: str, message: Dict[str, Any]):
        self._append_jsonl(self._messages_path(session_id), message)

    def _update_message(self, session_id: str, message: Dict[str, Any]):
        rows = self._read_jsonl(self._messages_path(session_id))
        for idx, row in enumerate(rows):
            if row["message_id"] == message["message_id"]:
                rows[idx] = message
                self._rewrite_jsonl(self._messages_path(session_id), rows)
                return

    def _message_for_request(self, session_id: str, request_id: str, role: str):
        for message in self._read_jsonl(self._messages_path(session_id)):
            if message.get("request_id") == request_id and message.get("role") == role:
                return message
        return None

    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        client_id: Optional[str] = None,
        include_all: bool = False,
    ):
        owner_client_id = normalize_client_id(client_id)
        with self.lock:
            self._rebuild_session_locations()
            sessions = []
            for item in self.session_locations:
                try:
                    session = self._load_session(item)
                    session_owner = normalize_client_id(
                        session.get("owner_client_id")
                    )
                    if (
                        owner_client_id is not None
                        and session_owner != owner_client_id
                    ):
                        continue
                    if owner_client_id is None and not include_all and session_owner:
                        continue
                    # Ownership must be decided before the potentially
                    # expensive project-tree synchronization.  A shared-service
                    # history poll must never scan every other participant's
                    # workspace.
                    self._sync_session_projects(item)
                    session = self._load_session(item)
                    sessions.append(session)
                except KeyError:
                    continue
            sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
            return sessions[offset : offset + limit]

    def session_belongs_to(self, session_id: str, client_id: str) -> bool:
        """Return whether one exact session belongs to the browser identity."""

        owner_client_id = normalize_client_id(client_id)
        if owner_client_id is None:
            return False
        with self.lock:
            try:
                session = self._load_session(session_id)
            except KeyError:
                return False
            return normalize_client_id(session.get("owner_client_id")) == owner_client_id

    def _participant_has_active_generation_unlocked(
        self,
        owner_client_id: str,
        *,
        excluding_session_id: str,
    ) -> bool:
        for candidate_session_id in self.session_locations:
            if candidate_session_id == excluding_session_id:
                continue
            try:
                candidate = self._load_session(candidate_session_id)
            except KeyError:
                continue
            if (
                normalize_client_id(candidate.get("owner_client_id"))
                == owner_client_id
                and candidate.get("status") in ACTIVE_SESSION_STATUSES
            ):
                return True
        return False

    def get_frontend_config(self):
        openrouter_available = bool(openrouter_api_key())
        return {
            "default_provider": "openai",
            "default_model": first_preset_model(FRONTEND_MODEL_PRESETS),
            "api_key_available": {
                "openai": openrouter_available,
            },
            "model_presets": FRONTEND_MODEL_PRESETS,
            # Interactive review is the student-facing default. The wire value
            # remains `guided`; Automatic uses the same pipeline without pauses.
            "default_generation_mode": "guided",
            "default_auto_check": True,
        }

    def parse_model_for_visualizer(self, class_name: str, code_content: str, provider: str, model: str, api_key: Optional[str]):
        try:
            return parse_model_for_visualizer_impl(class_name, code_content, provider, model, api_key)
        except Exception as exc:
            local = local_parse_xdevs_structure(class_name, code_content)
            if local:
                print(f"[Visualizer] LLM parse failed for {class_name}; using local parser: {exc}")
                return local
            raise

    def create_session(
        self,
        title: Optional[str],
        clone_projects: List[CloneProjectSpec],
        client_id: Optional[str] = None,
    ):
        with self.lock:
            self._ensure_storage_capacity("creating a new design session")
            self._ensure_participant_storage_capacity_unlocked(
                client_id,
                "creating a new design session",
            )
            session_id = new_id("sess")
            session_workspace = self._new_session_workspace()
            self._ensure_workspace_storage(session_workspace)
            self.session_locations[session_id] = {"workspace": session_workspace, "storage_id": session_id}
            session = {
                "session_id": session_id,
                "title": title or "New Session",
                "owner_client_id": normalize_client_id(client_id),
                "status": "idle",
                "active_request_id": None,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "project_count": 0,
            }
            os.makedirs(self._session_dir(session_id), exist_ok=True)
            self._write_json(self._session_path(session_id), session)
            self._save_projects(session_id, [])
            for path in (
                self._messages_path(session_id),
                self._requests_path(session_id),
                self._events_path(session_id),
                self._trace_path(session_id),
                self._evaluations_path(session_id),
            ):
                open(path, "a", encoding="utf-8").close()
            self._register_existing_storage_session(session_workspace, session_id)
            self._rebuild_session_locations()
            projects = self._clone_projects_unlocked(session_id, clone_projects) if clone_projects else []
            self._record_trace_unlocked(
                session_id,
                "session_created",
                actor="system",
                payload={"title": session["title"], "cloned_project_count": len(projects)},
            )
            self._schedule_collector_sync(
                session_id,
                include_snapshots=bool(projects),
            )
            return self._load_session(session_id), projects

    def get_session(self, session_id: str):
        with self.lock:
            self._sync_session_projects(session_id)
            return self._load_session(session_id)

    def update_session(self, session_id: str, title: str):
        title = (title or "").strip()
        if not title:
            raise ValueError("Session title cannot be empty")
        with self.lock:
            session = self._load_session(session_id)
            session["title"] = title
            self._save_session(session)
            self._record_trace_unlocked(
                session_id,
                "session_renamed",
                actor="user",
                payload={"title": title},
            )
            self._schedule_collector_sync(session_id)
            return self._load_session(session_id)

    def delete_session(self, session_id: str):
        with self.lock:
            session = self._load_session(session_id)
            if session.get("status") in ACTIVE_SESSION_STATUSES:
                raise RuntimeError("Cannot delete a session while it has active work")
            if self._session_has_active_simulation_unlocked(session_id):
                raise RuntimeError(
                    "Stop or wait for the running simulation before deleting this session."
                )

            location = self._session_location(session_id)
            workspace = location["workspace"]
            storage_id = location["storage_id"]
            session_dir = os.path.join(workspace, META_DIR_NAME, "sessions", storage_id)

            registry = self._read_registry()
            registry["sessions"] = [
                entry for entry in registry.get("sessions", [])
                if entry.get("session_id") != session_id
            ]
            self._write_registry(registry)
            self.session_locations.pop(session_id, None)
            self.workspace_agents.pop(workspace, None)
            with self.event_lock:
                self.event_next_ids.pop(session_id, None)

            deleted_workspace = False
            if os.path.basename(workspace).startswith("session_workspace_") and os.path.isdir(workspace):
                shutil.rmtree(workspace)
                deleted_workspace = True
            elif os.path.isdir(session_dir):
                shutil.rmtree(session_dir)

            return {
                "session_id": session_id,
                "deleted": True,
                "deleted_workspace": deleted_workspace,
                "workspace_path": workspace,
            }

    def list_projects(self, session_id: str):
        with self.lock:
            self._load_session(session_id)
            self._sync_session_projects(session_id)
            return self._load_projects(session_id)

    def upload_project(self, session_id: str, display_name: str, files: Dict[str, str]):
        with self.lock:
            session = self._load_session(session_id)
            if session.get("status") in ACTIVE_SESSION_STATUSES:
                raise RuntimeError(
                    "Wait for the current agent request to finish before uploading a simulation."
                )
            if self._session_has_active_simulation_unlocked(session_id):
                raise RuntimeError(
                    "Stop or wait for the running simulation before uploading files."
                )
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("Simulation name cannot be empty.")
            if not files:
                raise ValueError("Choose at least one file to upload.")
            if len(files) > MAX_UPLOAD_FILES:
                raise ValueError(
                    f"An uploaded simulation may contain at most {MAX_UPLOAD_FILES} files."
                )
            validated_files: List[tuple[PurePosixPath, str]] = []
            canonical_names: set[str] = set()
            total_bytes = 0
            for rel_path, content in files.items():
                if not isinstance(content, str):
                    raise ValueError("Uploaded files must contain UTF-8 text.")
                safe_rel = canonical_relative_file_path(rel_path)
                canonical_name = safe_rel.as_posix().casefold()
                if canonical_name in canonical_names:
                    raise ValueError(
                        f"Uploaded file paths collide after normalization: {rel_path!r}"
                    )
                canonical_names.add(canonical_name)
                content_bytes = len(content.encode("utf-8"))
                if content_bytes > MAX_UPLOAD_FILE_BYTES:
                    raise ValueError(
                        f"Uploaded file exceeds {MAX_UPLOAD_FILE_BYTES} bytes: {rel_path!r}"
                    )
                total_bytes += content_bytes
                if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
                    raise ValueError(
                        f"Uploaded simulation exceeds {MAX_UPLOAD_TOTAL_BYTES} bytes."
                    )
                validated_files.append((safe_rel, content))

            self._ensure_storage_capacity(
                "uploading a simulation",
                estimated_new_bytes=total_bytes,
            )
            self._ensure_participant_storage_capacity_unlocked(
                session.get("owner_client_id"),
                "uploading a simulation",
                estimated_new_bytes=total_bytes,
            )
            projects = self._load_projects(session_id)
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            target_rel = self._unique_project_path(display_name, self._session_workspace(session_id))
            workspace = Path(self._session_workspace(session_id)).resolve()
            target_abs = contained_path(workspace, target_rel)
            staging = Path(
                tempfile.mkdtemp(prefix=".simulation-upload-", dir=str(workspace))
            )
            try:
                for safe_rel, content in validated_files:
                    file_path = contained_path(staging, safe_rel)
                    file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with open(file_path, "x", encoding="utf-8") as stream:
                        stream.write(content)
                os.replace(staging, target_abs)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            project = {
                "project_id": project_id,
                "display_name": display_name,
                "status": "ready",
                "version": 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "path": target_rel,
                "source": {"type": "upload"},
                "validation": {
                    "status": "unverified",
                    "message": "This uploaded simulation has not been test-run yet.",
                },
            }
            projects.append(project)
            self._save_projects(session_id, projects)
            self._update_session_project_count(session_id)
            self._record_trace_unlocked(
                session_id,
                "project_uploaded",
                actor="user",
                project_id=project_id,
                payload={
                    "display_name": display_name,
                    "file_count": len(validated_files),
                    "total_bytes": total_bytes,
                },
            )
            self._schedule_collector_sync(
                session_id,
                include_snapshots=True,
            )
            return project

    def upload_project_archive(
        self,
        session_id: str,
        display_name: str,
        archive_bytes: bytes,
    ):
        """Import one bounded, text-only simulation ZIP through normal upload validation."""

        if not archive_bytes:
            raise ValueError("Choose a ZIP archive to upload.")
        if len(archive_bytes) > MAX_UPLOAD_TOTAL_BYTES:
            raise ValueError("ZIP archive is too large.")

        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except (OSError, zipfile.BadZipFile):
            raise ValueError("The selected file is not a valid ZIP archive.") from None

        with archive:
            members: List[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            total_bytes = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                normalized_name = info.filename.replace("\\", "/")
                raw_parts = PurePosixPath(normalized_name).parts
                if "__MACOSX" in raw_parts or raw_parts[-1:] == (".DS_Store",):
                    continue
                safe_path = canonical_relative_file_path(normalized_name)
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ValueError(
                        f"ZIP archive may not contain symbolic links: {info.filename!r}"
                    )
                if info.flag_bits & 0x1:
                    raise ValueError("Encrypted ZIP archives are not supported.")
                if info.file_size > MAX_UPLOAD_FILE_BYTES:
                    raise ValueError(
                        f"Archived file exceeds {MAX_UPLOAD_FILE_BYTES} bytes: {info.filename!r}"
                    )
                total_bytes += info.file_size
                if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
                    raise ValueError(
                        f"ZIP contents exceed {MAX_UPLOAD_TOTAL_BYTES} bytes."
                    )
                members.append((info, safe_path))
                if len(members) > MAX_UPLOAD_FILES:
                    raise ValueError(
                        f"A ZIP simulation may contain at most {MAX_UPLOAD_FILES} files."
                    )

            if not members:
                raise ValueError("ZIP archive does not contain any importable files.")

            first_parts = members[0][1].parts
            common_root = (
                first_parts[0]
                if len(first_parts) > 1
                and all(
                    len(path.parts) > 1 and path.parts[0] == first_parts[0]
                    for _, path in members
                )
                else ""
            )
            files: Dict[str, str] = {}
            canonical_names: set[str] = set()
            for info, safe_path in members:
                relative_path = PurePosixPath(*safe_path.parts[1:]) if common_root else safe_path
                canonical_name = relative_path.as_posix().casefold()
                if canonical_name in canonical_names:
                    raise ValueError(
                        f"ZIP file paths collide after normalization: {info.filename!r}"
                    )
                canonical_names.add(canonical_name)
                try:
                    raw_content = archive.read(info)
                except (RuntimeError, zipfile.BadZipFile):
                    raise ValueError(
                        f"Could not read archived file: {info.filename!r}"
                    ) from None
                if len(raw_content) != info.file_size:
                    raise ValueError(
                        f"Archived file size changed while reading: {info.filename!r}"
                    )
                try:
                    files[relative_path.as_posix()] = raw_content.decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError(
                        f"Archived simulations must contain UTF-8 text files: {info.filename!r}"
                    ) from None

        inferred_name = common_root or str(display_name or "").strip()
        if inferred_name.lower().endswith(".zip"):
            inferred_name = inferred_name[:-4]
        return self.upload_project(
            session_id,
            inferred_name or "Uploaded simulation",
            files,
        )

    def _unique_project_id(self, projects: List[Dict[str, Any]], project_id: str) -> str:
        existing_ids = {p["project_id"] for p in projects}
        suffix = 2
        base_id = project_id
        while project_id in existing_ids:
            project_id = f"{base_id}_{suffix}"
            suffix += 1
        return project_id

    def _unique_project_path(self, display_name: str, workspace: Optional[str] = None) -> str:
        workspace = workspace or self.working_dir
        base_name = safe_project_directory_name(display_name)
        candidate = base_name
        suffix = 2
        while os.path.lexists(contained_path(Path(workspace), candidate)):
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        return candidate

    def _is_valid_project_dir(self, rel_path: str, workspace: Optional[str] = None) -> bool:
        if not rel_path or rel_path.startswith(".") or rel_path == META_DIR_NAME:
            return False
        workspace = workspace or self.working_dir
        try:
            abs_path = contained_path(Path(workspace), rel_path)
        except ValueError:
            return False
        if not abs_path.is_dir() or abs_path.is_symlink():
            return False
        if has_devs_project_marker(str(abs_path)):
            return True
        nested_marker = abs_path / "devs_project"
        return (
            nested_marker.is_dir()
            and not nested_marker.is_symlink()
            and has_devs_project_marker(str(nested_marker))
        )

    def _sync_changed_projects_unlocked(self, session_id: str, changed_top_dirs: List[str]) -> List[str]:
        workspace = self._session_workspace(session_id)
        projects, _records_changed = self._canonicalize_project_records(
            self._load_projects(session_id), workspace
        )
        existing_ids = {project["project_id"] for project in projects}
        by_path = {str(project.get("path", "")).replace("\\", "/"): project for project in projects}
        updated_project_ids = []

        changed_project_paths = set()
        for changed_name in changed_top_dirs:
            changed_rel = changed_name.replace("\\", "/")
            top_dir = changed_rel.split("/")[0]
            if not top_dir or top_dir == META_DIR_NAME:
                continue
            for registered_path in by_path:
                if (
                    registered_path == changed_rel
                    or registered_path.startswith(changed_rel.rstrip("/") + "/")
                    or changed_rel.startswith(registered_path.rstrip("/") + "/")
                ):
                    changed_project_paths.add(registered_path)
            changed_project_paths.update(self._discover_project_rel_paths(top_dir, workspace))
        if not changed_project_paths:
            changed_project_paths.update(path for path in changed_top_dirs if self._is_valid_project_dir(path, workspace))

        for rel_path in sorted(changed_project_paths):
            project = by_path.get(rel_path)
            if project:
                project["version"] = int(project.get("version", 1)) + 1
                new_display_name = self._project_display_name(rel_path, workspace)
                project["display_name"] = new_display_name
                project["updated_at"] = utc_now()
                project["status"] = "updating"
                project["validation"] = {
                    "status": "stale",
                    "message": "The simulation changed after its previous test.",
                }
                self._delete_graph_cache(session_id, project["project_id"])
                updated_project_ids.append(project["project_id"])
                continue

            if not self._is_valid_project_dir(rel_path, workspace):
                continue

            display_name = self._project_display_name(rel_path, workspace)
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            existing_ids.add(project_id)

            new_project = self._make_project_record(
                project_id,
                display_name,
                rel_path,
                "agent_generated",
                status="updating",
            )
            projects.append(new_project)
            by_path[rel_path] = new_project
            updated_project_ids.append(project_id)

        self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)
        return updated_project_ids

    def _clone_projects_unlocked(self, session_id: str, clone_specs: List[CloneProjectSpec]):
        target_session = self._load_session(session_id)
        if target_session.get("status") in ACTIVE_SESSION_STATUSES:
            raise RuntimeError(
                "Wait for the current agent request to finish before adding a simulation."
            )
        if self._session_has_active_simulation_unlocked(session_id):
            raise RuntimeError(
                "Stop or wait for the running simulation before adding another one."
            )
        projects = self._load_projects(session_id)
        created = []
        for spec in clone_specs:
            source_session = self._load_session(spec.source_session_id)
            if source_session.get("status") in ACTIVE_SESSION_STATUSES:
                raise RuntimeError(
                    "The source simulation is still being changed. Wait for it to finish first."
                )
            if self._session_has_active_simulation_unlocked(
                spec.source_session_id
            ):
                raise RuntimeError(
                    "The source simulation is running. Wait for it to finish first."
                )
            source_project = self._project_by_id(spec.source_session_id, spec.source_project_id)
            display_name = spec.display_name or source_project["display_name"]
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            target_rel = self._unique_project_path(display_name, self._session_workspace(session_id))
            source_root = Path(
                self._project_abs_path(source_project, spec.source_session_id)
            )
            _assert_regular_source_tree(source_root)
            source_bytes = self._tree_regular_file_size(source_root)
            self._ensure_storage_capacity(
                "copying a simulation",
                estimated_new_bytes=source_bytes,
            )
            self._ensure_participant_storage_capacity_unlocked(
                target_session.get("owner_client_id"),
                "copying a simulation",
                estimated_new_bytes=source_bytes,
            )
            shutil.copytree(
                source_root,
                contained_path(Path(self._session_workspace(session_id)), target_rel),
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", META_DIR_NAME),
            )
            project = {
                "project_id": project_id,
                "display_name": display_name,
                "status": "ready",
                "version": 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "path": target_rel,
                "source": {
                    "type": "session_project",
                    "session_id": spec.source_session_id,
                    "project_id": spec.source_project_id,
                    "version": spec.source_version,
                },
                "validation": {
                    "status": "unverified",
                    "message": "This copied simulation has not been test-run yet.",
                },
            }
            projects.append(project)
            created.append(project)
            self._record_trace_unlocked(
                session_id,
                "project_cloned",
                actor="user",
                project_id=project_id,
                payload={
                    "display_name": display_name,
                    "source_session_id": spec.source_session_id,
                    "source_project_id": spec.source_project_id,
                },
            )
        self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)
        return created

    def clone_projects(self, session_id: str, clone_specs: List[CloneProjectSpec]):
        with self.lock:
            projects = self._clone_projects_unlocked(session_id, clone_specs)
            self._schedule_collector_sync(
                session_id,
                include_snapshots=bool(projects),
            )
            return projects

    def get_project_files(self, session_id: str, project_id: str) -> Dict[str, Any]:
        with self.lock:
            session = self._load_session(session_id)
            project = self._project_by_id(session_id, project_id)
            project_path = self._project_abs_path(project, session_id)
        if not os.path.exists(project_path):
            raise FileNotFoundError(project_id)
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [
                d
                for d in dirs
                if d != META_DIR_NAME
                and not d.startswith(".")
                and not (Path(root) / d).is_symlink()
            ]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                try:
                    metadata = os.lstat(abs_path)
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        files_data[rel_path] = f.read()
                except Exception:
                    files_data[rel_path] = "[Binary Content]"
        return {"files": files_data, "project": project, "session_status": session["status"]}

    def download_project_archive(self, session_id: str, project_id: str) -> tuple[bytes, str]:
        with self.lock:
            project = self._project_by_id(session_id, project_id)
            project_path = self._project_abs_path(project, session_id)
            display_name = str(project.get("display_name") or project_id)
        root_path = Path(project_path)
        if not root_path.exists():
            raise FileNotFoundError(project_id)
        if root_path.is_symlink() or not root_path.is_dir():
            raise ValueError("Project folder must be a real directory.")

        archive_root = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "._-")
            else "_"
            for character in display_name.strip()
        ) or project_id
        buffer = io.BytesIO()
        file_count = 0
        total_bytes = 0
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for current_root, dirs, files in os.walk(root_path, followlinks=False):
                current = Path(current_root)
                dirs[:] = [
                    name
                    for name in dirs
                    if not (current / name).is_symlink()
                ]
                for filename in files:
                    source = current / filename
                    try:
                        metadata = source.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    file_count += 1
                    if file_count > MAX_PROJECT_DOWNLOAD_FILES:
                        raise OverflowError(
                            "Project archive contains too many files."
                        )
                    size = int(metadata.st_size)
                    if size > MAX_PROJECT_DOWNLOAD_FILE_BYTES:
                        raise OverflowError(
                            f"Project file is too large to archive: {filename}"
                        )
                    total_bytes += size
                    if total_bytes > MAX_PROJECT_DOWNLOAD_BYTES:
                        raise OverflowError("Project archive is too large.")
                    relative = source.relative_to(root_path).as_posix()
                    archive.write(source, f"{archive_root}/{relative}")
        self._record_trace_unlocked(
            session_id,
            "project_downloaded",
            actor="user",
            project_id=project_id,
            payload={
                "display_name": display_name,
                "file_count": file_count,
                "total_bytes": total_bytes,
            },
        )
        return buffer.getvalue(), f"{archive_root}.zip"

    def _schedule_collector_sync(
        self,
        session_id: str,
        *,
        include_snapshots: bool = False,
    ) -> None:
        if self.collector_reporter is not None:
            self.collector_reporter.schedule(
                session_id,
                include_snapshots=include_snapshots,
            )

    @staticmethod
    def _collector_project_archive(project_root: Path, display_name: str) -> bytes:
        """Build the private collector snapshot without logs or credentials."""

        resolved = project_root.resolve(strict=True)
        if project_root.is_symlink() or not resolved.is_dir():
            raise ValueError("Project folder must be a real directory.")
        archive_root = "".join(
            character
            if character.isascii()
            and (character.isalnum() or character in "._-")
            else "_"
            for character in display_name.strip()
        ).strip("._") or "devs-model"
        excluded_directories = {
            META_DIR_NAME,
            ".git",
            ".hg",
            ".svn",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "_analysis_logs",
            "logs",
            "node_modules",
        }
        excluded_files = {
            ".env",
            ".env.local",
            ".participant-cookie-secret",
            "raw_simulation_output.log",
        }
        buffer = io.BytesIO()
        file_count = 0
        total_bytes = 0
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for current_root, dirs, files in os.walk(resolved, followlinks=False):
                current = Path(current_root)
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if name not in excluded_directories
                    and not (current / name).is_symlink()
                )
                for filename in sorted(files):
                    if (
                        filename in excluded_files
                        or filename.startswith(".env.")
                        or filename.endswith(".log")
                    ):
                        continue
                    source = current / filename
                    try:
                        metadata = source.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        continue
                    if metadata.st_size > 32 * 1024 * 1024:
                        continue
                    file_count += 1
                    total_bytes += int(metadata.st_size)
                    if file_count > MAX_PROJECT_DOWNLOAD_FILES:
                        raise OverflowError(
                            "Collector snapshot contains too many files."
                        )
                    if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
                        raise OverflowError("Collector snapshot is too large.")
                    relative = source.relative_to(resolved).as_posix()
                    archive.write(source, f"{archive_root}/{relative}")
        content = buffer.getvalue()
        if len(content) > MAX_UPLOAD_TOTAL_BYTES:
            raise OverflowError("Compressed collector snapshot is too large.")
        return content

    @staticmethod
    def _collector_llm_usage(project_root: Path) -> Optional[Dict[str, Any]]:
        """Return bounded, aggregate LLM metrics without prompts or outputs."""

        analysis_directories = (
            project_root / "_analysis_logs",
            project_root / "devs_project" / "_analysis_logs",
        )
        analysis_directory = next(
            (candidate for candidate in analysis_directories if candidate.is_dir()),
            None,
        )
        if analysis_directory is None:
            return None

        summary_files = (
            ("detailed_planning", "detailed_planning_llm_summary.json"),
            ("implementation", "llm_call_summary.json"),
        )
        calls: List[tuple[str, Dict[str, Any]]] = []
        included_summaries: List[str] = []
        for logical_stage, filename in summary_files:
            path = analysis_directory / filename
            try:
                if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                summary = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            raw_calls = summary.get("calls") if isinstance(summary, dict) else None
            if not isinstance(raw_calls, list):
                continue
            included_summaries.append(logical_stage)
            calls.extend(
                (logical_stage, call)
                for call in raw_calls[:2_000]
                if isinstance(call, dict)
            )

        if not calls:
            return None

        def nonnegative_int(value: Any) -> Optional[int]:
            if isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if number >= 0 else None

        def nonnegative_float(value: Any) -> float:
            if isinstance(value, bool):
                return 0.0
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return number if number >= 0 and number != float("inf") else 0.0

        def new_bucket() -> Dict[str, Any]:
            return {
                "calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "duration_seconds": 0.0,
                "calls_with_reported_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
            }

        total = new_bucket()
        by_model: Dict[str, Dict[str, Any]] = {}
        by_phase: Dict[str, Dict[str, Any]] = {}

        def add_call(bucket: Dict[str, Any], call: Dict[str, Any]) -> None:
            bucket["calls"] += 1
            status = str(call.get("status") or "unknown").strip().lower()
            if status == "success":
                bucket["successful_calls"] += 1
            else:
                bucket["failed_calls"] += 1
            bucket["duration_seconds"] += nonnegative_float(
                call.get("duration_sec")
            )
            bucket["estimated_input_tokens"] += nonnegative_int(
                call.get("input_tokens_est")
            ) or 0
            bucket["estimated_output_tokens"] += nonnegative_int(
                call.get("output_tokens_est")
            ) or 0
            token_usage = call.get("token_usage")
            if not isinstance(token_usage, dict):
                return
            prompt_tokens = nonnegative_int(token_usage.get("prompt_tokens"))
            completion_tokens = nonnegative_int(
                token_usage.get("completion_tokens")
            )
            total_tokens = nonnegative_int(token_usage.get("total_tokens"))
            if (
                prompt_tokens is None
                and completion_tokens is None
                and total_tokens is None
            ):
                return
            bucket["calls_with_reported_tokens"] += 1
            bucket["prompt_tokens"] += prompt_tokens or 0
            bucket["completion_tokens"] += completion_tokens or 0
            bucket["total_tokens"] += (
                total_tokens
                if total_tokens is not None
                else (prompt_tokens or 0) + (completion_tokens or 0)
            )

        for logical_stage, call in calls:
            model = str(call.get("model") or "unknown")[:256]
            raw_phase = str(call.get("phase") or "unknown")[:256]
            phase = f"{logical_stage}:{raw_phase}"
            model_bucket = by_model.setdefault(model, new_bucket())
            phase_bucket = by_phase.setdefault(phase, new_bucket())
            add_call(total, call)
            add_call(model_bucket, call)
            add_call(phase_bucket, call)

        def finish_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
            result = dict(bucket)
            result["duration_seconds"] = round(
                float(result["duration_seconds"]), 3
            )
            return result

        return {
            "schema": "devs.llm-usage-summary.v1",
            # Initial structure planning, intent review, graph parsing and the
            # Codex CLI do not yet expose comparable per-call token metrics.
            "coverage": "project_generation_summaries_only",
            "included_summaries": included_summaries,
            **finish_bucket(total),
            "models": [
                {"model": model, **finish_bucket(bucket)}
                for model, bucket in sorted(by_model.items())
            ],
            "phases": [
                {"phase": phase, **finish_bucket(bucket)}
                for phase, bucket in sorted(by_phase.items())
            ],
        }

    def _collector_bundle(
        self,
        session_id: str,
        include_snapshots: bool,
    ) -> Dict[str, Any]:
        """Read one complete, pseudonymous session for optional reporting."""

        with self.lock:
            session = dict(self._load_session(session_id))
            projects = [dict(project) for project in self._load_projects(session_id)]
            workspace = Path(self._session_workspace(session_id)).resolve(strict=True)
            messages = self._read_jsonl(self._messages_path(session_id))
            requests = self._read_jsonl(self._requests_path(session_id))
            with self.event_lock:
                events = self._read_jsonl(self._events_path(session_id))
            with self.analytics_lock:
                trace = self._read_jsonl(self._trace_path(session_id))
                evaluations = self._read_jsonl(self._evaluations_path(session_id))

        participant_id = normalize_client_id(session.get("owner_client_id"))
        if participant_id is None:
            participant_id = f"session_{short_hash(session_id)}"
        for private_key in (
            "storage_session_id",
            "workspace_path",
            "is_current_workspace",
        ):
            session.pop(private_key, None)

        project_payloads: List[Dict[str, Any]] = []
        if include_snapshots:
            execution_root = Path(self.registry_path).parent / "simulation_executions"
            for project in projects:
                project_id = str(project.get("project_id") or "")
                if not project_id:
                    continue
                project_root: Optional[Path] = None
                try:
                    project_root = contained_path(
                        workspace,
                        str(project.get("path") or ""),
                    )
                    snapshot = self._collector_project_archive(
                        project_root,
                        str(project.get("display_name") or project_id),
                    )
                except (FileNotFoundError, OSError, OverflowError, ValueError):
                    snapshot = b""

                collector_project = dict(project)
                llm_usage = (
                    self._collector_llm_usage(project_root)
                    if project_root is not None
                    else None
                )
                if llm_usage is not None:
                    collector_project["llm_usage"] = llm_usage

                graph_cache = self._read_json(
                    self._graph_cache_path(session_id, project_id),
                    {},
                )
                graph = (
                    graph_cache.get("graph")
                    if isinstance(graph_cache, dict)
                    else None
                )
                validation = project.get("validation")
                execution_id = (
                    str(validation.get("execution_id") or "")
                    if isinstance(validation, dict)
                    else ""
                )
                summary = None
                event_trace = ""
                if re.fullmatch(r"exec_[A-Za-z0-9_-]+", execution_id):
                    result_root = execution_root / execution_id / "results"
                    summary = self._read_json(str(result_root / "summary.json"), None)
                    try:
                        event_trace = (result_root / "event_trace.jsonl").read_text(
                            encoding="utf-8"
                        )
                    except (FileNotFoundError, OSError, UnicodeDecodeError):
                        event_trace = ""
                project_payloads.append(
                    {
                        "project": collector_project,
                        "snapshot_base64": (
                            base64.b64encode(snapshot).decode("ascii")
                            if snapshot
                            else None
                        ),
                        "graph": graph,
                        "event_trace": event_trace,
                        "summary": summary,
                    }
                )

        return {
            # The reporter replaces source with its configured deployment id.
            "source": "devs-gen",
            "participant_id": participant_id,
            "session": session,
            "messages": messages,
            "requests": requests,
            "events": events,
            "trace": trace,
            "evaluations": evaluations,
            "projects": project_payloads,
        }

    @staticmethod
    def _default_validation(project: Dict[str, Any]) -> Dict[str, Any]:
        validation = project.get("validation")
        if isinstance(validation, dict):
            return dict(validation)
        return {
            "status": "unverified",
            "message": "This simulation has not been test-run yet.",
        }

    def _session_has_active_simulation_unlocked(self, session_id: str) -> bool:
        terminal = {"succeeded", "failed", "timed_out", "stopped"}
        for execution_id, context in self.simulation_execution_context.items():
            if context.get("session_id") != session_id:
                continue
            # The process and its durable result are one user-visible operation.
            # Keep the design immutable until publication has sealed the exact
            # version that was executed, even if the child process has exited.
            if context.get("finalization_status") == "pending":
                return True
            try:
                status = self.simulation_execution_service.get_record(execution_id)[
                    "status"
                ]
            except (ExecutionStateError, KeyError):
                continue
            if status not in terminal:
                return True
        return False

    def _simulation_bundle_unlocked(
        self, session_id: str, project: Dict[str, Any]
    ) -> Path:
        workspace = Path(self._session_workspace(session_id)).resolve(strict=True)
        project_root = Path(self._project_abs_path(project, session_id))
        candidates = [project_root]
        if project_root.name == "devs_project":
            candidates.insert(0, project_root.parent)
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(workspace)
                metadata = resolved.lstat()
                entrypoint = resolved / "run.py"
                entrypoint_metadata = entrypoint.lstat()
            except (FileNotFoundError, OSError, ValueError):
                continue
            if (
                stat.S_ISDIR(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and stat.S_ISREG(entrypoint_metadata.st_mode)
                and not stat.S_ISLNK(entrypoint_metadata.st_mode)
            ):
                return resolved
        raise FileNotFoundError(
            "This simulation does not have a runnable top-level run.py yet."
        )

    def _save_project_validation_unlocked(
        self,
        session_id: str,
        project_id: str,
        validation: Dict[str, Any],
    ) -> Dict[str, Any]:
        projects = self._load_projects(session_id)
        for project in projects:
            if project.get("project_id") == project_id:
                project["validation"] = dict(validation)
                validation_status = str(validation.get("status") or "")
                if validation_status == "failed":
                    project["status"] = "error"
                elif validation_status in {"validating", "stale"}:
                    project["status"] = "updating"
                elif validation_status in {"ready", "unverified"}:
                    project["status"] = "ready"
                project["updated_at"] = utc_now()
                self._save_projects(session_id, projects)
                return dict(project)
        raise KeyError(project_id)

    @staticmethod
    def _validation_failure_message(record: Dict[str, Any]) -> str:
        message = str(record.get("message") or "").strip()
        stderr = str(record.get("stderr") or "").strip()
        if not message and stderr:
            message = stderr.splitlines()[-1]
        if not message:
            message = "The simulation did not complete successfully."
        return message[:1000]

    @staticmethod
    def _json_business_events(stdout: Any) -> List[Dict[str, Any]]:
        """Parse bounded JSONL business events retained by the execution record."""

        if not isinstance(stdout, str) or not stdout:
            return []
        events: List[Dict[str, Any]] = []
        # The execution service already bounds retained stdout. Keep this pass
        # defensive so a noisy simulator cannot make validation expensive.
        for raw_line in stdout.splitlines()[:20000]:
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            name = row.get("event") or row.get("event_type")
            if isinstance(name, str) and name.strip():
                events.append(row)
        return events

    @staticmethod
    def _event_name(row: Dict[str, Any]) -> str:
        return str(row.get("event") or row.get("event_type") or "").strip().lower()

    @staticmethod
    def _event_time(row: Dict[str, Any]) -> Optional[float]:
        for key in ("time", "timestamp", "sim_time", "simulation_time"):
            value = row.get(key)
            if type(value) in (int, float):
                return float(value)
        return None

    @staticmethod
    def _event_entity_id(row: Dict[str, Any]) -> Optional[Any]:
        for key in (
            "order_id",
            "job_id",
            "task_id",
            "customer_id",
            "item_id",
            "pizza_id",
            "entity_id",
        ):
            if key in row:
                return row.get(key)
        return None

    @staticmethod
    def _event_worker_id(row: Dict[str, Any]) -> Optional[Any]:
        for key in (
            "courier_id",
            "worker_id",
            "server_id",
            "processor_id",
            "resource_id",
            "machine_id",
            "oven_id",
        ):
            if key in row:
                return row.get(key)
        value = row.get("chef_ids")
        if isinstance(value, list):
            return tuple(value)
        if "chef_id" in row:
            return row.get("chef_id")
        return None

    @classmethod
    def _codex_acceptance_issues(cls, record: Dict[str, Any]) -> List[str]:
        """Return generic, high-confidence business-flow issues.

        This is deliberately not a domain oracle.  It rejects only strong
        evidence that a generated simulator is not fit to publish even though
        its process exited 0: repeated broadcast of one job to many workers,
        stage starts with no completions over a substantial trace, or declared
        multi-resource capacity that is never used across many allocations.
        The follow-up repair pass is still handled by the agent/Codex with the
        source files and logs in context.
        """

        events = cls._json_business_events(record.get("stdout"))
        if len(events) < 10:
            return []

        names = [cls._event_name(row) for row in events]
        times = [
            time_value
            for row in events
            if (time_value := cls._event_time(row)) is not None
        ]
        issues: List[str] = []

        # One addressed job should not be broadcast to many identical workers.
        repeated: Dict[tuple[str, Any, Optional[float]], List[Any]] = {}
        for row in events:
            name = cls._event_name(row)
            if "start" not in name:
                continue
            entity_id = cls._event_entity_id(row)
            worker_id = cls._event_worker_id(row)
            if entity_id is None or worker_id is None:
                continue
            time_value = cls._event_time(row)
            rounded_time = round(time_value, 6) if time_value is not None else None
            key = (name, entity_id, rounded_time)
            repeated.setdefault(key, []).append(worker_id)
        for (name, entity_id, time_value), workers in repeated.items():
            unique_workers = {repr(worker) for worker in workers}
            if len(workers) >= 4 and len(unique_workers) >= 4:
                issues.append(
                    "one entity appears to start the same stage on many workers: "
                    f"event={name!r}, entity={entity_id!r}, time={time_value!r}, "
                    f"workers={len(unique_workers)}"
                )
                break

        # A stage with many starts spread over time and no completions is
        # usually a broken timed-job lifecycle, not a valid sink.
        for stage in ("delivery", "cooking", "reception", "service"):
            stage_start_times = [
                cls._event_time(row)
                for row in events
                if stage in cls._event_name(row) and "start" in cls._event_name(row)
            ]
            stage_start_times = [
                value for value in stage_start_times if value is not None
            ]
            completion_count = sum(
                1
                for name in names
                if stage in name
                and any(token in name for token in ("complete", "completion", "finish", "done"))
            )
            if (
                len(stage_start_times) >= 5
                and completion_count == 0
                and max(stage_start_times) - min(stage_start_times) >= 10.0
            ):
                issues.append(
                    f"{stage} has {len(stage_start_times)} start events across "
                    "positive simulation time but no completion/finish/done event"
                )

        # If a runner declares multiple resources and many allocations occur,
        # using only resource #1 is strong evidence that concurrency was
        # accidentally serialized.
        arguments = record.get("arguments")
        if isinstance(arguments, dict):
            num_ovens = arguments.get("num_ovens")
            if type(num_ovens) is int and num_ovens >= 2:
                oven_ids = [
                    row.get("oven_id")
                    for row in events
                    if "oven_id" in row
                    and (
                        "allocation" in cls._event_name(row)
                        or "cooking_start" in cls._event_name(row)
                        or cls._event_name(row).endswith("start")
                    )
                ]
                if len(oven_ids) >= max(8, num_ovens * 3) and len(set(oven_ids)) == 1:
                    issues.append(
                        f"runner declares num_ovens={num_ovens}, but "
                        f"{len(oven_ids)} cooking/allocation events used only "
                        f"oven_id={oven_ids[0]!r}"
                    )

            num_chefs = arguments.get("num_chefs")
            if type(num_chefs) is int and num_chefs >= 4:
                chef_ids: set[Any] = set()
                chef_events = 0
                for row in events:
                    name = cls._event_name(row)
                    if "chef" not in json.dumps(row, ensure_ascii=False).lower():
                        continue
                    if "allocation" not in name and "cooking_start" not in name:
                        continue
                    value = row.get("chef_ids")
                    if isinstance(value, list):
                        chef_events += 1
                        chef_ids.update(value)
                    elif "chef_id" in row:
                        chef_events += 1
                        chef_ids.add(row.get("chef_id"))
                if chef_events >= max(8, num_chefs * 2) and len(chef_ids) <= 2:
                    issues.append(
                        f"runner declares num_chefs={num_chefs}, but "
                        f"{chef_events} chef allocation/start events used only "
                        f"{len(chef_ids)} distinct chefs"
                    )

        # Do not turn this into a broad semantic oracle.  Keep diagnostics
        # bounded and high signal for the repair prompt.
        return issues[:5]

    def _apply_codex_final_gate(
        self,
        session_id: str,
        request_id: Optional[str],
        project_id: str,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Reject executable but clearly broken generated simulations.

        The name reflects the product role: this gate feeds concrete runtime
        evidence into the existing agent/Codex repair loop.  It intentionally
        reuses the already isolated execution record instead of reimplementing
        process execution.
        """

        if os.getenv("DEVS_DISPLAY_CODEX_FINAL_GATE", "1").strip() in {"0", "false", "False"}:
            return record
        if record.get("status") != "succeeded":
            return record
        issues = self._codex_acceptance_issues(record)
        if not issues:
            return record

        message = (
            "Codex final gate rejected this executable simulation because "
            "the retained run log shows likely broken business flow: "
            + "; ".join(issues)
        )[:1000]
        failure = {
            **record,
            "status": "failed",
            "failure_kind": "acceptance_failed",
            "message": message,
            "acceptance_check": {
                "status": "failed",
                "issues": issues,
            },
        }
        with self.lock:
            try:
                project = self._project_by_id(session_id, project_id)
                current_validation = self._default_validation(project)
                validation = {
                    **current_validation,
                    "status": "failed",
                    "message": message,
                    "failure_kind": "acceptance_failed",
                    "execution_id": record.get("execution_id"),
                    "acceptance_check": failure["acceptance_check"],
                    "completed_at": utc_now(),
                }
                self._save_project_validation_unlocked(
                    session_id, project_id, validation
                )
            except KeyError:
                pass
        if request_id:
            self._add_activity(
                session_id,
                request_id,
                activity_key=f"validate:{project_id}",
                state="failed",
                title="Codex final gate needs a repair",
                detail=message,
                technical_name="codex final gate",
            )
        return failure

    @staticmethod
    def _codex_finalizer_enabled() -> bool:
        value = os.getenv("DEVS_DISPLAY_AUTOMATIC_CHECK")
        if value is None:
            value = os.getenv("DEVS_DISPLAY_CODEX_FINALIZER", "0")
        return value.strip() not in {
            "0",
            "false",
            "False",
        }

    @staticmethod
    def _codex_finalizer_attempt_limit() -> int:
        try:
            value = int(
                os.getenv(
                    "DEVS_DISPLAY_AUTOMATIC_CHECK_MAX_ATTEMPTS",
                    os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_MAX_ATTEMPTS", "2"),
                )
            )
        except ValueError:
            value = 2
        return max(1, min(value, 3))

    @staticmethod
    def _codex_finalizer_max_steps() -> int:
        try:
            value = int(
                os.getenv(
                    "DEVS_DISPLAY_AUTOMATIC_CHECK_MAX_STEPS",
                    os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_MAX_STEPS", "14"),
                )
            )
        except ValueError:
            value = 14
        return max(8, min(value, 28))

    @staticmethod
    def _codex_finalizer_driver() -> str:
        value = os.getenv(
            "DEVS_DISPLAY_AUTOMATIC_CHECK_DRIVER",
            os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_DRIVER", "agent"),
        )
        return value.strip().lower() or "agent"

    @staticmethod
    def _automatic_repair_driver() -> str:
        value = os.getenv("DEVS_DISPLAY_AUTOMATIC_REPAIR_DRIVER", "agent")
        return value.strip().lower() or "agent"

    @staticmethod
    def _codex_finalizer_timeout_seconds() -> int:
        try:
            value = int(
                os.getenv(
                    "DEVS_DISPLAY_AUTOMATIC_CHECK_TIMEOUT_SECONDS",
                    os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_TIMEOUT_SECONDS", "900"),
                )
            )
        except ValueError:
            value = 900
        return max(120, min(value, 3600))

    @staticmethod
    def _codex_cli_bin() -> str:
        codex_bin = os.getenv("DEVS_DISPLAY_CODEX_CLI_BIN") or shutil.which("codex")
        if not codex_bin:
            raise RuntimeError("Codex CLI is not available on PATH.")
        return codex_bin

    @staticmethod
    def _pi_cli_bin() -> str:
        pi_bin = os.getenv("DEVS_DISPLAY_PI_CLI_BIN") or shutil.which("pi")
        if not pi_bin:
            raise RuntimeError("Pi CLI is not available on PATH.")
        return pi_bin

    @staticmethod
    def _codex_cli_run_instruction(project_rel: str) -> str:
        prepared_python = os.getenv("OPTPILOT_INTERFACE_PYTHON", "").strip()
        python_command = shlex.quote(prepared_python) if prepared_python else "python3"
        command = f"cd {shlex.quote(project_rel)} && {python_command} run.py"
        if prepared_python:
            environment_note = (
                "Use the prepared interface Python, not the system python3, "
                "so the review matches the deployed runtime."
            )
        else:
            environment_note = (
                "OPTPILOT_INTERFACE_PYTHON is not set; use python3 as the fallback."
            )
        return (
            "Use shell commands inside the workspace. "
            f"{environment_note} Run the exact bundle with `{command}`, "
            "and keep any inspection output compact."
        )

    @staticmethod
    def _pi_cli_run_instruction(
        project_rel: str,
        *,
        bundle_root: Path,
        arguments: Dict[str, Any],
    ) -> str:
        prepared_python = os.getenv("OPTPILOT_INTERFACE_PYTHON", "").strip()
        command = shlex.join(
            [
                prepared_python or "python3",
                "-u",
                "run.py",
                *simulation_command_arguments(bundle_root, arguments),
            ]
        )
        return (
            f"The resolved top-level command is `cd {shlex.quote(project_rel)} && "
            f"{command}`. For every execution, use the devs_run tool with "
            f"project_path={project_rel!r} and parameters="
            f"{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}. The tool "
            "uses that prepared runtime and retains the raw output. Do not run the "
            "simulator directly through bash."
        )

    def _write_pi_finalizer_evidence(
        self,
        *,
        bundle_root: Path,
        review_id: str,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe_review_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", review_id)[-160:]
        log_dir = bundle_root / "devs_project" / "_analysis_logs"
        evidence_base = Path(
            os.getenv("OPTPILOT_INTERFACE_EPHEMERAL_ROOT", "").strip()
            or log_dir
        )
        evidence_root = evidence_base / "pi-finalizer-evidence" / safe_review_id
        evidence_root.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence_root / "default.stdout.txt"
        stderr_path = evidence_root / "default.stderr.txt"
        stdout_path.write_text(str(record.get("stdout") or ""), encoding="utf-8")
        stderr_path.write_text(str(record.get("stderr") or ""), encoding="utf-8")

        retained_results: List[str] = []
        execution_id = str(record.get("execution_id") or "")
        if re.fullmatch(r"exec_[A-Za-z0-9_-]+", execution_id):
            result_root = (
                self.simulation_execution_service.execution_root
                / execution_id
                / "results"
            )
            for item in record.get("result_files") or []:
                if not isinstance(item, dict):
                    continue
                relative = item.get("path")
                if not isinstance(relative, str):
                    continue
                try:
                    result_path = contained_path(result_root, relative)
                    metadata = result_path.lstat()
                except (FileNotFoundError, OSError, ValueError):
                    continue
                if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(
                    metadata.st_mode
                ):
                    retained_results.append(str(result_path))

        return {
            "status": str(record.get("status") or "unknown"),
            "exit_code": record.get("exit_code"),
            "duration_seconds": record.get("duration_seconds"),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_truncated": bool(record.get("stdout_truncated")),
            "stderr_truncated": bool(record.get("stderr_truncated")),
            "result_paths": retained_results,
        }

    @staticmethod
    def _remote_codex_run_instruction(project_rel: str) -> str:
        command = f"cd {shlex.quote(project_rel)} && python run.py"
        return (
            "Use shell commands inside the temporary host workspace. The host "
            "service places its prepared simulation Python first on PATH. Run "
            f"the exact bundle with `{command}`, and keep inspection output compact."
        )

    def _run_remote_codex_finalizer(
        self,
        *,
        prompt: str,
        bundle_root: Path,
        project_rel: str,
        review_id: str,
    ) -> None:
        endpoint = os.getenv(
            "DEVS_DISPLAY_REMOTE_FINALIZER_URL",
            os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_URL", ""),
        ).strip()
        token = os.getenv("DEVS_DISPLAY_REMOTE_FINALIZER_TOKEN", "").strip()
        client = RemoteCodexFinalizerClient(
            endpoint=endpoint,
            token=token,
            timeout_seconds=self._codex_finalizer_timeout_seconds(),
        )
        result = client.run(
            review_id=review_id,
            project_rel=project_rel,
            prompt=prompt,
            bundle_root=bundle_root,
        )
        if bool(result.get("timed_out")):
            raise RuntimeError("Remote automatic check timed out.")
        if int(result.get("returncode", -1)) != 0 and not bool(
            result.get("result_present")
        ):
            raise RuntimeError(
                "Remote automatic check exited with code "
                f"{result.get('returncode')} without a result."
            )

    @staticmethod
    def _codex_finalizer_result_path(bundle_root: Path) -> Path:
        return bundle_root / "devs_project" / "_analysis_logs" / "codex_finalizer_result.json"

    @staticmethod
    def _read_codex_finalizer_result(path: Path, review_id: str) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "verdict": "inconclusive",
                "issues": [
                    "Automatic check did not write a valid result file."
                ],
            }
        if not isinstance(payload, dict):
            return {
                "verdict": "inconclusive",
                "issues": ["Automatic-check result was not a JSON object."],
            }
        if str(payload.get("review_id") or "") != review_id:
            return {
                **payload,
                "verdict": "inconclusive",
                "issues": [
                    *(
                        payload.get("issues")
                        if isinstance(payload.get("issues"), list)
                        else []
                    ),
                    "Automatic-check result did not match the current review id.",
                ],
            }
        verdict = str(payload.get("verdict") or "").strip().lower()
        if verdict not in {"pass", "fail", "inconclusive"}:
            payload["verdict"] = "inconclusive"
            payload["issues"] = [
                *(
                    payload.get("issues")
                    if isinstance(payload.get("issues"), list)
                    else []
                ),
                "Automatic check returned an unsupported verdict.",
            ]
        else:
            payload["verdict"] = verdict
        return payload

    @staticmethod
    def _codex_finalizer_prompt(
        *,
        review_id: str,
        project_rel: str,
        validation_record: Dict[str, Any],
        diagnostic: str,
        run_instruction: str,
        execution_evidence: Optional[Dict[str, Any]] = None,
    ) -> str:
        result_rel = (
            f"{project_rel}/devs_project/_analysis_logs/"
            "codex_finalizer_result.json"
        )
        smoke_status = str(validation_record.get("status") or "unknown")
        failure_kind = str(validation_record.get("failure_kind") or "")
        message = str(validation_record.get("message") or "")[:1000]
        if execution_evidence is None:
            evidence_section = (
                "Initial backend smoke-test status:\n"
                f"status={smoke_status}\n"
                f"failure_kind={failure_kind}\n"
                f"message={message}\n"
                f"diagnostic:\n{diagnostic}"
            )
        else:
            result_paths = execution_evidence.get("result_paths") or []
            evidence_section = (
                "The backend already ran the exact default command. Read its raw "
                "output whether the process succeeded or failed; no behavioral "
                "interpretation has been added.\n"
                f"status={execution_evidence.get('status')}\n"
                f"exit_code={execution_evidence.get('exit_code')}\n"
                f"duration_seconds={execution_evidence.get('duration_seconds')}\n"
                f"stdout={execution_evidence.get('stdout_path')}\n"
                f"stdout_truncated={execution_evidence.get('stdout_truncated')}\n"
                f"stderr={execution_evidence.get('stderr_path')}\n"
                f"stderr_truncated={execution_evidence.get('stderr_truncated')}\n"
                "result_files="
                + json.dumps(result_paths, ensure_ascii=False)
            )
        if execution_evidence is not None:
            review_instruction = (
                "Your job is to decide whether the generated simulation really "
                "implements its own requirements and plan, not merely whether it "
                f"exits with code 0. First read {project_rel}/README.md, "
                f"{project_rel}/simulation.json, and "
                f"{project_rel}/devs_project/system_model_info.json if present, "
                "then the relevant generated Python files. Avoid reading full "
                "llm_calls logs. Read the supplied raw stdout and stderr, then "
                "inspect only the generated source files relevant to the requirements "
                "and observed behavior. If it is wrong, make the smallest coherent "
                "code fix and rerun the exact bundle. "
                f"Execution instruction: {run_instruction}"
            )
        else:
            review_instruction = (
                "Your job is to decide whether the generated simulation really "
                "implements its own requirements and plan, not merely whether it "
                "exits with code 0. First read README.md, simulation.json, "
                "approved_structure_plan.json or system_model_info.json if needed, "
                "and the relevant generated Python files. Avoid reading full llm_calls "
                "logs or full raw stdout/stderr; inspect compact snippets, counts, and "
                "targeted matching lines only. If it is wrong, make the smallest "
                "coherent code fix and rerun the exact bundle. "
                f"Execution instruction: {run_instruction}"
            )
        return "\n\n".join(
            (
                "You are the final reviewer and repairer for one generated xDEVS simulation.",
                (
                    "Work only inside this runnable simulation bundle relative to "
                    f"the current workspace: {project_rel}"
                ),
                (
                    "Do not call devs_construct_tree. Do not create a replacement "
                    "project folder. Do not use any old automatic repair workflow. "
                    "You may read files, edit the generated Python files in this "
                    "bundle, and rerun this exact bundle using the execution "
                    "instruction below."
                ),
                review_instruction,
                (
                    "Use a generous but bounded effort. If the issue is unclear, "
                    "inspect the directly relevant code rather than guessing. Do "
                    "not paste or print large file contents; keep observations small. "
                    "If you cannot fix it confidently, report fail or inconclusive "
                    "instead of passing it."
                ),
                evidence_section,
                (
                    "At the very end, write this JSON file exactly:\n"
                    f"{result_rel}\n\n"
                    "The JSON object must have these fields:\n"
                    "{\n"
                    f'  "review_id": "{review_id}",\n'
                    '  "verdict": "pass | fail | inconclusive",\n'
                    '  "fixed": true,\n'
                    '  "confidence": "low | medium | high",\n'
                    '  "summary": "short human-readable result",\n'
                    '  "likely_source": "plan | implementation | validation | runtime | unknown",\n'
                    '  "issues": ["blocking issue summaries, or empty if pass"],\n'
                    '  "files_modified": ["relative paths you changed"],\n'
                    '  "evidence": ["brief facts from requirements, code, and run output"]\n'
                    "}\n\n"
                    "Set verdict=pass only if the final code both runs and you judge "
                    "it satisfies the stated requirements. If you changed files, "
                    "fixed must be true; otherwise fixed must be false."
                ),
            )
        )

    def _run_codex_cli_finalizer(
        self,
        *,
        prompt: str,
        workspace_root: Path,
        bundle_root: Path,
        review_id: str,
    ) -> None:
        codex_bin = self._codex_cli_bin()

        log_dir = bundle_root / "devs_project" / "_analysis_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_review_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", review_id)[-160:]
        stdout_path = log_dir / f"codex_cli_finalizer_{safe_review_id}.stdout.txt"
        stderr_path = log_dir / f"codex_cli_finalizer_{safe_review_id}.stderr.txt"
        last_message_path = log_dir / f"codex_cli_finalizer_{safe_review_id}.last.txt"

        command = [
            codex_bin,
            "exec",
            "--cd",
            str(workspace_root),
            "--sandbox",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        model = os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_MODEL", "").strip()
        if model:
            command.extend(["--model", model])
        command.append("-")

        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace_root),
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self._codex_finalizer_timeout_seconds(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(str(exc.stdout or "")[-12000:], encoding="utf-8")
            stderr_path.write_text(str(exc.stderr or "")[-12000:], encoding="utf-8")
            raise RuntimeError(
                f"Codex CLI finalizer timed out after {exc.timeout} seconds."
            ) from exc

        stdout_path.write_text(str(completed.stdout or "")[-12000:], encoding="utf-8")
        stderr_path.write_text(str(completed.stderr or "")[-12000:], encoding="utf-8")
        if completed.returncode != 0 and not self._codex_finalizer_result_path(
            bundle_root
        ).exists():
            raise RuntimeError(
                "Codex CLI finalizer exited with code "
                f"{completed.returncode}. See {stderr_path.name}."
            )

    def _run_pi_cli_finalizer(
        self,
        *,
        prompt: str,
        workspace_root: Path,
        bundle_root: Path,
        review_id: str,
    ) -> None:
        """Run the self-contained Pi automatic check inside the Interface runtime."""

        pi_bin = self._pi_cli_bin()
        log_dir = bundle_root / "devs_project" / "_analysis_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_review_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", review_id)[-160:]
        stdout_path = log_dir / f"pi_finalizer_{safe_review_id}.jsonl"
        stderr_path = log_dir / f"pi_finalizer_{safe_review_id}.stderr.txt"
        extension_path = Path(__file__).with_name("pi_devs_run.ts").resolve(strict=True)
        runner_path = Path(__file__).with_name("pi_finalizer_run.py").resolve(strict=True)
        command = [
            pi_bin,
            "--provider",
            os.getenv("DEVS_DISPLAY_PI_PROVIDER", "openrouter").strip()
            or "openrouter",
            "--model",
            os.getenv(
                "DEVS_DISPLAY_PI_MODEL", "deepseek/deepseek-v4.1-flash"
            ).strip()
            or "deepseek/deepseek-v4.1-flash",
            "--thinking",
            os.getenv("DEVS_DISPLAY_PI_THINKING", "off").strip() or "off",
            "--mode",
            "json",
            "--print",
            "--no-session",
            "--no-extensions",
            "--extension",
            str(extension_path),
            "--no-skills",
            "--no-context-files",
            "--no-approve",
            "--tools",
            "read,bash,edit,write,devs_run",
            "--",
            prompt,
        ]
        process_env = dict(os.environ)
        pi_state_root = Path(
            os.getenv("OPTPILOT_INTERFACE_EPHEMERAL_ROOT", "").strip()
            or (Path(self.registry_path).parent / "pi-finalizer-runtime")
        )
        process_env["PI_CODING_AGENT_DIR"] = str(
            pi_state_root / "pi-finalizer" / safe_review_id
        )
        process_env["PI_TELEMETRY"] = "0"
        process_env["PI_DEVS_PYTHON"] = sys.executable
        process_env["PI_DEVS_RUNNER"] = str(runner_path)
        process_env["PI_DEVS_WORKSPACE_ROOT"] = str(workspace_root)
        process_env["PI_DEVS_PROJECT_PATH"] = (
            bundle_root.resolve(strict=True)
            .relative_to(workspace_root.resolve(strict=True))
            .as_posix()
        )
        process_env["PI_DEVS_RUN_ROOT"] = str(
            pi_state_root / "pi-finalizer-runs" / safe_review_id
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace_root),
                text=True,
                capture_output=True,
                timeout=self._codex_finalizer_timeout_seconds(),
                check=False,
                env=process_env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(str(exc.stdout or "")[-12000:], encoding="utf-8")
            stderr_path.write_text(str(exc.stderr or "")[-12000:], encoding="utf-8")
            raise RuntimeError(
                f"Pi automatic check timed out after {exc.timeout} seconds."
            ) from exc

        stdout_path.write_text(str(completed.stdout or "")[-12000:], encoding="utf-8")
        stderr_path.write_text(str(completed.stderr or "")[-12000:], encoding="utf-8")
        if completed.returncode != 0 and not self._codex_finalizer_result_path(
            bundle_root
        ).exists():
            raise RuntimeError(
                "Pi automatic check exited with code "
                f"{completed.returncode}. See {stderr_path.name}."
            )

    def _run_codex_cli_repair(
        self,
        *,
        prompt: str,
        workspace_root: Path,
        bundle_root: Path,
        repair_id: str,
    ) -> None:
        codex_bin = self._codex_cli_bin()

        log_dir = bundle_root / "devs_project" / "_analysis_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_repair_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", repair_id)[-160:]
        stdout_path = log_dir / f"codex_cli_repair_{safe_repair_id}.stdout.txt"
        stderr_path = log_dir / f"codex_cli_repair_{safe_repair_id}.stderr.txt"
        last_message_path = log_dir / f"codex_cli_repair_{safe_repair_id}.last.txt"

        command = [
            codex_bin,
            "exec",
            "--cd",
            str(workspace_root),
            "--sandbox",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        model = (
            os.getenv("DEVS_DISPLAY_CODEX_REPAIR_MODEL", "").strip()
            or os.getenv("DEVS_DISPLAY_CODEX_FINALIZER_MODEL", "").strip()
        )
        if model:
            command.extend(["--model", model])
        command.append("-")

        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace_root),
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self._codex_finalizer_timeout_seconds(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(str(exc.stdout or "")[-12000:], encoding="utf-8")
            stderr_path.write_text(str(exc.stderr or "")[-12000:], encoding="utf-8")
            raise RuntimeError(
                f"Codex CLI repair timed out after {exc.timeout} seconds."
            ) from exc

        stdout_path.write_text(str(completed.stdout or "")[-12000:], encoding="utf-8")
        stderr_path.write_text(str(completed.stderr or "")[-12000:], encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                "Codex CLI repair exited with code "
                f"{completed.returncode}. See {stderr_path.name}."
            )

    def _run_codex_finalizer_for_project(
        self,
        *,
        agent: CodeAgent | ToolCallingAgent,
        session_id: str,
        request_id: str,
        project_id: str,
        validation_record: Dict[str, Any],
        session_workspace: str,
    ) -> Dict[str, Any]:
        if validation_record.get("status") == "awaiting_user_run":
            return validation_record

        try:
            attempt_limit = self._codex_finalizer_attempt_limit()
            workspace_root = Path(session_workspace).resolve(strict=True)
            with self.lock:
                project = self._project_by_id(session_id, project_id)
                bundle_root = self._simulation_bundle_unlocked(session_id, project)
            bundle_root = bundle_root.resolve(strict=True)
            bundle_root.relative_to(workspace_root)
            project_rel = bundle_root.relative_to(workspace_root).as_posix()
        except Exception as exc:
            failed = {
                **validation_record,
                "status": "failed",
                "failure_kind": "codex_finalizer_setup_failed",
                "message": (
                    "Codex finalizer could not prepare the generated bundle: "
                    f"{exc}"
                )[:1000],
            }
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id,
                        project_id,
                        {
                            "status": "failed",
                            "message": failed["message"],
                            "failure_kind": failed["failure_kind"],
                            "completed_at": utc_now(),
                        },
                    )
                except KeyError:
                    pass
            return failed

        review_result: Dict[str, Any] = {}
        current_record = dict(validation_record)
        max_steps = self._codex_finalizer_max_steps()
        driver = self._codex_finalizer_driver()
        for attempt in range(1, attempt_limit + 1):
            review_id = f"{request_id}-{project_id}-{attempt}-{uuid.uuid4().hex[:8]}"
            result_path = self._codex_finalizer_result_path(bundle_root)
            run_arguments = current_record.get("arguments")
            if not isinstance(run_arguments, dict):
                run_arguments = {}
            execution_evidence: Optional[Dict[str, Any]] = None
            if driver == "pi_cli":
                diagnostic = (
                    "No backend behavioral interpretation is supplied; inspect the "
                    "requirements, design, code, and raw execution evidence."
                )
                execution_evidence = self._write_pi_finalizer_evidence(
                    bundle_root=bundle_root,
                    review_id=review_id,
                    record=current_record,
                )
                run_instruction = self._pi_cli_run_instruction(
                    project_rel,
                    bundle_root=bundle_root,
                    arguments=run_arguments,
                )
            else:
                diagnostic = self._codex_finalizer_diagnostic_for_model(
                    current_record, session_workspace
                )
                run_instruction = (
                    f"Use the devs_execute tool exactly as "
                    f"devs_execute(project_path={project_rel!r}, main_file='run.py')."
                    if driver not in {"codex_cli", "remote_codex_cli"}
                    else (
                        self._codex_cli_run_instruction(project_rel)
                        if driver == "codex_cli"
                        else self._remote_codex_run_instruction(project_rel)
                    )
                )
            prompt = self._codex_finalizer_prompt(
                review_id=review_id,
                project_rel=project_rel,
                validation_record=current_record,
                diagnostic=diagnostic,
                run_instruction=run_instruction,
                execution_evidence=execution_evidence,
            )
            activity_key = f"codex_finalizer:{project_id}"
            self._add_activity(
                session_id,
                request_id,
                activity_key=activity_key,
                state="started" if attempt == 1 else "progress",
                title=(
                    "Automatic check is reviewing and repairing the simulation"
                    if attempt == 1
                    else "Automatic check is retrying final repair"
                ),
                detail=(
                    f"Inspecting {project_rel}, fixing if needed, and rerunning "
                    f"the exact bundle (attempt {attempt} of {attempt_limit})."
                ),
                technical_name="automatic check",
            )
            before = self._get_snapshot(session_workspace)
            finalizer_exception: Optional[Exception] = None
            finalizer_started_at = utc_now()
            finalizer_started_monotonic = time.monotonic()
            try:
                if driver == "codex_cli":
                    self._run_codex_cli_finalizer(
                        prompt=prompt,
                        workspace_root=workspace_root,
                        bundle_root=bundle_root,
                        review_id=review_id,
                    )
                elif driver == "pi_cli":
                    self._run_pi_cli_finalizer(
                        prompt=prompt,
                        workspace_root=workspace_root,
                        bundle_root=bundle_root,
                        review_id=review_id,
                    )
                elif driver == "remote_codex_cli":
                    self._run_remote_codex_finalizer(
                        prompt=prompt,
                        bundle_root=bundle_root,
                        project_rel=project_rel,
                        review_id=review_id,
                    )
                elif driver in {"agent", "smolagents"}:
                    with self._agent_progress_context(agent, session_id, request_id):
                        agent.run(prompt, reset=True, max_steps=max_steps)
                else:
                    raise RuntimeError(
                        "Unsupported automatic-check driver "
                        f"{driver!r}; use 'pi_cli', 'codex_cli', "
                        "'remote_codex_cli', or 'agent'."
                    )
                if (bundle_root / "devs_project" / "system_model_info.json").is_file():
                    refresh_generated_interface_registry(bundle_root)
                ensure_simulation_manifest(
                    bundle_root, refresh_derived_metadata=True
                )
            except Exception as exc:
                finalizer_exception = exc
                print(
                    "[Backend] Automatic check stopped "
                    f"({type(exc).__name__}): {exc}"
                )
                traceback.print_exc()

            after = self._get_snapshot(session_workspace)
            changed_names = self._detect_updated_project_names(
                session_workspace, before, after
            )
            changed_files = self._detect_updated_project_files(
                session_workspace, before, after
            )
            changed_source_files = self._changed_source_files(changed_files)
            review_result = self._read_codex_finalizer_result(
                result_path, review_id
            )
            review_result = {
                **review_result,
                "auto_check": True,
                "driver": driver,
                "attempt": attempt,
                "max_attempts": attempt_limit,
                "started_at": finalizer_started_at,
                "completed_at": utc_now(),
                "duration_seconds": round(
                    max(0.0, time.monotonic() - finalizer_started_monotonic),
                    3,
                ),
                "changed_project_names": changed_names,
                "changed_files": changed_files,
                "changed_files_detected": bool(changed_names),
                "changed_source_files": changed_source_files,
                "changed_source_files_detected": bool(changed_source_files),
            }
            if finalizer_exception is not None:
                review_result = {
                    **review_result,
                    "verdict": "fail",
                    "issues": [
                        *(
                            review_result.get("issues")
                            if isinstance(review_result.get("issues"), list)
                            else []
                        ),
                        (
                            "Automatic check stopped before completing: "
                            f"{type(finalizer_exception).__name__}: "
                            f"{finalizer_exception}"
                        ),
                    ],
                }

            if changed_names:
                with self.lock:
                    self._sync_changed_projects_unlocked(session_id, changed_names)
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key=activity_key,
                    state="progress",
                    title="Automatic check changed files",
                    detail=(
                        "Testing the exact files left by the automatic check before "
                        "deciding whether they are ready."
                    ),
                    technical_name="automatic check",
                )
                revalidated = self._validate_simulation_for_publication(
                    session_id, project_id
                )
                if revalidated is not None:
                    current_record = revalidated

            verdict = str(review_result.get("verdict") or "").lower()
            current_status = str(current_record.get("status") or "")
            if (
                finalizer_exception is not None
                and changed_names
                and current_status == "succeeded"
            ):
                summary = (
                    "Automatic check changed files and the exact simulation "
                    "passed the backend smoke test, but the final review did "
                    "not complete cleanly."
                )
                review_result = {
                    **review_result,
                    "verdict": "inconclusive",
                    "summary": summary,
                    "issues": [
                        *(
                            review_result.get("issues")
                            if isinstance(review_result.get("issues"), list)
                            else []
                        ),
                    ],
                }
                with self.lock:
                    try:
                        project = self._project_by_id(session_id, project_id)
                        validation = self._default_validation(project)
                        validation = {
                            **validation,
                            "status": "ready",
                            "message": summary,
                            "codex_finalizer": review_result,
                            "completed_at": utc_now(),
                        }
                        self._save_project_validation_unlocked(
                            session_id, project_id, validation
                        )
                    except KeyError:
                        pass
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key=activity_key,
                    state="completed",
                    title="Automatic check left a runnable simulation",
                    detail=summary,
                    technical_name="automatic check",
                )
                return {
                    **current_record,
                    "status": "succeeded",
                    "codex_finalizer": review_result,
                }
            if verdict == "pass" and current_status == "succeeded":
                with self.lock:
                    try:
                        project = self._project_by_id(session_id, project_id)
                        validation = self._default_validation(project)
                        validation = {
                            **validation,
                            "status": "ready",
                            "message": (
                                "Automatic check reviewed this exact simulation "
                                "and marked it ready."
                            ),
                            "codex_finalizer": review_result,
                            "completed_at": utc_now(),
                        }
                        self._save_project_validation_unlocked(
                            session_id, project_id, validation
                        )
                    except KeyError:
                        pass
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key=activity_key,
                    state="completed",
                    title="Automatic check passed the simulation",
                    detail=str(review_result.get("summary") or "Final review passed.")[:1000],
                    technical_name="automatic check",
                )
                return {
                    **current_record,
                    "status": "succeeded",
                    "codex_finalizer": review_result,
                }

            if attempt >= attempt_limit:
                break

        issues = review_result.get("issues")
        if not isinstance(issues, list):
            issues = []
        summary = str(review_result.get("summary") or "").strip()
        message = (
            summary
            or "Automatic check did not pass this generated simulation."
        )
        if issues:
            message = f"{message} Issues: {'; '.join(str(item) for item in issues[:5])}"
        message = message[:1000]
        failed = {
            **current_record,
            "status": "failed",
            "failure_kind": "codex_finalizer_failed",
            "message": message,
            "codex_finalizer": review_result,
        }
        with self.lock:
            try:
                project = self._project_by_id(session_id, project_id)
                validation = self._default_validation(project)
                validation = {
                    **validation,
                    "status": "failed",
                    "message": message,
                    "failure_kind": "codex_finalizer_failed",
                    "codex_finalizer": review_result,
                    "completed_at": utc_now(),
                }
                self._save_project_validation_unlocked(
                    session_id, project_id, validation
                )
            except KeyError:
                pass
        self._add_activity(
            session_id,
            request_id,
            activity_key=f"codex_finalizer:{project_id}",
            state="failed",
            title="Automatic check did not pass the simulation",
            detail=message,
            technical_name="automatic check",
        )
        return failed

    def _repair_diagnostic_for_model(
        self,
        record: Dict[str, Any],
        workspace: str,
    ) -> str:
        """Build a bounded diagnostic without forwarding local credentials.

        Execution output is generated code and therefore untrusted.  Before it
        is sent to an external model, remove credential-shaped assignments,
        bearer tokens, sensitive host environment values, and host path roots.
        """

        parts: List[str] = []
        for candidate in (
            self._validation_failure_message(record),
            str(record.get("stderr") or "").strip(),
            str(record.get("stdout") or "").strip(),
        ):
            if candidate and candidate not in parts:
                parts.append(candidate)
        diagnostic = "\n".join(parts)
        for name, value in os.environ.items():
            normalized_name = name.lower().replace("-", "_")
            if not any(part in normalized_name for part in _SENSITIVE_ENV_NAME_PARTS):
                continue
            if len(value) >= 4:
                diagnostic = diagnostic.replace(value, "[redacted]")
        diagnostic = _REPAIR_SECRET_ASSIGNMENT_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
            diagnostic,
        )
        diagnostic = _REPAIR_BEARER_RE.sub("Bearer [redacted]", diagnostic)
        path_roots = {
            str(Path(workspace).resolve()): "[session-workspace]",
            str(Path.home().resolve()): "[home]",
            str(Path(tempfile.gettempdir()).resolve()): "[temporary]",
        }
        for path_root, replacement in sorted(
            path_roots.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if path_root and path_root != os.sep:
                diagnostic = diagnostic.replace(path_root, replacement)
        diagnostic = "".join(
            character
            for character in diagnostic
            if character in "\n\r\t" or ord(character) >= 32
        )
        encoded = diagnostic.encode("utf-8", errors="replace")
        if len(encoded) > 6000:
            encoded = encoded[-6000:]
            diagnostic = "...[earlier diagnostic truncated]\n" + encoded.decode(
                "utf-8", errors="ignore"
            )
        return diagnostic or "The bounded smoke test failed without a diagnostic."

    def _codex_finalizer_diagnostic_for_model(
        self,
        record: Dict[str, Any],
        workspace: str,
    ) -> str:
        """Build a compact final-review hint without raw simulator logs."""

        stdout = str(record.get("stdout") or "")
        stderr = str(record.get("stderr") or "")
        parts: List[str] = [
            f"validation_message={self._validation_failure_message(record)}",
            f"stdout_lines={len(stdout.splitlines())}",
            f"stderr_lines={len(stderr.splitlines())}",
            f"stdout_bytes={len(stdout.encode('utf-8', errors='replace'))}",
            f"stderr_bytes={len(stderr.encode('utf-8', errors='replace'))}",
        ]

        events = self._json_business_events(stdout)
        if events:
            event_counts: Dict[str, int] = {}
            resource_counts: Dict[str, int] = {}
            for event in events:
                event_name = self._event_name(event) or "<missing>"
                event_counts[event_name] = event_counts.get(event_name, 0) + 1
                resource_type = event.get("resource_type")
                if isinstance(resource_type, str):
                    key = f"{event_name}:{resource_type}"
                    resource_counts[key] = resource_counts.get(key, 0) + 1
            parts.append(
                "event_counts="
                + json.dumps(
                    dict(sorted(event_counts.items())[:40]),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if resource_counts:
                parts.append(
                    "resource_event_counts="
                    + json.dumps(
                        dict(sorted(resource_counts.items())[:40]),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )

        keywords = (
            "warning",
            "error",
            "traceback",
            "exception",
            "failed",
            "timeout",
            "ignored",
            "busy",
            "invalid",
            "inconsistent",
            "none",
            "null",
            "deadlock",
            "stall",
        )
        suspicious: List[str] = []
        for stream_name, text in (("stderr", stderr), ("stdout", stdout)):
            for line_no, raw_line in enumerate(text.splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                lower_line = line.lower()
                if not any(keyword in lower_line for keyword in keywords):
                    continue
                compact = line[:240]
                suspicious.append(f"{stream_name}:{line_no}: {compact}")
                if len(suspicious) >= 30:
                    break
            if len(suspicious) >= 30:
                break
        if suspicious:
            parts.append("suspicious_lines=\n" + "\n".join(suspicious))

        diagnostic = "\n".join(parts)
        return self._repair_diagnostic_for_model(
            {"message": diagnostic},
            workspace,
        )

    @staticmethod
    def _automatic_repair_is_appropriate(record: Dict[str, Any]) -> bool:
        """Return whether generated files can plausibly fix this failure.

        A model repair pass can address a bad entry point, a non-zero exit, or
        bounded-output/result contract failures. It cannot install or repair
        the execution service, recover supervisor capacity, or fix an internal
        launch failure. Keeping this as an explicit allowlist prevents new
        infrastructure failures from silently consuming another LLM request.
        """

        return (
            record.get("status") in {"failed", "timed_out"}
            and str(record.get("failure_kind") or "")
            in _AUTOMATIC_REPAIR_FAILURE_KINDS
        )

    @staticmethod
    def _retryable_agent_transport_error(exc: Exception) -> bool:
        """Recognize a bounded set of temporary model-transport failures.

        This intentionally does not classify authentication, configuration, or
        arbitrary provider errors as retryable.  It is used only for a targeted
        repair pass after the backend has independently reproduced a generated
        code failure, so retrying cannot repeat the original generation plan.
        """

        description = f"{type(exc).__name__}: {exc}".lower()
        retryable_fragments = (
            "incomplete chunked read",
            "peer closed connection",
            "connection reset",
            "connection aborted",
            "connection closed",
            "read timeout",
            "readtimeout",
            "temporarily unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "too many requests",
            "rate limit",
        )
        return any(fragment in description for fragment in retryable_fragments)

    def _terminalize_unverified_request_projects_unlocked(
        self,
        session_id: str,
        request: Dict[str, Any],
        *,
        message: str,
        failure_kind: str,
    ) -> None:
        """Fail only project states that cannot become terminal by themselves."""

        for project_id in request.get("updated_project_ids") or []:
            try:
                project = self._project_by_id(session_id, str(project_id))
            except KeyError:
                continue
            validation = self._default_validation(project)
            if (
                project.get("status") != "updating"
                and validation.get("status") not in {"stale", "validating"}
            ):
                continue
            self._save_project_validation_unlocked(
                session_id,
                str(project_id),
                {
                    "status": "failed",
                    "message": message[:1000],
                    "failure_kind": failure_kind,
                    "completed_at": utc_now(),
                },
            )

    def _prepare_simulation_execution_unlocked(
        self,
        session_id: str,
        project_id: str,
        arguments: Optional[Dict[str, Any]],
        *,
        purpose: str,
        permit_active_agent_request: bool = False,
        publish_on_success: bool = False,
    ) -> Dict[str, Any]:
        session = self._load_session(session_id)
        if (
            not permit_active_agent_request
            and session.get("status") in ACTIVE_SESSION_STATUSES
        ):
            raise RuntimeError(
                "Wait for the agent request to finish before running this simulation."
            )
        if (
            not permit_active_agent_request
            and self._session_has_active_simulation_unlocked(session_id)
        ):
            raise RuntimeError(
                "Another simulation in this design session is still running."
            )
        project = self._project_by_id(session_id, project_id)
        bundle = self._simulation_bundle_unlocked(session_id, project)
        _assert_regular_source_tree(bundle)
        self._ensure_storage_capacity(
            "preparing a simulation run",
            estimated_new_bytes=self._tree_regular_file_size(bundle),
        )
        queued = self.simulation_execution_service.prepare(
            bundle,
            arguments or {},
            purpose=purpose,
        )
        execution_id = str(queued["execution_id"])
        self.simulation_execution_context[execution_id] = {
            "session_id": session_id,
            "project_id": project_id,
            "project_version": int(project.get("version", 1)),
            "bundle_path": str(bundle),
            "bundle_digest": queued.get("bundle_digest")
            or queued.get("snapshot_digest"),
            "purpose": purpose,
            "publish_on_success": publish_on_success,
            # The process record becomes terminal before exact-version
            # validation and optional output publication have completed.  Keep
            # that post-processing visible as part of the public Run lifecycle
            # so clients cannot mistake a half-finalized Run for a finished one.
            "finalization_status": (
                "pending" if purpose == "validation" else "not_required"
            ),
            "finalization_error": None,
            "validation_result": None,
        }
        if purpose == "validation":
            self._save_project_validation_unlocked(
                session_id,
                project_id,
                {
                    "status": "validating",
                    "message": "Running a small smoke test of this exact version.",
                    "execution_id": execution_id,
                    "project_version": int(project.get("version", 1)),
                    "bundle_digest": queued.get("bundle_digest")
                    or queued.get("snapshot_digest"),
                    "started_at": utc_now(),
                },
            )
        return queued

    def _finalize_simulation_execution(
        self, execution_id: str, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        context = self.simulation_execution_context.get(execution_id)
        if context is None or context.get("purpose") != "validation":
            return record
        publication: Optional[Dict[str, Any]] = None
        with self.lock:
            try:
                project = self._project_by_id(
                    context["session_id"], context["project_id"]
                )
                bundle = self._simulation_bundle_unlocked(
                    context["session_id"], project
                )
                current_digest = stable_tree_digest(bundle)
            except (KeyError, FileNotFoundError, OSError, RuntimeError, ValueError):
                context["finalization_error"] = (
                    "The simulation process finished, but its exact-version "
                    "result could not be checked. You can run this version again."
                )
                return record

            same_version = int(project.get("version", 1)) == int(
                context["project_version"]
            )
            same_content = current_digest == context.get("bundle_digest")
            if not same_version or not same_content:
                validation = {
                    "status": "stale",
                    "message": "The simulation changed while its smoke test was running.",
                    "execution_id": execution_id,
                    "completed_at": utc_now(),
                }
                context["finalization_error"] = validation["message"]
            elif record.get("status") == "succeeded":
                # This method returns before reaching here for every purpose
                # except deterministic exact-version validation. A future
                # observational run therefore cannot silently become a repair
                # or publication verdict.
                behavior = assess_behavior_smoke(
                    bundle,
                    self.simulation_execution_service.execution_root
                    / execution_id
                    / "results",
                )
                if behavior.status == "stalled":
                    validation = {
                        "status": "unverified",
                        "message": behavior.message,
                        "failure_kind": "behavior_stalled",
                        "behavior_check": behavior.to_dict(),
                        "execution_id": execution_id,
                        "project_version": int(project.get("version", 1)),
                        "bundle_digest": current_digest,
                        "completed_at": utc_now(),
                    }
                elif (
                    not self._codex_finalizer_enabled()
                    and
                    os.getenv("DEVS_DISPLAY_CODEX_FINAL_GATE", "1").strip()
                    not in {"0", "false", "False"}
                    and (acceptance_issues := self._codex_acceptance_issues(record))
                ):
                    message = (
                        "Codex final gate rejected this executable simulation "
                        "because the retained run log shows likely broken "
                        "business flow: "
                        + "; ".join(acceptance_issues)
                    )[:1000]
                    validation = {
                        "status": "failed",
                        "message": message,
                        "failure_kind": "acceptance_failed",
                        "acceptance_check": {
                            "status": "failed",
                            "issues": acceptance_issues,
                        },
                        "behavior_check": behavior.to_dict(),
                        "execution_id": execution_id,
                        "project_version": int(project.get("version", 1)),
                        "bundle_digest": current_digest,
                        "completed_at": utc_now(),
                    }
                else:
                    validation = {
                        "status": "ready",
                        "message": "This exact simulation version completed its smoke test.",
                        "execution_id": execution_id,
                        "project_version": int(project.get("version", 1)),
                        "bundle_digest": current_digest,
                        "completed_at": utc_now(),
                    }
                    validation["behavior_check"] = behavior.to_dict()
            elif record.get("status") == "stopped":
                validation = {
                    "status": "unverified",
                    "message": (
                        "This run was stopped before the simulation could be "
                        "verified. You can run this version again."
                    ),
                    "execution_id": execution_id,
                    "project_version": int(project.get("version", 1)),
                    "bundle_digest": current_digest,
                    "completed_at": utc_now(),
                }
            elif record.get("failure_kind") in (
                _EXECUTION_INFRASTRUCTURE_FAILURE_KINDS
            ):
                reason = self._validation_failure_message(record)
                validation = {
                    "status": "unverified",
                    "message": (
                        "The simulation runner was unavailable, so this exact "
                        "version could not be verified. Its generated files "
                        f"were kept. {reason}"
                    )[:1000],
                    "failure_kind": record.get("failure_kind"),
                    "execution_id": execution_id,
                    "project_version": int(project.get("version", 1)),
                    "bundle_digest": current_digest,
                    "completed_at": utc_now(),
                }
            else:
                validation = {
                    "status": "failed",
                    "message": self._validation_failure_message(record),
                    "failure_kind": record.get("failure_kind"),
                    "execution_id": execution_id,
                    "project_version": int(project.get("version", 1)),
                    "bundle_digest": current_digest,
                    "completed_at": utc_now(),
                }
            self._save_project_validation_unlocked(
                context["session_id"], context["project_id"], validation
            )
            context["validation_result"] = dict(validation)
            if (
                validation.get("status") == "ready"
                and context.get("publish_on_success")
                and self.interface_output_publisher is not None
            ):
                publication = {
                    "session_id": context["session_id"],
                    "workspace": Path(
                        self._session_workspace(context["session_id"])
                    ),
                    "project": dict(project),
                    "bundle_digest": current_digest,
                }
        if publication is not None:
            try:
                output = self.interface_output_publisher.publish_ready_project(
                    session_id=publication["session_id"],
                    request_id="manual-validation",
                    workspace=publication["workspace"],
                    project=publication["project"],
                    expected_content_digest=publication["bundle_digest"],
                )
                if output is None:
                    raise RuntimeError(
                        "The tested simulation is missing README.md or devs_project/."
                    )
                with self.lock:
                    published_validation = {
                        **validation,
                        "message": (
                            "This exact simulation version passed and is ready "
                            "to save as a Workspace."
                        ),
                        "interface_output_id": output["id"],
                    }
                    self._save_project_validation_unlocked(
                        context["session_id"],
                        context["project_id"],
                        published_validation,
                    )
                    context["validation_result"] = dict(published_validation)
            except Exception as exc:
                finalization_error = (
                    "The simulation passed, but its output could not be "
                    f"prepared: {exc}"
                )[:1000]
                with self.lock:
                    context["finalization_error"] = finalization_error
                    failed_validation = {
                        **validation,
                        "status": "unverified",
                        "message": finalization_error,
                        "failure_kind": "finalization_failed",
                    }
                    self._save_project_validation_unlocked(
                        context["session_id"],
                        context["project_id"],
                        failed_validation,
                    )
                    context["validation_result"] = dict(failed_validation)
        return record

    def _complete_simulation_execution(
        self, execution_id: str, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Finish validation/publication before exposing a terminal Run.

        The execution supervisor owns process completion, while this service
        owns the exact-version validation and interface-output publication that
        make a successful Run useful to OptPilot.  Treat both as one public
        lifecycle even when finalization itself encounters an unexpected error.
        """

        try:
            return self._finalize_simulation_execution(execution_id, record)
        except Exception as exc:
            # The child process may already have succeeded, but a failure while
            # validating or publishing its exact bytes is still a failed
            # user-visible Run.  Retain that second-stage outcome separately
            # instead of letting later reads fall back to the process status.
            with self.lock:
                context = self.simulation_execution_context.get(execution_id)
                if context is not None:
                    context["finalization_error"] = (
                        "The simulation process finished, but its exact-version "
                        f"result could not be prepared: {exc}"
                    )[:1000]
            return record
        finally:
            with self.lock:
                context = self.simulation_execution_context.get(execution_id)
                if context is not None:
                    # Any unexpected finalization path must release the
                    # persisted `validating` state too; otherwise Run would be
                    # disabled forever even though the process is over.
                    try:
                        project = self._project_by_id(
                            context["session_id"], context["project_id"]
                        )
                        validation = self._default_validation(project)
                        if (
                            validation.get("status") == "validating"
                            and validation.get("execution_id") == execution_id
                        ):
                            self._save_project_validation_unlocked(
                                context["session_id"],
                                context["project_id"],
                                {
                                    "status": "unverified",
                                    "message": (
                                        "The exact-version check could not be "
                                        "finished. You can run this version again."
                                    ),
                                    "failure_kind": "finalization_failed",
                                    "execution_id": execution_id,
                                    "completed_at": utc_now(),
                                },
                            )
                    except (KeyError, OSError, RuntimeError, ValueError):
                        pass
                    context["finalization_status"] = (
                        "failed"
                        if context.get("finalization_error")
                        else "complete"
                    )
                    try:
                        self._record_trace_unlocked(
                            context["session_id"],
                            "simulation_run_completed",
                            actor="system",
                            project_id=context["project_id"],
                            payload={
                                "execution_id": execution_id,
                                "process_status": record.get("status"),
                                "finalization_status": context.get(
                                    "finalization_status"
                                ),
                                "validation_status": (
                                    context.get("validation_result") or {}
                                ).get("status"),
                                "failure_kind": (
                                    context.get("validation_result") or {}
                                ).get("failure_kind")
                                or record.get("failure_kind"),
                            },
                        )
                    except Exception as exc:
                        print(
                            "[Backend] Failed to record simulation trace: "
                            f"{type(exc).__name__}: {exc}"
                        )

    def _run_simulation_execution(self, execution_id: str) -> None:
        collector_session_id: Optional[str] = None
        try:
            record = self.simulation_execution_service.run(execution_id)
            self._complete_simulation_execution(execution_id, record)
        finally:
            with self.lock:
                context = self.simulation_execution_context.get(execution_id)
                if context is not None:
                    collector_session_id = context.get("session_id")
                self.simulation_execution_threads.pop(execution_id, None)
            if collector_session_id:
                self._schedule_collector_sync(
                    collector_session_id,
                    include_snapshots=True,
                )

    def _validate_simulation_for_publication(
        self, session_id: str, project_id: str
    ) -> Optional[Dict[str, Any]]:
        """Smoke-test one generated bundle and retain the exact-version result."""

        # Automatic validation can only choose a scenario when every required
        # input has a declared default.  A missing student choice is not broken
        # simulator code and must never send the agent into an automatic repair
        # loop.  Leave the exact version unverified until the student supplies
        # the scenario through the Run tab.
        try:
            with self.lock:
                project = self._project_by_id(session_id, project_id)
                bundle = self._simulation_bundle_unlocked(session_id, project)
            metadata = simulation_metadata(
                bundle,
                maximum_timeout_seconds=(
                    self.simulation_execution_service.maximum_timeout_seconds
                ),
            )
            required_inputs = [
                str(parameter.get("label") or parameter.get("name"))
                for parameter in metadata.get("parameters", [])
                if isinstance(parameter, dict)
                and parameter.get("required") is True
                and "default" not in parameter
            ]
        except FileNotFoundError as exc:
            validation = {
                "status": "failed",
                "message": str(exc)[:1000],
                "failure_kind": "invalid_bundle",
                "completed_at": utc_now(),
            }
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id, project_id, validation
                    )
                except KeyError:
                    pass
            return validation
        except (SimulationExecutionError, OSError, ValueError) as exc:
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id,
                        project_id,
                        {
                            "status": "failed",
                            "message": str(exc)[:1000],
                            "completed_at": utc_now(),
                        },
                    )
                except KeyError:
                    pass
            return {
                "status": "failed",
                "message": str(exc),
                "failure_kind": "invalid_bundle",
            }

        if required_inputs:
            names = ", ".join(required_inputs)
            message = (
                "This simulation needs your input before it can be verified. "
                f"Open Run and provide: {names}."
            )
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id,
                        project_id,
                        {
                            "status": "unverified",
                            "message": message[:1000],
                            "required_inputs": required_inputs,
                            "completed_at": utc_now(),
                        },
                    )
                except KeyError:
                    pass
            return {
                "status": "awaiting_user_run",
                "message": message,
                "failure_kind": "required_input",
            }

        try:
            with self.lock:
                queued = self._prepare_simulation_execution_unlocked(
                    session_id,
                    project_id,
                    {},
                    purpose="validation",
                    permit_active_agent_request=True,
                )
        except FileNotFoundError as exc:
            validation = {
                "status": "failed",
                "message": str(exc)[:1000],
                "failure_kind": "invalid_bundle",
                "completed_at": utc_now(),
            }
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id, project_id, validation
                    )
                except KeyError:
                    pass
            return validation
        except (SimulationExecutionError, OSError, ValueError) as exc:
            with self.lock:
                try:
                    self._save_project_validation_unlocked(
                        session_id,
                        project_id,
                        {
                            "status": "failed",
                            "message": str(exc)[:1000],
                            "completed_at": utc_now(),
                        },
                    )
                except KeyError:
                    pass
            return {
                "status": "failed",
                "message": str(exc),
                "failure_kind": "invalid_bundle",
            }

        execution_id = str(queued["execution_id"])
        record = self.simulation_execution_service.run(execution_id)
        self._complete_simulation_execution(execution_id, record)
        context = self.simulation_execution_context.get(execution_id)
        validation_result = (
            context.get("validation_result")
            if isinstance(context, dict)
            else None
        )
        if (
            isinstance(validation_result, dict)
            and validation_result.get("failure_kind")
            in {"acceptance_failed", "behavior_stalled"}
        ):
            return {
                **record,
                "status": "failed",
                "failure_kind": validation_result.get("failure_kind"),
                "message": validation_result.get("message"),
                "behavior_check": validation_result.get("behavior_check"),
                "acceptance_check": validation_result.get("acceptance_check"),
            }
        return record

    def _public_simulation_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        public = dict(record)
        public["run_id"] = public.get("execution_id")
        public["completed_at"] = public.get("finished_at")
        public["error"] = public.get("message")
        metrics: Dict[str, Any] = {}
        execution_id = str(public.get("execution_id") or "")
        # Some callers render the queued response while already holding the
        # service lock.  Context values are only replaced atomically, so this
        # read intentionally avoids recursively acquiring the non-reentrant
        # lock.
        context = self.simulation_execution_context.get(execution_id)
        finalization_status = (
            context.get("finalization_status")
            if isinstance(context, dict)
            else None
        )
        execution_status = str(public.get("status") or "")
        if (
            execution_status in {"succeeded", "failed", "timed_out", "stopped"}
            and finalization_status == "pending"
        ):
            public["execution_status"] = execution_status
            public["status"] = "finalizing"
            public["error"] = None
            public["message"] = (
                "The simulation process finished; preparing its exact-version result."
            )
        elif (
            execution_status in {"succeeded", "failed", "timed_out", "stopped"}
            and finalization_status == "failed"
        ):
            finalization_error = str(
                context.get("finalization_error")
                or "The exact-version result could not be prepared."
            )
            public["execution_status"] = execution_status
            public["status"] = "failed"
            public["failure_kind"] = "finalization_failed"
            public["error"] = finalization_error
            public["message"] = finalization_error
        elif (
            execution_status == "succeeded"
            and finalization_status == "complete"
            and isinstance(context, dict)
            and isinstance(context.get("validation_result"), dict)
            and context["validation_result"].get("failure_kind")
            == "behavior_stalled"
        ):
            behavior_message = str(
                context["validation_result"].get("message")
                or "The simulation ran, but its expected main behavior stalled."
            )
            public["execution_status"] = execution_status
            public["status"] = "failed"
            public["failure_kind"] = "behavior_stalled"
            public["behavior_check"] = context["validation_result"].get(
                "behavior_check"
            )
            public["error"] = behavior_message
            public["message"] = behavior_message
        elif public.get("stop_requested") and execution_status in {"queued", "running"}:
            public["status"] = "stopping"
            public["error"] = None
            public["message"] = "Stopping the simulation safely."
        result_files = public.get("result_files")
        if isinstance(result_files, (list, tuple)):
            described_results: List[Dict[str, Any]] = []
            for item in result_files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                described = dict(item)
                media_type = _simulation_result_media_type(item["path"])
                size = item.get("size")
                described["media_type"] = media_type
                described["previewable"] = bool(
                    type(size) is int
                    and size <= MAX_SIMULATION_RESULT_PREVIEW_BYTES
                    and _simulation_result_is_previewable(item["path"], media_type)
                )
                described["downloadable"] = bool(
                    type(size) is int
                    and size <= MAX_SIMULATION_RESULT_DOWNLOAD_BYTES
                )
                described_results.append(described)
            public["result_files"] = described_results
            result_files = described_results
            preferred = ("metrics.json", "summary.json", "results.json")
            available_paths = [
                str(item.get("path"))
                for item in result_files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]
            result_root = (
                self.simulation_execution_service.execution_root
                / execution_id
                / "results"
            ).resolve()
            summary_paths: List[str] = []
            for preferred_name in preferred:
                matching = sorted(
                    relative
                    for relative in available_paths
                    if PurePosixPath(relative).name == preferred_name
                )
                if preferred_name in matching:
                    matching.remove(preferred_name)
                    matching.insert(0, preferred_name)
                summary_paths.extend(matching)
            for relative in summary_paths:
                try:
                    candidate_path = result_root.joinpath(
                        *PurePosixPath(relative).parts
                    )
                    metadata = candidate_path.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        continue
                    result_path = candidate_path.resolve(strict=True)
                    result_path.relative_to(result_root)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size > 1024 * 1024
                    ):
                        continue
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    continue
                candidate = (
                    payload.get("metrics")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("metrics"), dict)
                    else payload
                )
                if isinstance(candidate, dict):
                    metrics = {
                        str(name): value
                        for name, value in candidate.items()
                        if type(value) in (str, bool, int, float) or value is None
                    }
                break
        if metrics:
            public["metrics"] = metrics
        return public

    def get_simulation_result_file(
        self,
        session_id: str,
        project_id: str,
        execution_id: str,
        relative_path: str,
        *,
        download: bool = False,
    ) -> Dict[str, Any]:
        """Read one retained result through its execution inventory allowlist."""

        canonical_path = _canonical_simulation_result_path(relative_path)
        with self.lock:
            context = self.simulation_execution_context.get(execution_id)
            if (
                context is None
                or context.get("session_id") != session_id
                or context.get("project_id") != project_id
            ):
                raise KeyError(execution_id)

        try:
            record = self.simulation_execution_service.get_record(execution_id)
        except ExecutionStateError as exc:
            raise KeyError(execution_id) from exc
        inventory_item = next(
            (
                item
                for item in record.get("result_files", ())
                if isinstance(item, dict) and item.get("path") == canonical_path
            ),
            None,
        )
        if inventory_item is None:
            raise KeyError(canonical_path)
        expected_size = inventory_item.get("size")
        expected_sha256 = inventory_item.get("sha256")
        if (
            type(expected_size) is not int
            or expected_size < 0
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise FileNotFoundError("The retained result record is invalid.")

        media_type = _simulation_result_media_type(canonical_path)
        previewable = _simulation_result_is_previewable(canonical_path, media_type)
        if not download and not previewable:
            raise TypeError("This result type is available as a download, not a text preview.")
        byte_limit = (
            MAX_SIMULATION_RESULT_DOWNLOAD_BYTES
            if download
            else MAX_SIMULATION_RESULT_PREVIEW_BYTES
        )
        if expected_size > byte_limit:
            action = "download" if download else "preview"
            raise OverflowError(f"This result is too large to {action}.")

        result_root = (
            self.simulation_execution_service.execution_root
            / execution_id
            / "results"
        )
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptors: List[int] = []
        try:
            current_descriptor = os.open(result_root, directory_flags)
            descriptors.append(current_descriptor)
            root_metadata = os.fstat(current_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise FileNotFoundError("Simulation result storage is unavailable.")
            parts = PurePosixPath(canonical_path).parts
            for part in parts[:-1]:
                current_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
                descriptors.append(current_descriptor)
                metadata = os.fstat(current_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise FileNotFoundError("Simulation result path is unavailable.")
            file_descriptor = os.open(
                parts[-1],
                file_flags,
                dir_fd=current_descriptor,
            )
            descriptors.append(file_descriptor)
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise FileNotFoundError(
                    "The result no longer matches its retained execution record."
                )
            chunks: List[bytes] = []
            total = 0
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_descriptor, min(128 * 1024, byte_limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
                total += len(chunk)
                if total > byte_limit:
                    action = "download" if download else "preview"
                    raise OverflowError(f"This result is too large to {action}.")
            after = os.fstat(file_descriptor)
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or total != expected_size
                or digest.hexdigest() != expected_sha256
            ):
                raise FileNotFoundError(
                    "The result no longer matches its retained execution record."
                )
            content = b"".join(chunks)
        except (FileNotFoundError, NotADirectoryError):
            raise
        except OSError as exc:
            raise FileNotFoundError("Simulation result file is unavailable.") from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        response: Dict[str, Any] = {
            "path": canonical_path,
            "size": expected_size,
            "sha256": expected_sha256,
            "media_type": media_type,
            "previewable": previewable,
            "downloadable": expected_size <= MAX_SIMULATION_RESULT_DOWNLOAD_BYTES,
        }
        if download:
            response["content"] = content
            return response
        try:
            response["content"] = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TypeError("This result is not valid UTF-8 text; download it instead.") from exc
        return response

    def get_project_simulation(
        self, session_id: str, project_id: str
    ) -> Dict[str, Any]:
        with self.lock:
            session = self._load_session(session_id)
            project = self._project_by_id(session_id, project_id)
            validation = self._default_validation(project)
            if session.get("status") in ACTIVE_SESSION_STATUSES:
                return {
                    "available": False,
                    "parameters": [],
                    "validation_status": "validating",
                    "validation_message": (
                        "The agent is changing this design. Running becomes available "
                        "after it finishes."
                    ),
                }
            try:
                bundle = self._simulation_bundle_unlocked(session_id, project)
                _assert_regular_source_tree(bundle)
                metadata = simulation_metadata(
                    bundle,
                    maximum_timeout_seconds=(
                        self.simulation_execution_service.maximum_timeout_seconds
                    ),
                )
            except (FileNotFoundError, SimulationExecutionError, ValueError) as exc:
                return {
                    "available": False,
                    "parameters": [],
                    "validation_status": validation.get("status", "unverified"),
                    "validation_message": str(exc),
                }
            return {
                "available": True,
                "entrypoint": metadata["entrypoint"],
                "parameters": metadata["parameters"],
                "result_files": metadata["result_files"],
                "validation_status": validation.get("status", "unverified"),
                "validation_message": validation.get("message"),
            }

    def start_simulation_run(
        self,
        session_id: str,
        project_id: str,
        *,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            queued = self._prepare_simulation_execution_unlocked(
                session_id,
                project_id,
                arguments,
                purpose="validation",
                publish_on_success=True,
            )
            execution_id = str(queued["execution_id"])
            thread = Thread(
                target=self._run_simulation_execution,
                args=(execution_id,),
                daemon=True,
            )
            self.simulation_execution_threads[execution_id] = thread
            thread.start()
            self._record_trace_unlocked(
                session_id,
                "simulation_run_started",
                actor="user",
                project_id=project_id,
                payload={
                    "execution_id": execution_id,
                    "argument_names": sorted((arguments or {}).keys()),
                },
            )
            return self._public_simulation_record(queued)

    def start_simulation_validation(
        self,
        session_id: str,
        project_id: str,
        *,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.lock:
            queued = self._prepare_simulation_execution_unlocked(
                session_id,
                project_id,
                arguments,
                purpose="validation",
                publish_on_success=True,
            )
            execution_id = str(queued["execution_id"])
            thread = Thread(
                target=self._run_simulation_execution,
                args=(execution_id,),
                daemon=True,
            )
            self.simulation_execution_threads[execution_id] = thread
            thread.start()
            self._record_trace_unlocked(
                session_id,
                "simulation_validation_started",
                actor="user",
                project_id=project_id,
                payload={
                    "execution_id": execution_id,
                    "argument_names": sorted((arguments or {}).keys()),
                },
            )
            return self._public_simulation_record(queued)

    def get_simulation_run(
        self, session_id: str, project_id: str, execution_id: str
    ) -> Dict[str, Any]:
        with self.lock:
            context = self.simulation_execution_context.get(execution_id)
            if (
                context is None
                or context.get("session_id") != session_id
                or context.get("project_id") != project_id
            ):
                raise KeyError(execution_id)
        return self._public_simulation_record(
            self.simulation_execution_service.get_record(execution_id)
        )

    def stop_simulation_run(
        self, session_id: str, project_id: str, execution_id: str
    ) -> Dict[str, Any]:
        with self.lock:
            context = self.simulation_execution_context.get(execution_id)
            if (
                context is None
                or context.get("session_id") != session_id
                or context.get("project_id") != project_id
            ):
                raise KeyError(execution_id)
        self.simulation_execution_service.stop(execution_id)
        return self._public_simulation_record(
            self.simulation_execution_service.get_record(execution_id)
        )

    def _read_project_files_unlocked(self, project: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, str]:
        project_path = self._project_abs_path(project, session_id)
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [
                d
                for d in dirs
                if d != META_DIR_NAME
                and not d.startswith(".")
                and d != "__pycache__"
                and not (Path(root) / d).is_symlink()
            ]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                try:
                    metadata = os.lstat(abs_path)
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        files_data[rel_path] = f.read()
                except Exception:
                    continue
        return files_data

    def _load_graph_cache(self, session_id: str, project_id: str):
        return self._read_json(self._graph_cache_path(session_id, project_id), None)

    def _save_graph_cache(self, session_id: str, project_id: str, payload: Dict[str, Any]):
        self._write_json(self._graph_cache_path(session_id, project_id), payload)

    def get_project_graph(self, session_id: str, project_id: str, start_if_missing: bool = True):
        with self.lock:
            self._load_session(session_id)
            self._project_by_id(session_id, project_id)
            cached = self._load_graph_cache(session_id, project_id)
            if cached:
                return cached
            if not start_if_missing:
                return {"parse": {"status": "missing"}, "graph": None}
            return self._start_project_graph_parse_unlocked(session_id, project_id, "openai", DEFAULT_GRAPH_PARSE_MODEL, None, False)

    def start_project_graph_parse(self, session_id: str, project_id: str, provider: str, model: str, api_key: Optional[str], force: bool = False):
        with self.lock:
            self._load_session(session_id)
            self._project_by_id(session_id, project_id)
            return self._start_project_graph_parse_unlocked(session_id, project_id, provider, model, api_key, force)

    def _start_project_graph_parse_unlocked(self, session_id: str, project_id: str, provider: str, model: str, api_key: Optional[str], force: bool):
        cached = self._load_graph_cache(session_id, project_id)
        if cached and cached.get("parse", {}).get("status") == "running" and not force:
            return cached
        if cached and cached.get("parse", {}).get("status") == "completed" and not force:
            return cached

        payload = {
            "parse": {
                "status": "running",
                "started_at": utc_now(),
                "completed_at": None,
                "error": None,
                "provider": provider,
                "model": model,
            },
            "graph": None,
        }
        self._save_graph_cache(session_id, project_id, payload)

        thread = Thread(
            target=self._run_project_graph_parse,
            args=(session_id, project_id, provider, model, api_key),
            daemon=True,
        )
        thread.start()
        return payload

    def _run_project_graph_parse(self, session_id: str, project_id: str, provider: str, model: str, api_key: Optional[str]):
        try:
            with self.lock:
                project = self._project_by_id(session_id, project_id)
                files = self._read_project_files_unlocked(project, session_id)
            graph = build_project_graph(files, provider, model, api_key)
            payload = {
                "parse": {
                    "status": "completed",
                    "started_at": self._load_graph_cache(session_id, project_id).get("parse", {}).get("started_at"),
                    "completed_at": utc_now(),
                    "error": None,
                    "provider": provider,
                    "model": model,
                    "root_model": graph.get("root_model"),
                    "node_count": len(graph.get("nodes", [])),
                    "link_count": len(graph.get("links", [])),
                },
                "graph": graph,
            }
        except Exception as exc:
            payload = {
                "parse": {
                    "status": "failed",
                    "started_at": self._load_graph_cache(session_id, project_id).get("parse", {}).get("started_at") if self._load_graph_cache(session_id, project_id) else None,
                    "completed_at": utc_now(),
                    "error": str(exc),
                    "provider": provider,
                    "model": model,
                },
                "graph": None,
            }
        with self.lock:
            self._save_graph_cache(session_id, project_id, payload)

    def get_messages(self, session_id: str, limit: int = 5, before: Optional[str] = None, order: str = "desc"):
        with self.lock:
            self._load_session(session_id)
            rows = self._read_jsonl(self._messages_path(session_id))
            rows = [{**row, "session_id": session_id} for row in rows]
            if before:
                before_idx = next((i for i, row in enumerate(rows) if row["message_id"] == before), len(rows))
                rows = rows[:before_idx]
            selected = rows[-limit:] if limit > 0 else rows
            next_before = selected[0]["message_id"] if len(rows) > len(selected) and selected else None
            if order == "desc":
                selected = list(reversed(selected))
            return {"messages": selected, "next_before": next_before}

    def submit_chat(
        self,
        session_id: str,
        content: str,
        active_project_id: Optional[str],
        include_project_context: bool,
        idempotency_key: Optional[str],
        generation_mode: str = "automatic",
        auto_check: bool = True,
    ):
        generation_mode = str(generation_mode or "automatic").strip().lower()
        if generation_mode not in GENERATION_MODES:
            raise ValueError(
                "generation_mode must be either 'automatic' or 'guided'."
            )
        with self.lock:
            session = self._load_session(session_id)
            if self._session_workspace(session_id) != self.working_dir and not self.agent_factory:
                raise RuntimeError(
                    "This session belongs to a previous backend workspace. "
                    "Start the backend with that workspace to continue chatting in it."
                )
            if session["status"] in ACTIVE_SESSION_STATUSES:
                raise RuntimeError(
                    "Session already has an active generation request."
                )
            owner_client_id = normalize_client_id(session.get("owner_client_id"))
            self._ensure_participant_storage_capacity_unlocked(
                owner_client_id,
                "starting a generation request",
            )
            if owner_client_id and self._participant_has_active_generation_unlocked(
                owner_client_id,
                excluding_session_id=session_id,
            ):
                raise RuntimeError(
                    "You already have an active generation request in another "
                    "design. Wait for it to finish before starting another one."
                )
            if self._session_has_active_simulation_unlocked(session_id):
                raise RuntimeError(
                    "Stop or wait for the running simulation before asking the agent "
                    "to change this design."
                )
            if active_project_id:
                self._project_by_id(session_id, active_project_id)
            if idempotency_key:
                for request in self._load_requests(session_id):
                    if request.get("idempotency_key") == idempotency_key:
                        return request, self._message_for_request(session_id, request["request_id"], "user")
            request_id = new_id("req")
            user_message = {
                "message_id": new_id("msg"),
                "session_id": session_id,
                "request_id": request_id,
                "role": "user",
                "status": "visible",
                "content": content,
                "created_at": utc_now(),
                "withdrawn_at": None,
            }
            request = {
                "request_id": request_id,
                "session_id": session_id,
                "status": "queued",
                "user_message_id": user_message["message_id"],
                "assistant_message_id": None,
                "active_project_id": active_project_id,
                "include_project_context": include_project_context,
                "updated_project_ids": [],
                "updated_project_names": [],
                "started_at": None,
                "completed_at": None,
                "cancel_requested_at": None,
                "error": None,
                "idempotency_key": idempotency_key,
                "generation_mode": generation_mode,
                "auto_check": bool(auto_check),
                # Both modes use the same interpretation -> plan -> build
                # pipeline. Automatic mode auto-confirms the two artifacts;
                # guided mode exposes them as durable review checkpoints.
                # Only older persisted requests default directly to `build`.
                "phase": "interpret_intent",
                "phase_started_at": None,
                "pending_interaction": None,
                "interactions": [],
                "approved_intent": None,
                "approved_structure": None,
                "review_feedback": None,
            }
            self._save_message(session_id, user_message)
            self._save_request(session_id, request)
            session["status"] = "queued"
            session["active_request_id"] = request_id
            self._save_session(session)
            self._add_event(session_id, request_id, "request_started", "Request queued.")
            self._record_trace_unlocked(
                session_id,
                "chat_submitted",
                actor="user",
                request_id=request_id,
                project_id=active_project_id,
                payload={
                    "generation_mode": generation_mode,
                    "include_project_context": include_project_context,
                    "auto_check": bool(auto_check),
                    "content_length": len(content),
                },
            )
            self._schedule_collector_sync(session_id)
            self.worker_queue.put(request_id)
            return request, user_message

    def get_request(self, session_id: str, request_id: str):
        with self.lock:
            self._load_session(session_id)
            return self._get_request(session_id, request_id)

    def get_events(self, session_id: str, after: int = 0, request_id: Optional[str] = None, limit: int = 100):
        with self.lock:
            session = self._load_session(session_id)
            request_status = None
            if request_id:
                try:
                    request_status = self._get_request(session_id, request_id)["status"]
                except KeyError:
                    request_status = None
            session_status = session["status"]
        # Reading a longer event history must not hold the global session-state
        # lock. Appends remain serialized by the dedicated event lock.
        with self.event_lock:
            stored_events = self._read_jsonl(self._events_path(session_id))
        events = [
            {**event, "session_id": session_id}
            for event in stored_events
            if event["event_id"] > after
        ]
        if request_id:
            events = [
                event for event in events if event["request_id"] == request_id
            ]
        events = events[:limit]
        next_after = events[-1]["event_id"] if events else after
        return {
            "events": events,
            "next_after": next_after,
            "request_status": request_status or session_status,
        }

    def get_request_activity_file(
        self,
        session_id: str,
        request_id: str,
        file_path: str,
    ) -> Dict[str, Any]:
        """Read one bounded text file named by this request's public activity."""

        relative = canonical_relative_file_path(file_path)
        if (
            any(
                part.startswith(".") or part in _ACTIVITY_INTERNAL_PARTS
                for part in relative.parts
            )
            or relative.suffix.lower() not in _ACTIVITY_FILE_SUFFIXES
        ):
            raise FileNotFoundError(relative.as_posix())
        canonical = relative.as_posix()
        with self.lock:
            self._load_session(session_id)
            self._get_request(session_id, request_id)
            workspace = Path(self._session_workspace(session_id)).resolve()
            registered_project_paths = [
                PurePosixPath(str(project.get("path") or "").replace("\\", "/"))
                for project in self._load_projects(session_id)
                if project.get("path")
            ]
        with self.event_lock:
            events = self._read_jsonl(self._events_path(session_id))
        declared = any(
            event.get("request_id") == request_id
            and any(
                isinstance(change, dict)
                and change.get("path") == canonical
                for change in (event.get("file_changes") or [])
            )
            for event in events
        )
        if not declared:
            raise FileNotFoundError(canonical)
        try:
            absolute = contained_path(workspace, relative)
            metadata = absolute.lstat()
        except (OSError, ValueError) as exc:
            raise FileNotFoundError(canonical) from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise FileNotFoundError(canonical)
        if metadata.st_size > MAX_ACTIVITY_FILE_PREVIEW_BYTES:
            raise OverflowError(
                "This generated file is too large for the live preview."
            )
        try:
            content = absolute.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise TypeError("This generated file is not UTF-8 text.") from exc

        # Prefer an already registered simulation root. During generation the
        # registry marker may not exist yet, so fall back to the activity
        # file's top-level output folder. This lets the Files tab show the
        # whole in-progress simulation rather than a synthetic one-file tree.
        matching_roots = [
            project_path
            for project_path in registered_project_paths
            if relative == project_path or project_path in relative.parents
        ]
        preview_root = (
            max(matching_roots, key=lambda path: len(path.parts))
            if matching_roots
            else PurePosixPath(relative.parts[0])
            if len(relative.parts) > 1
            else None
        )
        project_files: Dict[str, str] = {}
        files_truncated = False
        selected_path = canonical
        if preview_root is not None:
            try:
                root_absolute = contained_path(workspace, preview_root)
                root_metadata = root_absolute.lstat()
                if stat.S_ISDIR(root_metadata.st_mode) and not stat.S_ISLNK(
                    root_metadata.st_mode
                ):
                    selected_path = relative.relative_to(preview_root).as_posix()
                    project_files[selected_path] = content
                    total_bytes = metadata.st_size
                    stop = False
                    for walk_root, dirs, file_names in os.walk(root_absolute):
                        dirs[:] = sorted(
                            directory
                            for directory in dirs
                            if not directory.startswith(".")
                            and directory not in _ACTIVITY_INTERNAL_PARTS
                            and not (Path(walk_root) / directory).is_symlink()
                        )
                        for name in sorted(file_names):
                            if len(project_files) >= MAX_ACTIVITY_PROJECT_FILES:
                                files_truncated = True
                                stop = True
                                break
                            if name.startswith("."):
                                continue
                            candidate = Path(walk_root) / name
                            candidate_relative = candidate.relative_to(
                                root_absolute
                            )
                            if candidate_relative.as_posix() == selected_path:
                                continue
                            if (
                                any(
                                    part.startswith(".")
                                    or part in _ACTIVITY_INTERNAL_PARTS
                                    for part in candidate_relative.parts
                                )
                                or candidate_relative.suffix.lower()
                                not in _ACTIVITY_FILE_SUFFIXES
                            ):
                                continue
                            try:
                                candidate_metadata = candidate.lstat()
                            except OSError:
                                continue
                            if (
                                not stat.S_ISREG(candidate_metadata.st_mode)
                                or stat.S_ISLNK(candidate_metadata.st_mode)
                                or candidate_metadata.st_size
                                > MAX_ACTIVITY_FILE_PREVIEW_BYTES
                            ):
                                continue
                            if (
                                total_bytes + candidate_metadata.st_size
                                > MAX_ACTIVITY_PROJECT_PREVIEW_BYTES
                            ):
                                files_truncated = True
                                stop = True
                                break
                            try:
                                candidate_content = candidate.read_text(
                                    encoding="utf-8"
                                )
                            except (OSError, UnicodeDecodeError):
                                continue
                            project_files[
                                candidate_relative.as_posix()
                            ] = candidate_content
                            total_bytes += candidate_metadata.st_size
                        if stop:
                            break
            except (OSError, ValueError):
                preview_root = None

        # The selected file is authoritative and must remain available even
        # when a concurrent write or a bundle cap truncated enumeration.
        project_files[selected_path] = content
        return {
            "path": canonical,
            "content": content,
            "size": metadata.st_size,
            "root_path": preview_root.as_posix() if preview_root else "",
            "selected_path": selected_path,
            "files": project_files,
            "files_truncated": files_truncated,
        }

    @staticmethod
    def _bounded_interaction_text(value: Any, limit: int = 4_000) -> str:
        text = str(value or "").strip()
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        encoded = encoded[:limit]
        while encoded:
            try:
                return encoded.decode("utf-8").rstrip()
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        return ""

    @staticmethod
    def _interaction_text_items(value: Any) -> List[Any]:
        """Normalize a review text field without iterating scalar strings."""
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return list(value)
        return []

    @staticmethod
    def _pascal_name(value: str) -> str:
        words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
        ignored = {
            "a",
            "an",
            "the",
            "please",
            "generate",
            "create",
            "build",
            "simple",
            "simulation",
            "simulator",
            "model",
        }
        useful = [word for word in words if word.lower() not in ignored][:5]
        candidate = "".join(word[:1].upper() + word[1:] for word in useful)
        if not candidate:
            return "GeneratedSimulation"
        if candidate[0].isdigit():
            candidate = f"Simulation{candidate}"
        return candidate[:96]

    @staticmethod
    def _snake_name(value: str) -> str:
        words = re.findall(r"[A-Za-z0-9]+", str(value or "").lower())
        ignored = {
            "a",
            "an",
            "the",
            "please",
            "generate",
            "create",
            "build",
            "simple",
            "simulation",
            "simulator",
            "model",
        }
        useful = [word for word in words if word not in ignored][:6]
        return ("_".join(useful) or "generated")[:80] + "_sim"

    @staticmethod
    def _extract_json_object(value: Any) -> Optional[Dict[str, Any]]:
        content = getattr(value, "content", value)
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text_parts.append(str(item.get("text") or ""))
                else:
                    text_parts.append(str(item))
            content = "\n".join(text_parts)
        if not isinstance(content, str):
            return None
        candidate = content.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.S)
        if fenced:
            candidate = fenced.group(1)
        else:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                candidate = candidate[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _minimal_output_contract_from_text(value: Any) -> Optional[str]:
        text = str(value or "")
        lowered = text.lower()
        if "stdout" not in lowered or "jsonl" not in lowered:
            return None
        if re.search(
            r'"time"\s*:\s*<\s*float\s*>.*'
            r'"entity"\s*:\s*<\s*str\s*>.*'
            r'"event"\s*:\s*<\s*str\s*>.*'
            r'"payload"\s*:\s*<\s*dict\s*>',
            text,
            re.I | re.S,
        ):
            return (
                'Each stdout JSONL record MUST follow: {"time": <float>, '
                '"entity": <str>, "event": <str>, "payload": <dict>}.'
            )
        return None

    def _normalize_intent_payload(
        self,
        raw: Any,
        user_content: str,
        *,
        edited_intent: Optional[Dict[str, Any]] = None,
        answers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(raw) if isinstance(raw, dict) else {}
        if isinstance(edited_intent, dict):
            payload.update(edited_intent)
        summary = self._bounded_interaction_text(
            payload.get("summary") or user_content, 2_000
        )
        root_model_name = self._bounded_interaction_text(
            payload.get("root_model_name") or self._pascal_name(user_content),
            96,
        )
        root_model_name = re.sub(r"[^A-Za-z0-9_]", "", root_model_name)
        if not root_model_name or root_model_name[0].isdigit():
            root_model_name = self._pascal_name(user_content)
        project_folder = safe_project_directory_name(
            payload.get("project_folder") or self._snake_name(user_content)
        )
        requirements = self._bounded_interaction_text(
            payload.get("requirements") or user_content,
            MAX_INTERACTION_TEXT_BYTES,
        )
        output_contract = self._bounded_interaction_text(
            payload.get("output_contract")
            or self._minimal_output_contract_from_text(user_content),
            500,
        )
        assumptions = []
        for item in self._interaction_text_items(payload.get("assumptions")):
            text = self._bounded_interaction_text(item, 500)
            if text and text not in assumptions:
                assumptions.append(text)
            if len(assumptions) >= 12:
                break
        structured_lists: Dict[str, List[str]] = {}
        for field_name in ("entities", "event_flow", "parameters", "metrics"):
            values: List[str] = []
            for item in self._interaction_text_items(payload.get(field_name)):
                text = self._bounded_interaction_text(item, 300)
                if text and text not in values:
                    values.append(text)
                if len(values) >= 24:
                    break
            structured_lists[field_name] = values
        questions = []
        for index, item in enumerate(payload.get("questions") or []):
            item = item if isinstance(item, dict) else {"prompt": item}
            prompt = self._bounded_interaction_text(
                item.get("prompt") or item.get("question"), 500
            )
            if not prompt:
                continue
            question_id = re.sub(
                r"[^A-Za-z0-9_-]", "_", str(item.get("question_id") or f"q{index + 1}")
            )[:80]
            options = []
            for option in item.get("options") or []:
                option = option if isinstance(option, dict) else {
                    "value": option,
                    "label": option,
                }
                value = self._bounded_interaction_text(option.get("value"), 120)
                label = self._bounded_interaction_text(
                    option.get("label") or value, 160
                )
                if value and label:
                    normalized_option = {"value": value, "label": label}
                    description = self._bounded_interaction_text(
                        option.get("description"), 300
                    )
                    if description:
                        normalized_option["description"] = description
                    if option.get("recommended") is not None:
                        normalized_option["recommended"] = bool(
                            option.get("recommended")
                        )
                    options.append(normalized_option)
                if len(options) >= 8:
                    break
            question = {
                "question_id": question_id or f"q{index + 1}",
                "prompt": prompt,
                "required": bool(item.get("required", False)),
            }
            if options:
                question["options"] = options
            recommended_value = self._bounded_interaction_text(
                item.get("recommended_value"), 120
            )
            if recommended_value and any(
                option["value"] == recommended_value for option in options
            ):
                question["recommended_value"] = recommended_value
            questions.append(question)
            if len(questions) >= 4:
                break
        normalized = {
            "summary": summary,
            "root_model_name": root_model_name,
            "project_folder": project_folder,
            "requirements": requirements,
            "assumptions": assumptions,
            "entities": structured_lists["entities"],
            "event_flow": structured_lists["event_flow"],
            "parameters": structured_lists["parameters"],
            "metrics": structured_lists["metrics"],
            "questions": questions,
        }
        if output_contract:
            normalized["output_contract"] = output_contract
        if answers:
            normalized["answers"] = self._jsonable(answers)
            clarification_lines = [
                f"{key}: {value}"
                for key, value in answers.items()
                if self._bounded_interaction_text(value, 1_000)
            ]
            if clarification_lines:
                normalized["requirements"] = (
                    requirements
                    + "\n\nConfirmed clarifications:\n"
                    + "\n".join(clarification_lines)
                )[:MAX_INTERACTION_TEXT_BYTES]
        return normalized

    def _fallback_intent_payload(self, user_content: str) -> Dict[str, Any]:
        return self._normalize_intent_payload({}, user_content)

    def _generation_requirements_from_intent(self, intent: Dict[str, Any]) -> str:
        """Return the requirements text consumed by structure/detail/code generation."""

        try:
            intent_json = json.dumps(
                self._jsonable(intent),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except (TypeError, ValueError):
            intent_json = str(intent)
        text = (
            "Use this complete interpreted intent JSON as the authoritative "
            "generation requirements. Preserve all specific details, including "
            "fields not explicitly named by the generator.\n\n"
            f"{intent_json}"
        )
        return self._bounded_interaction_text(text, MAX_INTERACTION_TEXT_BYTES)


    @staticmethod
    def _invoke_capability(capability: Callable[..., Any], **kwargs: Any) -> Any:
        """Call an injected or tool-owned capability with compatible kwargs."""

        try:
            signature = inspect.signature(capability)
        except (TypeError, ValueError):
            return capability(**kwargs)
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if accepts_kwargs:
            return capability(**kwargs)
        accepted = {
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
            and signature.parameters[name].kind
            not in {inspect.Parameter.POSITIONAL_ONLY}
        }
        return capability(**accepted)

    @staticmethod
    def _constructor_tool(agent: Any) -> Optional[Any]:
        tools = getattr(agent, "tools", None)
        candidates = tools.values() if isinstance(tools, dict) else tools or []
        for tool in candidates:
            if getattr(tool, "name", None) == "devs_construct_tree":
                return tool
        return None

    def _interpret_intent(
        self,
        agent: Any,
        *,
        user_content: str,
        feedback: str,
        edited_intent: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raw: Any = None
        if callable(self.intent_interpreter):
            raw = self._invoke_capability(
                self.intent_interpreter,
                user_content=user_content,
                feedback=feedback,
                edited_intent=edited_intent,
            )
        else:
            model = getattr(agent, "model", None)
            generate = getattr(model, "generate", None)
            if callable(generate):
                prompt = "\n".join(
                    (
                        "Interpret this DEVS simulation request without generating code.",
                        "Return exactly one JSON object with keys summary, root_model_name,",
                        "project_folder, requirements, assumptions, entities, event_flow,",
                        "parameters, metrics, and questions. assumptions/entities/event_flow/",
                        "parameters/metrics must be concise string arrays suitable for a visual review.",
                        "questions is an array of {question_id,prompt,required,recommended_value,options};",
                        "each option is {value,label,description,recommended}. Ask only",
                        "questions whose answers materially change the model. Do not include reasoning.",
                        f"Prior review feedback: {feedback or 'none'}",
                        f"User request: {user_content}",
                    )
                )
                retry_guidance = ""
                for attempt in range(2):
                    response = None
                    try:
                        response = generate(
                            [
                                {
                                    "role": "user",
                                    "content": append_retry_guidance(
                                        prompt, retry_guidance
                                    ),
                                }
                            ]
                        )
                        raw = self._extract_json_object(response)
                        if raw is None:
                            raise ValueError(
                                "The intent response was not one valid JSON object."
                            )
                        break
                    except Exception as exc:
                        model_id = str(getattr(model, "model_id", "") or "")
                        if response is not None and model_id and attempt == 0:
                            retry_guidance = json_retry_guidance(
                                model=model_id,
                                target="intent interpretation",
                                schema=(
                                    "One JSON object with summary, root_model_name, "
                                    "project_folder, requirements, assumptions, entities, "
                                    "event_flow, parameters, metrics, and questions."
                                ),
                                error=exc,
                                attempt=attempt,
                            )
                            continue
                        # A failed side-effect-free interpretation must not make
                        # guided mode less reliable than automatic generation.
                        print(
                            "[Backend] Intent interpretation fell back to a local "
                            f"summary ({type(exc).__name__}: {exc})."
                        )
                        break
        return self._normalize_intent_payload(
            raw,
            user_content,
            edited_intent=edited_intent,
        )

    def _structure_public_payload(
        self, raw: Any, intent: Dict[str, Any]
    ) -> Dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        for key in ("public", "preview", "structure", "structure_review"):
            if isinstance(source.get(key), dict):
                source = source[key]
                break

        graph = source.get("graph") if isinstance(source.get("graph"), dict) else {}
        components: List[Dict[str, Any]] = []
        raw_components = (
            source.get("components")
            or source.get("nodes")
            or graph.get("nodes")
            or []
        )
        raw_component_count = sum(
            1 for component in raw_components if isinstance(component, dict)
        )
        for index, component in enumerate(raw_components):
            if not isinstance(component, dict):
                continue
            component_id = self._bounded_interaction_text(
                component.get("id")
                or component.get("class_name")
                or component.get("name")
                or f"component_{index + 1}",
                160,
            )
            components.append(
                {
                    "id": component_id,
                    "name": self._bounded_interaction_text(
                        component.get("name")
                        or component.get("class_name")
                        or component_id,
                        160,
                    ),
                    "model_type": self._bounded_interaction_text(
                        component.get("model_type")
                        or component.get("type")
                        or "component",
                        80,
                    ),
                    "description": self._bounded_interaction_text(
                        component.get("description")
                        or component.get("function")
                        or component.get("responsibility")
                        or component.get("summary"),
                        600,
                    ),
                    "parent_id": component.get("parent_id")
                    or component.get("parent"),
                    "input_ports": self._jsonable(
                        component.get("input_ports") or []
                    ),
                    "output_ports": self._jsonable(
                        component.get("output_ports") or []
                    ),
                }
            )
            if len(components) >= 200:
                break

        # Constructor artifacts may expose the hierarchy as a nested plan tree.
        def walk_tree(node: Any, parent_id: Optional[str] = None) -> None:
            if len(components) >= 200 or not isinstance(node, dict):
                return
            model_info = node.get("model_info") or {}
            plan = node.get("plan") or node.get("plan_phase") or {}
            node_id = self._bounded_interaction_text(
                node.get("class_name")
                or model_info.get("class_name")
                or node.get("name"),
                160,
            )
            if node_id and not any(item["id"] == node_id for item in components):
                specification = model_info.get("specification") or {}
                components.append(
                    {
                        "id": node_id,
                        "name": node_id,
                        "model_type": self._bounded_interaction_text(
                            plan.get("model_type") or "component", 80
                        ),
                        "description": self._bounded_interaction_text(
                            specification.get("function")
                            or plan.get("function")
                            or plan.get("description"),
                            600,
                        ),
                        "parent_id": parent_id,
                        "input_ports": self._jsonable(
                            specification.get("input_ports") or []
                        ),
                        "output_ports": self._jsonable(
                            specification.get("output_ports") or []
                        ),
                    }
                )
            for child in node.get("children") or []:
                walk_tree(child, node_id or parent_id)

        tree = (
            source.get("plan_tree")
            or source.get("tree")
            or source.get("root")
            or (raw.get("plan_tree") if isinstance(raw, dict) else None)
        )
        walk_tree(tree)
        if not components:
            root_name = intent["root_model_name"]
            components.append(
                {
                    "id": root_name,
                    "name": root_name,
                    "model_type": "coupled",
                    "description": intent["summary"],
                    "parent_id": None,
                    "input_ports": [],
                    "output_ports": [],
                }
            )

        connections = []
        raw_connections = source.get("connections") or source.get("links") or []
        if not raw_connections and graph:
            raw_connections = []
            for coupling in graph.get("couplings") or []:
                if not isinstance(coupling, dict):
                    continue
                source_endpoint = coupling.get("source") or {}
                target_endpoint = coupling.get("target") or {}
                raw_connections.append(
                    {
                        "source": (
                            f"{source_endpoint.get('node_id', '')}."
                            f"{source_endpoint.get('port_name', '')}"
                        ).strip("."),
                        "target": (
                            f"{target_endpoint.get('node_id', '')}."
                            f"{target_endpoint.get('port_name', '')}"
                        ).strip("."),
                        "label": coupling.get("coupling_type") or "",
                        "owner_node_id": coupling.get("owner_node_id"),
                        "coupling_type": coupling.get("coupling_type"),
                        "source_boundary": source_endpoint.get("boundary"),
                        "target_boundary": target_endpoint.get("boundary"),
                        "multiplicity": coupling.get("multiplicity") or 1,
                    }
                )
        raw_connection_count = sum(
            1 for link in raw_connections if isinstance(link, dict)
        )
        for link in raw_connections:
            if not isinstance(link, dict):
                continue
            connections.append(
                {
                    "source": self._bounded_interaction_text(
                        link.get("source"), 240
                    ),
                    "target": self._bounded_interaction_text(
                        link.get("target"), 240
                    ),
                    "label": self._bounded_interaction_text(
                        link.get("label")
                        or link.get("port")
                        or link.get("type"),
                        160,
                    ),
                    "owner_node_id": self._bounded_interaction_text(
                        link.get("owner_node_id"), 160
                    ),
                    "coupling_type": self._bounded_interaction_text(
                        link.get("coupling_type") or link.get("type"), 16
                    ),
                    "source_boundary": self._bounded_interaction_text(
                        link.get("source_boundary"), 32
                    ),
                    "target_boundary": self._bounded_interaction_text(
                        link.get("target_boundary"), 32
                    ),
                    "multiplicity": max(1, int(link.get("multiplicity") or 1)),
                }
            )
            if len(connections) >= 400:
                break
        truncated_component_count = max(0, raw_component_count - len(components))
        truncated_connection_count = max(
            0, raw_connection_count - len(connections)
        )
        omitted_coupling_count = max(
            0,
            int(graph.get("omitted_coupling_count") or 0),
        )
        omitted_connection_count = (
            omitted_coupling_count + truncated_connection_count
        )
        is_complete = (
            omitted_connection_count == 0 and truncated_component_count == 0
        )
        review_scope = self._bounded_interaction_text(
            source.get("review_scope")
            or (
                "component_hierarchy"
                if source.get("schema_version") == "devs.structure-plan.v1"
                else "detailed_devs_plan"
            ),
            80,
        )
        connections_defined = (
            False
            if review_scope == "component_hierarchy"
            else bool(source.get("connections_defined", True))
        )
        return {
            "title": self._bounded_interaction_text(
                source.get("title") or intent["root_model_name"], 160
            ),
            "summary": self._bounded_interaction_text(
                source.get("summary") or intent["summary"], 2_000
            ),
            "root_model_name": intent["root_model_name"],
            "root_node_id": self._bounded_interaction_text(
                graph.get("root_node_id") or intent["root_model_name"], 160
            ),
            "component_count": raw_component_count or len(components),
            "components": components,
            "connections": connections,
            "omitted_coupling_count": omitted_coupling_count,
            "omitted_connection_count": omitted_connection_count,
            "truncated_component_count": truncated_component_count,
            "truncated_connection_count": truncated_connection_count,
            "is_complete": is_complete,
            "review_scope": review_scope,
            "review_scope_complete": is_complete,
            "connections_defined": connections_defined,
            "assumptions": intent.get("assumptions") or [],
        }

    def _prepare_structure_plan(
        self,
        agent: Any,
        *,
        intent: Dict[str, Any],
        feedback: str,
        session_workspace: str,
    ) -> tuple[Any, Dict[str, Any]]:
        plan_requirements = self._generation_requirements_from_intent(intent)
        if feedback:
            # The constructor's stable split-phase API intentionally accepts
            # only planning inputs. Carry a requested revision through that
            # boundary without mutating the already-approved intent artifact.
            plan_requirements = (
                plan_requirements
                + "\n\nRequested structure revision:\n"
                + self._bounded_interaction_text(feedback, 4_000)
            )[:MAX_INTERACTION_TEXT_BYTES]
        capability = self.plan_preparer
        if not callable(capability):
            tool = self._constructor_tool(agent)
            capability = getattr(tool, "prepare_plan", None) if tool else None
        if callable(capability):
            result = self._invoke_capability(
                capability,
                root_model_name=intent["root_model_name"],
                requirements=plan_requirements,
                base_folder=intent["project_folder"],
                intent=intent,
                feedback=feedback,
                working_directory=session_workspace,
            )
            durable = self._jsonable(result)
            if isinstance(durable, dict):
                plan_data = durable.get("plan_artifact")
                if plan_data is None:
                    plan_data = durable.get("plan", durable.get("data", durable))
            else:
                plan_data = durable
            return plan_data, self._structure_public_payload(durable, intent)

        raise RuntimeError(
            "This prepared DEVS Generator runtime does not support exact "
            "structure review. Stop and relaunch the interface so OptPilot "
            "can prepare the current runtime."
        )

    def _interaction_for_artifact(
        self,
        *,
        phase: str,
        artifact: Dict[str, Any],
    ) -> Dict[str, Any]:
        kind = "intent_review" if phase == "interpret_intent" else "structure_review"
        return {
            "interaction_id": new_id("int"),
            "kind": kind,
            "phase": phase,
            "status": "open",
            "revision": artifact["revision"],
            "artifact_id": artifact["artifact_id"],
            "artifact_digest": artifact.get("review_digest") or artifact["digest"],
            "created_at": utc_now(),
            "prompt": (
                "Review how the generator understood your request."
                if kind == "intent_review"
                else "Confirm the DEVS structure before code is generated."
            ),
            "payload": artifact.get("public") or {},
        }

    def _approved_artifact_ref(self, artifact: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "artifact_id": artifact["artifact_id"],
            "artifact_digest": artifact["digest"],
            "revision": artifact["revision"],
        }

    def _run_guided_phase(
        self,
        session_id: str,
        request_id: str,
        phase: str,
    ) -> None:
        with self.lock:
            request = self._get_request(session_id, request_id)
            if request.get("status") != "queued" or request.get("phase") != phase:
                return
            request["status"] = "running"
            request["phase_started_at"] = utc_now()
            if request.get("started_at") is None:
                request["started_at"] = request["phase_started_at"]
            self._save_request(session_id, request)
            session = self._load_session(session_id)
            session["status"] = "running"
            session["active_request_id"] = request_id
            self._save_session(session)
            user_message = self._message_for_request(
                session_id, request_id, "user"
            )
            user_content = str((user_message or {}).get("content") or "")
            revision = 1 + sum(
                1
                for item in request.get("interactions") or []
                if item.get("phase") == phase
            )
            feedback = self._bounded_interaction_text(
                request.get("review_feedback"), 4_000
            )
            edited_intent = request.get("edited_intent")
            session_workspace = self._session_workspace(session_id)
            self._add_event(
                session_id,
                request_id,
                "phase_started",
                "Guided review preparation started.",
                phase=phase,
            )
            self._add_activity(
                session_id,
                request_id,
                activity_key=phase,
                state="started",
                title=(
                    "Interpreting your request"
                    if phase == "interpret_intent"
                    else "Planning the model structure"
                ),
                detail=(
                    "Preparing a concise intent review without generating code."
                    if phase == "interpret_intent"
                    else "Preparing the component hierarchy and responsibilities for your confirmation."
                ),
            )

        agent = self._agent_for_workspace(session_workspace)
        if phase == "interpret_intent":
            intent = self._interpret_intent(
                agent,
                user_content=user_content,
                feedback=feedback,
                edited_intent=(
                    edited_intent if isinstance(edited_intent, dict) else None
                ),
            )
            artifact = self._save_request_artifact(
                session_id,
                request_id,
                kind="intent_review",
                revision=revision,
                data=intent,
                public=intent,
            )
        else:
            approved_intent = request.get("approved_intent") or {}
            intent_artifact = self._load_request_artifact(
                session_id,
                request_id,
                str(approved_intent.get("artifact_id") or ""),
            )
            intent = self._normalize_intent_payload(
                intent_artifact.get("data"), user_content
            )
            with self._agent_progress_context(agent, session_id, request_id):
                plan_data, public = self._prepare_structure_plan(
                    agent,
                    intent=intent,
                    feedback=feedback,
                    session_workspace=session_workspace,
                )
            artifact = self._save_request_artifact(
                session_id,
                request_id,
                kind="structure_review",
                revision=revision,
                data=plan_data,
                public=public,
            )

        interaction = self._interaction_for_artifact(
            phase=phase, artifact=artifact
        )
        should_queue = False
        with self.lock:
            request = self._get_request(session_id, request_id)
            if request.get("status") != "running" or request.get("phase") != phase:
                return
            request["review_feedback"] = None
            request["edited_intent"] = None
            interactions = list(request.get("interactions") or [])
            if request.get("generation_mode") == "automatic":
                interaction["status"] = "resolved"
                interaction["resolved_at"] = utc_now()
                interaction["resolution"] = {
                    "action": "confirm",
                    "automatic": True,
                }
                interactions.append(interaction)
                if phase == "interpret_intent":
                    request["approved_intent"] = self._approved_artifact_ref(
                        artifact
                    )
                    request["phase"] = "plan_structure"
                else:
                    request["approved_structure"] = self._approved_artifact_ref(
                        artifact
                    )
                    request["phase"] = "build"
                request["phase_started_at"] = None
                request["pending_interaction"] = None
                request["status"] = "queued"
                session = self._load_session(session_id)
                session["status"] = "queued"
                session["active_request_id"] = request_id
                should_queue = True
            else:
                interactions.append(interaction)
                request["pending_interaction"] = interaction
                request["status"] = "waiting_for_user"
                session = self._load_session(session_id)
                session["status"] = "waiting_for_user"
                session["active_request_id"] = request_id
            request["interactions"] = interactions
            self._save_request(session_id, request)
            self._save_session(session)
            self._add_activity(
                session_id,
                request_id,
                activity_key=phase,
                state="completed",
                title=(
                    "Intent review ready"
                    if phase == "interpret_intent"
                    else "Structure review ready"
                ),
                detail=(
                    "Review the interpretation before planning the DEVS structure."
                    if phase == "interpret_intent"
                    else "Review the component hierarchy before source files are generated."
                ),
            )
            self._add_event(
                session_id,
                request_id,
                (
                    "interaction_resolved"
                    if should_queue
                    else "interaction_required"
                ),
                (
                    "Guided checkpoint confirmed automatically."
                    if should_queue
                    else "User confirmation is required before generation continues."
                ),
                interaction=interaction,
            )
            if not should_queue:
                self._release_session_agent_unlocked(session_id)
        if should_queue:
            self.worker_queue.put(request_id)

    def resolve_interaction(
        self,
        *,
        session_id: str,
        request_id: str,
        interaction_id: str,
        action: str,
        artifact_digest: Optional[str] = None,
        answers: Optional[Dict[str, Any]] = None,
        feedback: Optional[str] = None,
        edited_intent: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if action not in {
            "confirm",
            "revise",
            "continue_automatically",
            "cancel",
        }:
            raise ValueError("Unsupported interaction action.")
        if action != "cancel" and not artifact_digest:
            raise ValueError(
                "artifact_digest is required when resolving a review."
            )
        feedback = self._bounded_interaction_text(feedback, 4_000)
        answers = answers if isinstance(answers, dict) else {}
        encoded_answers = json.dumps(answers, ensure_ascii=False).encode("utf-8")
        if len(encoded_answers) > MAX_INTERACTION_TEXT_BYTES:
            raise ValueError("Interaction answers are too large.")
        encoded_edited_intent = json.dumps(
            edited_intent or {}, ensure_ascii=False
        ).encode("utf-8")
        if len(encoded_edited_intent) > MAX_INTERACTION_TEXT_BYTES:
            raise ValueError("The edited interpretation is too large.")
        resolution_fingerprint = self._artifact_digest(
            {
                "action": action,
                "artifact_digest": artifact_digest,
                "answers": self._jsonable(answers),
                "feedback": feedback or None,
                "edited_intent": self._jsonable(edited_intent or {}),
            }
        )
        should_queue = False
        with self.lock:
            self._load_session(session_id)
            request = self._get_request(session_id, request_id)
            for historical in request.get("interactions") or []:
                resolution = historical.get("resolution") or {}
                if (
                    idempotency_key
                    and resolution.get("idempotency_key") == idempotency_key
                ):
                    existing_fingerprint = resolution.get("request_fingerprint")
                    if (
                        existing_fingerprint
                        and existing_fingerprint != resolution_fingerprint
                    ):
                        raise RuntimeError(
                            "This idempotency key was already used for a different response."
                        )
                    return request, historical

            pending = request.get("pending_interaction")
            if not isinstance(pending, dict):
                raise RuntimeError("This request has no interaction awaiting review.")
            if pending.get("interaction_id") != interaction_id:
                raise RuntimeError(
                    "This review is stale. Refresh the request before responding."
                )
            if request.get("status") != "waiting_for_user":
                raise RuntimeError("This review is no longer awaiting a response.")
            if artifact_digest and artifact_digest != pending.get("artifact_digest"):
                raise RuntimeError(
                    "The reviewed artifact changed. Refresh it before confirming."
                )
            artifact = self._load_request_artifact(
                session_id,
                request_id,
                pending["artifact_id"],
            )
            public_payload = artifact.get("public") or {}
            if action in {"confirm", "continue_automatically"} and pending.get(
                "kind"
            ) == "intent_review":
                questions = [
                    item
                    for item in public_payload.get("questions") or []
                    if isinstance(item, dict) and item.get("question_id")
                ]
                known_question_ids = {
                    str(item["question_id"]) for item in questions
                }
                unknown_answers = sorted(set(answers) - known_question_ids)
                if unknown_answers:
                    raise ValueError(
                        "Answers were provided for unknown clarification "
                        f"questions: {', '.join(unknown_answers)}."
                    )
                for question in questions:
                    question_id = str(question["question_id"])
                    answer = self._bounded_interaction_text(
                        answers.get(question_id), 1_000
                    )
                    if question.get("required") and not answer:
                        raise ValueError(
                            "Answer every required clarification before "
                            "continuing."
                        )
                    option_values = {
                        str(option.get("value"))
                        for option in question.get("options") or []
                        if isinstance(option, dict) and option.get("value") is not None
                    }
                    if answer and option_values and answer not in option_values:
                        raise ValueError(
                            f"The answer for {question_id} is not one of the "
                            "available choices."
                        )
            if (
                action == "confirm"
                and pending.get("kind") == "structure_review"
                and public_payload.get("is_complete") is False
            ):
                raise ValueError(
                    "This structure preview is incomplete. Request a revision, "
                    "or explicitly choose Continue automatically to proceed "
                    "without another review pause."
                )
            resolved = dict(pending)
            resolved["status"] = "resolved"
            resolved["resolved_at"] = utc_now()
            resolved["resolution"] = {
                "action": action,
                "answers": self._jsonable(answers),
                "feedback": feedback or None,
                "idempotency_key": idempotency_key,
                "automatic": action == "continue_automatically",
                "request_fingerprint": resolution_fingerprint,
            }
            if isinstance(edited_intent, dict):
                resolved["resolution"]["edited_intent"] = self._jsonable(
                    edited_intent
                )
            interactions = list(request.get("interactions") or [])
            for index, item in enumerate(interactions):
                if item.get("interaction_id") == interaction_id:
                    interactions[index] = resolved
                    break
            else:
                interactions.append(resolved)
            request["interactions"] = interactions
            request["pending_interaction"] = None
            request["phase_started_at"] = None

            if action == "cancel":
                request["status"] = "cancelled"
                request["completed_at"] = utc_now()
                session = self._load_session(session_id)
                session["status"] = "idle"
                session["active_request_id"] = None
                self._release_session_agent_unlocked(session_id)
            elif action == "revise":
                request["review_feedback"] = feedback
                request["edited_intent"] = (
                    self._jsonable(edited_intent)
                    if isinstance(edited_intent, dict)
                    else None
                )
                request["phase"] = pending["phase"]
                request["status"] = "queued"
                session = self._load_session(session_id)
                session["status"] = "queued"
                session["active_request_id"] = request_id
                should_queue = True
            else:
                if pending["kind"] == "intent_review":
                    intent = self._normalize_intent_payload(
                        artifact.get("data"),
                        str(
                            (
                                self._message_for_request(
                                    session_id, request_id, "user"
                                )
                                or {}
                            ).get("content")
                            or ""
                        ),
                        edited_intent=(
                            edited_intent
                            if isinstance(edited_intent, dict)
                            else None
                        ),
                        answers=answers,
                    )
                    if edited_intent or answers:
                        artifact = self._save_request_artifact(
                            session_id,
                            request_id,
                            kind="intent_review",
                            revision=int(pending.get("revision") or 1) + 1,
                            data=intent,
                            public=intent,
                        )
                    request["approved_intent"] = self._approved_artifact_ref(
                        artifact
                    )
                    request["phase"] = "plan_structure"
                else:
                    request["approved_structure"] = (
                        self._approved_artifact_ref(artifact)
                    )
                    request["phase"] = "build"
                if action == "continue_automatically":
                    request["generation_mode"] = "automatic"
                request["status"] = "queued"
                session = self._load_session(session_id)
                session["status"] = "queued"
                session["active_request_id"] = request_id
                should_queue = True

            self._save_request(session_id, request)
            self._save_session(session)
            self._add_event(
                session_id,
                request_id,
                "interaction_resolved",
                "Guided review response recorded.",
                interaction=resolved,
            )
            self._record_trace_unlocked(
                session_id,
                "review_resolved",
                actor="user",
                request_id=request_id,
                project_id=request.get("active_project_id"),
                payload={
                    "interaction_id": interaction_id,
                    "kind": pending.get("kind"),
                    "phase": pending.get("phase"),
                    "action": action,
                    "answer_count": len(answers),
                    "has_feedback": bool(feedback),
                    "has_edited_intent": isinstance(edited_intent, dict),
                },
            )
        if should_queue:
            self.worker_queue.put(request_id)
        self._schedule_collector_sync(session_id)
        return request, resolved

    def _run_confirmed_plan(
        self,
        *,
        agent: Any,
        session_id: str,
        request_id: str,
        request: Dict[str, Any],
        session_workspace: str,
        prompt: str,
    ) -> tuple[bool, Any]:
        approved = request.get("approved_structure") or {}
        artifact_id = approved.get("artifact_id")
        if not artifact_id:
            return False, None
        artifact = self._load_request_artifact(
            session_id, request_id, str(artifact_id)
        )
        if artifact.get("digest") != approved.get("artifact_digest"):
            raise RuntimeError("The confirmed structure artifact is inconsistent.")
        capability = self.plan_builder
        if not callable(capability):
            tool = self._constructor_tool(agent)
            capability = getattr(tool, "build_from_plan", None) if tool else None
        if not callable(capability):
            raise RuntimeError(
                "The approved DEVS architecture cannot be built by this prepared "
                "runtime. Stop and relaunch the interface to refresh it."
            )
        intent_data = None
        intent_ref = request.get("approved_intent") or {}
        if intent_ref.get("artifact_id"):
            intent_data = self._load_request_artifact(
                session_id,
                request_id,
                str(intent_ref["artifact_id"]),
            ).get("data")
        result = self._invoke_capability(
            capability,
            plan_artifact=artifact.get("data"),
            plan=artifact.get("data"),
            intent=intent_data,
            working_directory=session_workspace,
            expected_digest=artifact.get("digest"),
            prompt=prompt,
        )
        return True, result

    def cancel_request(self, session_id: str, request_id: str, withdraw_user_message: bool = True):
        with self.lock:
            request = self._get_request(session_id, request_id)
            if request["status"] in {"queued", "waiting_for_user"}:
                was_waiting = request["status"] == "waiting_for_user"
                request["status"] = "cancelled"
                request["completed_at"] = utc_now()
                pending = request.get("pending_interaction")
                if isinstance(pending, dict):
                    resolved = dict(pending)
                    resolved["status"] = "resolved"
                    resolved["resolved_at"] = utc_now()
                    resolved["resolution"] = {"action": "cancel"}
                    request["interactions"] = [
                        resolved
                        if item.get("interaction_id")
                        == pending.get("interaction_id")
                        else item
                        for item in (request.get("interactions") or [])
                    ]
                    request["pending_interaction"] = None
                self._save_request(session_id, request)
                user_message = self._message_for_request(session_id, request_id, "user")
                if user_message and withdraw_user_message:
                    user_message["status"] = "withdrawn"
                    user_message["withdrawn_at"] = utc_now()
                    self._update_message(session_id, user_message)
                session = self._load_session(session_id)
                if session.get("active_request_id") == request_id:
                    session["status"] = "idle"
                    session["active_request_id"] = None
                    self._save_session(session)
                self._release_session_agent_unlocked(session_id)
                self._add_event(
                    session_id,
                    request_id,
                    "request_cancelled",
                    (
                        "Guided generation cancelled."
                        if was_waiting
                        else "Queued request withdrawn."
                    ),
                )
                self._schedule_collector_sync(session_id)
                return request, user_message
            if request["status"] == "running":
                raise RuntimeError("Running request cancellation is not supported in this MVP")
            return request, self._message_for_request(session_id, request_id, "user")

    def _worker_loop(self):
        while True:
            request_id = self.worker_queue.get()
            try:
                self._run_queued_request(request_id)
            except Exception as exc:
                # No unexpected implementation or I/O failure may strand a
                # request in `running` or permanently kill this worker.
                print(
                    "[Backend] Unhandled request worker failure "
                    f"({type(exc).__name__}): {exc}"
                )
                self._fail_request_after_worker_exception(request_id)
            finally:
                self.worker_queue.task_done()

    def _fail_request_after_worker_exception(self, request_id: str) -> None:
        """Best-effort terminalization for an unexpected worker failure.

        The persisted/UI message is deliberately generic. Detailed exception
        text remains in the backend log and never enters public activity.
        """

        session_id: Optional[str] = None
        try:
            with self.lock:
                for candidate_session_id in tuple(self.session_locations):
                    try:
                        matching = any(
                            request.get("request_id") == request_id
                            for request in self._load_requests(
                                candidate_session_id
                            )
                        )
                    except (OSError, ValueError):
                        continue
                    if matching:
                        session_id = candidate_session_id
                        break
                if session_id is None:
                    return

                request = self._get_request(session_id, request_id)
                if request.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return

                message = (
                    "Generation stopped because an internal step could not be "
                    "completed. The session files and completed activity were "
                    "retained; you can try again or inspect the generated files."
                )
                if not request.get("assistant_message_id"):
                    assistant_message = {
                        "message_id": new_id("msg"),
                        "session_id": session_id,
                        "request_id": request_id,
                        "role": "assistant",
                        "status": "visible",
                        "content": message,
                        "created_at": utc_now(),
                        "withdrawn_at": None,
                    }
                    self._save_message(session_id, assistant_message)
                    request["assistant_message_id"] = assistant_message[
                        "message_id"
                    ]
                request["status"] = "failed"
                request["completed_at"] = utc_now()
                request["error"] = "An internal generation step could not be completed."
                self._terminalize_unverified_request_projects_unlocked(
                    session_id,
                    request,
                    message=(
                        "Generation finalization stopped unexpectedly. The retained "
                        "files were not verified; test or repair this simulation again."
                    ),
                    failure_kind="generation_finalization_failed",
                )
                self._save_request(session_id, request)

                session = self._load_session(session_id)
                if session.get("active_request_id") == request_id:
                    session["active_request_id"] = None
                session["status"] = "failed"
                self._save_session(session)
        except Exception as finalization_error:
            print(
                "[Backend] Could not persist worker failure state "
                f"({type(finalization_error).__name__}): {finalization_error}"
            )
            return

        try:
            self._add_activity(
                session_id,
                request_id,
                activity_key="worker_failure",
                state="failed",
                title="Generation stopped unexpectedly",
                detail=(
                    "The session files and completed activity were retained for "
                    "inspection or another attempt."
                ),
            )
            self._add_event(
                session_id,
                request_id,
                "request_failed",
                "Agent run stopped unexpectedly.",
            )
        except Exception as event_error:
            print(
                "[Backend] Could not record worker failure event "
                f"({type(event_error).__name__}): {event_error}"
            )
        if session_id is not None:
            self._schedule_collector_sync(
                session_id,
                include_snapshots=True,
            )

    def _run_queued_request(self, request_id: str):
        guided_target: Optional[tuple[str, str]] = None
        with self.lock:
            for candidate_session_id in list(self.session_locations):
                try:
                    candidate = self._get_request(
                        candidate_session_id, request_id
                    )
                except KeyError:
                    continue
                phase = candidate.get("phase", "build")
                if (
                    candidate.get("status") == "queued"
                    and phase in {"interpret_intent", "plan_structure"}
                ):
                    guided_target = (candidate_session_id, phase)
                break
        if guided_target is not None:
            self._run_guided_phase(
                guided_target[0], request_id, guided_target[1]
            )
            return

        session_id = None
        with self.lock:
            for sid in list(self.session_locations):
                if any(request["request_id"] == request_id for request in self._load_requests(sid)):
                    session_id = sid
                    break
            if not session_id:
                return
            request = self._get_request(session_id, request_id)
            if request["status"] != "queued":
                return
            request["status"] = "running"
            request["started_at"] = request.get("started_at") or utc_now()
            request["phase_started_at"] = utc_now()
            self._save_request(session_id, request)
            session = self._load_session(session_id)
            session["status"] = "running"
            session["active_request_id"] = request_id
            self._save_session(session)
            self._add_event(session_id, request_id, "agent_started", "Agent run started.")
            self._add_activity(
                session_id,
                request_id,
                activity_key="understand_request",
                state="started",
                title="Understanding your request",
                detail="The agent is interpreting the simulation goals and constraints.",
            )
            user_message = self._message_for_request(session_id, request_id, "user")
            active_project = None
            if request.get("active_project_id"):
                active_project = self._project_by_id(session_id, request["active_project_id"])
            session_workspace = self._session_workspace(session_id)

        prompt = self._build_agent_prompt(
            session_id,
            request_id,
            user_message["content"] if user_message else "",
            active_project,
            bool(request.get("include_project_context")),
        )

        pre_snapshot = self._get_snapshot(session_workspace)
        with self.lock:
            request = self._get_request(session_id, request_id)
            request["workspace_baseline"] = self._workspace_recovery_baseline(
                session_workspace, pre_snapshot
            )
            self._save_request(session_id, request)
        agent = None
        agent_error_was_retryable = False
        try:
            agent = self._agent_for_workspace(session_workspace)
            with self._agent_progress_context(agent, session_id, request_id):
                used_confirmed_plan, planned_result = self._run_confirmed_plan(
                    agent=agent,
                    session_id=session_id,
                    request_id=request_id,
                    request=request,
                    session_workspace=session_workspace,
                    prompt=prompt,
                )
                result = (
                    planned_result
                    if used_confirmed_plan
                    else agent.run(prompt, reset=False)
                )
            error = None
        except Exception as exc:
            agent_error_was_retryable = self._retryable_agent_transport_error(exc)
            print(
                "[Backend] Agent run failed "
                f"({type(exc).__name__}): {exc}"
            )
            # Keep the full chained diagnostic in the private backend log. Public
            # request, message, activity, and event records below intentionally
            # retain only backend-owned, non-sensitive copy.
            traceback.print_exc()
            # The accurate public explanation depends on the post-run workspace
            # diff captured below. Do not claim files were retained before we
            # know that this attempt actually changed one.
            result = ""
            error = "Agent generation failed."
        post_snapshot = self._get_snapshot(session_workspace)
        updated_names = self._detect_updated_project_names(session_workspace, pre_snapshot, post_snapshot)
        if error is not None:
            files_were_changed = bool(updated_names)
            if files_were_changed:
                result = (
                    "Generation stopped before completion. The session files and "
                    "completed activity were retained; try again or inspect the "
                    "generated files."
                )
                failure_detail = (
                    "Files already written were retained. OptPilot is checking "
                    "the exact saved simulation before deciding whether it can "
                    "be recovered."
                    if agent_error_was_retryable
                    else "The agent encountered a problem. The completed activity "
                    "history and any files already created have been retained."
                )
            else:
                result = (
                    "Generation stopped before a simulation was created. No "
                    "simulation files were created in this attempt. Try again."
                )
                failure_detail = (
                    "No simulation files were created in this attempt. Try the "
                    "request again."
                )
            self._add_activity(
                session_id,
                request_id,
                activity_key="agent_run",
                state="failed",
                title=(
                    "Model connection interrupted"
                    if agent_error_was_retryable
                    else "Generation stopped before completion"
                ),
                detail=failure_detail,
            )
        if updated_names:
            self._add_activity(
                session_id,
                request_id,
                activity_key="generated_files",
                state="completed",
                title="Generated files updated",
                detail=(
                    f"Detected file changes in {len(updated_names)} simulation "
                    f"folder{'s' if len(updated_names) != 1 else ''}."
                ),
            )

        with self.lock:
            request = self._get_request(session_id, request_id)
            updated_project_ids = self._sync_changed_projects_unlocked(session_id, updated_names)
            # Persist ownership before validation/publication.  If a later
            # implementation or I/O failure escapes this worker, the fallback
            # finalizer can move these projects out of `updating` reliably.
            request["updated_project_ids"] = updated_project_ids
            request["updated_project_names"] = updated_names
            self._save_request(session_id, request)

        validation_results: Dict[str, Dict[str, Any]] = {}
        request_auto_check = bool(request.get("auto_check", True))
        run_codex_finalizer = (
            request_auto_check
            and self._codex_finalizer_enabled()
            and agent is not None
        )
        for project_id in updated_project_ids:
            with self.lock:
                project_for_validation = self._project_by_id(
                    session_id, project_id
                )
            project_label = str(
                project_for_validation.get("display_name") or "the simulation"
            )
            self._add_activity(
                session_id,
                request_id,
                activity_key=f"validate:{project_id}",
                state="started",
                title="Testing the generated simulation",
                detail=f"Running a bounded smoke test for {project_label}.",
                technical_name="backend smoke test",
            )
            validation_record = self._validate_simulation_for_publication(
                session_id, project_id
            )
            if (
                validation_record is not None
                and not run_codex_finalizer
            ):
                validation_record = self._apply_codex_final_gate(
                    session_id, request_id, project_id, validation_record
                )
            if (
                validation_record is not None
                and run_codex_finalizer
            ):
                validation_record = self._run_codex_finalizer_for_project(
                    agent=agent,
                    session_id=session_id,
                    request_id=request_id,
                    project_id=project_id,
                    validation_record=validation_record,
                    session_workspace=session_workspace,
                )
            if validation_record is not None:
                validation_results[project_id] = validation_record
                validation_status = validation_record.get("status")
                if validation_status == "succeeded":
                    validation_state = "completed"
                    validation_title = "Simulation test passed"
                    validation_detail = (
                        f"{project_label} completed its bounded smoke test."
                    )
                elif validation_status == "awaiting_user_run":
                    validation_state = "completed"
                    validation_title = "Scenario input is needed"
                    validation_detail = (
                        f"{project_label} remains unverified until you provide its "
                        "required scenario input."
                    )
                else:
                    validation_state = "failed"
                    validation_title = "Simulation test needs attention"
                    validation_detail = (
                        f"{project_label} did not pass its bounded smoke test."
                    )
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key=f"validate:{project_id}",
                    state=validation_state,
                    title=validation_title,
                    detail=validation_detail,
                    technical_name="backend smoke test",
                )

        try:
            repair_attempt_limit = int(
                os.getenv("DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS", "2")
            )
        except ValueError:
            repair_attempt_limit = 2
        repair_attempt_limit = max(0, min(repair_attempt_limit, 2))
        repair_driver = self._automatic_repair_driver()
        if run_codex_finalizer:
            repair_attempt_limit = 0
        repaired_success_names: List[str] = []
        if repair_attempt_limit and (agent is not None or repair_driver == "codex_cli"):
            initially_failed = [
                project_id
                for project_id, record in validation_results.items()
                if self._automatic_repair_is_appropriate(record)
            ]
            for project_id in initially_failed:
                for repair_attempt in range(1, repair_attempt_limit + 1):
                    failure = validation_results[project_id]
                    if not self._automatic_repair_is_appropriate(failure):
                        break
                    with self.lock:
                        try:
                            failed_project = self._project_by_id(
                                session_id, project_id
                            )
                        except KeyError:
                            break
                        # Project discovery points at the inner ``devs_project``
                        # marker so the visualizer can inspect model sources.  A
                        # runnable generated simulation, however, is the parent
                        # bundle containing run.py.  Repair and agent-side
                        # execution must use the same bundle root as the Run tab.
                        workspace_root = Path(session_workspace).resolve(
                            strict=True
                        )
                        discovered_root = contained_path(
                            workspace_root, failed_project["path"]
                        )
                        repair_root = (
                            discovered_root.parent
                            if discovered_root.name == "devs_project"
                            else discovered_root
                        ).resolve(strict=True)
                        repair_root.relative_to(workspace_root)
                        repair_bundle_rel = repair_root.relative_to(
                            workspace_root
                        ).as_posix()
                        self._add_event(
                            session_id,
                            request_id,
                            "simulation_repair_started",
                            (
                                f"Repairing {failed_project.get('display_name', project_id)} "
                                f"after smoke-test failure ({repair_attempt}/"
                                f"{repair_attempt_limit})."
                            ),
                        )
                    diagnostic = self._repair_diagnostic_for_model(
                        failure, session_workspace
                    )
                    repair_reason = (
                        "Codex final gate of the generated simulation failed."
                        if failure.get("failure_kind") == "acceptance_failed"
                        else "The backend smoke test of the generated simulation failed."
                    )
                    if repair_driver == "codex_cli":
                        repair_instruction = (
                            "Inspect the failure, make the smallest coherent fix, "
                            "then verify the exact bundle with a shell command like "
                            f"`cd {repair_bundle_rel} && {sys.executable} run.py`. "
                            "Do not create a replacement in another folder. Keep "
                            "inspection and run output compact."
                        )
                    else:
                        repair_instruction = (
                            "Inspect the failure, make the smallest coherent fix, "
                            "then verify the exact bundle with "
                            f"devs_execute(project_path={repair_bundle_rel!r}, "
                            "main_file='run.py'). Do not pass stdin_content and do "
                            "not create a replacement in another folder."
                        )
                    repair_prompt = "\n\n".join(
                        (
                            repair_reason,
                            (
                                "Repair only this runnable simulation bundle relative "
                                f"to the session workspace: {repair_bundle_rel}"
                            ),
                            repair_instruction,
                            f"Bounded smoke-test diagnostic:\n{diagnostic}",
                        )
                    )
                    repair_key = f"repair:{project_id}"
                    self._add_activity(
                        session_id,
                        request_id,
                        activity_key=repair_key,
                        state="started",
                        title=(
                            "Codex is repairing an execution problem"
                            if repair_driver == "codex_cli"
                            else "Repairing an execution problem"
                        ),
                        detail=(
                            f"{'Codex' if repair_driver == 'codex_cli' else 'The agent'} "
                            f"is correcting {failed_project.get('display_name', project_id)} "
                            f"after its smoke test (attempt {repair_attempt} of "
                            f"{repair_attempt_limit})."
                        ),
                        technical_name=(
                            "codex repair"
                            if repair_driver == "codex_cli"
                            else "automatic repair"
                        ),
                    )
                    repair_before = self._get_snapshot(session_workspace)
                    repair_exception: Optional[Exception] = None
                    try:
                        if repair_driver == "codex_cli":
                            repair_id = (
                                f"{request_id}-{project_id}-{repair_attempt}-"
                                f"{uuid.uuid4().hex[:8]}"
                            )
                            self._run_codex_cli_repair(
                                prompt=repair_prompt,
                                workspace_root=workspace_root,
                                bundle_root=repair_root,
                                repair_id=repair_id,
                            )
                        elif repair_driver in {"agent", "smolagents"}:
                            if agent is None:
                                raise RuntimeError(
                                    "Automatic repair driver 'agent' requires an agent."
                                )
                            with self._agent_progress_context(
                                agent, session_id, request_id
                            ):
                                agent.run(repair_prompt, reset=False)
                        else:
                            raise RuntimeError(
                                "Unsupported automatic repair driver "
                                f"{repair_driver!r}; use 'codex_cli' or 'agent'."
                            )
                    except Exception as exc:
                        repair_exception = exc
                        print(
                            "[Backend] Automatic repair response stopped "
                            f"({type(exc).__name__}): {exc}"
                        )
                        traceback.print_exc()
                    repair_after = self._get_snapshot(session_workspace)
                    repaired_names = self._detect_updated_project_names(
                        session_workspace, repair_before, repair_after
                    )
                    if not repaired_names:
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key=repair_key,
                            state="failed",
                            title=(
                                (
                                    "Codex repair stopped before changing files"
                                    if repair_driver == "codex_cli"
                                    else "Repair response stopped before changing files"
                                )
                                if repair_exception is not None
                                else (
                                    "Codex repair made no file changes"
                                    if repair_driver == "codex_cli"
                                    else "Automatic repair made no file changes"
                                )
                            ),
                            detail=(
                                "The original generated files were retained for "
                                "inspection and manual correction. The same repair "
                                "turn was not replayed."
                                if repair_exception is not None
                                else "inspection and manual correction."
                            ),
                        )
                        break
                    with self.lock:
                        repaired_project_ids = self._sync_changed_projects_unlocked(
                            session_id, repaired_names
                        )
                    updated_project_ids = list(
                        dict.fromkeys(
                            [*updated_project_ids, *repaired_project_ids]
                        )
                    )
                    updated_names = list(
                        dict.fromkeys([*updated_names, *repaired_names])
                    )
                    with self.lock:
                        repair_request = self._get_request(
                            session_id, request_id
                        )
                        repair_request["updated_project_ids"] = (
                            updated_project_ids
                        )
                        repair_request["updated_project_names"] = updated_names
                        self._save_request(session_id, repair_request)

                    # A repair turn may touch more than its intended bundle.
                    # Validate every discovered project before making another
                    # model call so no saved tree remains in `updating` and any
                    # later repair receives a diagnostic from the exact bytes
                    # that survived the interrupted turn.
                    for repaired_project_id in repaired_project_ids:
                        with self.lock:
                            repaired_project = self._project_by_id(
                                session_id, repaired_project_id
                            )
                        repaired_label = str(
                            repaired_project.get("display_name")
                            or repaired_project_id
                        )
                        if repaired_project_id != project_id:
                            self._add_activity(
                                session_id,
                                request_id,
                                activity_key=f"validate:{repaired_project_id}",
                                state="started",
                                title="Testing a changed simulation",
                                detail=(
                                    f"The repair also changed {repaired_label}; "
                                    "running its bounded smoke test."
                                ),
                                technical_name="backend smoke test",
                            )
                        repaired_validation = (
                            self._validate_simulation_for_publication(
                                session_id, repaired_project_id
                            )
                        )
                        if repaired_validation is not None:
                            repaired_validation = self._apply_codex_final_gate(
                                session_id,
                                request_id,
                                repaired_project_id,
                                repaired_validation,
                            )
                        if repaired_validation is None:
                            repaired_validation = {
                                "status": "failed",
                                "message": (
                                    "The changed simulation could not be checked "
                                    "after automatic repair."
                                ),
                                "failure_kind": "repair_validation_failed",
                                "completed_at": utc_now(),
                            }
                            with self.lock:
                                self._save_project_validation_unlocked(
                                    session_id,
                                    repaired_project_id,
                                    repaired_validation,
                                )
                        validation_results[repaired_project_id] = repaired_validation
                        if repaired_project_id != project_id:
                            repaired_status = repaired_validation.get("status")
                            self._add_activity(
                                session_id,
                                request_id,
                                activity_key=f"validate:{repaired_project_id}",
                                state=(
                                    "completed"
                                    if repaired_status
                                    in {"succeeded", "awaiting_user_run"}
                                    else "failed"
                                ),
                                title=(
                                    "Changed simulation test passed"
                                    if repaired_status == "succeeded"
                                    else "Changed simulation needs your input"
                                    if repaired_status == "awaiting_user_run"
                                    else "Changed simulation needs attention"
                                ),
                                detail=(
                                    f"{repaired_label} passed its bounded smoke test."
                                    if repaired_status == "succeeded"
                                    else f"{repaired_label} remains unverified until "
                                    "you provide its required scenario input."
                                    if repaired_status == "awaiting_user_run"
                                    else f"{repaired_label} did not pass its bounded "
                                    "smoke test."
                                ),
                                technical_name="backend smoke test",
                            )

                    repaired_validation = validation_results.get(project_id)
                    if repaired_validation is None:
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key=repair_key,
                            state="failed",
                            title="Repaired simulation could not be checked",
                            detail="The changed files were retained for inspection.",
                        )
                        break
                    if repaired_validation.get("status") == "succeeded":
                        repaired_success_names.append(
                            str(failed_project.get("display_name", project_id))
                        )
                        with self.lock:
                            self._add_event(
                                session_id,
                                request_id,
                                "simulation_repair_completed",
                                (
                                    f"{failed_project.get('display_name', project_id)} "
                                    "passed its exact-version smoke test after repair."
                                ),
                            )
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key=repair_key,
                            state="completed",
                            title="Execution problem repaired",
                            detail=(
                                f"{failed_project.get('display_name', project_id)} "
                                "now passes its bounded smoke test."
                            ),
                        )
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key=f"validate:{project_id}",
                            state="completed",
                            title="Simulation test passed after repair",
                            detail=(
                                f"{failed_project.get('display_name', project_id)} "
                                "is executable in the prepared runtime."
                            ),
                            technical_name="backend smoke test",
                        )
                        break
                    if repaired_validation.get("status") == "awaiting_user_run":
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key=repair_key,
                            state="completed",
                            title="Repair saved; scenario input is needed",
                            detail=(
                                "The changed simulation remains unverified until "
                                "you provide its required scenario input in Run."
                            ),
                        )
                        break
                    can_try_again = (
                        repair_attempt < repair_attempt_limit
                        and self._automatic_repair_is_appropriate(
                            repaired_validation
                        )
                    )
                    self._add_activity(
                        session_id,
                        request_id,
                        activity_key=repair_key,
                        state="progress" if can_try_again else "failed",
                        title=(
                            "Saved repair still needs attention; trying once more"
                            if can_try_again
                            else "Execution problem remains"
                        ),
                        detail=(
                            "OptPilot tested the files left by the interrupted repair "
                            "and will use that new diagnostic for one bounded follow-up."
                            if repair_exception is not None and can_try_again
                            else "The repair attempt completed, but the smoke test "
                            "still needs attention."
                        ),
                    )
                    if not can_try_again:
                        break

        if repaired_success_names:
            result = (
                f"{result}\n\n"
                "Validation: the backend found and repaired an execution problem "
                "before publishing "
                + ", ".join(repaired_success_names)
                + "."
            )

        if error is not None and updated_project_ids:
            recovered_project_ids = [
                project_id
                for project_id in updated_project_ids
                if validation_results.get(project_id, {}).get("status")
                in {"succeeded", "awaiting_user_run"}
            ]
            if len(recovered_project_ids) == len(updated_project_ids):
                error = None
                recovered_awaiting_input = any(
                    validation_results.get(project_id, {}).get("status")
                    == "awaiting_user_run"
                    for project_id in recovered_project_ids
                )
                if recovered_awaiting_input:
                    recovery_outcome = (
                        "OptPilot retained the saved files, but this exact version "
                        "is still unverified because required scenario input is "
                        "needed."
                    )
                elif repaired_success_names:
                    recovery_outcome = (
                        "OptPilot tested the saved files, repaired the execution "
                        "problem, and verified the resulting simulation."
                    )
                else:
                    recovery_outcome = (
                        "OptPilot tested the saved files and verified the resulting "
                        "simulation."
                    )
                result = (
                    "The model response ended unexpectedly after writing the "
                    f"simulation. {recovery_outcome}"
                )
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key="agent_run",
                    state="completed",
                    title=(
                        "Generation retained after interruption"
                        if recovered_awaiting_input
                        else "Generation recovered after interruption"
                    ),
                    detail=recovery_outcome,
                )
                self._add_event(
                    session_id,
                    request_id,
                    "request_recovered",
                    (
                        "Saved generated files were retained after interruption and "
                        "need user input before verification."
                        if recovered_awaiting_input
                        else "Saved generated files passed post-interruption verification."
                    ),
                )

        awaiting_user_runs = [
            project_id
            for project_id, record in validation_results.items()
            if record.get("status") == "awaiting_user_run"
        ]
        if awaiting_user_runs:
            with self.lock:
                awaiting_names = [
                    self._project_by_id(session_id, project_id).get(
                        "display_name", project_id
                    )
                    for project_id in awaiting_user_runs
                ]
            result = (
                f"{result}\n\n"
                "Unverified until you run a scenario: "
                + ", ".join(awaiting_names)
                + " needs required input before this exact version can be "
                "verified. Open Run, enter the scenario, and run it once."
            )

        unavailable_validations = [
            project_id
            for project_id, record in validation_results.items()
            if record.get("failure_kind")
            in _EXECUTION_INFRASTRUCTURE_FAILURE_KINDS
        ]
        if unavailable_validations:
            with self.lock:
                unavailable_names = [
                    self._project_by_id(session_id, project_id).get(
                        "display_name", project_id
                    )
                    for project_id in unavailable_validations
                ]
            result = (
                f"{result}\n\n"
                "Generated, but not execution-verified: "
                + ", ".join(unavailable_names)
                + ". The simulation runner was unavailable, so the files were "
                "kept without starting an automatic code-repair pass."
            )

        failed_validations = [
            project_id
            for project_id, record in validation_results.items()
            if record.get("status") not in {
                "succeeded",
                "awaiting_user_run",
            }
            and project_id not in unavailable_validations
        ]
        if failed_validations:
            with self.lock:
                failed_names = [
                    self._project_by_id(session_id, project_id).get(
                        "display_name", project_id
                    )
                    for project_id in failed_validations
                ]
            result = (
                f"{result}\n\n"
                "Validation needs attention: "
                + ", ".join(failed_names)
                + ". The files were kept in this session, but this version was "
                "not published as a completed output because its smoke test failed."
            )

        interface_outputs = []
        if error is None and self.interface_output_publisher is not None:
            ready_project_count = 0
            for project_id in updated_project_ids:
                with self.lock:
                    project = self._project_by_id(session_id, project_id)
                if self._default_validation(project).get("status") == "ready":
                    ready_project_count += 1
            if ready_project_count:
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key="prepare_output",
                    state="started",
                    title="Preparing the completed simulation",
                    detail=(
                        f"Publishing {ready_project_count} verified result"
                        f"{'s' if ready_project_count != 1 else ''} to the interface."
                    ),
                    current=0,
                    total=ready_project_count,
                    technical_name="output publication",
                )
            try:
                for project_id in updated_project_ids:
                    with self.lock:
                        project = self._project_by_id(session_id, project_id)
                    if project is None:
                        continue
                    validation = self._default_validation(project)
                    if validation.get("status") != "ready":
                        continue
                    output = self.interface_output_publisher.publish_ready_project(
                        session_id=session_id,
                        request_id=request_id,
                        workspace=Path(session_workspace),
                        project=project,
                        expected_content_digest=validation.get("bundle_digest"),
                    )
                    if output is not None:
                        interface_outputs.append(output)
                        self._add_activity(
                            session_id,
                            request_id,
                            activity_key="prepare_output",
                            state="progress",
                            title="Completed simulation prepared",
                            detail=(
                                f"Prepared {len(interface_outputs)} of "
                                f"{ready_project_count} verified results."
                            ),
                            current=len(interface_outputs),
                            total=ready_project_count,
                            technical_name="output publication",
                        )
                if ready_project_count:
                    if interface_outputs:
                        output_detail = (
                            f"{len(interface_outputs)} verified result"
                            f"{'s are' if len(interface_outputs) != 1 else ' is'} "
                            "available below the chat."
                        )
                    else:
                        output_detail = (
                            "No output card was created; the generated files remain "
                            "available in this session."
                        )
                    self._add_activity(
                        session_id,
                        request_id,
                        activity_key="prepare_output",
                        state="completed",
                        title="Output preparation finished",
                        detail=output_detail,
                        current=len(interface_outputs),
                        total=ready_project_count,
                        technical_name="output publication",
                    )
            except Exception as exc:
                print(
                    "[Backend] Generated output reporting failed "
                    f"({type(exc).__name__}): {exc}"
                )
                error = "Generated output reporting failed."
                result = (
                    f"{result}\n\nThe generated files were retained, but the "
                    "completed output card could not be prepared."
                )
                self._add_activity(
                    session_id,
                    request_id,
                    activity_key="prepare_output",
                    state="failed",
                    title="Completed output could not be prepared",
                    detail=(
                        "The generated session files were retained, but the "
                        "read-only output card could not be created."
                    ),
                    technical_name="output publication",
                )

        result_state = None
        result_title = None
        result_detail = None
        if error is None:
            if failed_validations:
                result_state = "failed"
                result_title = "Simulation needs attention"
                result_detail = (
                    "The generated files were kept, but the exact runner still "
                    "fails. Review the test result or ask the agent to repair it."
                )
            elif unavailable_validations:
                result_state = "completed"
                result_title = "Simulation generated; test unavailable"
                result_detail = (
                    "The files were kept, but the execution service could not "
                    "verify this version."
                )
            elif awaiting_user_runs:
                result_state = "completed"
                result_title = "Simulation needs your scenario input"
                result_detail = (
                    "This exact version is unverified. Open Run, provide the "
                    "required scenario values, and test it once."
                )
            elif updated_project_ids:
                result_state = "completed"
                result_title = "Simulation ready"
                result_detail = (
                    "The exact generated runner passed its bounded test. Review "
                    "the simulation or run another scenario."
                )
            else:
                result_state = "completed"
                result_title = "Request complete"
                result_detail = "Review the agent's response below."
            self._add_activity(
                session_id,
                request_id,
                activity_key="request_result",
                state=result_state,
                title=result_title,
                detail=result_detail,
            )

        with self.lock:
            request = self._get_request(session_id, request_id)
            assistant_message = {
                "message_id": new_id("msg"),
                "session_id": session_id,
                "request_id": request_id,
                "role": "assistant",
                "status": "visible",
                "content": self._format_assistant_result_message(
                    result,
                    error=error,
                    result_title=result_title,
                    result_detail=result_detail,
                    updated_project_names=updated_names,
                    interface_output_count=len(interface_outputs),
                ),
                "created_at": utc_now(),
                "withdrawn_at": None,
            }
            self._save_message(session_id, assistant_message)
            request["assistant_message_id"] = assistant_message["message_id"]
            request["updated_project_ids"] = updated_project_ids
            request["updated_project_names"] = updated_names
            request["interface_output_ids"] = [
                output["id"] for output in interface_outputs
            ]
            request["completed_at"] = utc_now()
            request["error"] = error
            request["status"] = "failed" if error else "completed"
            self._save_request(session_id, request)
            session = self._load_session(session_id)
            session["status"] = "failed" if error else "idle"
            session["active_request_id"] = None
            self._save_session(session)
            self._release_session_agent_unlocked(session_id)
            self._add_event(session_id, request_id, "request_failed" if error else "request_completed", "Agent run finished.")
        self._schedule_collector_sync(
            session_id,
            include_snapshots=bool(updated_project_ids),
        )

    def _get_snapshot(self, workspace: Optional[str] = None) -> Dict[str, float]:
        workspace = workspace or self.working_dir
        snapshot = {}
        for root, dirs, files in os.walk(workspace, followlinks=False):
            safe_dirs = []
            for directory in dirs:
                path = os.path.join(root, directory)
                try:
                    metadata = os.lstat(path)
                except OSError:
                    continue
                if (
                    directory != META_DIR_NAME
                    and not directory.startswith(".")
                    and stat.S_ISDIR(metadata.st_mode)
                    and not stat.S_ISLNK(metadata.st_mode)
                ):
                    safe_dirs.append(directory)
            dirs[:] = safe_dirs
            for file in files:
                path = os.path.join(root, file)
                try:
                    metadata = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    snapshot[path] = metadata.st_mtime_ns
        return snapshot

    def _detect_updated_project_names(self, workspace: str, pre_snapshot: Dict[str, float], post_snapshot: Dict[str, float]) -> List[str]:
        updated = set()
        for path, mtime in post_snapshot.items():
            if path not in pre_snapshot or pre_snapshot[path] != mtime:
                rel_path = os.path.relpath(path, workspace)
                top_dir = rel_path.split(os.sep)[0]
                if (
                    top_dir
                    and top_dir != META_DIR_NAME
                    and not top_dir.startswith(".")
                ):
                    updated.add(top_dir)
        return sorted(updated)

    @staticmethod
    def _is_student_visible_project_path(relative_path: str) -> bool:
        parts = PurePosixPath(relative_path.replace("\\", "/")).parts
        if not parts:
            return False
        top_dir = parts[0]
        return bool(top_dir and top_dir != META_DIR_NAME and not top_dir.startswith("."))

    @classmethod
    def _detect_updated_project_files(
        cls,
        workspace: str,
        pre_snapshot: Dict[str, float],
        post_snapshot: Dict[str, float],
    ) -> List[str]:
        updated: set[str] = set()
        for path in set(pre_snapshot) | set(post_snapshot):
            if pre_snapshot.get(path) == post_snapshot.get(path):
                continue
            rel_path = os.path.relpath(path, workspace).replace(os.sep, "/")
            if cls._is_student_visible_project_path(rel_path):
                updated.add(rel_path)
        return sorted(updated)

    @staticmethod
    def _changed_source_files(changed_files: List[str]) -> List[str]:
        internal_parts = {
            "__pycache__",
            "_analysis_logs",
            "simulation_outputs",
            "results",
        }
        ignored_suffixes = {".pyc", ".pyo", ".log"}
        source_files: List[str] = []
        for relative_path in changed_files:
            pure_path = PurePosixPath(relative_path.replace("\\", "/"))
            if any(part in internal_parts or part.startswith(".") for part in pure_path.parts):
                continue
            if pure_path.suffix.lower() in ignored_suffixes:
                continue
            source_files.append(pure_path.as_posix())
        return source_files

    def _build_agent_prompt(
        self,
        session_id: str,
        current_request_id: str,
        user_content: str,
        active_project: Optional[Dict[str, Any]],
        include_project_context: bool,
    ) -> str:
        history_rows = [
            row for row in self._read_jsonl(self._messages_path(session_id))
            if row.get("status") == "visible" and row.get("request_id") != current_request_id
        ][-12:]
        sections = [
            f"Current session_id: {session_id}",
            (
                "Work only on the requested simulation and relevant session files. "
                "Keep each simulation in its own named subfolder and finish with "
                "that folder's top-level run.py; never create run.py at the session "
                "workspace root. The backend will execute "
                "an exact snapshot as a bounded smoke test; do not claim success "
                "unless the generated runner and its inputs are coherent. When you "
                "create or change a runnable simulation, use devs_execute with the "
                "dedicated subfolder as project_path and main_file='run.py'. Do not "
                "pass project_path='.' or stdin_content. In xDEVS, Port has no "
                "singular .value attribute; use stable model state for summary "
                "metrics, or .values only when its transient semantics are "
                "intentional. For an ordinary generation request, a successful "
                "devs_execute call with a valid result summary is the completion "
                "condition: report the result and stop. Do not add debug "
                "instrumentation or keep changing code merely because a valid "
                "metric value looks surprising. Continue after a successful run "
                "only when the user explicitly asked to verify a domain property "
                "that the output disproves. Make at most two coherent repair "
                "passes after a failed execution. If it still fails, preserve "
                "the files and report the exact diagnostic instead of claiming success or "
                "repeating repairs indefinitely."
            ),
        ]
        if include_project_context and active_project:
            sections.append(
                "\n".join(
                    [
                        f"Selected project for optional UI context: {active_project['display_name']}",
                        f"Selected project folder relative to working directory: {active_project['path']}",
                        "This selected project is context only; you may inspect or modify any relevant files in the session workspace.",
                    ]
                )
            )
        if history_rows:
            formatted_history = []
            for row in history_rows:
                role = "User" if row.get("role") == "user" else "Assistant"
                content = str(row.get("content", ""))
                limit = (
                    MAX_HISTORY_USER_MESSAGE_BYTES
                    if row.get("role") == "user"
                    else MAX_HISTORY_ASSISTANT_MESSAGE_BYTES
                )
                if len(content.encode("utf-8")) > limit:
                    content = self._bounded_interaction_text(content, limit)
                    content += "\n...[truncated]"
                formatted_history.append(f"{role}: {content}")
            sections.append("Conversation history:\n" + "\n\n".join(formatted_history))
        try:
            request = self._get_request(session_id, current_request_id)
            approved_intent = request.get("approved_intent") or {}
            if approved_intent.get("artifact_id"):
                intent_artifact = self._load_request_artifact(
                    session_id,
                    current_request_id,
                    str(approved_intent["artifact_id"]),
                )
                sections.append(
                    "Confirmed interpretation (use this instead of "
                    "reinterpreting the request):\n"
                    + json.dumps(
                        intent_artifact.get("data") or {},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            approved_structure = request.get("approved_structure") or {}
            if approved_structure.get("artifact_id"):
                structure_artifact = self._load_request_artifact(
                    session_id,
                    current_request_id,
                    str(approved_structure["artifact_id"]),
                )
                sections.append(
                    "Confirmed DEVS structure. Implement this plan without "
                    "changing its component hierarchy:\n"
                    + json.dumps(
                        structure_artifact.get("data") or {},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        except (KeyError, OSError, ValueError):
            # Requests created by an older backend have no review artifacts.
            pass
        sections.append(f"Current user request:\n{user_content}")
        return "\n\n".join(sections)

    def default_session_id(self) -> str:
        sessions = self.list_sessions(limit=1)
        if not sessions:
            raise KeyError("No sessions are registered")
        return sessions[0]["session_id"]

    def scan_projects(self) -> List[str]:
        return [project["display_name"] for project in self.list_projects(self.default_session_id())]

    def legacy_get_project_files(self, project_name: str) -> Dict[str, str]:
        session_id = self.default_session_id()
        for project in self.list_projects(session_id):
            if project["display_name"] == project_name:
                return self.get_project_files(session_id, project["project_id"])["files"]
        raise FileNotFoundError(project_name)


def run_devs_display_backend(
    manager_agent: CodeAgent | ToolCallingAgent,
    working_directory: str,
    agent_factory: Optional[Callable[[str], CodeAgent | ToolCallingAgent]] = None,
    registry_path: Optional[str] = None,
):
    backend_service = DEVSBackendService(
        agent=manager_agent,
        working_directory=working_directory,
        registry_path=registry_path,
        discover_existing=True,
        agent_factory=agent_factory,
        worker_count=max_active_generations(),
    )
    fastapi_app = create_app(backend_service)
    backend_host = os.environ.get("DEVS_INTERFACE_BACKEND_HOST", "0.0.0.0")
    backend_port = int(os.environ.get("DEVS_INTERFACE_BACKEND_PORT", "8000"))
    uvicorn.run(fastapi_app, host=backend_host, port=backend_port)


if __name__ == "__main__":
    print("Please run server from `devs_app.run`, setting the `--mode` to be `server`")
