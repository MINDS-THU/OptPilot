from __future__ import annotations

import json
import os
import hashlib
import shlex
import sys
from datetime import datetime
import math
import time
from dataclasses import dataclass

from optpilot_studio.stop_gate import decide as stop_gate_decide
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


JsonDict = Dict[str, Any]
ToolExecutor = Callable[[str, JsonDict], JsonDict]


class OpenHandsConversationNotFound(RuntimeError):
    """The OpenHands process no longer knows a retained conversation id."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = str(conversation_id or "")
        super().__init__(
            f"OpenHands conversation {self.conversation_id or '<unknown>'} was not found."
        )

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENHANDS_SESSION_ENDPOINT = "/api/conversations"
DEFAULT_OPENHANDS_NATIVE_TOOLS = ("grep", "glob", "task_tracker")
ALLOWED_OPENHANDS_NATIVE_TOOLS = frozenset(DEFAULT_OPENHANDS_NATIVE_TOOLS)
OPENHANDS_COMPAT_AGENT_TOOLS = ("optpilot_terminal", "optpilot_file_editor")
FALLBACK_OPTPILOT_ASSISTANT_SYSTEM_PROMPT = """You are OptPilot Assistant inside OptPilot Studio.
Answer using the OptPilot context packet provided by the GUI. Keep public
OptPilot explanations centered on environment-owned evaluator.settings and
method-visible methodContext.references. On the Runs page, use
optpilot_run_detail for status, metrics, failures, candidates, and evidence
instead of reading raw run files or creating a Workspace for a recorded Run.
Catalog list and search results are evidence: inspect a small relevant
shortlist, and emit UI cards only for objects the user is likely to act on.
For file and command work, use Studio-backed workspace tools in the context
packet's selected Workspace, which is normally the Conversation's default
Workspace, or in another attached Workspace the user explicitly names.
Changing the default Workspace may recreate the runtime with bounded recent
Conversation context; continue the same goal, but do not assume ephemeral
process or terminal state survived.
Do not claim you modified files, launched studies, or registered catalog
entries unless the runtime confirms it. For frontend services, start
them in the attached workspace runtime on 0.0.0.0 and use
optpilot_workspace_preview_open with the service port to open Studio Preview.
When available, use optpilot_conversation_title after the first substantive
request to give the Conversation a short, specific title. Use it again only when the primary goal
changes materially; do not rename greetings, thanks, confirmations, or minor
follow-ups, and do not mention title updates in your reply."""


OPTPILOT_AGENT_TOOLS = [
    "optpilot_conversation_title",
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
    "optpilot_terminal",
    "optpilot_file_editor",
    "optpilot_workspace_preview_open",
    "optpilot_catalog_list",
    "optpilot_catalog_detail",
    "optpilot_resource_action_list",
    "optpilot_resource_action_run",
    "optpilot_resource_action_status",
    "optpilot_compatibility_check",
    "optpilot_config_discover",
    "optpilot_config_validate",
    "optpilot_catalog_setup",
    "optpilot_package_plan_prepare",
    "optpilot_package_plan_update",
    "optpilot_package_plan_validate",
    "optpilot_package_plan_smoke",
    "optpilot_package_plan_apply",
    "optpilot_study_draft",
    "optpilot_study_save",
    "optpilot_study_launch",
    "optpilot_run_list",
    "optpilot_run_detail",
    "optpilot_job_stop",
    "optpilot_run_compare",
    "optpilot_smoke_test_study",
    "optpilot_interface_launch",
    "optpilot_interface_status",
    "optpilot_docs_search",
    "optpilot_capability_list",
    "optpilot_capability_detail",
]
SUPPORTED_CLIENT_TOOL_NAMES = {*OPTPILOT_AGENT_TOOLS, *OPENHANDS_COMPAT_AGENT_TOOLS}

STUDIO_UI_CARD_SCHEMA = "optpilot.studio-ui-card.v1"
STUDIO_UI_CARD_KINDS = frozenset({"catalog-use", "interface", "run-setup", "run"})
STUDIO_UI_CARD_OPERATIONS = frozenset(
    {
        "configure-run",
        "open-catalog",
        "open-interface",
        "open-launch",
        "open-run",
        "open-workspace",
        "start-run",
    }
)
STUDIO_UI_CARD_MAX_COUNT = 12
STUDIO_UI_CARD_MAX_BYTES = 16 * 1024
STUDIO_UI_CARDS_MAX_BYTES = 64 * 1024


def explain_runtime_error(raw: Any) -> tuple[str, str]:
    """Return an actionable user-facing explanation and the original detail."""

    text = str(raw or "").strip()
    if not text:
        return ("The Assistant stopped without saying why.", "")
    lowered = text.lower()
    provider = ""
    for marker in ('"provider_name":"', "'provider_name': '"):
        if marker in text:
            rest = text.split(marker, 1)[1]
            provider = rest.split('"', 1)[0].split("'", 1)[0]
            break
    if "authentication" in lowered or "invalid api key" in lowered or "401" in lowered:
        service = "OpenRouter" if "openrouter" in lowered else "The model provider"
        return (
            f"{service} rejected the API key (401). Check or replace the key "
            "in Settings, then send the message again. Switching models will "
            "not fix an invalid key.",
            text,
        )
    if "rate limit" in lowered or "429" in lowered:
        return (
            "The model provider is rate-limiting this key. Wait a moment "
            "and send the message again, or use a different key.",
            text,
        )
    if "openrouterexception" in lowered or "provider returned error" in lowered:
        named = f" ({provider})" if provider else ""
        return (
            f"The model you chose{named} returned an error to OptPilot. "
            "That happened at the model provider, not in OptPilot or in "
            "anything you configured here, and it is usually temporary. "
            "Send the message again; if it keeps happening, choose a "
            "different model in Settings.",
            text,
        )
    return (f"The Assistant stopped with an error: {text}", text)


def _studio_ui_card_text(
    value: Any,
    *,
    maximum: int,
    required: bool = False,
    normalize: bool = False,
) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()) if normalize else value.strip()
    if (required and not text) or len(text.encode("utf-8")) > maximum:
        return None
    if any(ord(char) < 32 for char in text):
        return None
    return text


def _studio_ui_card_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _studio_ui_card_portable_path(value: Any) -> Optional[str]:
    text = _studio_ui_card_text(value, maximum=2048, required=True)
    if text is None or "\\" in text or text.startswith("/"):
        return None
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return text


def _sanitize_studio_ui_card_coordinate(value: Any) -> Optional[JsonDict]:
    if not isinstance(value, dict):
        return None
    kind = _studio_ui_card_text(
        value.get("kind"), maximum=40, required=True
    )
    opaque = lambda item: _studio_ui_card_text(  # noqa: E731
        item, maximum=12_000, required=True
    )
    if kind == "catalog-entry":
        config_kind = _studio_ui_card_text(
            value.get("config_kind"), maximum=32, required=True
        )
        uid = opaque(value.get("uid"))
        if config_kind not in {"environment", "method", "resource", "study"} or uid is None:
            return None
        return {"kind": kind, "config_kind": config_kind, "uid": uid}
    if kind == "workspace":
        workspace_id = opaque(value.get("workspace_id"))
        if workspace_id is None:
            return None
        return {"kind": kind, "workspace_id": workspace_id}
    if kind == "study-workspace":
        workspace_id = opaque(value.get("workspace_id"))
        workspace_revision = _studio_ui_card_positive_int(
            value.get("workspace_revision")
        )
        study_relative_path = _studio_ui_card_portable_path(
            value.get("study_relative_path")
        )
        if (
            workspace_id is None
            or workspace_revision is None
            or study_relative_path is None
        ):
            return None
        result: JsonDict = {
            "kind": kind,
            "workspace_id": workspace_id,
            "workspace_revision": workspace_revision,
            "study_relative_path": study_relative_path,
        }
        for key in ("environment_uid", "method_uid"):
            item = opaque(value.get(key)) if value.get(key) else None
            if item is not None:
                result[key] = item
        draft_id = opaque(value.get("draft_id")) if value.get("draft_id") else None
        if draft_id is not None:
            result["draft_id"] = draft_id
        draft_revision = (
            _studio_ui_card_positive_int(value.get("draft_revision"))
            if value.get("draft_revision") is not None
            else None
        )
        if draft_revision is not None:
            result["draft_revision"] = draft_revision
        return result
    if kind == "study-launch":
        launch_id = opaque(value.get("launch_id"))
        if launch_id is None:
            return None
        result = {"kind": kind, "launch_id": launch_id}
        run_id = opaque(value.get("run_id")) if value.get("run_id") else None
        if run_id is not None:
            result["run_id"] = run_id
        return result
    if kind == "interface-launch":
        launch_id = opaque(value.get("launch_id"))
        config_kind = _studio_ui_card_text(
            value.get("config_kind"), maximum=32, required=True
        )
        uid = opaque(value.get("uid"))
        if (
            launch_id is None
            or config_kind not in {"environment", "method", "resource"}
            or uid is None
        ):
            return None
        return {
            "kind": kind,
            "launch_id": launch_id,
            "config_kind": config_kind,
            "uid": uid,
        }
    if kind == "run":
        run_id = opaque(value.get("run_id"))
        if run_id is None:
            return None
        return {"kind": kind, "run_id": run_id}
    return None


def _sanitize_studio_ui_card_facts(value: Any) -> List[JsonDict]:
    result: List[JsonDict] = []
    for raw in value[:8] if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        label = _studio_ui_card_text(
            raw.get("label"), maximum=120, required=True, normalize=True
        )
        fact_value = raw.get("value")
        if label is None or not isinstance(
            fact_value, (str, int, float, bool, type(None))
        ):
            continue
        if isinstance(fact_value, str):
            fact_value = _studio_ui_card_text(
                fact_value, maximum=1000, normalize=True
            )
            if fact_value is None:
                continue
        if isinstance(fact_value, float) and not math.isfinite(fact_value):
            continue
        result.append({"label": label, "value": fact_value})
    return result


def _sanitize_studio_ui_card_actions(value: Any) -> List[JsonDict]:
    result: List[JsonDict] = []
    seen: set[str] = set()
    for raw in value[:6] if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        operation = _studio_ui_card_text(
            raw.get("operation"), maximum=64, required=True
        )
        action_id = _studio_ui_card_text(
            raw.get("id"), maximum=160, required=True
        )
        label = _studio_ui_card_text(
            raw.get("label"), maximum=120, required=True, normalize=True
        )
        eligible = raw.get("eligible")
        approval_required = raw.get("approval_required", False)
        if (
            operation not in STUDIO_UI_CARD_OPERATIONS
            or action_id is None
            or action_id in seen
            or label is None
            or not isinstance(eligible, bool)
            or not isinstance(approval_required, bool)
        ):
            continue
        reason = _studio_ui_card_text(
            raw.get("reason", ""), maximum=600, normalize=True
        )
        if reason is None:
            continue
        seen.add(action_id)
        result.append(
            {
                "id": action_id,
                "label": label,
                "operation": operation,
                "eligible": eligible,
                "reason": reason,
                "approval_required": (
                    True if operation == "start-run" else approval_required
                ),
            }
        )
    return result


def _studio_ui_card_coordinate_operations(coordinate: JsonDict) -> frozenset[str]:
    kind = coordinate.get("kind")
    if kind == "catalog-entry":
        common = {"open-catalog", "open-interface"}
        if coordinate.get("config_kind") == "study":
            common.update({"configure-run", "start-run"})
        return frozenset(common)
    if kind == "study-workspace":
        return frozenset({"configure-run", "open-workspace", "start-run"})
    if kind == "workspace":
        return frozenset({"open-workspace", "start-run"})
    if kind == "study-launch":
        return frozenset({"open-launch", "open-run"})
    if kind == "interface-launch":
        return frozenset({"open-interface"})
    if kind == "run":
        return frozenset({"open-run"})
    return frozenset()


def sanitize_studio_ui_cards(raw_cards: Any) -> List[JsonDict]:
    """Return the bounded, non-executable card projection safe for Studio UI.

    Cards contain opaque coordinates and allowlisted operation names only. The
    browser must re-read the referenced object's current capability before an
    operation is offered or executed; cards never carry URLs or request bodies.
    """

    if not isinstance(raw_cards, list):
        return []
    cards: List[JsonDict] = []
    total_bytes = 2
    for raw in raw_cards[:STUDIO_UI_CARD_MAX_COUNT]:
        if not isinstance(raw, dict) or raw.get("schema") != STUDIO_UI_CARD_SCHEMA:
            continue
        card_id = _studio_ui_card_text(
            raw.get("id"), maximum=160, required=True
        )
        kind = _studio_ui_card_text(
            raw.get("kind"), maximum=40, required=True
        )
        coordinate = _sanitize_studio_ui_card_coordinate(raw.get("coordinate"))
        title = _studio_ui_card_text(
            raw.get("title"), maximum=300, required=True, normalize=True
        )
        if (
            card_id is None
            or kind not in STUDIO_UI_CARD_KINDS
            or coordinate is None
            or title is None
        ):
            continue
        coordinate_kind = coordinate.get("kind")
        if (
            (
                kind == "catalog-use"
                and (
                    coordinate_kind != "catalog-entry"
                    or coordinate.get("config_kind") == "study"
                )
            )
            or (
                kind == "run-setup"
                and (
                    coordinate_kind
                    not in {"catalog-entry", "study-workspace", "workspace"}
                    or (
                        coordinate_kind == "catalog-entry"
                        and coordinate.get("config_kind") != "study"
                    )
                )
            )
            or (kind == "run" and coordinate_kind not in {"study-launch", "run"})
            or (kind == "interface" and coordinate_kind != "interface-launch")
        ):
            continue
        description = _studio_ui_card_text(
            raw.get("description", ""), maximum=1000, normalize=True
        )
        status = _studio_ui_card_text(
            raw.get("status", ""), maximum=80, normalize=True
        )
        if description is None or status is None:
            continue
        allowed_operations = _studio_ui_card_coordinate_operations(coordinate)
        actions = [
            action
            for action in _sanitize_studio_ui_card_actions(raw.get("actions"))
            if action["operation"] in allowed_operations
        ]
        card: JsonDict = {
            "schema": STUDIO_UI_CARD_SCHEMA,
            "id": card_id,
            "kind": kind,
            "coordinate": coordinate,
            "title": title,
            "description": description,
            "status": status,
            "facts": _sanitize_studio_ui_card_facts(raw.get("facts")),
            "actions": actions,
        }
        encoded_size = len(
            json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > STUDIO_UI_CARD_MAX_BYTES:
            continue
        if total_bytes + encoded_size > STUDIO_UI_CARDS_MAX_BYTES:
            break
        cards.append(card)
        total_bytes += encoded_size
    return cards


def sanitize_openhands_native_tools(raw_tools: Any) -> tuple[str, ...]:
    if not isinstance(raw_tools, list):
        return DEFAULT_OPENHANDS_NATIVE_TOOLS
    tools: List[str] = []
    for name in raw_tools:
        normalized = str(name).strip()
        if normalized and normalized in ALLOWED_OPENHANDS_NATIVE_TOOLS and normalized not in tools:
            tools.append(normalized)
    return tuple(tools)


def _tool_schema(properties: JsonDict, required: Optional[List[str]] = None) -> JsonDict:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required or []}


CONFIG_KIND_SCHEMA = {"type": "string", "enum": ["environment", "method", "resource", "study"]}
OBJECTIVE_DIRECTION_SCHEMA = {"type": "string", "enum": ["maximize", "minimize"]}
OBJECTIVE_AGGREGATION_SCHEMA = {"type": "string", "enum": ["mean", "median", "min", "max", "sum", "last", "weighted_mean"]}
EVIDENCE_LEVEL_SCHEMA = {"type": "string", "enum": ["minimal", "standard", "full"]}
_CATALOG_ENTRY_REF_OBJECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "enum": ["optpilot.catalog-entry-ref.v1"]},
        "source_kind": {
            "type": "string",
            "enum": ["realm-catalog", "configured-filesystem-import"],
        },
        "source_id": {"type": "string"},
        "source_revision": {"type": ["integer", "null"]},
        "source_digest": {"type": ["string", "null"]},
        "kind": CONFIG_KIND_SCHEMA,
        "entry_id": {"type": "string"},
        "focus_path": {"type": "string"},
        "ref_digest": {"type": "string"},
    },
    "required": [
        "schema",
        "source_kind",
        "source_id",
        "source_revision",
        "source_digest",
        "kind",
        "entry_id",
        "focus_path",
        "ref_digest",
    ],
}

#: What a tool will accept for "which catalog entry". The structured form
#: below is exact -- it names the source revision and carries a digest -- but
#: reproducing it, or the equivalent ref token, means echoing hundreds of
#: characters back without a slip. A language model does not reliably manage
#: that: one re-encoded a token, dropped a field, and sent 485 characters where
#: 489 were required, so the call failed and the conversation stalled with
#: nothing on screen explaining why.
#:
#: A readable name is therefore accepted as well, and is what the listings
#: advertise. It names the entry as it stands now, which is what a person
#: clicking that entry in the Catalog also gets.
CATALOG_ENTRY_REF_SCHEMA = {
    "anyOf": [
        {
            "type": "string",
            "description": (
                "A catalog entry's qualified_id, for example "
                "or_solving/method/coopa-solver, or its plain id when only one "
                "entry of that kind has it."
            ),
        },
        _CATALOG_ENTRY_REF_OBJECT_SCHEMA,
    ]
}

STUDY_LAUNCH_INPUTS_SCHEMA = {
    "type": "object",
    "description": (
        "Per-launch values for the Run setup's declared inputs, keyed by the "
        "declared input name (for example the problem statement a one-shot "
        "solving Run setup expects). Read the declared names, types, and "
        "descriptions from the Run setup's validation.inputs. These values are "
        "the problem payload and are retained in Run evidence, so never place "
        "credentials or secrets here."
    ),
    "additionalProperties": True,
}


OPTPILOT_AGENT_TOOL_SPECS: List[JsonDict] = [
    {
        "name": "optpilot_conversation_title",
        "description": (
            "Give the current Conversation a short, specific title that reflects "
            "its primary goal. Call this after the first substantive request and "
            "later only when the primary goal changes materially. Do not call it "
            "for greetings, thanks, confirmations, continuations, or minor follow-ups, "
            "and do not mention the title update to the user."
        ),
        "parameters": _tool_schema(
            {
                "title": {
                    "type": "string",
                    "description": "A concise 2-7 word Conversation title.",
                }
            },
            ["title"],
        ),
    },
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
        "description": "List files under an attached workspace path. For package curation, prefer focused paths such as optpilot_configs, src, docs, or package directories before a root-wide scan.",
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
            "cwd": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": "integer"},
            "description": {"type": "string"},
        }, ["command"]),
    },
    {
        "name": "optpilot_terminal",
        "description": "OpenHands-compatible terminal interface executed by OptPilot Studio inside the selected editable workspace runtime. Runs one bounded shell command; risky commands return a Studio approval request.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "cwd": {"type": "string"},
            "command": {"type": "string"},
            "description": {"type": "string"},
            "timeout": {"type": "integer"},
            "timeout_seconds": {"type": "integer"},
            "is_input": {"type": "boolean"},
            "reset": {"type": "boolean"},
        }, ["command"]),
    },
    {
        "name": "optpilot_file_editor",
        "description": "OpenHands-compatible file editor executed by OptPilot Studio under attached-workspace and editable-copy rules. Supports view, create, exact str_replace, and insert.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
            "max_files": {"type": "integer"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
        }, ["command", "path"]),
    },
    {
        "name": "optpilot_workspace_preview_open",
        "description": "Open the Studio Preview tab for a web service already running inside an attached workspace runtime. The service must listen on 0.0.0.0 inside the workspace container. Use optpilot_shell_run first if you need to start the service.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "port": {"type": "integer"},
            "extra_ports": {"type": "array", "items": {"type": "integer"}},
        }, ["port"]),
    },
    {
        "name": "optpilot_catalog_list",
        "description": "List reusable catalog environments, methods, resources, plus saved study plans. Each entry carries a short qualified_id such as or_solving/method/coopa-solver; pass that to any tool that needs the entry. Prefer a free-text query when matching a user goal: every query term must match the entry's id, name, description, package, purpose, or tags. Optional tags must all be declared on an entry.",
        "parameters": _tool_schema({
            "config_kind": CONFIG_KIND_SCHEMA,
            "query": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        }),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_catalog_detail",
        "description": "Inspect one reusable catalog entry or saved study plan by kind and its exact uid token.",
        "parameters": _tool_schema({"config_kind": CONFIG_KIND_SCHEMA, "uid": {"type": "string", "description": "A catalog entry's qualified_id (for example or_solving/method/coopa-solver), or its plain id when only one entry of that kind has it."}}, ["config_kind", "uid"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_resource_action_list",
        "description": "List the named actions a catalog Resource declares, each with its typed inputs. Resources are the tools that make things -- for example generating a simulator from a description. Take resource_uid from an optpilot_catalog_list result.",
        "parameters": _tool_schema({"resource_uid": {"type": "string", "description": "A catalog entry's qualified_id (for example or_solving/method/coopa-solver), or its plain id when only one entry of that kind has it."}}, ["resource_uid"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_resource_action_run",
        "description": "Run one declared Resource action. Requires approval. Pass workspace_id whenever the action produces something the person will keep or register, such as a generated simulator: the results are then written inside that attached Workspace instead of Studio's private folder, and can be registered without copying. Returns a request_id; poll it with optpilot_resource_action_status.",
        "parameters": _tool_schema(
            {
                "resource_uid": {"type": "string", "description": "A catalog entry's qualified_id (for example or_solving/method/coopa-solver), or its plain id when only one entry of that kind has it."},
                "action_id": {"type": "string"},
                "inputs": {"type": "object"},
                "workspace_id": {"type": "string"},
            },
            ["resource_uid", "action_id"],
        ),
    },
    {
        "name": "optpilot_resource_action_status",
        "description": "Check an action started with optpilot_resource_action_run, including where its output was written.",
        "parameters": _tool_schema({"request_id": {"type": "string"}}, ["request_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_compatibility_check",
        "description": "Check method/environment compatibility, optionally for a selected pair.",
        "parameters": _tool_schema({"environment_ref": CATALOG_ENTRY_REF_SCHEMA, "method_ref": CATALOG_ENTRY_REF_SCHEMA}),
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
        "description": "Validate an OptPilot environment, method, resource, or study YAML file. Validation errors are actionable repair instructions: fix the reported config/source/import/setup issue and rerun validation.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}}, ["path"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_catalog_setup",
        "description": (
            "Set up an attached editable Workspace for the Catalog: writes the "
            "starter settings file for the chosen role, and for a generated "
            "simulator bundle with a declared policy hook, the full "
            "policy-search environment -- baseline candidate files, model "
            "instructions, validation rules, seeded evaluation, and the "
            "trace-replay capability. The same step as the Set up for Catalog "
            "button. After it succeeds, validate and apply the package plan to "
            "register the result."
        ),
        "parameters": _tool_schema(
            {
                "workspace_id": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": [
                        "environment", "method", "generator", "viewer",
                        "template", "reference",
                    ],
                },
                "id": {
                    "type": "string",
                    "description": "Catalog id for the component; defaults to the Workspace title.",
                },
                "description": {"type": "string"},
            },
            ["workspace_id", "role"],
        ),
    },
    {
        "name": "optpilot_package_plan_prepare",
        "description": "Prepare a package-level curation plan for an attached external workspace, including environments, methods, resources, and studies. After preparing, call optpilot_package_plan_validate before broad source reading.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "package_id": {"type": "string"},
            "config_paths": {"type": "array", "items": {"type": "string"}},
            "resource_id": {"type": "string"},
            "image_placement": {
                "type": "string",
                "enum": ["component", "package"],
                "description": "Where captured installed software is recorded when the package already has an image: only the components being registered (default) or the whole package. Ask the person before choosing 'package'.",
            },
        }),
    },
    {
        "name": "optpilot_package_plan_update",
        "description": "Update package plan includes, excludes, source hints, package id, or smoke-study choices before validation.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "package_id": {"type": "string"},
            "components": {"type": "array", "items": {"type": "object"}},
            "resources": {"type": "array", "items": {"type": "object"}},
            "studies": {"type": "array", "items": {"type": "object"}},
            "image_placement": {
                "type": "string",
                "enum": ["component", "package"],
                "description": "Where captured installed software is recorded when the package already has an image: only the components being registered (default) or the whole package. Ask the person before choosing 'package'.",
            },
        }, ["workspace_id", "plan_id"]),
    },
    {
        "name": "optpilot_package_plan_validate",
        "description": "Materialize and seal a package plan, then run non-executing schema, source, setup-file, retained-study semantic, and local source-closure checks. Python imports and callable construction are deliberately deferred to the approval-gated package smoke. If validation fails, repair missing adapters, source hints, setup files, or unsupported study semantics, then rerun validation.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "plan_id": {"type": "string"}}, ["workspace_id", "plan_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_package_plan_smoke",
        "description": "Request an approval-gated smoke study for a validated package plan in a temporary package. Studio pauses the assistant and asks the user to approve or reject before the study runs.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "study": {"type": "string"},
            "max_trials": {"type": "integer", "minimum": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        }, ["workspace_id", "plan_id"]),
    },
    {
        "name": "optpilot_package_plan_apply",
        "description": "Publish a validated package artifact as the next canonical Realm catalog package revision after approval. Environment-plus-method packages must pass a smoke study first; one-sided packages must at least be component-ready.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "plan_id": {"type": "string"}}, ["workspace_id", "plan_id"]),
    },
    {
        "name": "optpilot_study_draft",
        "description": "Create or update a durable managed study workspace from exact catalog environment and method refs.",
        "parameters": _tool_schema({
            "workspace_id": {"type": "string"},
            "expected_workspace_revision": {"type": "integer", "minimum": 1},
            "environment_ref": CATALOG_ENTRY_REF_SCHEMA,
            "method_ref": CATALOG_ENTRY_REF_SCHEMA,
            "name": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "metric": {"type": "string"},
            "direction": OBJECTIVE_DIRECTION_SCHEMA,
            "aggregation": OBJECTIVE_AGGREGATION_SCHEMA,
            "secondaryMetrics": {"type": "array", "items": {"type": "string"}},
            "maxTrials": {"type": "integer", "minimum": 1},
            "maxWallClockSeconds": {"type": "integer", "minimum": 1},
            "maxFailures": {"type": "integer", "minimum": 1},
            "parallelism": {"type": "integer", "minimum": 1},
            "timeoutSeconds": {"type": "integer", "minimum": 1},
            "maxRetries": {"type": "integer", "minimum": 0},
            "evidenceLevel": EVIDENCE_LEVEL_SCHEMA,
            "seed": {"type": "integer"},
        }, ["environment_ref", "method_ref"]),
    },
    {
        "name": "optpilot_study_save",
        "description": "Save study YAML into an editable attached workspace.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "path": {"type": "string"}, "yaml": {"type": "string"}}, ["path", "yaml"]),
    },
    {
        "name": "optpilot_study_launch",
        "description": "Launch either an exact saved catalog study ref or an exact managed-workspace study revision into the local Realm after approval. When the Run setup declares per-launch inputs, supply their values in `inputs`; a launch that leaves a required input unbound is blocked with code study_inputs_required, which names the missing inputs so you can ask the user for them instead of guessing.",
        "parameters": _tool_schema({
            "study_ref": CATALOG_ENTRY_REF_SCHEMA,
            "workspace_id": {"type": "string"},
            "study_relative_path": {"type": "string"},
            "expected_workspace_revision": {"type": "integer", "minimum": 1},
            "inputs": STUDY_LAUNCH_INPUTS_SCHEMA,
        }),
    },
    {
        "name": "optpilot_job_stop",
        "description": "Request cancellation of one durable study launch or its handed-off canonical run.",
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
        "description": "Read one bounded, path-free Realm Workbench head with candidates, logical trials, attempts, observations, and artifacts for a canonical run id. Use workbench.overview.best_candidate for Run-wide Candidate decisions; summary.best is only a low-level observation.",
        "parameters": _tool_schema({"run_id": {"type": "string"}}, ["run_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_run_compare",
        "description": "Compare canonical Realm runs by run id and summarize each run's best comparable Candidate plus compatibility caveats.",
        "parameters": _tool_schema({"runs": {"type": "array", "items": {"type": "string"}}}, ["runs"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_smoke_test_study",
        "description": "Run a small validated study in a temporary private Realm.",
        "parameters": _tool_schema({"workspace_id": {"type": "string"}, "study_path": {"type": "string"}, "max_trials": {"type": "integer", "minimum": 1}, "timeout_seconds": {"type": "integer", "minimum": 10}}, ["study_path"]),
    },
    {
        "name": "optpilot_interface_launch",
        "description": (
            "Open a Catalog component's own web interface, such as the DEVS "
            "simulation generator. Only entries whose listing shows "
            "has_interface true have one. This starts the component's web "
            "application in a container and always asks the person first."
        ),
        "parameters": _tool_schema(
            {
                "config_kind": CONFIG_KIND_SCHEMA,
                "uid": {
                    "type": "string",
                    "description": (
                        "A catalog entry's qualified_id (for example "
                        "devs_gallery/resource/devs-gen-interface), or its "
                        "plain id when only one entry of that kind has it."
                    ),
                },
                "profile_id": {"type": "string"},
            },
            ["config_kind", "uid"],
        ),
    },
    {
        "name": "optpilot_interface_status",
        "description": (
            "Check an interface that was launched, and get the address it is "
            "reachable at once it is ready."
        ),
        "parameters": _tool_schema({"launch_id": {"type": "string"}}, ["launch_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_docs_search",
        "description": "Search curated OptPilot docs and schema files for compact excerpts.",
        "parameters": _tool_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_capability_list",
        "description": "List configured assistant skills, MCP servers, custom tools, and permission defaults.",
        "parameters": _tool_schema({"capability_kind": {"type": "string", "enum": ["skill", "mcp_server", "custom_tool"]}}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "optpilot_capability_detail",
        "description": "Inspect one configured assistant capability by kind and id.",
        "parameters": _tool_schema({"capability_kind": {"type": "string", "enum": ["skill", "mcp_server", "custom_tool"]}, "id": {"type": "string"}}, ["capability_kind", "id"]),
        "annotations": {"readOnlyHint": True},
    },
]


#: Where OptPilot's own agent-server listens. Which port an internal service
#: uses is wiring, not a preference, so this is a real default rather than a
#: greyed-out placeholder: leaving the field empty means "the one OptPilot
#: starts", not "no server, answer without tools".
DEFAULT_OPENHANDS_BASE_URL = "http://127.0.0.1:8781"

#: The explicit way to ask for the toolless mode that an empty field used to
#: select by accident. Kept because that mode still works and someone with no
#: local agent-server may want it.
OPENHANDS_NO_SERVER_VALUES = frozenset({"none", "off", "disabled", "-"})


def resolve_openhands_base_url(value: Any) -> str:
    """The server URL to actually use, given whatever was configured."""

    text = str(value or "").strip().rstrip("/")
    if not text:
        return DEFAULT_OPENHANDS_BASE_URL
    if text.lower() in OPENHANDS_NO_SERVER_VALUES:
        return ""
    return text


@dataclass(frozen=True)
class OpenHandsRuntimeConfig:
    base_url: str = ""
    session_endpoint: str = ""
    model: str = ""
    api_key: str = ""
    enabled: bool = False
    native_tools: tuple[str, ...] = DEFAULT_OPENHANDS_NATIVE_TOOLS

    @classmethod
    def from_env(cls) -> "OpenHandsRuntimeConfig":
        # Derive `enabled` from what was actually CONFIGURED, before the
        # default below fills the URL in -- otherwise every install would look
        # configured and the Assistant would switch itself on with no model.
        configured_base_url = (
            os.environ.get("OPTPILOT_OPENHANDS_URL", "").strip().rstrip("/")
        )
        base_url = resolve_openhands_base_url(configured_base_url)
        session_endpoint = os.environ.get("OPTPILOT_OPENHANDS_SESSION_ENDPOINT", "").strip()
        model = os.environ.get("OPTPILOT_OPENHANDS_MODEL", os.environ.get("LLM_MODEL", "")).strip()
        api_key = (
            os.environ.get("OPTPILOT_OPENHANDS_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        enabled = _env_flag(
            "OPTPILOT_OPENHANDS_ENABLED",
            bool(configured_base_url or model or api_key),
        )
        return cls(base_url=base_url, session_endpoint=session_endpoint, model=model, api_key=api_key, enabled=enabled)

    @classmethod
    def from_mapping(cls, payload: JsonDict) -> "OpenHandsRuntimeConfig":
        return cls(
            base_url=resolve_openhands_base_url(payload.get("base_url")),
            session_endpoint=str(payload.get("session_endpoint") or "").strip(),
            model=str(payload.get("model") or "").strip(),
            api_key=str(payload.get("api_key") or "").strip(),
            enabled=bool(payload.get("enabled")),
            native_tools=sanitize_openhands_native_tools(payload.get("native_tools")),
        )


#: Addresses that never leave this machine, where plain HTTP is fine because
#: there is no network hop for anyone to read.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _is_local_address(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip().lower()
    if host in LOOPBACK_HOSTS:
        return True
    # `foo.localhost` resolves to loopback by convention and is used for
    # per-service names on a development machine.
    return host.endswith(".localhost")


def require_encrypted_transport_for_secret(url: str) -> None:
    """Refuse to put a credential on the wire unencrypted.

    Studio lets a person point the Assistant at any address, and the key for
    the configured model is sent to it as a bearer token. If that address is
    plain HTTP on another machine, the key crosses the network in the clear
    where anyone on the path can take it -- and a key taken this way is valid
    everywhere, not only here.

    Loopback is exempt: nothing is on the wire, and requiring certificates for
    a locally-run agent server would only push people to disable the check.
    """

    if _is_local_address(url):
        return
    if urlparse(url).scheme.lower() == "https":
        return
    raise RuntimeError(
        f"Refusing to send the model key to {url}: it is on another machine "
        "and the address is not https, so the key would cross the network in "
        "readable form. Use an https address, or run the service on this "
        "machine."
    )


class OpenHandsAdapter:
    """Small boundary between OptPilot Studio and an OpenHands runtime.

    OptPilot owns Studio sessions, context packets, and tool permission checks.
    OpenHands receives bounded HTTP dispatches through this adapter.
    """

    def __init__(self, config: Optional[OpenHandsRuntimeConfig] = None) -> None:
        self.config = config or OpenHandsRuntimeConfig.from_env()
        self.system_prompt = load_assistant_system_prompt()
        #: conversation id -> whether the server holds a Stop hook for it.
        #: Only definitive answers are cached; unknown is re-asked.
        self._stop_gate_states: Dict[str, bool] = {}

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
            "client_tools": sorted(SUPPORTED_CLIENT_TOOL_NAMES),
            "native_tools": list(self._openhands_native_tools()),
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
        except OpenHandsConversationNotFound as exc:
            return {
                "status": "conversation_missing",
                "mode": status.get("mode"),
                "dispatch": status.get("dispatch"),
                "conversation_id": exc.conversation_id or conversation_id,
                "assistant_message": {
                    "role": "assistant",
                    "title": "OpenHands",
                    "content": "",
                },
                "events": [
                    {
                        "type": "openhands_conversation_missing",
                        "payload": {
                            "conversation_id": exc.conversation_id or conversation_id,
                        },
                    }
                ],
            }
        except Exception as exc:
            if self._is_agent_server_unreachable(exc):
                return {
                    "status": "failed",
                    "mode": status.get("mode"),
                    "dispatch": status.get("dispatch"),
                    "conversation_id": conversation_id,
                    "assistant_message": {
                        "role": "assistant",
                        "title": "The Assistant's helper is not running",
                        "content": (
                            "OptPilot runs a helper process that lets the "
                            "Assistant use its tools -- reading the Catalog, "
                            "preparing a Run, and so on -- and it is not "
                            "answering at "
                            f"{self.config.base_url}.\n\n"
                            "Start OptPilot again so it can bring the helper "
                            "up with it. If you started Studio by hand, run "
                            "the agent-server alongside it.\n\n"
                            "Nothing was sent anywhere, and the rest of "
                            "OptPilot works meanwhile: you can browse the "
                            "Catalog, open a Run setup, and start a Run "
                            "yourself."
                        ),
                    },
                    "events": [
                        {
                            "type": "openhands_dispatch_failed",
                            "payload": {
                                "error": str(exc),
                                "reason": "agent_server_unreachable",
                                "base_url": self.config.base_url,
                            },
                        }
                    ],
                }
            if self._is_client_tool_schema_conflict(exc):
                return {
                    "status": "failed",
                    "mode": status.get("mode"),
                    "dispatch": status.get("dispatch"),
                    "conversation_id": conversation_id,
                    "assistant_message": {
                        "role": "assistant",
                        "title": "OpenHands tool schema changed",
                        "content": (
                            "OpenHands has an older OptPilot tool schema cached in its running process. "
                            "Restart the OpenHands agent server, then retry this message."
                        ),
                    },
                    "events": [
                        {
                            "type": "openhands_tool_schema_conflict",
                            "payload": {"error": str(exc), "dispatch": status.get("dispatch")},
                        }
                    ],
                }
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

    def _explain_runtime_error(self, raw: Any) -> tuple[str, str]:
        """Turn a runtime failure into a sentence, keeping the detail aside.

        The raw text is whatever the model provider returned, and it went
        straight onto the screen: a nested JSON blob naming litellm, an
        exception class, an HTTP code and the person's own account id. Someone
        reading it cannot tell whether they configured something wrongly,
        whether their key is wrong, or whether a service they have never heard
        of is having a bad afternoon -- and the one time it happened here, the
        question asked was whether a completely unrelated package needed keys.

        Returns the sentence to show and the original text to keep for a bug
        report.
        """

        return explain_runtime_error(raw)

    @staticmethod
    def _is_agent_server_unreachable(error: BaseException) -> bool:
        """Whether this failure is simply "nothing is listening there".

        Worth separating from every other dispatch failure: it is the one a
        person can actually act on, and the raw connection error names a port
        rather than saying which part of OptPilot is missing.
        """

        if isinstance(error, (ConnectionError, TimeoutError)):
            return True
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "connection refused",
                "failed to establish a new connection",
                "max retries exceeded",
                "cannot connect",
                "connection reset",
            )
        )

    def _is_client_tool_schema_conflict(self, error: Exception) -> bool:
        text = str(error)
        return (
            "HTTP 422" in text
            and "Client tool" in text
            and "different parameters schema" in text
        )

    def context_packet(
        self,
        *,
        session_id: str,
        selected_workspace: Optional[JsonDict],
        attached_workspaces: List[JsonDict],
        catalog_counts: JsonDict,
        run_count: int,
        study_plan_count: int = 0,
        current_page: str = "editor",
        registration_menu: Optional[JsonDict] = None,
        selected_catalog_entry: Optional[JsonDict] = None,
        selected_study_plan: Optional[JsonDict] = None,
        selected_run: Optional[JsonDict] = None,
        selected_interface: Optional[JsonDict] = None,
        code_editor: Optional[JsonDict] = None,
        workspace_preview: Optional[JsonDict] = None,
        visible_state: Optional[JsonDict] = None,
        assistant_capabilities: Optional[JsonDict] = None,
        conversation: Optional[JsonDict] = None,
    ) -> JsonDict:
        return {
            "session_id": session_id,
            "conversation": conversation or {},
            "current_page": current_page,
            "selected_workspace": selected_workspace,
            "attached_workspaces": attached_workspaces,
            "catalog_counts": catalog_counts,
            "study_plan_count": study_plan_count,
            "run_count": run_count,
            "registration_menu": registration_menu,
            "selected_catalog_entry": selected_catalog_entry,
            "selected_study_plan": selected_study_plan,
            "selected_run": selected_run,
            "selected_interface": selected_interface,
            "code_editor": code_editor,
            "workspace_preview": workspace_preview,
            "visible_state": visible_state or {},
            "assistant_capabilities": assistant_capabilities or {},
            "available_tools": OPTPILOT_AGENT_TOOLS,
            "client_tools": sorted(SUPPORTED_CLIENT_TOOL_NAMES),
            "runtime": self.status(),
        }

    #: What to tell someone whose message cannot be answered, keyed by what is
    #: missing. Each says the same three things in order: nothing is coming,
    #: why, and the one action that fixes it. The old text said only that the
    #: message had been "stored" and that a runtime they had never heard of
    #: was "disabled" -- true, unhelpful, and easy to read as "still working".
    ASSISTANT_OFF_NOTICES: Dict[str, str] = {
        "disabled": (
            "The Assistant is switched off, so no one is reading this. Your "
            "message is saved here and nothing else will happen to it.\n\n"
            "To switch it on, open Settings and turn the Assistant on, then "
            "choose a model and enter the key for it. Send your message again "
            "afterwards.\n\n"
            "Everything else in OptPilot works without the Assistant: you can "
            "browse the Catalog, open a Run setup, and start a Run yourself."
        ),
        "missing model": (
            "The Assistant is on but has not been told which model to use, so "
            "no one is reading this. Your message is saved here and nothing "
            "else will happen to it.\n\n"
            "Open Settings, choose a model for the Assistant, and send your "
            "message again."
        ),
        "missing API key": (
            "The Assistant is on and has a model chosen, but no key to reach "
            "it with, so no one is reading this. Your message is saved here "
            "and nothing else will happen to it.\n\n"
            "Open Settings, enter the key for the chosen model, and send your "
            "message again."
        ),
    }

    def _queued_result(self, status: JsonDict) -> JsonDict:
        reason = str(status.get("mode") or "not configured")
        content = self.ASSISTANT_OFF_NOTICES.get(
            reason,
            (
                "The Assistant cannot answer at the moment, so your message is "
                "saved here and nothing else will happen to it. Open Settings "
                "and check that the Assistant is on, a model is chosen, and "
                "the key for it is entered, then send your message again."
            ),
        )
        return {
            "status": "queued",
            "mode": reason,
            "dispatch": status.get("dispatch", "queued"),
            "assistant_message": {
                "role": "assistant",
                "title": "The Assistant is not set up yet",
                "content": content,
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
        elif (
            self._stop_hook_config() is not None
            and self._conversation_stop_gate_state(conversations_url, next_conversation_id)
            is False
        ):
            # A conversation created before the Stop hook existed has no
            # stall protection at all -- no hook, and no always-finish
            # teaching in its stored system prompt. Recreating it is the
            # same recovery the caller already performs for a conversation
            # the server has forgotten, and carries the history back in via
            # the recent-messages context.
            raise OpenHandsConversationNotFound(next_conversation_id)
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
        answer, tool_events, runtime_error, paused_approval_id = self._poll_openhands_answer(
            conversations_url,
            next_conversation_id,
            tool_executor=tool_executor,
            ignored_tool_calls=ignored_tool_calls,
            ignored_event_ids=ignored_event_ids,
            ignored_response_texts=ignored_texts,
            poll_seconds=3.0,
        )
        if paused_approval_id:
            return {
                "status": "awaiting_user_approval",
                "mode": "openhands agent server",
                "dispatch": "openhands_http",
                "conversation_id": next_conversation_id,
                "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                "events": [
                    *tool_events,
                    {
                        "type": "openhands_dispatch_paused_for_approval",
                        "payload": {"conversation_id": next_conversation_id, "approval_id": paused_approval_id},
                    },
                ],
                "sync_state": {
                    "ignored_event_ids": sorted(ignored_event_ids),
                    "ignored_tool_call_ids": sorted(ignored_tool_calls),
                    "ignored_response_texts": sorted(ignored_texts),
                    "paused_approval_id": paused_approval_id,
                },
            }
        if runtime_error:
            return {
                "status": "failed",
                "mode": "openhands agent server",
                "dispatch": "openhands_http",
                "conversation_id": next_conversation_id,
                "assistant_message": {
                    "role": "assistant",
                    "title": "The Assistant could not finish",
                    "content": self._explain_runtime_error(runtime_error)[0],
                    "technical": self._explain_runtime_error(runtime_error)[1],
                },
                "events": [
                    *tool_events,
                    {
                        "type": "openhands_dispatch_failed",
                        "payload": {"conversation_id": next_conversation_id, "error": runtime_error},
                    },
                ],
            }
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
        poll_seconds: float = 3.0,
    ) -> JsonDict:
        status = self.status()
        if not conversation_id or status.get("dispatch") != "openhands_http":
            return {"status": "unavailable", "events": []}
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        answer, tool_events, runtime_error, paused_approval_id = self._poll_openhands_answer(
            conversations_url,
            conversation_id,
            tool_executor=tool_executor,
            ignored_tool_calls=ignored_tool_calls,
            ignored_event_ids=ignored_event_ids,
            ignored_response_texts=ignored_response_texts,
            poll_seconds=poll_seconds,
            allow_silent_finish=True,
        )
        if paused_approval_id:
            return {
                "status": "awaiting_user_approval",
                "conversation_id": conversation_id,
                "assistant_message": {"role": "assistant", "title": "OpenHands", "content": ""},
                "events": tool_events,
                "sync_state": {
                    "ignored_event_ids": sorted(ignored_event_ids or []),
                    "ignored_response_texts": sorted(ignored_response_texts or []),
                    "paused_approval_id": paused_approval_id,
                },
            }
        if runtime_error:
            return {
                "status": "failed",
                "conversation_id": conversation_id,
                "assistant_message": {
                    "role": "assistant",
                    "title": "The Assistant could not finish",
                    "content": self._explain_runtime_error(runtime_error)[0],
                    "technical": self._explain_runtime_error(runtime_error)[1],
                },
                "events": tool_events,
                "sync_state": {
                    "ignored_event_ids": sorted(ignored_event_ids or []),
                    "ignored_response_texts": sorted(ignored_response_texts or []),
                },
            }
        return {
            "status": "answered" if answer else "running",
            "conversation_id": conversation_id,
            "assistant_message": {"role": "assistant", "title": "OpenHands", "content": answer},
            "events": tool_events,
            "sync_state": {
                "ignored_event_ids": sorted(ignored_event_ids or []),
                "ignored_response_texts": sorted(ignored_response_texts or []),
            },
        }

    def cancel_conversation(self, conversation_id: str) -> JsonDict:
        status = self.status()
        if not conversation_id:
            return {"cancelled": False, "reason": "missing conversation id"}
        if status.get("dispatch") != "openhands_http" or not self.config.base_url:
            return {
                "cancelled": False,
                "reason": "OpenHands HTTP bridge is not active",
                "dispatch": status.get("dispatch"),
            }
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        errors: List[str] = []
        for action in ("interrupt", "pause"):
            try:
                self._request_json(
                    "POST",
                    f"{conversations_url}/{conversation_id}/{action}",
                    payload=None,
                    timeout=3.0,
                )
                return {
                    "cancelled": True,
                    "action": action,
                    "conversation_id": conversation_id,
                    "dispatch": "openhands_http",
                }
            except Exception as exc:
                errors.append(f"{action}: {exc}")
        return {
            "cancelled": False,
            "conversation_id": conversation_id,
            "dispatch": "openhands_http",
            "error": "; ".join(errors),
        }

    def _start_conversation_payload(self, context: JsonDict) -> JsonDict:
        workspace = context.get("selected_workspace") if isinstance(context.get("selected_workspace"), dict) else None
        working_dir = str((workspace or {}).get("root") or ".")
        payload: JsonDict = {
            "agent": {
                "kind": "Agent",
                "llm": self._openhands_llm_payload(),
                "tools": self._openhands_tool_records(),
                "agent_context": {"system_message_suffix": self.system_prompt},
            },
            "client_tools": self._client_tool_specs(),
            "workspace": {"kind": "LocalWorkspace", "working_dir": working_dir},
            "confirmation_policy": {"kind": "NeverConfirm"},
            "initial_message": None,
            "max_iterations": 40,
            "stuck_detection": True,
        }
        hook_config = self._stop_hook_config()
        if hook_config:
            payload["hook_config"] = hook_config
        return payload

    def _stop_hook_config(self) -> Optional[JsonDict]:
        """Stop hook that lets a turn end only on finish or a pending dispatch.

        OpenHands finishes a run on ANY plain assistant message, so a model
        that narrates "I'm awaiting the tool results" ends its turn and the
        person sees a hang. The gate (stop_gate.py) vetoes that stop with
        corrective feedback unless the run ended with the finish tool or
        with an OptPilot dispatch whose result Studio still owes.

        The gate reads the agent-server's on-disk event files: hooks run via
        a blocking subprocess on the server's only event loop, so calling
        back over HTTP would deadlock. The conversations directory is
        resolved from this process's working directory because launch.json
        starts Studio and the agent-server from the same one; deployments
        that split them must set OPTPILOT_OPENHANDS_CONVERSATIONS_DIR.
        A missing directory only ever fails open -- the gate allows the stop.
        """

        gate_source = Path(__file__).resolve().with_name("stop_gate.py")
        if not gate_source.exists():
            return None
        conversations_root = os.environ.get(
            "OPTPILOT_OPENHANDS_CONVERSATIONS_DIR"
        ) or str(Path.cwd() / "workspace" / "conversations")
        # The hook command is persisted inside the conversation record and
        # outlives this checkout (worktrees are pruned after merge), while a
        # missing script makes python itself exit 2 -- the DENY code. Two
        # defences: the gate is copied next to the conversations it reads,
        # which shares their lifetime, and the command tests readability
        # first so a vanished or unreadable gate allows the stop instead of
        # deny-looping every turn into the max-iterations error.
        gate_path = gate_source
        stable_gate = Path(conversations_root).parent / "optpilot_stop_gate.py"
        try:
            data = gate_source.read_bytes()
            if not stable_gate.exists() or stable_gate.read_bytes() != data:
                stable_gate.parent.mkdir(parents=True, exist_ok=True)
                stable_gate.write_bytes(data)
            gate_path = stable_gate
        except OSError:
            pass
        command = f"[ -r {shlex.quote(str(gate_path))} ] || exit 0; " + " ".join(
            shlex.quote(part)
            for part in (
                sys.executable,
                str(gate_path),
                "--conversations-root",
                conversations_root,
            )
        )
        return {
            "stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 10}
                    ],
                }
            ]
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

    def _openhands_native_tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for name in self.config.native_tools if name))

    def _openhands_tool_records(self) -> List[JsonDict]:
        return [{"name": name, "params": {}} for name in self._openhands_native_tools()]

    def _poll_openhands_answer(
        self,
        conversations_url: str,
        conversation_id: str,
        *,
        tool_executor: Optional[ToolExecutor],
        ignored_tool_calls: Optional[set[str]] = None,
        ignored_event_ids: Optional[set[str]] = None,
        ignored_response_texts: Optional[set[str]] = None,
        poll_seconds: float = 75.0,
        allow_silent_finish: bool = False,
    ) -> tuple[str, List[JsonDict], str, str]:
        search_url = f"{conversations_url}/{conversation_id}/events/search?limit=50&sort_order=TIMESTAMP_DESC"
        deadline = time.monotonic() + max(float(poll_seconds), 0.1)
        # The caller's sets are used in place, not copied: the finish-event
        # retirements and newly handled call ids recorded during this poll
        # must be visible in the sync_state the caller persists, or a stale
        # finish surfaces as the answer on the NEXT poll.
        handled_tool_calls: set[str] = (
            ignored_tool_calls if ignored_tool_calls is not None else set()
        )
        ignored_events: set[str] = (
            ignored_event_ids if ignored_event_ids is not None else set()
        )
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
            except Exception as exc:
                if self._is_missing_conversation_error(exc, conversation_id):
                    raise OpenHandsConversationNotFound(conversation_id) from exc
                data = {}
            events = data.get("items", []) if isinstance(data, dict) else []
            tool_events.extend(self._trace_openhands_events(events, seen_openhands_events))
            runtime_error = self._best_runtime_error(events, ignored_events)
            if runtime_error:
                return "", tool_events, runtime_error, ""
            if tool_executor:
                new_tool_events, paused_approval_id = self._execute_openhands_client_tools(
                    events,
                    conversations_url,
                    conversation_id,
                    tool_executor,
                    handled_tool_calls,
                )
                tool_events.extend(new_tool_events)
                if new_tool_events or paused_approval_id:
                    # Work was just forwarded (or paused for approval), so
                    # the run resumes later: a "finished" state from before
                    # the forward is stale -- and so is any finish call
                    # emitted alongside the dispatch. Retire those finish
                    # events so the resumed run's own ending is the one
                    # surfaced. Pending dispatches run before the finish
                    # check so a batch of tool call plus finish can never
                    # surface the finish text while the call sits unexecuted.
                    for event in events if isinstance(events, list) else []:
                        if self._event_finish_text(event):
                            finish_event_id = self._openhands_event_id(event)
                            if finish_event_id:
                                ignored_events.add(finish_event_id)
                if paused_approval_id:
                    return "", tool_events, "", paused_approval_id
                if new_tool_events:
                    continue
            finish_text = self._best_finish_text(events, ignored_events, ignored_texts)
            if finish_text:
                return finish_text, tool_events, "", ""
            if self._execution_finished(events):
                # An agent may end its turn with a plain final message instead
                # of a finish tool call; the conversation then flips
                # execution_status to "finished" with no FinishAction event.
                # Without this, the session would show "Working…" forever
                # while the finished answer sits unread in the event stream.
                # This check runs only after any pending client-tool results
                # were forwarded, so a mid-turn tool dispatch is not mistaken
                # for the closing answer.
                if self._plain_finish_awaits_stop_gate(
                    conversations_url, conversation_id, events
                ):
                    # The Stop hook is about to rule on this ending and would
                    # deny it; surfacing now would show the denied message as
                    # the final answer and orphan the corrected one. The deny
                    # flips the status back to "running" within the hook
                    # timeout; if the hook fails open, the age bound inside
                    # the helper surfaces the plain message anyway.
                    time.sleep(2.0)
                    continue
                final_text = self._best_final_message_text(
                    events, ignored_events, ignored_texts
                )
                if final_text:
                    return final_text, tool_events, "", ""
                if allow_silent_finish:
                    return (
                        "The Assistant finished this turn without a closing "
                        "message. Open Technical details to see the steps it "
                        "took, or send a follow-up message.",
                        tool_events,
                        "",
                        "",
                    )
            time.sleep(2.0)
        return "", tool_events, "", ""

    def _is_tool_result_feedback_event(self, event: JsonDict) -> bool:
        """Whether this user message is Studio handing back a tool result."""

        text = self._event_user_text(event) if isinstance(event, dict) else ""
        return str(text or "").lstrip().startswith(self.TOOL_RESULT_FEEDBACK_PREFIX)

    def _execution_finished(self, events: Any) -> bool:
        # Events arrive newest-first; only the newest execution_status update
        # counts. An older "finished" from before a client-tool resume must
        # not settle a conversation that is running again — and a "finished"
        # left over from the PREVIOUS turn must not close a turn whose user
        # message was only just posted, so the status update also has to be
        # newer than the latest user message event.
        source_events = events if isinstance(events, list) else []
        newest_status: Optional[JsonDict] = None
        newest_user: Optional[JsonDict] = None
        for event in source_events:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
            if (
                newest_status is None
                and kind == "ConversationStateUpdateEvent"
                and str(event.get("key") or "") == "execution_status"
            ):
                newest_status = event
            if (
                newest_user is None
                and kind == "MessageEvent"
                and str(event.get("source") or "") == "user"
                and not self._is_tool_result_feedback_event(event)
            ):
                # Studio posts each tool's result back into the conversation as
                # a user message, so those look like fresh user turns here.
                # Counting them made the comparison below unwinnable: a result
                # posted after the agent had already finished left the turn
                # looking permanently unfinished, and the session sat on
                # "Working" with the finished answer unread beside it. Only a
                # real request from the person should hold a turn open.
                newest_user = event
            if newest_status is not None and newest_user is not None:
                break
        if newest_status is None:
            return False
        if str(newest_status.get("value") or "").lower() != "finished":
            return False
        if newest_user is not None:
            status_at = str(newest_status.get("timestamp") or "")
            user_at = str(newest_user.get("timestamp") or "")
            if status_at and user_at and status_at <= user_at:
                return False
        return True

    def _best_final_message_text(
        self, events: Any, ignored_events: set[str], ignored_texts: set[str]
    ) -> str:
        # Events arrive newest-first; the first agent-authored message with
        # user-facing text is the closing answer for the finished turn.
        source_events = events if isinstance(events, list) else []
        newest_user_at = self._newest_user_message_timestamp(source_events)
        for event in source_events:
            if not isinstance(event, dict):
                continue
            event_id = self._openhands_event_id(event)
            if event_id and event_id in ignored_events:
                continue
            kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
            if kind != "MessageEvent" or str(event.get("source") or "") != "agent":
                continue
            if self._is_stale_for_user_anchor(event, newest_user_at):
                continue
            text = self._event_assistant_text(event)
            normalized = self._normalize_response_text(text)
            if text and normalized and normalized not in ignored_texts:
                return text
        return ""

    def _newest_user_message_timestamp(self, events: Any) -> str:
        """Timestamp of the newest user-role message, tool results included.

        Any user message -- the person's or one Studio posted -- starts a new
        run server-side, so agent output older than it belongs to an earlier
        run and must not be surfaced as the current run's answer.
        """

        source_events = events if isinstance(events, list) else []
        for event in source_events:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
            if kind == "MessageEvent" and str(event.get("source") or "") == "user":
                return str(event.get("timestamp") or "")
        return ""

    @staticmethod
    def _is_stale_for_user_anchor(event: Any, newest_user_at: str) -> bool:
        if not newest_user_at or not isinstance(event, dict):
            return False
        event_at = str(event.get("timestamp") or "")
        return bool(event_at) and event_at <= newest_user_at

    def _conversation_stop_gate_state(
        self, conversations_url: str, conversation_id: str
    ) -> Optional[bool]:
        """Whether the server holds a Stop hook for this conversation.

        True/False only when the server's stored record answers the question
        (the response must look like a real conversation record); None when
        it cannot be reached or is unrecognisable, so callers fail open.
        """

        cached = self._stop_gate_states.get(conversation_id)
        if cached is not None:
            return cached
        try:
            info, _headers = self._request_json(
                "GET",
                f"{conversations_url}/{conversation_id}",
                payload=None,
                timeout=10.0,
            )
        except Exception:
            return None
        if not isinstance(info, dict) or not info.get("id"):
            return None
        hook_config = info.get("hook_config")
        stop_hooks = (
            hook_config.get("stop") if isinstance(hook_config, dict) else None
        )
        state = bool(stop_hooks)
        self._stop_gate_states[conversation_id] = state
        return state

    def _plain_finish_awaits_stop_gate(
        self, conversations_url: str, conversation_id: str, events: Any
    ) -> bool:
        """Whether surfacing a plain-message finish must wait for the gate.

        The FINISHED status and the plain message reach the events API
        before the Stop hook has ruled on them. If the gate is registered
        for this conversation and would deny this ending, the deny is about
        to inject feedback and flip the status back to running -- surfacing
        now would show the denied message as the final answer and orphan
        the corrected one. Bounded by the age of the finished status: past
        the hook timeout plus margin the hook has clearly failed open, and
        the plain message stands.
        """

        if not self._conversation_stop_gate_state(conversations_url, conversation_id):
            return False
        source_events = events if isinstance(events, list) else []
        for event in source_events:
            if not isinstance(event, dict):
                continue
            if str(event.get("key") or "") != "execution_status":
                continue
            if str(event.get("value") or "").lower() != "finished":
                return False
            status_at = str(event.get("timestamp") or "")
            try:
                age = (datetime.now() - datetime.fromisoformat(status_at)).total_seconds()
            except (ValueError, TypeError):
                # Unparseable or timezone-aware timestamps: fail open and
                # surface the plain message rather than defer on guesswork.
                return False
            if age > 15.0:
                return False
            ordered = [e for e in reversed(source_events) if isinstance(e, dict)]
            allow, _feedback = stop_gate_decide(ordered)
            return not allow
        return False

    def _best_finish_text(self, events: Any, ignored_events: set[str], ignored_texts: set[str]) -> str:
        source_events = events if isinstance(events, list) else []
        newest_user_at = self._newest_user_message_timestamp(source_events)
        for event in source_events:
            event_id = self._openhands_event_id(event)
            if event_id and event_id in ignored_events:
                continue
            if self._is_stale_for_user_anchor(event, newest_user_at):
                # A finish written before the last posted user message (a
                # person's, or a tool result that resumed the run) closed an
                # EARLIER run; the current run owes its own ending.
                continue
            text = self._event_finish_text(event)
            normalized = self._normalize_response_text(text)
            if text and normalized and normalized not in ignored_texts:
                return text
        return ""

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
        runtime_error = self._event_runtime_error_text(event)
        if runtime_error:
            return self._compact_text(runtime_error, 300)
        action = event.get("action")
        if isinstance(action, dict):
            keys = [str(key) for key in action.keys() if key != "security_risk"]
            if keys:
                return f"Action fields: {', '.join(keys[:8])}"
        return str(event.get("kind") or event.get("type") or "OpenHands event")

    def _best_runtime_error(self, events: Any, ignored_events: set[str]) -> str:
        source_events = events if isinstance(events, list) else []
        newest_user_at = ""
        for event in source_events:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
            if (
                kind == "MessageEvent"
                and str(event.get("source") or "") == "user"
                and not self._is_tool_result_feedback_event(event)
            ):
                newest_user_at = str(event.get("timestamp") or "")
                break
        generic_status_error = ""
        saw_status_event = False
        for event in source_events:
            if not isinstance(event, dict):
                continue
            event_id = self._openhands_event_id(event)
            if event_id and event_id in ignored_events:
                continue
            kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
            if kind == "ConversationStateUpdateEvent" and str(event.get("key") or "") == "execution_status":
                # Status updates supersede each other: only the newest may
                # report an error, and not when a user message has already
                # re-opened the conversation past it -- a stale "stuck" or
                # "error" from a previous run must not fail the current one.
                if saw_status_event:
                    continue
                saw_status_event = True
                status_at = str(event.get("timestamp") or "")
                if newest_user_at and status_at and status_at <= newest_user_at:
                    continue
            error_text = self._event_runtime_error_text(event)
            if not error_text:
                continue
            if error_text == "OpenHands conversation entered error state.":
                generic_status_error = error_text
                continue
            return error_text
        return generic_status_error

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
        if str(event.get("error") or "") or self._event_runtime_error_text(event):
            return "error"
        return "status"

    def _event_payload_preview(self, event: JsonDict) -> str:
        redacted = self._redact_trace_payload(event)
        return self._json_preview(redacted, 1600)

    def _existing_openhands_events(self, conversations_url: str, conversation_id: str) -> List[JsonDict]:
        search_url = f"{conversations_url}/{conversation_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC"
        try:
            data, _headers = self._request_json("GET", search_url, payload=None, timeout=15.0)
        except Exception as exc:
            if self._is_missing_conversation_error(exc, conversation_id):
                raise OpenHandsConversationNotFound(conversation_id) from exc
            return []
        events = data.get("items", []) if isinstance(data, dict) else []
        return [event for event in events if isinstance(event, dict)]

    @staticmethod
    def _is_missing_conversation_error(error: Exception, conversation_id: str) -> bool:
        """Recognize only a 404 for an already-bound OpenHands conversation."""

        return bool(conversation_id and "HTTP 404 from" in str(error))

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
    ) -> tuple[List[JsonDict], str]:
        tool_events: List[JsonDict] = []
        for event in events if isinstance(events, list) else []:
            name, arguments, call_id = self._openhands_tool_call(event)
            if not name or name not in SUPPORTED_CLIENT_TOOL_NAMES or not call_id or call_id in handled_tool_calls:
                continue
            handled_tool_calls.add(call_id)
            try:
                result = tool_executor(name, {**arguments, "_openhands_tool_call_id": call_id})
            except Exception as exc:
                result = {
                    "ok": False,
                    "tool": name,
                    "summary": str(exc),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
                # A refusal raised rather than returned still says what would
                # fix it, when whoever raised it knew.
                remedy = getattr(exc, "remedy", None)
                if isinstance(remedy, dict) and remedy:
                    result["remedy"] = remedy
            approval_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if approval_data.get("approval_required") or result.get("status") == "approval_required":
                approval = approval_data.get("approval") if isinstance(approval_data.get("approval"), dict) else {}
                approval_id = str(approval.get("id") or approval_data.get("approval_id") or "")
                tool_events.append(
                    {
                        "id": f"optpilot-approval-pause-{call_id}",
                        "type": "optpilot_approval_pause",
                        "payload": {
                            "tool": name,
                            "tool_call_id": call_id,
                            "approval_id": approval_id,
                            "summary": str(result.get("summary") or ""),
                            "delivery_status": "paused",
                        },
                    }
                )
                return tool_events, approval_id
            result = self._redact_tool_result(result)
            result_preview = self._json_preview(result, 2400)
            delivery_status = "sent"
            delivery_error = ""
            try:
                self._send_tool_result_message(conversations_url, conversation_id, name, call_id, result, timeout=2.0)
            except Exception as exc:
                if self._is_timeout_error(exc) and self._tool_result_feedback_exists(conversations_url, conversation_id, call_id):
                    delivery_status = "confirmed_after_timeout"
                else:
                    delivery_status = "timeout" if self._is_timeout_error(exc) else "failed"
                    delivery_error = str(exc) or type(exc).__name__
            payload = {
                "tool": name,
                "tool_call_id": call_id,
                "ok": bool(result.get("ok")),
                "summary": str(result.get("summary") or ""),
                "result_preview": result_preview,
                "delivery_status": delivery_status,
                "delivery_error": delivery_error,
            }
            ui_cards = sanitize_studio_ui_cards(result.get("ui_cards"))
            if ui_cards:
                payload["ui_cards"] = ui_cards
            if delivery_status in {"timeout", "failed"}:
                payload["result"] = result
            tool_events.append(
                {
                    "id": f"optpilot-tool-result-{call_id}",
                    "type": "optpilot_tool_result",
                    "payload": payload,
                }
            )
        return tool_events, ""

    def _send_tool_result_message(
        self,
        conversations_url: str,
        conversation_id: str,
        name: str,
        call_id: str,
        result: JsonDict,
        *,
        timeout: float = 15.0,
    ) -> None:
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
        self._request_json("POST", f"{conversations_url}/{conversation_id}/events", payload=payload, timeout=timeout)

    def post_background_result(self, conversation_id: str, text: str) -> JsonDict:
        """Post a background job's outcome into the conversation and re-enter it.

        A resource action can run for minutes, and a model cannot hold a turn
        open that long -- the turn ends, and until now nothing ever re-entered
        the loop, so "I'll continue when the result arrives" was a promise the
        architecture could not keep. This delivers the promise from outside:
        the finished job's outcome is posted as a user-role message with
        run=true, the agent server resumes the loop, and Studio's session
        sync harvests whatever the agent does next.
        """

        if not conversation_id:
            return {"sent": False, "reason": "missing conversation id"}
        status = self.status()
        if status.get("dispatch") != "openhands_http" or not self.config.base_url:
            return {"sent": False, "reason": "OpenHands HTTP bridge is not active"}
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        try:
            self._request_json(
                "POST",
                f"{conversations_url}/{conversation_id}/events",
                payload={
                    "role": "user",
                    "content": [
                        {"type": "text", "cache_prompt": False, "text": text}
                    ],
                    "run": True,
                },
                timeout=15.0,
            )
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}
        return {"sent": True, "conversation_id": conversation_id}

    def submit_tool_result(self, conversation_id: str, name: str, call_id: str, result: JsonDict) -> JsonDict:
        if not conversation_id:
            return {"sent": False, "reason": "missing conversation id"}
        if not call_id:
            return {"sent": False, "reason": "missing OpenHands tool call id"}
        status = self.status()
        if status.get("dispatch") != "openhands_http" or not self.config.base_url:
            return {"sent": False, "reason": "OpenHands HTTP bridge is not active", "dispatch": status.get("dispatch")}
        conversations_url = self._join_url(self.config.base_url, self.session_endpoint)
        try:
            self._send_tool_result_message(conversations_url, conversation_id, name, call_id, self._redact_tool_result(result))
        except Exception as exc:
            if self._is_timeout_error(exc) and self._tool_result_feedback_exists(conversations_url, conversation_id, call_id):
                return {
                    "sent": True,
                    "conversation_id": conversation_id,
                    "tool_call_id": call_id,
                    "delivery_status": "confirmed_after_timeout",
                }
            return {"sent": False, "reason": str(exc), "conversation_id": conversation_id, "tool_call_id": call_id}
        return {"sent": True, "conversation_id": conversation_id, "tool_call_id": call_id}

    def _is_timeout_error(self, exc: Exception) -> bool:
        return type(exc).__name__ in {"TimeoutError", "TimeoutExpired"} or "timed out" in str(exc).lower()

    def _tool_result_feedback_exists(self, conversations_url: str, conversation_id: str, call_id: str) -> bool:
        if not call_id:
            return False
        search_url = f"{conversations_url}/{conversation_id}/events/search?limit=100&sort_order=TIMESTAMP_DESC"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                data, _headers = self._request_json("GET", search_url, payload=None, timeout=5.0)
            except Exception:
                return False
            events = data.get("items", []) if isinstance(data, dict) else []
            for event in events if isinstance(events, list) else []:
                text = self._event_user_text(event)
                if "OptPilot tool result for " in text and f"({call_id})" in text:
                    return True
            time.sleep(0.25)
        return False

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
            text = self._user_facing_assistant_text(candidate.get("content") or candidate.get("text") or candidate.get("message"))
            if text:
                return text
        return ""

    def _event_finish_text(self, event: JsonDict) -> str:
        if not isinstance(event, dict):
            return ""
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        action_kind = str(action.get("kind") or "")
        if action_kind == "FinishAction":
            text = self._user_facing_assistant_text(action.get("message") or action.get("content") or action.get("text"))
            if text:
                return text
        observation = event.get("observation") if isinstance(event.get("observation"), dict) else {}
        observation_kind = str(observation.get("kind") or "")
        if observation_kind == "FinishObservation":
            text = self._user_facing_assistant_text(
                observation.get("message") or observation.get("content") or observation.get("text")
            )
            if text:
                return text
        return ""

    def _event_runtime_error_text(self, event: JsonDict) -> str:
        if not isinstance(event, dict):
            return ""
        event_kind = str(event.get("kind") or event.get("type") or event.get("event_type") or "")
        if event_kind == "HookExecutionEvent":
            # Hook failures fail open server-side (the stop still happens);
            # the error field on the event is observability. Sniffing it as
            # a conversation error would fail a turn that completed fine.
            return ""
        if event_kind == "ConversationErrorEvent":
            detail = self._content_text(
                event.get("detail")
                or event.get("message")
                or event.get("error")
                or event.get("content")
                or event.get("text")
            ).strip()
            code = str(event.get("code") or "").strip()
            message = detail or code or "OpenHands conversation failed."
            if code and detail and not detail.startswith(code):
                message = f"{code}: {detail}"
            return self._redact_secret_text(self._compact_text(message, 1200))
        if event_kind == "ConversationStateUpdateEvent":
            key = str(event.get("key") or "")
            value = str(event.get("value") or "").lower()
            if key == "execution_status" and value == "error":
                return "OpenHands conversation entered error state."
            if key == "execution_status" and value == "stuck":
                # stuck_detection is on in the start payload; without this
                # branch a stuck run matches neither "finished" nor "error"
                # and the session shows "running" forever.
                return (
                    "OpenHands detected the Assistant repeating itself and "
                    "ended the turn."
                )
        error = self._content_text(event.get("error")).strip()
        return self._redact_secret_text(self._compact_text(error, 1200)) if error else ""

    def _redact_secret_text(self, text: str) -> str:
        if self.config.api_key:
            return text.replace(self.config.api_key, "[redacted]")
        return text

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

    #: How Studio hands a tool's result back to the model. It is scaffolding
    #: between the two of them and must never be shown to a person as though
    #: the Assistant had written it.
    TOOL_RESULT_FEEDBACK_PREFIX = "OptPilot tool result for "

    def _user_facing_assistant_text(self, content: Any) -> str:
        text = self._content_text(content)
        if not text:
            return ""
        if text.lstrip().startswith(self.TOOL_RESULT_FEEDBACK_PREFIX):
            # Studio posts tool results into the conversation for the model to
            # read. They are addressed to the model, not to the person, and
            # surfacing one as the reply shows a wall of raw JSON where an
            # answer belongs. Every route to a displayed answer passes through
            # here, so one guard covers them all.
            return ""
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[1].strip()
        text = text.strip()
        if not text:
            return ""
        normalized = self._normalize_response_text(text).lower()
        if not normalized:
            return ""
        return text

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
            require_encrypted_transport_for_secret(url)
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
    package_root = Path(__file__).resolve().parent
    candidates.append(package_root / "assistant_assets" / "prompts" / "system.md")
    return candidates
