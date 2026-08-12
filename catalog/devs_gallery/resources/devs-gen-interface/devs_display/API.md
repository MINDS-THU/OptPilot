# DEVS Display API

This document defines the session-based API used by the DEVS Simulation
Generator Interface frontend and backend.

The legacy top-level APIs (`/projects`, `/projects/{name}/files`, `/chat`) are intentionally not part of this design. The frontend should treat a session as the top-level workspace, and every project, chat request, message, progress event, and file operation should be scoped to a session.

## Base URL

Development default:

```text
http://localhost:8000
```

## Authentication

Authentication is intentionally a lightweight single-password gate, not a user or permission system.

Set the backend environment variable to enable it:

```bash
DEVS_DISPLAY_PASSWORD=change-this-password
```

Optional settings:

| Variable | Meaning |
| --- | --- |
| `DEVS_DISPLAY_PASSWORD` | Enables password protection when non-empty. |
| `DEVS_DISPLAY_AUTH_SECRET` | Optional token signing secret. Defaults to a value derived from the password. |
| `DEVS_DISPLAY_AUTH_TOKEN_TTL_SECONDS` | Bearer token lifetime. Defaults to 7 days. |

When no password variable is configured, authentication is disabled for local development.

### `GET /auth/status`

Returns whether password authentication is required. This endpoint is always public.

```json
{
  "auth_required": true
}
```

### `POST /auth/login`

Validates the password and returns a bearer token. This endpoint is always public.

Request:

```json
{
  "password": "..."
}
```

Response:

```json
{
  "token": "...",
  "auth_required": true,
  "expires_in": 604800
}
```

All other backend endpoints require:

```text
Authorization: Bearer <token>
```

## Frontend/Visualizer APIs

### `GET /config/frontend`

Returns frontend-safe UI configuration. This endpoint must not return raw API keys. It only reports whether the backend has a usable key in its environment.

Response:

```json
{
  "default_provider": "openai",
  "default_model": "openrouter/openai/gpt-5.4-mini",
  "api_key_available": {
    "openai": true
  },
  "model_presets": [
    {
      "provider": "openai",
      "label": "OpenRouter GPT 5.4 Mini",
      "model": "openrouter/openai/gpt-5.4-mini"
    }
  ]
}
```

### `POST /visualizer/parse-model`

Parses one Python model class into graph structure for the visualizer. For OpenRouter/OpenAI-compatible models, the backend uses `OPENROUTER_API_KEY` from its local environment unless `api_key` is explicitly supplied. The backend calls LiteLLM with a Pydantic response schema and validates the returned JSON before handing it to the frontend. The request timeout defaults to 240 seconds and can be overridden with `DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS`.

Request:

```json
{
  "class_name": "RootModel",
  "code_content": "class RootModel: ...",
  "provider": "openai",
  "model": "openrouter/openai/gpt-5.4-mini",
  "api_key": null
}
```

Response:

```json
{
  "parsed": {
    "components": [
      {"name": "generator", "className": "Generator"}
    ],
    "couplings": [
      {
        "source_model": "generator",
        "source_port": "out",
        "target_model": "processor",
        "target_port": "in"
      }
    ]
  }
}
```

## Core Model

```text
Session = one workspace
Session -> many projects
Session -> one conversation history
Session -> many chat requests
Chat request -> many progress events
```

Concurrency rules:

- One session can run at most one chat request at a time.
- Read APIs for the same session can run while a chat request is running.
- The first implementation may use one global worker for all sessions; the API still models per-session execution so this can be relaxed later.
- File reads during a running request may observe intermediate file state in the first implementation. Responses should expose session/request status so the UI can label the project as updating.

Recommended backend storage:

```text
workspaces/
  sess_abc123/
    session.json
    projects.json
    messages.jsonl
    requests.jsonl
    events.jsonl
    projects/
      proj_abc123/
        system_model_info.json
        ...
```

Version history and rollback are out of scope for the first implementation. The API keeps lightweight `version` fields so versioned snapshots can be added later without changing response shapes.

## IDs

Use backend-generated stable IDs in API paths.

| ID | Example | Meaning |
| --- | --- | --- |
| `session_id` | `sess_01hxyz...` | Workspace/conversation ID. |
| `project_id` | `proj_01hxyz...` | Stable project ID inside a session. |
| `request_id` | `req_01hxyz...` | One user instruction and one agent run. |
| `message_id` | `msg_01hxyz...` | One conversation message. |
| `event_id` | `42` | Monotonic event number within a session. |

