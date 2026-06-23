from __future__ import annotations

import json
import os
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


JsonDict = Dict[str, Any]
ToolExecutor = Callable[[str, JsonDict], JsonDict]

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENHANDS_SESSION_ENDPOINT = "/api/conversations"
FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT = """You are OptPilot Assistant inside OptPilot Studio.
Answer using the OptPilot context packet provided by the GUI. Keep public
OptPilot explanations centered on environment-owned evaluator.settings and
method-visible methodContext.references. Do not claim you modified files,
launched studies, or registered catalog entries unless the runtime confirms it."""


OPTPILOT_AGENT_TOOLS = [
    "optpilot_workspace_list",
    "optpilot_workspace_create",
    "optpilot_workspace_attach",
    "optpilot_workspace_detach",
    "optpilot_workspace_focus",
    "optpilot_file_tree",
    "optpilot_file_read",
    "optpilot_file_write",
    "optpilot_file_diff",
    "optpilot_shell_run",
    "optpilot_catalog_list",
    "optpilot_catalog_detail",
    "optpilot_compatibility_check",
    "optpilot_config_discover",
    "optpilot_config_validate",
    "optpilot_registration_prepare",
    "optpilot_registration_validate",
    "optpilot_registration_apply",
    "optpilot_study_draft",
    "optpilot_study_save",
    "optpilot_study_launch",
    "optpilot_job_stop",
    "optpilot_run_list",
    "optpilot_run_detail",
    "optpilot_run_file_read",
    "optpilot_run_open_workspace",
    "optpilot_run_compare",
    "optpilot_smoke_test_study",
    "optpilot_docs_search",
]


def _tool_schema(properties: JsonDict, required: Optional[List[str]] = None) -> JsonDict:
    return {"type": "object", "properties": properties, "required": required or []}


