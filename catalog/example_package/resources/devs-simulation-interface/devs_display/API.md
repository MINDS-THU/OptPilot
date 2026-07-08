# devs_display API

This document defines the target API for the session-based devs_display frontend and backend.

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
| `HAMLET_DISPLAY_PASSWORD` | Backward-compatible alternative password variable. |
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
    "openai": true,
    "gemini": false
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
| `cancelling` | Reserved for future running-request cancellation. Not used in the MVP. |
| `completed` | Final assistant response is available. |
| `failed` | Request failed. |
| `cancelled` | Request was withdrawn before execution or stopped during execution. |

### Event

```json
{
  "event_id": 42,
  "session_id": "sess_01hxyz",
  "request_id": "req_01hxyz",
  "type": "agent_log",
  "content": "Agent step output...",
  "created_at": "2026-06-11T11:31:30Z"
}
```

Recommended event types:

| Type | Meaning |
| --- | --- |
| `request_started` | Backend accepted the user request. |
| `agent_started` | Agent execution began. |
| `agent_log` | Agent emitted progress text. |
| `tool_started` | Agent/tool work began. |
| `tool_finished` | Agent/tool work completed. |
| `files_changed` | Backend detected project file changes. |
| `assistant_message` | Final assistant response was stored. |
| `request_cancel_requested` | User requested cancellation. |
| `request_cancelled` | Request was cancelled or withdrawn. |
| `request_failed` | Request failed. |
| `request_completed` | Request completed successfully. |

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
  "idempotency_key": "frontend-generated-key"
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
- If the session already has a queued or running request, return `409 Conflict`.
- `idempotency_key` is optional but recommended. If the frontend retries the same submission, the backend should return the existing request instead of creating a duplicate.
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
| `409` | Session already has a queued or running request. |

### `GET /sessions/{session_id}/requests/{request_id}`

Returns execution status for one chat request.

Use this endpoint to poll or restore the backend state for a user instruction after `POST /sessions/{session_id}/chat` returns. It does not return the full conversation history; use `GET /sessions/{session_id}/messages` for messages.

Page reload recovery:

1. Call `GET /sessions`.
2. If the selected session has `active_request_id`, call this endpoint with that request ID.
3. If the returned request status is `queued` or `running`, the frontend should show the active processing UI and continue polling.
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
| `status` | Whether the request is `queued`, `running`, `completed`, `failed`, or `cancelled`. |
| `assistant_message_id` | Final assistant message ID once available. |
| `updated_project_ids` / `updated_project_names` | Projects detected as changed by this request. |
| `error` | Failure reason when `status` is `failed`. |

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

Withdraws a queued request.

Request:

```json
{
  "withdraw_user_message": true
}
```

Fields:

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `withdraw_user_message` | boolean | `true` | If the request is still `queued`, mark the user message as `withdrawn` and hide it by default in the UI. |

Response:

```json
{
  "request": {},
  "user_message": {}
}
```

Behavior:

- If the request is `queued`, cancellation is immediate. The request becomes `cancelled`; the user message can become `withdrawn`.
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