Project names shown in the UI should be stored as `display_name`, not used as route identifiers. Generated projects often reuse folder names such as `devs_project`, so the backend may format discovered project names as `<path-tail>:<root_model>` while keeping the full relative folder in `path`.

## Data Shapes

### Session

```json
{
  "session_id": "sess_01hxyz",
  "storage_session_id": "sess_01hxyz",
  "title": "Traffic model changes",
  "status": "idle",
  "active_request_id": null,
  "created_at": "2026-06-11T11:30:00Z",
  "updated_at": "2026-06-11T11:35:00Z",
  "project_count": 2,
  "workspace_path": "/abs/path/to/devs_app/working_dirs/session_workspace_...",
  "is_current_workspace": true
}
```

Session persistence:

- Session state is stored under the session workspace at `.devs_display_sessions/sessions/{storage_session_id}`.
- The backend keeps a local registry at `devs_display/.storage/session_registry.json` so backend restarts can rediscover sessions from previous workspaces.
- `session_id` is the public API ID. If multiple historical workspaces contain `sess_base`, older base sessions are exposed through stable alias IDs to avoid route conflicts.
- `workspace_path` is the workspace used by that session's project files and agent instance.

Session statuses:

| Status | Meaning |
| --- | --- |
| `idle` | No chat request is running. |
| `queued` | A chat request has been accepted but has not started. |
| `running` | The agent is currently processing a request. |
| `waiting_for_user` | Interactive generation is paused at a durable review checkpoint. The request remains active, but no worker or model call is running. |
| `failed` | The latest request failed. |

### Project

```json
{
  "project_id": "proj_01hxyz",
  "display_name": "demo/devs_project:HospitalRoot",
  "status": "ready",
  "version": 3,
  "created_at": "2026-06-11T11:30:00Z",
  "updated_at": "2026-06-11T11:35:00Z",
  "path": "catalog/example_package/demo/devs_project",
  "source": {
    "type": "session_project",
    "session_id": "sess_source",
    "project_id": "proj_source",
    "version": 2
  }
}
```

Project statuses:

| Status | Meaning |
| --- | --- |
| `ready` | Project can be viewed normally. |
| `updating` | A running request may be modifying this project. |
| `error` | The latest operation involving this project failed. |

### Message

```json
{
  "message_id": "msg_01hxyz",
  "session_id": "sess_01hxyz",
  "request_id": "req_01hxyz",
  "role": "user",
  "status": "visible",
  "content": "Modify the current project...",
  "created_at": "2026-06-11T11:31:00Z",
  "withdrawn_at": null
}
```

Roles:

| Role | Meaning |
| --- | --- |
| `user` | User input. |
| `assistant` | Final assistant answer. |
| `system` | Backend/system notice. |

Message statuses:

| Status | Meaning |
| --- | --- |
| `visible` | Message should be shown normally. |
| `withdrawn` | User withdrew the queued request before execution. |

### Request

```json
{
  "request_id": "req_01hxyz",
  "session_id": "sess_01hxyz",
  "status": "running",
  "user_message_id": "msg_user",
  "assistant_message_id": null,
  "active_project_id": "proj_01hxyz",
  "generation_mode": "guided",
  "phase": "interpret_intent",
  "pending_interaction": null,
  "updated_project_ids": [],
  "started_at": "2026-06-11T11:31:02Z",
  "completed_at": null,
  "cancel_requested_at": null,
  "error": null
}
```

Request statuses:

| Status | Meaning |
| --- | --- |
| `queued` | Request accepted but not started. |
| `running` | Agent is currently processing. |
| `waiting_for_user` | An Interactive intent or structure review is awaiting a response. |
| `cancelling` | Reserved for future cooperative running-request cancellation. It is not currently emitted. |
| `completed` | Final assistant response is available. |
| `failed` | Request failed. |
| `cancelled` | Request was withdrawn before execution or stopped during execution. |

### Event

```json
{
  "event_id": 42,
  "session_id": "sess_01hxyz",
  "request_id": "req_01hxyz",
  "type": "activity",
  "content": "Generating component code",
  "activity_key": "generate_components",
  "activity_state": "progress",
  "title": "Generated Warehouse",
  "detail": "Component 3 of 5 is ready.",
  "current": 3,
  "total": 5,
  "technical_name": "devs_construct_tree",
  "created_at": "2026-06-11T11:31:30Z"
}
```