OPTPILOT_AGENT_TOOL_SPECS: List[JsonDict] = [
    {
        "name": "optpilot_workspace_list",
        "description": "List OptPilot assistant workspaces and attachment state for the current assistant session.",
        "parameters": _tool_schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_workspace_create",
        "description": "Create a new editable OptPilot workspace or register an existing allowed local folder as a workspace.",
        "parameters": _tool_schema({
            "title": {"type": "string"},
            "root": {"type": "string"},
            "description": {"type": "string"},
            "source_type": {"type": "string"},
        }),
    },
    {
        "name": "optpilot_workspace_attach",
        "description": "Attach a known workspace to the current assistant session.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}}, ["workspace_id"]),
    },
    {
        "name": "optpilot_workspace_detach",
        "description": "Detach a workspace from the current assistant session without deleting files.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}}, ["workspace_id"]),
    },
    {
        "name": "optpilot_workspace_focus",
        "description": "Select an attached workspace and optional focus path for the current assistant session.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}}, ["workspace_id"]),
    },
    {
        "name": "optpilot_file_tree",
        "description": "List files under an attached workspace root.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}, "max_files": {"type": "integer"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_file_read",
        "description": "Read a text file under an attached workspace root.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}}, ["path"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_file_write",
        "description": "Write a text file under an editable attached workspace root.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    },
    {
        "name": "optpilot_file_diff",
        "description": "Preview a unified diff for writing content to a file under an attached workspace root.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_shell_run",
        "description": "Run a bounded command in an editable attached workspace. Risky commands return an approval request.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": "integer"},
        }, ["command"]),
    },
    {
        "name": "optpilot_catalog_list",
        "description": "List catalog environments, methods, studies, and builtins.",
        "parameters": _tool_schema({"config_kind": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_catalog_detail",
        "description": "Inspect one catalog environment, method, or study by kind and uid/path.",
        "parameters": _tool_schema({"config_kind": {"type": "string"}, "uid": {"type": "string"}, "path": {"type": "string"}}, ["config_kind"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_compatibility_check",
        "description": "Check method/environment compatibility, optionally for a selected pair.",
        "parameters": _tool_schema({"environment_path": {"type": "string"}, "method_path": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_config_discover",
        "description": "Discover OptPilot configs in an attached workspace.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_config_validate",
        "description": "Validate an OptPilot environment, method, or study YAML file in an attached workspace or allowed catalog path.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}}, ["path"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_registration_prepare",
        "description": "Create a registration manifest for selected configs in an attached workspace.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "config_paths": {"type": "array", "items": {"type": "string"}}}, ["workspace_id"]),
    },
    {
        "name": "optpilot_registration_validate",
        "description": "Validate a prepared registration manifest.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "registration_id": {"type": "string"}}, ["workspace_id", "registration_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_registration_apply",
        "description": "Apply a validated registration manifest into user_catalog after approval.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "registration_id": {"type": "string"}}, ["workspace_id", "registration_id"]),
    },
    {
        "name": "optpilot_study_draft",
        "description": "Draft a study from selected environment and method configs.",
        "parameters": _tool_schema({"environment_path": {"type": "string"}, "method_path": {"type": "string"}, "name": {"type": "string"}, "metric": {"type": "string"}, "direction": {"type": "string"}, "maxTrials": {"type": "integer"}}),
    },
    {
        "name": "optpilot_study_save",
        "description": "Save study YAML into an editable attached workspace.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}, "yaml": {"type": "string"}}, ["path", "yaml"]),
    },
    {
        "name": "optpilot_study_launch",
        "description": "Launch a validated study after approval.",
        "parameters": _tool_schema({"study_path": {"type": "string"}, "output_root": {"type": "string"}}, ["study_path"]),
    },
    {
        "name": "optpilot_job_stop",
        "description": "Stop a live OptPilot UI job after approval.",
        "parameters": _tool_schema({"job_id": {"type": "string"}}, ["job_id"]),
    },
    {
        "name": "optpilot_run_list",
        "description": "List live and completed OptPilot runs.",
        "parameters": _tool_schema({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_run_detail",
        "description": "Read structured run detail for a run id/path.",
        "parameters": _tool_schema({"run_id": {"type": "string"}, "path": {"type": "string"}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_run_file_read",
        "description": "Read one text file from a run directory.",
        "parameters": _tool_schema({"run_id": {"type": "string"}, "path": {"type": "string"}}, ["path"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_run_open_workspace",
        "description": "Attach a run directory as an analysis workspace.",
        "parameters": _tool_schema({"run_id": {"type": "string"}, "path": {"type": "string"}}),
    },
    {
        "name": "optpilot_run_compare",
        "description": "Compare compatible runs by id/path and summarize metrics and compatibility caveats.",
        "parameters": _tool_schema({"runs": {"type": "array", "items": {"type": "string"}}}, ["runs"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_smoke_test_study",
        "description": "Run a small validated study into a temporary output directory.",
        "parameters": _tool_schema({"study_path": {"type": "string"}, "max_trials": {"type": "integer"}}, ["study_path"]),
    },
    {
        "name": "optpilot_docs_search",
        "description": "Search curated OptPilot docs and schema files for compact excerpts.",
        "parameters": _tool_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
        "annotations": {"readOnlyHint": True},
    },
]


@dataclass(frozen=True)
class OpenHandsRuntimeConfig:
    base_url: str = ""
    session_endpoint: str = ""
    model: str = ""
    api_key: str = ""
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "OpenHandsRuntimeConfig":
        base_url = os.environ.get("OPTPILOT_OPENHANDS_URL", "").strip().rstrip("/")
        session_endpoint = os.environ.get("OPTPILOT_OPENHANDS_SESSION_ENDPOINT", "").strip()
        model = os.environ.get("OPTPILOT_OPENHANDS_MODEL", os.environ.get("LLM_MODEL", "")).strip()
        api_key = (
            os.environ.get("OPTPILOT_OPENHANDS_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        enabled = _env_flag("OPTPILOT_OPENHANDS_ENABLED", bool(base_url or model or api_key))
        return cls(base_url=base_url, session_endpoint=session_endpoint, model=model, api_key=api_key, enabled=enabled)

    @classmethod
    def from_mapping(cls, payload: JsonDict) -> "OpenHandsRuntimeConfig":
        return cls(
            base_url=str(payload.get("base_url") or "").strip().rstrip("/"),
            session_endpoint=str(payload.get("session_endpoint") or "").strip(),
            model=str(payload.get("model") or "").strip(),
            api_key=str(payload.get("api_key") or "").strip(),
            enabled=bool(payload.get("enabled")),
        )


class OpenHandsAdapter:
    """Small boundary between OptPilot Studio and an OpenHands runtime.

    The first UI implementation stores sessions, context packets, and messages
    in OptPilot. A real OpenHands connection can be plugged in here without
    changing the browser-side workflow.
    """

    def __init__(self, config: Optional[OpenHandsRuntimeConfig] = None) -> None:
        self.config = config or OpenHandsRuntimeConfig.from_env()
        self.system_prompt = load_assistant_system_prompt()

    def status(self) -> JsonDict:
        api_key_configured = bool(self.config.api_key)
        credentials_configured = bool(self.config.model and api_key_configured)
        server_configured = bool(self.config.base_url)
        runtime_configured = bool(self.config.enabled and credentials_configured and (server_configured or api_key_configured))
        if not self.config.enabled:
            mode = "disabled"
        elif not self.config.model:
            mode = "missing model"
        elif not api_key_configured:
            mode = "missing API key"
        elif not self.config.base_url:
            mode = "model chat"
        else:
            mode = "configured"
        if not self.config.enabled:
            dispatch = "queued"
        elif mode == "model chat":
            dispatch = "openrouter_chat"
        elif mode == "configured":
            dispatch = "openhands_http"
        else:
            dispatch = "queued"
        connected = self._server_reachable() if server_configured else False
        return {
            "runtime": "openhands",
            "enabled": self.config.enabled,
            "configured": runtime_configured,
            "credentials_configured": credentials_configured,
            "server_configured": server_configured,
            "connected": connected,
            "base_url": self.config.base_url,
            "session_endpoint": self.session_endpoint,
            "model": self.config.model,
            "api_key_configured": api_key_configured,
            "available_tools": OPTPILOT_AGENT_TOOLS,
            "mode": mode,
            "dispatch": dispatch,
        }

    def _server_reachable(self) -> bool:
        try:
            request = Request(self.config.base_url or "", method="GET")
            with urlopen(request, timeout=0.6) as response:
                return 200 <= response.status < 500
        except HTTPError as exc:
            return 200 <= exc.code < 500
        except (OSError, URLError, ValueError):
            return False

    @property
    def session_endpoint(self) -> str:
        return self.config.session_endpoint or DEFAULT_OPENHANDS_SESSION_ENDPOINT

    def dispatch_message(
        self,
        *,
        message: str,
        context: JsonDict,
        conversation_id: Optional[str] = None,
        tool_executor: Optional[ToolExecutor] = None,
        ignored_response_texts: Optional[set[str]] = None,
    ) -> JsonDict:
        status = self.status()
        if not status["configured"]:
            return self._queued_result(status)
        prompt = self._build_user_prompt(message, context)
        try:
            if self.config.base_url:
                endpoint = self.session_endpoint
                if "chat/completions" in endpoint or self.config.base_url.rstrip("/").endswith("/v1"):
                    return self._dispatch_chat_completion(prompt, context, conversation_id)
                return self._dispatch_openhands_agent_server(prompt, context, conversation_id, tool_executor, ignored_response_texts)
            return self._dispatch_openrouter_chat(prompt, context)
        except Exception as exc:
            return {
                "status": "failed",
                "mode": status.get("mode"),
                "dispatch": status.get("dispatch"),
                "conversation_id": conversation_id,
                "assistant_message": {
                    "role": "assistant",
                    "title": "OpenHands dispatch failed",
                    "content": f"OpenHands dispatch failed: {exc}",
                },
                "events": [
                    {
                        "type": "openhands_dispatch_failed",
                        "payload": {"error": str(exc), "dispatch": status.get("dispatch")},
                    }
                ],
            }

    def context_packet(
        self,
        *,
        session_id: str,
        selected_workspace: Optional[JsonDict],
        attached_workspaces: List[JsonDict],
        catalog_counts: JsonDict,
        run_count: int,
        current_page: str = "editor",
        registration_menu: Optional[JsonDict] = None,
        selected_catalog_entry: Optional[JsonDict] = None,
        selected_study_plan: Optional[JsonDict] = None,
        selected_run: Optional[JsonDict] = None,
        code_editor: Optional[JsonDict] = None,
        visible_state: Optional[JsonDict] = None,
    ) -> JsonDict:
        return {
            "session_id": session_id,
            "current_page": current_page,
            "selected_workspace": selected_workspace,
            "attached_workspaces": attached_workspaces,
            "catalog_counts": catalog_counts,
            "run_count": run_count,
            "registration_menu": registration_menu,
            "selected_catalog_entry": selected_catalog_entry,
            "selected_study_plan": selected_study_plan,
            "selected_run": selected_run,
            "code_editor": code_editor,
            "visible_state": visible_state or {},
            "available_tools": OPTPILOT_AGENT_TOOLS,
            "runtime": self.status(),
        }

    def _queued_result(self, status: JsonDict) -> JsonDict:
        reason = status.get("mode") or "not configured"
        return {
            "status": "queued",
            "mode": reason,
            "dispatch": status.get("dispatch", "queued"),
            "assistant_message": {
                "role": "assistant",
                "title": "Queued locally",
                "content": (
                    "This message was stored with the current OptPilot context, "
                    f"but the OpenHands runtime is {reason}."
                ),
            },
            "events": [{"type": "openhands_dispatch_queued", "payload": {"mode": reason}}],
        }

    def _dispatch_openrouter_chat(self, prompt: str, context: JsonDict) -> JsonDict:
        payload = {
            "model": self._openrouter_model(),
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        data, _headers = self._request_json(
            "POST",
            OPENROUTER_CHAT_COMPLETIONS_URL,
            payload=payload,
            bearer_token=self.config.api_key,
            extra_headers={
                "HTTP-Referer": "http://127.0.0.1/optpilot-studio",
                "X-Title": "OptPilot Studio",
            },
        )
        text = self._chat_completion_text(data)
        return {
            "status": "answered",
            "mode": "model chat",
            "dispatch": "openrouter_chat",
            "assistant_message": {
                "role": "assistant",
                "title": "Assistant",
                "content": text or "The model returned an empty response.",
            },
            "events": [{"type": "openhands_model_chat_completed", "payload": {"model": self.config.model}}],
        }

    def _dispatch_chat_completion(self, prompt: str, context: JsonDict, conversation_id: Optional[str]) -> JsonDict:
        endpoint = self.session_endpoint
        url = self._join_url(self.config.base_url, endpoint)
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        headers: JsonDict = {}
        if conversation_id:
            headers["X-OpenHands-ServerConversation-ID"] = conversation_id
        data, response_headers = self._request_json(
            "POST",
            url,
            payload=payload,
            bearer_token=self.config.api_key,
            extra_headers=headers,
        )
        next_conversation_id = (
            response_headers.get("X-OpenHands-ServerConversation-ID")
            or response_headers.get("x-openhands-serverconversation-id")
            or conversation_id
        )
        return {
            "status": "answered",
            "mode": "openhands chat completions",
            "dispatch": "openhands_http",
            "conversation_id": next_conversation_id,
            "assistant_message": {
                "role": "assistant",
                "title": "OpenHands",
                "content": self._chat_completion_text(data) or "OpenHands returned an empty response.",
            },
            "events": [{"type": "openhands_chat_completion_completed", "payload": {"conversation_id": next_conversation_id}}],
        }

    def _dispatch_openhands_agent_server(
        self,
        prompt: str,
        context: JsonDict,
        conversation_id: Optional[str],
        tool_executor: Optional[ToolExecutor],
        ignored_response_texts: Optional[set[str]],
    ) -> JsonDict:
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        next_conversation_id = conversation_id
        created = False
        if not next_conversation_id:
            payload = self._start_conversation_payload(context)
            data, _headers = self._request_json("POST", conversations_url, payload=payload)
            next_conversation_id = str(data.get("id") or data.get("conversation_id") or "")
            if not next_conversation_id:
                raise RuntimeError("OpenHands did not return a conversation id.")
            created = True
        existing_events = [] if created else self._existing_openhands_events(conversations_url, next_conversation_id)
        ignored_event_ids = {
            event_id
            for event_id in (self._openhands_event_id(event) for event in existing_events)
            if event_id
        }
        ignored_tool_calls = {
            call_id
            for event in existing_events
            for _name, _arguments, call_id in [self._openhands_tool_call(event)]
            if call_id
        }
        ignored_texts = {
            self._normalize_response_text(text)
            for text in (ignored_response_texts or set())
            if self._normalize_response_text(text)
        }
        ignored_texts.update(
            self._normalize_response_text(self._event_assistant_text(event))
            for event in existing_events
        )
        ignored_texts.discard("")
        send_payload = {
            "role": "user",
            "content": [{"type": "text", "text": prompt, "cache_prompt": False}],
            "run": True,
        }
        self._request_json("POST", f"{conversations_url}/{next_conversation_id}/events", payload=send_payload)
        answer, tool_events = self._poll_openhands_answer(
            conversations_url,
            next_conversation_id,
            tool_executor=tool_executor,
            ignored_tool_calls=ignored_tool_calls,
            ignored_event_ids=ignored_event_ids,
            ignored_response_texts=ignored_texts,
            allow_final_response_fallback=created,
            poll_seconds=3.0,
        )
        return {
            "status": "answered" if answer else "running",
            "mode": "openhands agent server",
            "dispatch": "openhands_http",
            "conversation_id": next_conversation_id,
            "assistant_message": {
                "role": "assistant",
                "title": "OpenHands",
                "content": answer,
            },
            "events": [
                *tool_events,
                {
                    "type": "openhands_dispatch_completed" if answer else "openhands_dispatch_started",
                    "payload": {"conversation_id": next_conversation_id, "created": created},
                }
            ],
            "sync_state": {
                "ignored_event_ids": sorted(ignored_event_ids),
                "ignored_tool_call_ids": sorted(ignored_tool_calls),
                "ignored_response_texts": sorted(ignored_texts),
                "allow_final_response_fallback": created,
            },
        }

    def sync_conversation(
        self,
        conversation_id: str,
        *,
        tool_executor: Optional[ToolExecutor] = None,
        ignored_tool_calls: Optional[set[str]] = None,
        ignored_event_ids: Optional[set[str]] = None,
        ignored_response_texts: Optional[set[str]] = None,
        allow_final_response_fallback: bool = False,
        poll_seconds: float = 3.0,
    ) -> JsonDict:
        status = self.status()
        if not conversation_id or status.get("dispatch") != "openhands_http":
            return {"status": "unavailable", "events": []}
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        answer, tool_events = self._poll_openhands_answer(
            conversations_url,
            conversation_id,
            tool_executor=tool_executor,
            ignored_tool_calls=ignored_tool_calls,
            ignored_event_ids=ignored_event_ids,
            ignored_response_texts=ignored_response_texts,
            allow_final_response_fallback=allow_final_response_fallback,
            poll_seconds=poll_seconds,
        )
        return {
            "status": "answered" if answer else "running",
            "conversation_id": conversation_id,
            "assistant_message": {"role": "assistant", "title": "OpenHands", "content": answer},
            "events": tool_events,
            "sync_state": {
                "ignored_event_ids": sorted(ignored_event_ids or []),
                "ignored_response_texts": sorted(ignored_response_texts or []),
                "allow_final_response_fallback": allow_final_response_fallback,
            },
        }

    def _start_conversation_payload(self, context: JsonDict) -> JsonDict:
        workspace = context.get("selected_workspace") if isinstance(context.get("selected_workspace"), dict) else None
        working_dir = str((workspace or {}).get("root") or ".")
        return {
            "agent": {
                "kind": "Agent",
                "llm": self._openhands_llm_payload(),
                "tools": [],
                "agent_context": {"system_message_suffix": self.system_prompt},
            },
            "client_tools": self._client_tool_specs(),
            "workspace": {"kind": "LocalWorkspace", "working_dir": working_dir},
            "confirmation_policy": {"kind": "AlwaysConfirm"},
            "initial_message": None,
            "max_iterations": 20,
            "stuck_detection": True,
        }

    def _openhands_llm_payload(self) -> JsonDict:
        model = self.config.model
        if self.config.api_key and "/" in model and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        return {
            "model": model,
            "api_key": self.config.api_key,
            "openrouter_site_url": "http://127.0.0.1/optpilot-studio",
            "openrouter_app_name": "OptPilot Studio",
        }

    def _client_tool_specs(self) -> List[JsonDict]:
        return OPTPILOT_AGENT_TOOL_SPECS

    def _poll_openhands_answer(
        self,
        conversations_url: str,
        conversation_id: str,
        *,
        tool_executor: Optional[ToolExecutor],
        ignored_tool_calls: Optional[set[str]] = None,
        ignored_event_ids: Optional[set[str]] = None,
        ignored_response_texts: Optional[set[str]] = None,
        allow_final_response_fallback: bool = True,
        poll_seconds: float = 75.0,
    ) -> tuple[str, List[JsonDict]]:
        search_url = f"{conversations_url}/{conversation_id}/events/search?limit=50&sort_order=TIMESTAMP_DESC"
        final_response_url = f"{conversations_url}/{conversation_id}/agent_final_response"
        deadline = time.monotonic() + max(float(poll_seconds), 0.1)
        handled_tool_calls: set[str] = set(ignored_tool_calls or set())
        ignored_events: set[str] = set(ignored_event_ids or set())
        ignored_texts: set[str] = {
            self._normalize_response_text(text)
            for text in (ignored_response_texts or set())
            if self._normalize_response_text(text)
        }
        tool_events: List[JsonDict] = []
        seen_openhands_events: set[str] = set()
        while time.monotonic() < deadline:
            try:
                data, _headers = self._request_json("GET", search_url, payload=None, timeout=15.0)
            except Exception:
                data = {}
            events = data.get("items", []) if isinstance(data, dict) else []
            tool_events.extend(self._trace_openhands_events(events, seen_openhands_events))
            if tool_executor:
                new_tool_events = self._execute_openhands_client_tools(
                    events,
                    conversations_url,
                    conversation_id,
                    tool_executor,
                    handled_tool_calls,
                )
                tool_events.extend(new_tool_events)
                if new_tool_events:
                    continue
            for event in events:
                event_id = self._openhands_event_id(event)
                if event_id and event_id in ignored_events:
                    continue
                text = self._event_assistant_text(event)
                if text and self._normalize_response_text(text) not in ignored_texts:
                    return text, tool_events
            if allow_final_response_fallback:
                try:
                    data, _headers = self._request_json("GET", final_response_url, payload=None, timeout=15.0)
                    text = str(data.get("response") or data.get("content") or data.get("text") or "").strip()
                    if text and self._normalize_response_text(text) not in ignored_texts:
                        return text, tool_events
                except Exception:
                    pass
            time.sleep(2.0)
        return "", tool_events

    def _trace_openhands_events(self, events: Any, seen_event_ids: set[str]) -> List[JsonDict]:
        traced: List[JsonDict] = []
        source_events = events if isinstance(events, list) else []
        for event in reversed(source_events):
            trace = self._openhands_event_trace(event)
            trace_id = str(trace.get("id") or "")
            if not trace_id or trace_id in seen_event_ids:
                continue
            seen_event_ids.add(trace_id)
            traced.append(trace)
        return traced

    def _openhands_event_trace(self, event: Any) -> JsonDict:
        if not isinstance(event, dict):
            fingerprint = hashlib.sha1(str(event).encode("utf-8", errors="replace")).hexdigest()[:16]
            return {
                "id": f"openhands-event-{fingerprint}",
                "type": "openhands_event",
                "payload": {"source": "openhands", "summary": str(event), "raw_preview": str(event)},
            }
        raw_id = str(event.get("id") or event.get("event_id") or event.get("uuid") or "")
        fingerprint = self._openhands_event_id(event)
        event_kind = str(event.get("kind") or event.get("type") or event.get("event_type") or event.get("source") or "event")
        tool_name, arguments, call_id = self._openhands_tool_call(event)
        reasoning = self._event_reasoning_text(event)
        user_text = self._event_user_text(event)
        assistant_text = self._event_assistant_text(event)
        payload: JsonDict = {
            "source": "openhands",
            "event_id": raw_id or fingerprint,
            "event_type": event_kind,
            "summary": self._compact_openhands_event_summary(event, tool_name=tool_name, call_id=call_id),
            "raw_preview": self._event_payload_preview(event),
            "category": self._openhands_event_category(
                event,
                tool_name=tool_name,
                reasoning=reasoning,
                user_text=user_text,
                assistant_text=assistant_text,
            ),
        }
        if reasoning:
            payload["reasoning"] = self._compact_text(reasoning, 1200)
        if assistant_text:
            payload["assistant_preview"] = self._compact_text(assistant_text, 900)
        if tool_name:
            payload["tool"] = tool_name
            payload["arguments_preview"] = self._json_preview(arguments, 1200)
        if call_id:
            payload["tool_call_id"] = call_id
        return {
            "id": f"openhands-event-{raw_id or fingerprint}",
            "type": "openhands_event",
            "payload": payload,
        }

    def _compact_openhands_event_summary(self, event: JsonDict, *, tool_name: str = "", call_id: str = "") -> str:
        user_text = self._event_user_text(event)
        if user_text:
            return self._compact_user_event_summary(user_text)
        reasoning = self._event_reasoning_text(event)
        if reasoning:
            return self._compact_text(reasoning, 600)
        text = self._event_assistant_text(event)
        if text:
            return self._compact_text(text, 600)
        if tool_name:
            return f"Tool call requested: {tool_name}" + (f" ({call_id})" if call_id else "")
        for key in ("message", "content", "thought", "text", "error"):
            value = event.get(key)
            text = self._content_text(value).strip()
            if text:
                return self._compact_text(text, 300)
        action = event.get("action")
        if isinstance(action, dict):
            keys = [str(key) for key in action.keys() if key != "security_risk"]
            if keys:
                return f"Action fields: {', '.join(keys[:8])}"
        return str(event.get("kind") or event.get("type") or "OpenHands event")

    def _openhands_event_category(
        self,
        event: JsonDict,
        *,
        tool_name: str,
        reasoning: str,
        user_text: str,
        assistant_text: str,
    ) -> str:
        if tool_name:
            return "tool_call"
        if reasoning:
            return "reasoning"
        if user_text:
            if user_text.startswith("OptPilot tool result for "):
                return "tool_result_feedback"
            return "user_message"
        if assistant_text:
            return "assistant_message"
        if str(event.get("error") or ""):
            return "error"
        return "status"

    def _event_payload_preview(self, event: JsonDict) -> str:
        redacted = self._redact_trace_payload(event)
        return self._json_preview(redacted, 1600)

    def _existing_openhands_events(self, conversations_url: str, conversation_id: str) -> List[JsonDict]:
        search_url = f"{conversations_url}/{conversation_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC"
        try:
            data, _headers = self._request_json("GET", search_url, payload=None, timeout=15.0)
        except Exception:
            return []
        events = data.get("items", []) if isinstance(data, dict) else []
        return [event for event in events if isinstance(event, dict)]

    def _existing_tool_call_ids(self, conversations_url: str, conversation_id: str) -> set[str]:
        events = self._existing_openhands_events(conversations_url, conversation_id)
        return {
            call_id
            for event in events
            for _name, _arguments, call_id in [self._openhands_tool_call(event)]
            if call_id
        }

    def _openhands_event_id(self, event: Any) -> str:
        if isinstance(event, dict):
            raw_id = str(event.get("id") or event.get("event_id") or event.get("uuid") or "")
            if raw_id:
                return raw_id
            payload = json.dumps(event, sort_keys=True, default=str)
        else:
            payload = str(event)
        return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _normalize_response_text(self, text: str) -> str:
        return " ".join(str(text or "").strip().split())

    def _execute_openhands_client_tools(
        self,
        events: Any,
        conversations_url: str,
        conversation_id: str,
        tool_executor: ToolExecutor,
        handled_tool_calls: set[str],
    ) -> List[JsonDict]:
        tool_events: List[JsonDict] = []
        for event in events if isinstance(events, list) else []:
            name, arguments, call_id = self._openhands_tool_call(event)
            if not name or name not in OPTPILOT_AGENT_TOOLS or not call_id or call_id in handled_tool_calls:
                continue
            handled_tool_calls.add(call_id)
            try:
                result = tool_executor(name, arguments)
            except Exception as exc:
                result = {
                    "ok": False,
                    "tool": name,
                    "summary": str(exc),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            result = self._redact_tool_result(result)
            result_preview = self._json_preview(result, 2400)
            tool_events.append(
                {
                    "type": "optpilot_tool_result",
                    "payload": {
                        "tool": name,
                        "tool_call_id": call_id,
                        "ok": bool(result.get("ok")),
                        "summary": str(result.get("summary") or ""),
                        "result_preview": result_preview,
                    },
                }
            )
            self._send_tool_result_message(conversations_url, conversation_id, name, call_id, result)
        return tool_events

    def _send_tool_result_message(self, conversations_url: str, conversation_id: str, name: str, call_id: str, result: JsonDict) -> None:
        result_json = json.dumps(result, indent=2, sort_keys=True, default=str)
        if len(result_json) > 18000:
            result_json = result_json[:18000] + "\n... truncated ..."
        payload = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "cache_prompt": False,
                    "text": (
                        f"OptPilot tool result for {name} ({call_id}). "
                        "Use this structured result to continue the task. Do not call the same tool again unless fresh data is needed.\n"
                        f"```json\n{result_json}\n```"
                    ),
                }
            ],
            "run": True,
        }
        self._request_json("POST", f"{conversations_url}/{conversation_id}/events", payload=payload, timeout=15.0)

    def _openhands_tool_call(self, event: Any) -> tuple[str, JsonDict, str]:
        if not isinstance(event, dict):
            return "", {}, ""
        kind = str(event.get("kind") or "")
        if kind and "ActionEvent" not in kind:
            return "", {}, ""
        name = str(event.get("tool_name") or "")
        call_id = str(event.get("tool_call_id") or "")
        tool_call = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        name = name or str(function.get("name") or tool_call.get("name") or "")
        call_id = call_id or str(tool_call.get("id") or "")
        arguments = self._tool_arguments_from_action(event.get("action"))
        if not arguments:
            raw_arguments = function.get("arguments") or tool_call.get("arguments")
            arguments = self._decode_tool_arguments(raw_arguments)
        return name, arguments, call_id

    def _tool_arguments_from_action(self, action: Any) -> JsonDict:
        if not isinstance(action, dict):
            return {}
        ignored = {"kind", "security_risk"}
        return {str(key): value for key, value in action.items() if key not in ignored}

    def _decode_tool_arguments(self, raw_arguments: Any) -> JsonDict:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _redact_tool_result(self, result: JsonDict) -> JsonDict:
        redacted = json.loads(json.dumps(result, default=str))
        secret_tokens = [self.config.api_key] if self.config.api_key else []

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                text = value
                for token in secret_tokens:
                    if token:
                        text = text.replace(token, "[redacted]")
                return text
            if isinstance(value, dict):
                return {key: scrub(child) for key, child in value.items()}
            if isinstance(value, list):
                return [scrub(child) for child in value]
            return value

        return scrub(redacted)

    def _redact_trace_payload(self, payload: JsonDict) -> JsonDict:
        redacted = self._redact_tool_result(payload)

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                return self._redact_internal_prompt_text(value)
            if isinstance(value, dict):
                return {key: scrub(child) for key, child in value.items()}
            if isinstance(value, list):
                return [scrub(child) for child in value]
            return value

        return scrub(redacted)

    def _build_user_prompt(self, message: str, context: JsonDict) -> str:
        compact_context = json.dumps(context, indent=2, sort_keys=True, default=str)
        if len(compact_context) > 24000:
            compact_context = compact_context[:24000] + "\n... truncated ..."
        return (
            f"User request:\n{message}\n\n"
            "Visible OptPilot Studio context packet:\n"
            f"{compact_context}"
        )

    def _compact_user_event_summary(self, text: str) -> str:
        text = str(text or "").strip()
        if text.startswith("User request:"):
            request = text[len("User request:"):].split("Visible OptPilot Studio context packet:", 1)[0].strip()
            request = " ".join(request.split())
            if request:
                return f"User request sent to OpenHands: {request[:220]}"
            return "User request and Studio context sent to OpenHands."
        if text.startswith("OptPilot tool result for "):
            return text.splitlines()[0][:300]
        return self._redact_internal_prompt_text(text)[:300]

    def _redact_internal_prompt_text(self, text: str) -> str:
        marker = "Visible OptPilot Studio context packet:"
        if marker not in text:
            return text
        prefix = text.split(marker, 1)[0].rstrip()
        return f"{prefix}\n\n[Studio context packet redacted from step preview]"

    def _compact_text(self, text: str, limit: int) -> str:
        compact = " ".join(self._redact_internal_prompt_text(str(text or "")).split())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."

    def _json_preview(self, payload: Any, limit: int) -> str:
        preview = json.dumps(payload, indent=2, sort_keys=True, default=str)
        return preview if len(preview) <= limit else preview[:limit].rstrip() + "\n... truncated ..."

    def _openrouter_model(self) -> str:
        model = self.config.model.strip()
        return model.removeprefix("openrouter/")

    def _chat_completion_text(self, payload: JsonDict) -> str:
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            return ""
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content")
        return self._content_text(content)

    def _event_assistant_text(self, event: JsonDict) -> str:
        if not isinstance(event, dict):
            return ""
        candidates = [event, event.get("message"), event.get("llm_message"), event.get("payload")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            role = str(candidate.get("role") or "").lower()
            source = str(candidate.get("source") or event.get("source") or "").lower()
            if role and role not in {"assistant", "agent"}:
                continue
            if source and source not in {"agent", "assistant"}:
                continue
            if role not in {"assistant", "agent"} and source not in {"agent", "assistant"}:
                continue
            text = self._content_text(candidate.get("content") or candidate.get("text") or candidate.get("message"))
            if text:
                return text
        return ""

    def _event_user_text(self, event: JsonDict) -> str:
        if not isinstance(event, dict):
            return ""
        candidates = [event, event.get("message"), event.get("llm_message"), event.get("payload")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("role") or "").lower() != "user":
                continue
            text = self._content_text(candidate.get("content") or candidate.get("text") or candidate.get("message"))
            if text:
                return text
        return ""

    def _event_reasoning_text(self, event: JsonDict) -> str:
        if not isinstance(event, dict):
            return ""
        candidates = [event, event.get("message"), event.get("llm_message"), event.get("payload")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("reasoning_content", "reasoning", "thought", "thinking", "analysis"):
                text = self._content_text(candidate.get(key))
                if text:
                    return text
            blocks = candidate.get("thinking_blocks")
            if isinstance(blocks, list):
                parts = [self._content_text(block) for block in blocks]
                text = "\n".join(part for part in parts if part).strip()
                if text:
                    return text
        return ""

    def _content_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            if "text" in content:
                return self._content_text(content.get("text"))
            if "content" in content:
                return self._content_text(content.get("content"))
            if "message" in content:
                return self._content_text(content.get("message"))
            return ""
        if isinstance(content, list):
            parts = [self._content_text(item) for item in content]
            return "\n".join(part for part in parts if part).strip()
        return str(content).strip()

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[JsonDict],
        bearer_token: str = "",
        extra_headers: Optional[JsonDict] = None,
        timeout: float = 60.0,
    ) -> tuple[JsonDict, JsonDict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items() if value})
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                return data if isinstance(data, dict) else {"data": data}, dict(response.headers.items())
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = raw[:500] if raw else exc.reason
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    def _join_url(self, base_url: str, path: str) -> str:
        if not path:
            return base_url.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_assistant_system_prompt() -> str:
    override = os.environ.get("OPTPILOT_ASSISTANT_SYSTEM_PROMPT")
    if override:
        return override
    for path in _assistant_prompt_candidates():
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT


def _assistant_prompt_candidates() -> List[Path]:
    names = (".agents/optpilot-assistant/prompts/system.md",)
    candidates = []
    cwd = Path.cwd()
    for name in names:
        candidates.append(cwd / name)
    source_root = Path(__file__).resolve().parents[2]
    for name in names:
        candidates.append(source_root / name)
    return candidates
