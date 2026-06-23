from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


OPTPILOT_AGENT_TOOLS = [
    "optpilot_workspace_list",
    "optpilot_workspace_create",
    "optpilot_workspace_focus",
    "optpilot_catalog_list",
    "optpilot_compatibility_check",
    "optpilot_addon_list",
    "optpilot_addon_attach",
    "optpilot_addon_open_workspace",
    "optpilot_config_discover",
    "optpilot_config_validate",
    "optpilot_registration_prepare",
    "optpilot_registration_apply",
    "optpilot_study_draft",
    "optpilot_study_launch",
    "optpilot_run_list",
    "optpilot_run_detail",
    "optpilot_run_open_workspace",
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

    def status(self) -> JsonDict:
        api_key_configured = bool(self.config.api_key)
        credentials_configured = bool(self.config.model and api_key_configured)
        runtime_configured = bool(self.config.enabled and self.config.base_url and credentials_configured)
        if not self.config.enabled:
            mode = "disabled"
        elif not self.config.base_url:
            mode = "missing server"
        elif not self.config.model:
            mode = "missing model"
        elif not api_key_configured:
            mode = "missing API key"
        else:
            mode = "configured"
        return {
            "runtime": "openhands",
            "enabled": self.config.enabled,
            "configured": runtime_configured,
            "credentials_configured": credentials_configured,
            "connected": False,
            "base_url": self.config.base_url,
            "session_endpoint": self.config.session_endpoint,
            "model": self.config.model,
            "api_key_configured": api_key_configured,
            "available_tools": OPTPILOT_AGENT_TOOLS,
            "mode": mode,
            "dispatch": "queued",
        }

    def context_packet(
        self,
        *,
        session_id: str,
        selected_workspace: Optional[JsonDict],
        attached_workspaces: List[JsonDict],
        catalog_counts: JsonDict,
        run_count: int,
        current_page: str = "workspace",
        enabled_addons: Optional[List[JsonDict]] = None,
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
            "enabled_addons": enabled_addons or [],
            "registration_menu": registration_menu,
            "selected_catalog_entry": selected_catalog_entry,
            "selected_study_plan": selected_study_plan,
            "selected_run": selected_run,
            "code_editor": code_editor,
            "visible_state": visible_state or {},
            "available_tools": OPTPILOT_AGENT_TOOLS,
            "runtime": self.status(),
        }


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