`activity_key` identifies one logical stage whose state may change over time.
`activity_state` is one of `started`, `progress`, `completed`, or `failed`.
`current` and `total` are present only when the generator knows an honest work
count. `technical_name` is an optional allowlisted implementation label shown
under technical details.

The activity channel is intentionally public and semantic. It must never carry
raw model reasoning, prompts, generated source, tool arguments or results,
environment values, credentials, absolute paths, or unrestricted logs. Those
values are neither a stable progress API nor safe student-facing content.

Persisted event types:

| Type | Meaning |
| --- | --- |
| `request_started` | Backend accepted the user request. |
| `phase_started` | An interpretation or structure-planning phase began. |
| `interaction_required` | A durable Interactive review checkpoint is ready. |
| `interaction_resolved` | An Interactive review response was recorded. |
| `request_recovered` | A request was restored after restart, or retained generated files were independently verified after the model response was interrupted. |
| `agent_started` | Agent execution began. |
| `activity` | Sanitized stage, tool, validation, repair, or publication progress. |
| `simulation_repair_started` | Compatibility lifecycle event for automatic repair. |
| `simulation_repair_completed` | Compatibility lifecycle event for successful automatic repair. |
| `request_cancelled` | Request was cancelled or withdrawn. |
| `request_failed` | Request failed. |
| `request_completed` | Request completed successfully. |

Older stored sessions may also contain `agent_log`, `tool_started`,
`tool_finished`, `files_changed`, or `assistant_message`. Clients may map these
to generic labels, but must not render their raw `content` as public activity.

## Session APIs

### `POST /sessions`

Creates a new session workspace.

Request:

```json
{
  "title": "Traffic model changes",
  "clone_projects": [
    {
      "source_session_id": "sess_source",
      "source_project_id": "proj_source",
      "source_version": 2,
      "display_name": "traffic_model_copy"
    }
  ]
}
```

Fields:

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `title` | string | no | User-facing session title. |
| `clone_projects` | array | no | List of source projects to clone into the new session. |

Clone behavior:

- `source_session_id.source_project_id` identifies the source project.
- Multiple source projects can be cloned in one request.
- `source_version` is optional and reserved for future versioned snapshots. If omitted, clone the latest project files.
- `display_name` is optional. If omitted, keep the source display name unless it conflicts in the target session.
- The backend should copy project files into the new workspace, excluding transient caches and hidden implementation directories.
- A new session gets its own workspace directory and a separate agent instance when chat work starts.
- When launched through `devs_app.run`, the backend receives an `agent_factory(working_directory)` and lazily creates agents for historical or newly-created session workspaces.

Response:

```json
{
  "session": {}
}
```

### `GET /sessions`

Lists recent sessions.

Query parameters:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | integer | `20` | Number of sessions to return. |
| `offset` | integer | `0` | Offset for pagination. |

Response:

```json
{
  "sessions": []
}
```

### `GET /sessions/{session_id}`

Returns session metadata.

Response:

```json
{
  "session": {}
}
```

### `PATCH /sessions/{session_id}`

Updates user-editable session metadata.

Request:

```json
{
  "title": "Airfreight demo"
}
```

Response:

```json
{
  "session": {}
}
```

Notes:

- `title` is trimmed and must not be empty.
- Updating the title also updates `updated_at`, so the session may move in recent-first session lists.

### `DELETE /sessions/{session_id}`

Deletes a session after the frontend has confirmed with the user.

Response:

```json
{
  "session_id": "sess_01hxyz",
  "deleted": true,
  "deleted_workspace": true,
  "workspace_path": "/abs/path/to/workspace"
}
```

Notes:

- The backend rejects deletion while the session is `queued`, `running`, or `cancelling`.
- The session is always removed from `devs_display/.storage/session_registry.json`.
- Automatically-created `session_workspace_*` workspaces are deleted from disk.
- For manually supplied workspaces, only that session's `.devs_display_sessions/sessions/{storage_session_id}` directory is deleted, so externally managed source files are not removed by accident.

## Project APIs

### `GET /sessions/{session_id}/projects`

Lists projects in a session.

Response:

```json
{
  "projects": []
}
```

### `POST /sessions/{session_id}/projects`

Creates or uploads a project into a session.

Request:

