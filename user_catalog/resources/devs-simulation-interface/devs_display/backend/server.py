import json
import os
import shutil
import tempfile
import uuid
import hashlib
from datetime import datetime, timezone
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
    infer_project_root_model,
    local_parse_xdevs_structure,
    parse_model_for_visualizer as parse_model_for_visualizer_impl,
)

load_dotenv(override=True)

META_DIR_NAME = ".devs_display_sessions"
HAMLET_CORE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_REGISTRY_PATH = os.path.join(HAMLET_CORE_DIR, "devs_display", ".storage", "session_registry.json")
DEFAULT_WORKING_DIRS_ROOT = os.path.join(HAMLET_CORE_DIR, "devs_app", "working_dirs")
DEFAULT_GRAPH_PARSE_MODEL = "openrouter/openai/gpt-5.4-mini"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def safe_project_id(display_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in display_name).strip("_")
    return f"proj_{cleaned or uuid.uuid4().hex[:8]}"


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
    ):
        self.agent = agent
        self.agent_factory = agent_factory
        self.workspace_agents: Dict[str, CodeAgent | ToolCallingAgent] = {}
        self.working_dir = os.path.abspath(working_directory)
        self.meta_dir = os.path.join(self.working_dir, META_DIR_NAME)
        self.sessions_dir = os.path.join(self.meta_dir, "sessions")
        if registry_path is None and self.working_dir.startswith(os.path.abspath(tempfile.gettempdir()) + os.sep):
            registry_path = os.path.join(self.meta_dir, "session_registry.json")
        self.registry_path = os.path.abspath(registry_path or DEFAULT_REGISTRY_PATH)
        self.session_locations: Dict[str, Dict[str, str]] = {}
        self.lock = Lock()
        self.worker_queue: Queue[str] = Queue()
        self.worker_count = max(1, worker_count)
        self.workspace_agents[self.working_dir] = agent

        self._ensure_storage()
        if discover_existing:
            self._register_discovered_sessions()
        self._register_workspace_session(self.working_dir)
        self._rebuild_session_locations()
        self._recover_incomplete_requests(requeue=start_worker)

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

    def _rewrite_jsonl(self, path: str, rows: List[Dict[str, Any]]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)

    def _recover_incomplete_requests(self, requeue: bool):
        for session_id in list(self.session_locations):
            try:
                session = self._load_session(session_id)
            except KeyError:
                continue
            changed = False
            for request in self._load_requests(session_id):
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
                    request["status"] = "failed"
                    request["completed_at"] = utc_now()
                    request["error"] = "Backend restarted while this request was running; the prior worker process cannot be resumed."
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
        projects = self._load_projects(session_id)
        by_path = {p.get("path"): p for p in projects}
        existing_ids = {p["project_id"] for p in projects}
        changed = False
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
            projects.append(self._make_project_record(project_id, display_name, rel_path, "legacy_working_directory"))
            by_path[rel_path] = projects[-1]
            changed = True
        if changed:
            self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)

    def _discover_project_rel_paths(self, search_rel: str = "", workspace: Optional[str] = None) -> List[str]:
        workspace = workspace or self.working_dir
        search_abs = os.path.join(workspace, search_rel)
        discovered = set()
        for root, dirs, _files in os.walk(search_abs):
            dirs[:] = [d for d in dirs if d != META_DIR_NAME and not d.startswith(".") and d != "__pycache__"]
            if has_devs_project_marker(root):
                discovered.add(os.path.relpath(root, workspace).replace("\\", "/"))
                dirs[:] = []
        if not discovered and search_rel and has_devs_project_marker(search_abs):
            discovered.add(search_rel.replace("\\", "/"))
        return sorted(discovered)

    def _read_text_files_from_abs_path(self, project_path: str) -> Dict[str, str]:
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d != META_DIR_NAME and not d.startswith(".") and d != "__pycache__"]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        files_data[rel_path] = f.read()
                except Exception:
                    continue
        return files_data

    def _project_display_name(self, rel_path: str, workspace: Optional[str] = None) -> str:
        workspace = workspace or self.working_dir
        abs_path = os.path.join(workspace, rel_path)
        root_model = infer_project_root_model(self._read_text_files_from_abs_path(abs_path))
        normalized = rel_path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else normalized)
        return f"{tail}:{root_model}" if root_model else tail

    def _make_project_record(self, project_id: str, display_name: str, rel_path: str, source_type: str) -> Dict[str, Any]:
        return {
            "project_id": project_id,
            "display_name": display_name,
            "status": "ready",
            "version": 1,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "path": rel_path,
            "source": {"type": source_type},
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
        session["project_count"] = len(self._load_projects(session_id))
        self._save_session(session)

    def _project_by_id(self, session_id: str, project_id: str) -> Dict[str, Any]:
        for project in self._load_projects(session_id):
            if project["project_id"] == project_id:
                return project
        raise KeyError(project_id)

    def _project_abs_path(self, project: Dict[str, Any], session_id: Optional[str] = None) -> str:
        workspace = self._session_workspace(session_id) if session_id else self.working_dir
        return os.path.join(workspace, project["path"])

    def _next_event_id(self, session_id: str) -> int:
        events = self._read_jsonl(self._events_path(session_id))
        return events[-1]["event_id"] + 1 if events else 1

    def _add_event(self, session_id: str, request_id: str, event_type: str, content: str):
        event = {
            "event_id": self._next_event_id(session_id),
            "session_id": session_id,
            "request_id": request_id,
            "type": event_type,
            "content": content,
            "created_at": utc_now(),
        }
        self._append_jsonl(self._events_path(session_id), event)
        return event

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

    def list_sessions(self, limit: int = 20, offset: int = 0):
        with self.lock:
            self._rebuild_session_locations()
            sessions = []
            for item in self.session_locations:
                try:
                    self._sync_session_projects(item)
                    sessions.append(self._load_session(item))
                except KeyError:
                    continue
            sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
            return sessions[offset : offset + limit]

    def get_frontend_config(self):
        openrouter_available = bool(os.getenv("OPENROUTER_API_KEY", ""))
        gemini_available = bool(
            os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
            or os.getenv("API_KEY", "")
        )
        default_provider = "openai" if openrouter_available else "gemini"
        default_model = next(
            preset["model"]
            for preset in FRONTEND_MODEL_PRESETS
            if preset["provider"] == default_provider
        )
        return {
            "default_provider": default_provider,
            "default_model": default_model,
            "api_key_available": {
                "openai": openrouter_available,
                "gemini": gemini_available,
            },
            "model_presets": FRONTEND_MODEL_PRESETS,
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

    def create_session(self, title: Optional[str], clone_projects: List[CloneProjectSpec]):
        with self.lock:
            session_id = new_id("sess")
            session_workspace = self._new_session_workspace()
            self._ensure_workspace_storage(session_workspace)
            self.session_locations[session_id] = {"workspace": session_workspace, "storage_id": session_id}
            session = {
                "session_id": session_id,
                "title": title or "New Session",
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
            ):
                open(path, "a", encoding="utf-8").close()
            self._register_existing_storage_session(session_workspace, session_id)
            self._rebuild_session_locations()
            projects = self._clone_projects_unlocked(session_id, clone_projects) if clone_projects else []
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
            return self._load_session(session_id)

    def delete_session(self, session_id: str):
        with self.lock:
            session = self._load_session(session_id)
            if session.get("status") in {"queued", "running", "cancelling"}:
                raise RuntimeError("Cannot delete a session while it has active work")

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
            self._load_session(session_id)
            projects = self._load_projects(session_id)
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            target_rel = self._unique_project_path(display_name, self._session_workspace(session_id))
            target_abs = os.path.join(self._session_workspace(session_id), target_rel)
            os.makedirs(target_abs, exist_ok=True)
            for rel_path, content in files.items():
                safe_rel = rel_path.lstrip("/\\")
                file_path = os.path.join(target_abs, safe_rel)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            project = {
                "project_id": project_id,
                "display_name": display_name,
                "status": "ready",
                "version": 1,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "path": target_rel,
                "source": {"type": "upload"},
            }
            projects.append(project)
            self._save_projects(session_id, projects)
            self._update_session_project_count(session_id)
            return project

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
        candidate = display_name
        suffix = 2
        while os.path.exists(os.path.join(workspace, candidate)):
            candidate = f"{display_name}_{suffix}"
            suffix += 1
        return candidate

    def _is_valid_project_dir(self, rel_path: str, workspace: Optional[str] = None) -> bool:
        if not rel_path or rel_path.startswith(".") or rel_path == META_DIR_NAME:
            return False
        workspace = workspace or self.working_dir
        abs_path = os.path.join(workspace, rel_path)
        return os.path.isdir(abs_path) and has_devs_project_marker(abs_path)

    def _sync_changed_projects_unlocked(self, session_id: str, changed_top_dirs: List[str]) -> List[str]:
        workspace = self._session_workspace(session_id)
        projects = self._load_projects(session_id)
        existing_ids = {project["project_id"] for project in projects}
        by_path = {str(project.get("path", "")).replace("\\", "/"): project for project in projects}
        updated_project_ids = []

        changed_project_paths = set()
        for changed_name in changed_top_dirs:
            changed_rel = changed_name.replace("\\", "/")
            top_dir = changed_rel.split("/")[0]
            if not top_dir or top_dir == META_DIR_NAME:
                continue
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
                self._delete_graph_cache(session_id, project["project_id"])
                updated_project_ids.append(project["project_id"])
                continue

            if not self._is_valid_project_dir(rel_path, workspace):
                continue

            display_name = self._project_display_name(rel_path, workspace)
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            existing_ids.add(project_id)

            new_project = self._make_project_record(project_id, display_name, rel_path, "agent_generated")
            projects.append(new_project)
            by_path[rel_path] = new_project
            updated_project_ids.append(project_id)

        self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)
        return updated_project_ids

    def _clone_projects_unlocked(self, session_id: str, clone_specs: List[CloneProjectSpec]):
        self._load_session(session_id)
        projects = self._load_projects(session_id)
        created = []
        for spec in clone_specs:
            source_project = self._project_by_id(spec.source_session_id, spec.source_project_id)
            display_name = spec.display_name or source_project["display_name"]
            project_id = self._unique_project_id(projects, safe_project_id(display_name))
            target_rel = self._unique_project_path(display_name, self._session_workspace(session_id))
            shutil.copytree(
                self._project_abs_path(source_project, spec.source_session_id),
                os.path.join(self._session_workspace(session_id), target_rel),
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
            }
            projects.append(project)
            created.append(project)
        self._save_projects(session_id, projects)
        self._update_session_project_count(session_id)
        return created

    def clone_projects(self, session_id: str, clone_specs: List[CloneProjectSpec]):
        with self.lock:
            return self._clone_projects_unlocked(session_id, clone_specs)

    def get_project_files(self, session_id: str, project_id: str) -> Dict[str, Any]:
        with self.lock:
            session = self._load_session(session_id)
            project = self._project_by_id(session_id, project_id)
            project_path = self._project_abs_path(project, session_id)
        if not os.path.exists(project_path):
            raise FileNotFoundError(project_id)
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d != META_DIR_NAME and not d.startswith(".")]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        files_data[rel_path] = f.read()
                except Exception:
                    files_data[rel_path] = "[Binary Content]"
        return {"files": files_data, "project": project, "session_status": session["status"]}

    def _read_project_files_unlocked(self, project: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, str]:
        project_path = self._project_abs_path(project, session_id)
        files_data = {}
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d != META_DIR_NAME and not d.startswith(".") and d != "__pycache__"]
            for file in files:
                if file.startswith(".") or file.endswith((".pyc", ".pyo")):
                    continue
                abs_path = os.path.join(root, file)
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
    ):
        with self.lock:
            session = self._load_session(session_id)
            if self._session_workspace(session_id) != self.working_dir and not self.agent_factory:
                raise RuntimeError(
                    "This session belongs to a previous backend workspace. "
                    "Start the backend with that workspace to continue chatting in it."
                )
            if session["status"] in {"queued", "running", "cancelling"}:
                raise RuntimeError("Session already has a queued or running request")
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
            }
            self._save_message(session_id, user_message)
            self._save_request(session_id, request)
            session["status"] = "queued"
            session["active_request_id"] = request_id
            self._save_session(session)
            self._add_event(session_id, request_id, "request_started", "Request queued.")
            self.worker_queue.put(request_id)
            return request, user_message

    def get_request(self, session_id: str, request_id: str):
        with self.lock:
            self._load_session(session_id)
            return self._get_request(session_id, request_id)

    def get_events(self, session_id: str, after: int = 0, request_id: Optional[str] = None, limit: int = 100):
        with self.lock:
            session = self._load_session(session_id)
            events = [{**event, "session_id": session_id} for event in self._read_jsonl(self._events_path(session_id)) if event["event_id"] > after]
            if request_id:
                events = [event for event in events if event["request_id"] == request_id]
            events = events[:limit]
            next_after = events[-1]["event_id"] if events else after
            request_status = None
            if request_id:
                try:
                    request_status = self._get_request(session_id, request_id)["status"]
                except KeyError:
                    request_status = None
            return {"events": events, "next_after": next_after, "request_status": request_status or session["status"]}

    def cancel_request(self, session_id: str, request_id: str, withdraw_user_message: bool = True):
        with self.lock:
            request = self._get_request(session_id, request_id)
            if request["status"] == "queued":
                request["status"] = "cancelled"
                request["completed_at"] = utc_now()
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
                self._add_event(session_id, request_id, "request_cancelled", "Queued request withdrawn.")
                return request, user_message
            if request["status"] == "running":
                raise RuntimeError("Running request cancellation is not supported in this MVP")
            return request, self._message_for_request(session_id, request_id, "user")

    def _worker_loop(self):
        while True:
            request_id = self.worker_queue.get()
            try:
                self._run_queued_request(request_id)
            finally:
                self.worker_queue.task_done()

    def _run_queued_request(self, request_id: str):
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
            request["started_at"] = utc_now()
            self._save_request(session_id, request)
            session = self._load_session(session_id)
            session["status"] = "running"
            session["active_request_id"] = request_id
            self._save_session(session)
            self._add_event(session_id, request_id, "agent_started", "Agent run started.")
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
        try:
            agent = self._agent_for_workspace(session_workspace)
            result = agent.run(prompt, reset=False)
            error = None
        except Exception as exc:
            result = f"Error: {exc}"
            error = str(exc)
        post_snapshot = self._get_snapshot(session_workspace)
        updated_names = self._detect_updated_project_names(session_workspace, pre_snapshot, post_snapshot)

        with self.lock:
            request = self._get_request(session_id, request_id)
            updated_project_ids = self._sync_changed_projects_unlocked(session_id, updated_names)
            assistant_message = {
                "message_id": new_id("msg"),
                "session_id": session_id,
                "request_id": request_id,
                "role": "assistant",
                "status": "visible",
                "content": str(result),
                "created_at": utc_now(),
                "withdrawn_at": None,
            }
            self._save_message(session_id, assistant_message)
            request["assistant_message_id"] = assistant_message["message_id"]
            request["updated_project_ids"] = updated_project_ids
            request["updated_project_names"] = updated_names
            request["completed_at"] = utc_now()
            request["error"] = error
            request["status"] = "failed" if error else "completed"
            self._save_request(session_id, request)
            session = self._load_session(session_id)
            session["status"] = "failed" if error else "idle"
            session["active_request_id"] = None
            self._save_session(session)
            self._add_event(session_id, request_id, "request_failed" if error else "request_completed", "Agent run finished.")

    def _get_snapshot(self, workspace: Optional[str] = None) -> Dict[str, float]:
        workspace = workspace or self.working_dir
        snapshot = {}
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d != META_DIR_NAME and not d.startswith(".")]
            for file in files:
                path = os.path.join(root, file)
                snapshot[path] = os.path.getmtime(path)
        return snapshot

    def _detect_updated_project_names(self, workspace: str, pre_snapshot: Dict[str, float], post_snapshot: Dict[str, float]) -> List[str]:
        updated = set()
        for path, mtime in post_snapshot.items():
            if path not in pre_snapshot or pre_snapshot[path] != mtime:
                rel_path = os.path.relpath(path, workspace)
                top_dir = rel_path.split(os.sep)[0]
                if top_dir and top_dir != META_DIR_NAME:
                    updated.add(top_dir)
        return sorted(updated)

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
            "Do not inspect existing folders. Only do the things you are required to do, do not test or verify unless specified."
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
                if len(content) > 4000:
                    content = content[:4000] + "\n...[truncated]"
                formatted_history.append(f"{role}: {content}")
            sections.append("Conversation history:\n" + "\n\n".join(formatted_history))
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
):
    backend_service = DEVSBackendService(
        agent=manager_agent,
        working_directory=working_directory,
        discover_existing=True,
        agent_factory=agent_factory,
    )
    fastapi_app = create_app(backend_service)
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    print("Please run server from `devs_app.run`, setting the `--mode` to be `server`")