```json
{
  "display_name": "hospital_model",
  "files": {
    "system_model_info.json": "{...}",
    "model.py": "..."
  }
}
```

Response:

```json
{
  "project": {}
}
```

Notes:

- The backend generates `project_id`.
- The backend should increment the project `version` after the upload is stored.
- After a chat request completes, the backend scans changed workspace areas recursively. A folder is auto-registered as a project only when that folder contains `_analysis_logs/`. Registry files such as `_analysis_logs/system_registry_v1_post_build.json` are preferred metadata, but the directory marker is the boundary signal. Source-only xDEVS folders are not auto-registered because coupled-model subfolders can otherwise be mistaken for separate projects; source-only projects can still be added through the upload API.
- Project identity is tracked by `project_id` and `path`, not by `display_name`.

### `POST /sessions/{session_id}/projects:clone`

Clones one or more projects into an existing session.

Request:

```json
{
  "clone_projects": [
    {
      "source_session_id": "sess_source",
      "source_project_id": "proj_source",
      "source_version": 2,
      "display_name": "copied_project"
    }
  ]
}
```

Response:

```json
{
  "projects": []
}
```

Notes:

- `clone_projects` is a list of source project descriptors.
- The backend should create one new `project_id` per cloned source project.
- `source_version` is optional and reserved for future versioned snapshots.

### `GET /sessions/{session_id}/projects/{project_id}`

Returns project metadata.

Response:

```json
{
  "project": {}
}
```

### `GET /sessions/{session_id}/projects/{project_id}/files`

Returns all readable project files.

Response:

```json
{
  "files": {
    "system_model_info.json": "{...}",
    "model.py": "..."
  },
  "project": {
    "project_id": "proj_01hxyz",
    "version": 3,
    "status": "ready"
  },
  "session_status": "idle"
}
```

Notes:

- Paths are relative to the project root.
- Binary or unreadable files can be returned as `"[Binary Content]"`.

### `GET /sessions/{session_id}/projects/{project_id}/graph`

Returns the cached graph parse result for one project. If no cache exists and `start_if_missing=true`, the backend starts parsing and immediately returns `parse.status = "running"`.

Query parameters:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `start_if_missing` | boolean | `true` | Start backend parsing if no cached graph exists. |

Response while parsing:

```json
{
  "parse": {
    "status": "running",
    "started_at": "2026-06-11T15:10:00Z",
    "completed_at": null,
    "error": null,
    "provider": "openai",
    "model": "openrouter/openai/gpt-5.4-mini"
  },
  "graph": null
}
```

Response when no cache exists and `start_if_missing=false`:

```json
{
  "parse": {
    "status": "missing"
  },
  "graph": null
}
```

Response after successful parsing:

```json
{
  "parse": {
    "status": "completed",
    "started_at": "2026-06-11T15:10:00Z",
    "completed_at": "2026-06-11T15:10:02Z",
    "error": null,
    "provider": "openai",
    "model": "openrouter/openai/gpt-5.4-mini",
    "root_model": "ExampleQueueModel",
    "node_count": 6,
    "link_count": 7
  },
  "graph": {
    "root_model": "ExampleQueueModel",
    "nodes": [
      {
        "id": "root",
        "name": "ExampleQueueModel",
        "className": "ExampleQueueModel",
        "type": "coupled",
        "parent": null,
        "expanded": true,
        "fixed": false,
        "x": 0,
        "y": 0,
        "width": 800,
        "height": 600,
        "ports": {
          "inputs": [],
          "outputs": ["kpi_report"]
        },
        "children": ["root/queue_system", "root/simulation_runner"]
      }
    ],
    "links": [
      {
        "id": "link-root-0",
        "source": "root/queue_system",
        "sourcePort": "kpi_report",
        "target": "root/simulation_runner",
        "targetPort": "kpi_in"
      }
    ]
  }
}
```

Parse statuses:

| Status | Meaning |
| --- | --- |
| `missing` | No cached parse result exists and no parse was started. |
| `running` | Backend is parsing project files and writing the cache. |
| `completed` | `graph` is available and can be rendered directly by the frontend. |
| `failed` | Parsing failed; see `parse.error`. |

Graph node fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable visual node ID. Root is `root`; child IDs use slash paths like `root/queue_system/server`. |
| `name` | Instance name shown in the graph. |
| `className` | Python model class name. |
| `type` | `coupled` or `atomic`; only coupled nodes are expandable. |
| `parent` | Parent node ID, or `null` for root. |
| `expanded` | Initial expansion state. |
| `ports.inputs` / `ports.outputs` | Port names rendered on the node. |
| `children` | Child node IDs. |

Graph link fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable visual link ID. |
| `source` / `target` | Source and target node IDs. |
| `sourcePort` / `targetPort` | Port names used by the coupling. |

Backend parsing behavior:

- When `OPENROUTER_API_KEY` or an explicit `api_key` is available, backend graph parsing tries the configured OpenRouter/OpenAI-compatible model first.
- Coupled model classes are parsed in parallel before graph assembly. Each class is parsed once per graph build; multiple instances of the same class reuse that parsed structure.
- If the LLM call times out, returns invalid JSON, fails schema validation, or raises another error, the backend falls back to the deterministic local parser.
- The graph parse timeout defaults to 240 seconds and can be changed with `DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS`.
- The graph parse LLM concurrency defaults to 6 workers and can be changed with `DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS` (`1` disables parallel LLM calls; values above `16` are capped).
- The frontend should poll this endpoint while `parse.status = "running"`.

### `POST /sessions/{session_id}/projects/{project_id}/graph:parse`

Forces or starts backend graph parsing for one project.

Request:

```json
{
  "provider": "openai",
  "model": "openrouter/openai/gpt-5.4-mini",
  "api_key": "optional-key-from-frontend",
  "force": true
}
```

Response:

Same shape as `GET /sessions/{session_id}/projects/{project_id}/graph`.

Notes:

- `force=true` overwrites any cached graph parse state and starts a fresh parse.
- `api_key` is optional. If omitted, deterministic parsing still runs. Fallback model parsing only runs when a key is supplied.
- The backend must not return `api_key` in any response.

## Chat and History APIs

### `GET /sessions/{session_id}/messages`

Returns conversation messages.

Query parameters:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | integer | `5` | Number of messages to return. |
| `before` | string | null | Optional cursor/message ID for older messages. |
| `order` | string | `desc` | `desc` for latest first, `asc` for oldest first. |

Response:

```json
{
  "messages": [],
  "next_before": "msg_..."
}
```

### `POST /sessions/{session_id}/chat`

Adds a user message and starts backend processing.

Request:

```json
{
  "content": "Modify the selected project...",
  "active_project_id": "proj_01hxyz",
  "include_project_context": false,
  "idempotency_key": "frontend-generated-key",
  "generation_mode": "guided"
}
```

Immediate response:

```json
{
  "request": {},
  "user_message": {}
}
```

Behavior:

- This endpoint must return quickly.
- If the session already has a queued, running, or waiting-for-user request, return `409 Conflict`.
- `idempotency_key` is optional but recommended. If the frontend retries the same submission, the backend should return the existing request instead of creating a duplicate.
- `generation_mode` is either `guided` or `automatic`. The UI calls these modes **Interactive** and **Automatic**, respectively, and sends `guided` by default. A raw API client that omits this field gets `automatic` for wire compatibility; older stored requests without the field are also treated as `automatic` so they retain their original behavior.
- Both modes use the same interpretation, architecture, private detail-planning, build, verification, and publication pipeline. Automatic mode accepts both review artifacts without pausing. Interactive mode creates no model source code until the student has approved the intent and architecture reviews.
- `active_project_id` identifies the frontend's selected project. It does not limit the agent's scope; the agent may operate on any relevant files in the session workspace.
- `include_project_context=false` means the selected project is only UI state and is not injected into the agent prompt. Set it to `true` only when the user explicitly wants the selected project added as context.
- The backend includes recent visible chat history in the agent prompt so context survives page refresh and backend restart.
- The backend runs the request with the agent instance bound to the session's `workspace_path`.
- The agent should run in a background worker.
- The final assistant message is written to session history when the request completes.

Error responses:

| Status | Meaning |
| --- | --- |
| `404` | Session or active project was not found. |
| `409` | Session already has an active generation request. |

### `GET /sessions/{session_id}/requests/{request_id}`

Returns execution status for one chat request.

Use this endpoint to poll or restore the backend state for a user instruction after `POST /sessions/{session_id}/chat` returns. It does not return the full conversation history; use `GET /sessions/{session_id}/messages` for messages.

Page reload recovery:

1. Call `GET /sessions`.
2. If the selected session has `active_request_id`, call this endpoint with that request ID.
3. If the returned request status is `queued` or `running`, the frontend should show the active processing UI and continue polling. If it is `waiting_for_user`, render `pending_interaction` and stop background activity indicators until the user responds.
4. If the returned request status is terminal, refresh messages and projects as needed.

Response:

```json
{
  "request": {
    "request_id": "req_01hxyz",
    "session_id": "sess_01hxyz",
    "status": "running",
    "user_message_id": "msg_user",
    "assistant_message_id": null,
    "active_project_id": "proj_01hxyz",
    "include_project_context": false,
    "generation_mode": "guided",
    "phase": "interpret_intent",
    "phase_started_at": "2026-06-11T11:31:02Z",
    "pending_interaction": null,
    "interactions": [],
    "updated_project_ids": [],
    "updated_project_names": [],
    "started_at": "2026-06-11T11:31:02Z",
    "completed_at": null,
    "cancel_requested_at": null,
    "error": null
  }
}
```

Frontend-relevant fields:

| Field | Meaning |
| --- | --- |
| `status` | Whether the request is `queued`, `running`, `waiting_for_user`, `completed`, `failed`, or `cancelled`. |
| `generation_mode` | `guided` (shown as **Interactive**) or `automatic` (shown as **Automatic**). The UI sends `guided` by default; omitted raw API values and older stored requests default to `automatic`. |
| `phase` | Current workflow phase: `interpret_intent`, `plan_structure`, or `build`. |
| `pending_interaction` | The open intent or structure review when `status=waiting_for_user`; otherwise `null`. |
| `interactions` | Durable history of review revisions and resolutions for recovery and audit. |
| `assistant_message_id` | Final assistant message ID once available. |
| `updated_project_ids` / `updated_project_names` | Projects detected as changed by this request. |
| `error` | Failure reason when `status` is `failed`. |

### Interactive generation reviews

The UI calls this mode **Interactive**; its API wire value is `guided`.
Interactive and Automatic generation share one pipeline. Interactive mode exposes
two persisted checkpoints before source generation, while Automatic mode accepts
the same artifacts without waiting:

1. `interpret_intent`: a concise, editable interpretation of the user's request.
2. `plan_structure`: the DEVS component hierarchy, model kinds, and responsibilities that the detailed plan must preserve.

Both checkpoints are stored under the session's private request-artifact area.
The request contains a bounded public projection in `pending_interaction`. After
architecture approval, the backend privately derives ports, protocols, and
couplings, links that detailed plan to the approved architecture digest, and
verifies that the approved topology did not change. A page close or backend
restart preserves `status=waiting_for_user` without rerunning planning,
generating source, or advancing the request. The UI should label this state
**Review needed**.

Intent interaction example:

```json
{
  "interaction_id": "int_intent",
  "kind": "intent_review",
  "phase": "interpret_intent",
  "status": "open",
  "revision": 1,
  "artifact_id": "artifact_intent",
  "artifact_digest": "sha256...",
  "created_at": "2026-06-11T11:31:10Z",
  "prompt": "Review how the generator understood your request.",
  "payload": {
    "summary": "A three-stage supply-chain simulation.",
    "root_model_name": "SupplyChain",
    "project_folder": "supply_chain_sim",
    "requirements": "Model demand, replenishment, and lead times.",
    "assumptions": ["Demand follows a Poisson process."],
    "entities": ["Factory", "Distributor", "Retailer"],
    "event_flow": ["Demand arrives", "Retailer reorders", "Factory ships"],
    "parameters": ["Demand rate", "Lead time"],
    "metrics": ["Stockouts", "Average inventory"],
    "questions": [
      {
        "question_id": "demand_model",
        "prompt": "How should demand arrive?",
        "required": true,
        "recommended_value": "poisson",
        "options": [
          {
            "value": "poisson",
            "label": "Poisson arrivals",
            "description": "Random independent arrivals at a configurable rate.",
            "recommended": true
          },
          {
            "value": "scheduled",
            "label": "Scheduled arrivals"
          }
        ]
      }
    ]
  }
}
```

The intent projection is a compact simulation brief. It may contain at most four
questions. Each question has a stable `question_id`, a prompt, and a `required`
flag. It may also provide a recommended value and a finite list of choices. A
choice answer is submitted using the option's `value`, not its display label.

Structure interaction example:

```json
{
  "interaction_id": "int_structure",
  "kind": "structure_review",
  "phase": "plan_structure",
  "status": "open",
  "revision": 1,
  "artifact_id": "artifact_structure",
  "artifact_digest": "sha256...",
  "created_at": "2026-06-11T11:32:10Z",
  "prompt": "Confirm the model architecture before code is generated.",
  "payload": {
    "title": "SupplyChain",
    "summary": "A root model containing a factory and distributor.",
    "root_model_name": "SupplyChain",
    "root_node_id": "SupplyChain",
    "component_count": 3,
    "review_scope": "component_hierarchy",
    "review_scope_complete": true,
    "connections_defined": false,
    "components": [
      {
        "id": "SupplyChain",
        "name": "SupplyChain",
        "model_type": "coupled",
        "description": "Coordinates production and distribution.",
        "parent_id": null,
        "input_ports": [],
        "output_ports": []
      },
      {
        "id": "SupplyChain.Factory",
        "name": "Factory",
        "model_type": "atomic",
        "description": "Produces replenishment shipments.",
        "parent_id": "SupplyChain",
        "input_ports": [],
        "output_ports": []
      },
      {
        "id": "SupplyChain.Distributor",
        "name": "Distributor",
        "model_type": "atomic",
        "description": "Receives and forwards inventory.",
        "parent_id": "SupplyChain",
        "input_ports": [],
        "output_ports": []
      }
    ],
    "connections": [],
    "omitted_coupling_count": 0,
    "omitted_connection_count": 0,
    "truncated_component_count": 0,
    "truncated_connection_count": 0,
    "is_complete": true,
    "assumptions": []
  }
}
```

Structure fields:

| Field | Meaning |
| --- | --- |
| `root_node_id` | Stable ID of the proposed root component. |
| `review_scope` | `component_hierarchy` for the pre-build architecture review. |
| `review_scope_complete` | Whether every proposed component is present in the bounded public projection. |
| `connections_defined` | `false` at architecture review because ports and couplings are refined only after approval. |
| `components` | Bounded component hierarchy, including each component's parent, model kind, and responsibility. Port arrays are empty at this checkpoint. |
| `connections` | Empty at architecture review. The private detail pass derives EIC/IC/EOC couplings after approval. |
| `omitted_coupling_count` | Zero for the hierarchy-only review because couplings have not been defined yet. |
| `truncated_component_count` / `truncated_connection_count` | Valid items omitted only because the public projection reached its size bound. |
| `omitted_connection_count` | Total omitted or truncated connections. It is normally zero for the hierarchy-only review. |
| `is_complete` | `true` only when the public projection contains the complete component hierarchy being approved. |

An incomplete hierarchy projection remains useful for inspection, but the UI
must not offer ordinary **Approve architecture and generate** confirmation. The
student must request a revision before approving the architecture.

### `GET /sessions/{session_id}/requests/{request_id}/artifacts/{artifact_id}`

Returns the bounded public projection and its **review digest**. It never returns
the backend's private plan representation or its build digest. The response
digest is the value the client must echo when resolving this review.

```json
{
  "artifact_id": "artifact_structure",
  "kind": "structure_review",
  "revision": 1,
  "digest": "sha256-review...",
  "public": {}
}
```

### `POST /sessions/{session_id}/requests/{request_id}/interactions/{interaction_id}:resolve`

Confirms, revises, cancels, or switches the remainder of an Interactive request
to Automatic mode.

```json
{
  "action": "confirm",
  "artifact_digest": "sha256...",
  "answers": {"demand_model": "poisson"},
  "feedback": null,
  "edited_intent": null,
  "idempotency_key": "frontend-resolution-key"
}
```

Behavior:

- `confirm` advances intent review to architecture planning, or architecture review to private detail planning and then code generation. The UI labels these actions **Continue to architecture** and **Approve architecture and generate**.
- `revise` reruns only the current side-effect-free review phase using the supplied feedback or edited intent. No generated model source is changed during either review phase.
- `continue_automatically` accepts the current artifact, changes this request to Automatic mode, and disables later pauses. The UI offers this action only at intent review, where it clearly means skipping the later architecture review. Raw API clients may also use it at structure review as an explicit escape hatch for an incomplete bounded projection.
- `cancel` ends the request without generating additional source.
- Non-cancel actions must echo the **review digest** from `pending_interaction.artifact_digest` or the artifact GET response. A mismatched digest or stale interaction returns `409 Conflict` instead of accepting a review the user did not see.
- Intent `confirm` and `continue_automatically` reject unknown question IDs, missing or blank answers to required questions, and values outside a question's declared `options`. When answers or `edited_intent` change the brief, the backend persists and approves a new derived intent artifact rather than silently mutating the displayed artifact.
- Ordinary structure `confirm` is rejected when `payload.is_complete=false`. `continue_automatically` is allowed because it explicitly records that the student chose to proceed without another review pause.
- `idempotency_key` makes retries safe. Reusing a key with different response content returns `409 Conflict`.
- Confirmed architecture generation consumes the exact persisted hierarchy artifact. The backend derives a private detailed plan, binds it to the approved architecture digest, and rejects it if component identity, type, containment, or order changed before creating source files.

The two digests have deliberately different jobs:

| Digest | Used for | Client responsibility |
| --- | --- | --- |
| Review digest | Binds the public projection, artifact identity, kind, and revision that the student actually saw. It appears as `pending_interaction.artifact_digest` and as `digest` from the artifact GET endpoint. | Echo it when resolving the interaction. |
| Build digest | Binds the complete private intent or approved architecture artifact consumed by the backend. The derived detailed plan records the approved architecture digest and is topology-checked before building. | None. Do not use it as a review response token. |

This separation lets the UI confirm a bounded, safe-to-display proposal while
the constructor remains pinned to the approved architecture and a validated
detailed expansion of it.

Error responses:

| Status | Meaning |
| --- | --- |
| `400` | Required answers are missing, a choice value is invalid, the review digest is absent, or ordinary confirmation was attempted for an incomplete structure projection. |
| `404` | Request, interaction, or artifact was not found. |
| `409` | Interaction or review digest is stale, or an idempotency key was reused with different response content. |

### `GET /sessions/{session_id}/events`

Returns progress events for a session or request.

Query parameters:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `after` | integer | `0` | Return events with `event_id > after`. |
| `request_id` | string | null | Filter events to one request. |
| `limit` | integer | `100` | Max events to return. |

Response:

```json
{
  "events": [],
  "next_after": 42,
  "request_status": "running"
}
```

### `POST /sessions/{session_id}/requests/{request_id}/cancel`

Withdraws a queued request or cancels a request paused at an Interactive review.

Request:

```json
{
  "withdraw_user_message": true
}
```

Fields:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `withdraw_user_message` | boolean | `true` | If the request is `queued` or `waiting_for_user`, mark the user message as `withdrawn` and hide it by default in the UI. |

Response:

```json
{
  "request": {},
  "user_message": {}
}
```

Behavior:

- If the request is `queued` or `waiting_for_user`, cancellation is immediate. The request becomes `cancelled`; the user message can become `withdrawn`.
- If the request is `running`, return `409 Conflict`. Running-request termination is intentionally out of scope for the MVP.
- If the request already completed, failed, or was cancelled, return the current request state without creating another cancellation.

## Future Versioning

The first implementation should not manage git repositories or rollback history. It should only maintain a simple integer `version` on project metadata and increment it after upload, clone, or successful chat modifications.

Optional future APIs:

```text
GET  /sessions/{session_id}/projects/{project_id}/versions
GET  /sessions/{session_id}/projects/{project_id}/versions/{version}/diff
POST /sessions/{session_id}/projects/{project_id}/versions/{version}:restore
```

The frontend should treat `version` as informational until these APIs exist.

## Migration From Existing Projects

Because the legacy top-level project API is removed from the target design, existing project directories should be imported into a session before the new frontend is used.

Recommended migration options:

1. Create a `base` session during backend startup if no sessions exist, importing every existing project from the old working directory.
2. Provide a one-time CLI or admin function that imports selected old projects into a new session.
3. Let users create a blank session and upload projects through `POST /sessions/{session_id}/projects`.

After migration, cloning should always use `source_session_id.source_project_id`, optionally with `source_version`.

## Stability Guidelines

- Do not keep chat submission HTTP requests open until the agent finishes.
- Store progress by `session_id` and `request_id`, not in a global "current progress" field.
- Keep a bounded event buffer or persist events to JSONL/SQLite.
- Prefer explicit progress logging hooks. Avoid process-wide `sys.stdout` redirection unless carefully isolated.
- The frontend should render backend session state and poll messages/events/requests.
- One-second polling is enough for local development and is more robust than depending on one streaming connection.
