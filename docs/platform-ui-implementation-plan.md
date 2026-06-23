---
title: Platform UI Implementation Plan
description: Concrete plan for turning the OptPilot Studio demo into a professional workspace, catalog, run-inspection, and OpenHands-backed assistant interface.
---

# Platform UI Implementation Plan

This plan turns the current Studio prototype into the target product model:

```text
Catalog / Studies / Runs are OptPilot pages.
Assistant is a global sidecar that can open on any page.
Workspaces are persistent code folders that can be inspected, edited, tested,
and registered to the catalog.
```

The important change is that a workspace is not inherently an environment or a
method. A workspace is just a project folder. One workspace may later be
registered as one environment, one method, several environments, several
methods, a study collection, or none of those.

## Current State

The current local UI already has useful pieces:

- `src/optpilot/ui/server.py` exposes catalog, compatibility, study draft,
  study launch, jobs, runs, run files, code-server status, and code-server
  open/start endpoints.
- `src/optpilot/ui/static/app.js` renders Catalog, Studies, Runs, an embedded
  code-server editor, workspace cards, and a prototype assistant panel.
- `optpilot validate` already validates public `environment`, `method`, and
  `study` configs through `validate_authoring_config`.
- Run directories already have structured evidence: `summary.json`,
  `study_spec.json`, `observations.jsonl`, `trials.jsonl`, `candidates.jsonl`,
  method events, scheduler events, and files.
- code-server is correctly treated as an editor service. It should remain
  separate from the assistant runtime.

The current local UI has already addressed the first demo mismatches:

- Workspace records are persisted under `.optpilot-ui/workspaces/` and can
  represent blank, catalog, and run workspaces.
- New workspace creates a generic project folder, not a new environment.
- The workspace action is `Register to Catalog`, so one workspace can later map
  to one or more catalog entries.
- The separate-window action is labeled `Open Separate Window`; selecting a
  workspace opens the embedded editor canvas.
- Assistant sessions, messages, workspace attachments, and context packets are
  persisted under `.optpilot-ui/agent_sessions/`.

The remaining gaps are the production OpenHands dispatch path, safe file and
shell tools scoped to attached workspaces, streaming tool/event updates, richer
workspace tooling, and production isolation for users, secrets, and quotas.

## Target Mental Model

### Pages

Use only three primary product pages in the left navigation:

- **Catalog**: reusable registered environments, methods, and study configs.
- **Studies**: study plans, compatibility review, and launch approval.
- **Runs**: live/completed studies, evidence, artifacts, logs, and analysis.

Add a secondary **Add-ons** page once the core workflow is stable. Add-ons are
agent-facing capabilities and context packages, not OptPilot-run study
components. They include MCP servers, OpenHands custom tools, skills,
knowledge bases, reusable prompt/instruction bundles, visualization launchers,
dataset profilers, and repository-backed utilities such as
`resource/devs_display_new`.

The editor is not a page tab. It is the central canvas opened by selecting a
workspace from the left rail. The user can move between pages while the selected
workspace and assistant remain available.

### Assistant

Assistant is a global sidecar, not a peer of Catalog, Studies, or Runs.

Visually:

- Use a light-color assistant launcher pane in the left rail, separated from the
  dark page navigation.
- The launcher should visually extend toward the content border, so it hints
  that clicking it expands a side pane.
- The expanded assistant panel opens beside the active page and can be resized.
- The panel subtitle should show current page context, for example
  `Catalog context`, `Runs context`, or `Workspace context`.

Behavior:

- Assistant can open on Catalog, Studies, Runs, or the editor canvas.
- Assistant receives the same high-level state the user sees: current page,
  selected catalog entry, selected study plan, selected run, selected workspace,
  attached workspaces, registration menu state, and recent validation results.
- Session list lives inside the assistant panel, similar to GitHub Copilot chat.
- Switching assistant session switches conversation history and suggested
  attached workspace context, but does not delete or own workspaces.

### Workspaces

Workspaces are persistent project folders. They are independent from assistant
sessions, but sessions can attach them as context.

Workspace source types:

- blank workspace
- cloned Git repository
- uploaded/local folder
- add-on inspection workspace
- catalog inspection workspace
- editable catalog copy
- generated workspace
- run workspace
- study-plan workspace

Workspace display fields:

- title
- path
- source type
- mode: `editable`, `read-only`, or `analysis`
- open status
- registration badges, such as `environment: job-shop-dispatch-rule` or
  `method: baseline-file-copy`
- selected focus path, such as the active config, evaluator, method entrypoint,
  or run artifact

Workspace list styling should be calm and readable. Avoid heavy bold text for
every line. Use normal-weight titles, small badges, and restrained status color.

### Attachment And Access Control

Attaching a workspace to an assistant session means:

- add the workspace path, title, description, registration badges, focus paths,
  and recent validation state to the session context packet
- allow the assistant to use tools inside that workspace root
- make the workspace easy for the user to switch to in the editor canvas

It does not mean the workspace is owned by that assistant session. Closing a
workspace from the sidebar should detach it from the current assistant session
only. It must not delete files. A separate archive/delete action can be added
later, with stronger confirmation and a clear distinction between "detach from
this session" and "remove workspace files from disk".

Access control must be enforced by the tool layer, not only by prompt
instructions. Every file-editing, shell, registration, and inspection tool
should receive an allowlist of attached workspace roots and reject paths outside
those roots. For read-only workspaces, tools may inspect files but must reject
write operations. Registration and study-launch tools may read catalog and run
metadata through OptPilot APIs, but direct filesystem access should still be
workspace-scoped.

### Catalog Edit Policy

The first implementation should use a simple policy:

- built-in examples and packaged resources open as read-only inspection
  workspaces
- user catalog entries can open as editable workspaces
- any read-only catalog entry can be turned into an editable copy under
  `.optpilot-ui/workspaces/`

This lets students inspect examples safely while still making user-owned
catalog entries straightforward to modify. For higher-stakes deployments, use
editable copies for all catalog changes and apply them through
`Register to Catalog` after validation and diff review.

### Code Editor

Selecting a workspace focuses that folder in the embedded code-server editor.

The separate-window action is labeled **Open Separate Window**. This action
means:

```text
Open the selected workspace folder in a new browser window backed by code-server.
```

It is different from embedded code-server, which is the default editor canvas.

The first implementation can continue to use code-server's `?folder=` URL. A
future enhancement can focus a specific file by using a code-server extension,
VS Code command URI, or a small local bridge if code-server supports the needed
file-focus behavior reliably.

Do not build a custom dirty-file detector in OptPilot. code-server is VS Code in
the browser, and VS Code already has Hot Exit behavior for unsaved changes.
OptPilot should avoid forcing iframe unloads when possible and should rely on
code-server/VS Code for editor-close or browser-close handling. If the user
clicks "detach workspace" while the embedded editor is focused, OptPilot can
show a lightweight reminder that detaching does not delete files and that
unsaved editor buffers are managed by code-server.

## Registration Workflow

Replace workspace-level **Validate** and **Promote** with one action:

```text
Register to Catalog
```

Clicking it opens the assistant panel and a structured registration menu. This
menu is both human-editable and agent-readable.

### Registration Menu

The menu has five steps:

1. **Discover configs**
   Scan the workspace for YAML files with:
   `apiVersion: optpilot.io/v1` and `config: environment | method | study`.

2. **Select targets**
   Let the user choose one or more configs to register. A workspace can register
   multiple catalog entries.

3. **Validate**
   Run `validate_authoring_config` on each selected config. For study configs,
   this compiles the study and checks environment/method compatibility.

4. **Review files**
   Build a registration manifest that includes only the selected config and
   required implementation/assets. Do not copy the whole workspace by default.
   Show the destination path and diff.

5. **Register**
   Copy the selected files into the chosen `user_catalog/` destination, refresh
   catalog scanning, and update workspace badges.

### Registration Data Shape

Persist registration state as JSON so the assistant can observe and modify it:

```json
{
  "id": "reg_...",
  "workspace_id": "ws_...",
  "status": "draft",
  "targets": [
    {
      "target_id": "target_...",
      "kind": "environment",
      "config_path": "environment.yaml",
      "catalog_id": "job-shop-dispatch-rule",
      "destination": "user_catalog/environments/job_shop_dispatch_rule",
      "focus_paths": ["environment.yaml", "evaluator.py"],
      "include": ["environment.yaml", "evaluator.py", "assets/**", "prompts/**"],
      "exclude": [".venv/**", "runs/**", ".git/**", "__pycache__/**"],
      "validation": {"valid": true, "errors": []}
    }
  ]
}
```

The assistant should be able to call the same registration endpoints as the UI:
discover configs, propose targets, validate, edit include/exclude lists, explain
errors, and apply registration after approval.

### Multiple Entries In One Codebase

A single codebase can contain:

- several environment configs using one evaluator
- several method configs using one implementation
- both environments and methods
- study configs binding entries inside the same repo

The catalog should treat each valid config file as one catalog entry. Entries
can share a `workspace_root` and `source_repository`.

Opening a catalog entry should:

1. open the shared workspace root
2. focus the selected config file
3. surface entry-specific focus files:
   - environment: `evaluator.python`, `evaluator.adapter`, command script,
     `methodContext.references`, relevant assets
   - method: `entrypoint.python`, command script, `produces`, `accepts`
   - study: study YAML plus referenced environment and method configs

If precise file focus is not available in code-server yet, the UI should show a
small "entry focus" strip above the editor with buttons for the config and
entrypoint files.

## Run Workspaces

Runs should be openable as workspaces.

From the Runs page, add:

```text
Open Run Workspace
```

This creates or attaches a read-only analysis workspace rooted at the run
directory. The assistant can then inspect the exact evidence files, artifacts,
candidate folders, trial workspaces, logs, and summaries for that run.

Run workspace metadata:

```json
{
  "source_type": "run",
  "mode": "analysis",
  "root": "runs/<run-id>",
  "focus_paths": [
    "summary.json",
    "observations.jsonl",
    "candidates.jsonl",
    "trials"
  ],
  "registration_enabled": false
}
```

If the user wants to create an analysis notebook, repair script, or report, make
an editable derived workspace that references the original run instead of
writing into the run folder.

## Backend Implementation

### Persistent State

Add durable state under `.optpilot-ui/`:

```text
.optpilot-ui/
  workspaces/
    index.json
    ws_<id>/
      workspace.json
      workspace/
      registrations/
        reg_<id>.json
      events.jsonl
  agent_sessions/
    as_<id>/
      session.json
      messages.jsonl
      events.jsonl
      attached_workspaces.json
  openhands/
    profile.json
    events/
```

`workspace.json` should be the source of truth for workspace metadata:

```json
{
  "id": "ws_...",
  "title": "Job shop experiments",
  "root": ".optpilot-ui/workspaces/ws_.../workspace",
  "source_type": "blank|git|catalog|run|local|generated",
  "mode": "editable|read-only|analysis",
  "attached_sessions": ["as_..."],
  "registered_entries": [
    {"kind": "environment", "id": "job-shop-dispatch-rule", "config_path": "environment.yaml"}
  ],
  "focus_paths": ["environment.yaml"],
  "created_at": "...",
  "updated_at": "..."
}
```

### New API Endpoints

Add workspace APIs:

```text
GET  /api/workspaces
POST /api/workspaces
GET  /api/workspaces/{workspace_id}
POST /api/workspaces/{workspace_id}/attach
POST /api/workspaces/{workspace_id}/detach
POST /api/workspaces/{workspace_id}/close
POST /api/workspaces/{workspace_id}/open-code
POST /api/workspaces/{workspace_id}/open-code-window
POST /api/workspaces/{workspace_id}/discover-configs
POST /api/workspaces/{workspace_id}/registrations
GET  /api/workspaces/{workspace_id}/registrations/{registration_id}
POST /api/workspaces/{workspace_id}/registrations/{registration_id}/validate
POST /api/workspaces/{workspace_id}/registrations/{registration_id}/apply
```

Add catalog/run workspace entry points:

```text
POST /api/catalog/{kind}/{uid}/open-workspace
POST /api/catalog/{kind}/{uid}/edit-copy
POST /api/runs/{run_id}/open-workspace
```

Add assistant APIs:

```text
GET  /api/agent-sessions
POST /api/agent-sessions
GET  /api/agent-sessions/{session_id}
GET  /api/agent-sessions/{session_id}/events
POST /api/agent-sessions/{session_id}/message
POST /api/agent-sessions/{session_id}/cancel
POST /api/agent-sessions/{session_id}/attach-workspace
POST /api/agent-sessions/{session_id}/detach-workspace
```

Event streaming can start with Server-Sent Events. If OpenHands event bridging
needs bidirectional control, add WebSocket later.

### Catalog Scanner Updates

Keep the current catalog scanner, but enrich entries with:

- `root_dir`
- `config_path`
- `focus_paths`
- `source_workspace_id`
- `source_repository`
- `registered_at`
- `variant_group`

Do not require a one-folder-one-entry assumption. The config file is the catalog
entry. The folder is the implementation root.

## Add-ons

Add-ons are reusable resources that help the assistant do work, but are not
themselves environment, method, or study entries.

Examples:

- MCP servers that expose formal callable tools
- OpenHands custom tools implemented by OptPilot or a third-party package
- Codex/OpenHands-style skills or instruction bundles
- knowledge bases such as papers, API docs, benchmark descriptions, and domain
  notes
- repository-backed utilities that the agent can inspect and run, even if they
  are not packaged as official agent tools
- local scripts, CLIs, notebooks, or simulator generators such as
  `resource/devs_display_new`

The Add-ons page should let users browse, register, inspect, test, enable, and
disable these resources. Registration means "make this available to the
assistant with clear usage instructions and safety policy"; it does not mean
"copy into the environment/method catalog."

### Add-on Data Shape

Represent add-ons with a small manifest:

```json
{
  "id": "devs-display-new",
  "name": "DEVS Display Generator",
  "kind": "repo_tool",
  "source": {
    "type": "local_path",
    "path": "resource/devs_display_new"
  },
  "description": "Generate and inspect xDEVS simulator projects.",
  "usage": {
    "summary": "Use when a user requests a discrete-event simulator or DEVS-style visualization.",
    "entrypoints": ["README.md", "examples/", "scripts/generate.py"],
    "commands": [
      {
        "name": "generate simulator",
        "cwd": "resource/devs_display_new",
        "command": "python scripts/generate.py --help"
      }
    ]
  },
  "agent_exposure": {
    "mode": "context_and_shell",
    "context_files": ["README.md", "docs/**/*.md"],
    "allowed_roots": ["resource/devs_display_new"],
    "write_policy": "read_only",
    "network_policy": "inherit_platform"
  },
  "status": "enabled",
  "created_at": "...",
  "updated_at": "..."
}
```

`kind` should stay open-ended. Initial values can include:

- `mcp_server`
- `openhands_tool`
- `skill`
- `knowledge_base`
- `repo_tool`
- `local_cli`
- `preview_adapter`

### Add-on Registration Flow

The registration UI should be similar to catalog registration, but it produces
an assistant capability manifest instead of an OptPilot config:

1. **Source**
   Pick a local folder, Git URL, uploaded archive, MCP server config, or
   knowledge-base folder.

2. **Inspect**
   Let the assistant or user summarize what the add-on does, what files are
   important, what commands are safe to run, and what outputs it produces.

3. **Expose**
   Choose how the assistant may use it:
   - context only: read docs/snippets and cite them in planning
   - shell/tool wrapper: run declared commands through OptPilot's tool service
   - MCP bridge: register MCP server tools
   - OpenHands custom tool: expose typed tool functions to OpenHands
   - workspace helper: clone/copy/use the repo inside a workspace when relevant

4. **Test**
   Run an optional smoke command, MCP capability listing, or knowledge-base
   retrieval check.

5. **Enable**
   Store the add-on manifest, add it to the current user's enabled add-on set,
   and include it in future assistant context packets.

### Add-ons And OpenHands

OpenHands should see add-ons through an OptPilot-controlled capability layer,
not by receiving unrestricted filesystem or network access.

Use this mapping:

| Add-on type | OpenHands exposure |
| --- | --- |
| MCP server | OptPilot starts/configures the MCP server and exposes selected tools through OpenHands custom-tool wrappers or an MCP bridge when OpenHands supports it cleanly |
| OpenHands custom tool | Register directly with the OpenHands runtime, but keep OptPilot as the source of permissions and UI events |
| Skill/instruction bundle | Add to the assistant system/developer context for sessions where the add-on is enabled |
| Knowledge base | Provide a retrieval/search tool that returns compact excerpts plus source paths |
| Repository-backed utility | Provide context files, declared commands, and an allowlisted shell wrapper rooted at the utility path |
| Preview adapter | Expose as a workspace preview tool that can launch a URL, artifact viewer, or remote-display adapter |

For unofficial tools like `resource/devs_display_new`, do not require them to
be rewritten as MCP servers before they are useful. Register them as
`repo_tool` add-ons with:

- a source root
- usage instructions
- allowed commands or command templates
- read/write policy
- expected outputs
- smoke-test command
- examples of when the assistant should choose the tool

Then OpenHands can use the add-on in two ways:

1. The context packet tells the agent the add-on exists and summarizes when to
   use it.
2. OptPilot exposes a controlled `optpilot_addon_run` tool that runs only the
   declared command templates inside the declared root.

This keeps the system compatible with formal MCP tools and skills while still
supporting ordinary codebases that function as practical tools.

### Add-on APIs

Add API endpoints after the workspace and registration APIs:

```text
GET  /api/addons
POST /api/addons
GET  /api/addons/{addon_id}
POST /api/addons/{addon_id}/inspect
POST /api/addons/{addon_id}/test
POST /api/addons/{addon_id}/enable
POST /api/addons/{addon_id}/disable
POST /api/addons/{addon_id}/open-workspace
POST /api/addons/{addon_id}/run
```

`/run` should accept only a registered command id plus structured arguments.
Avoid accepting arbitrary shell strings from the browser or agent.

### Add-ons In Assistant Context

Add enabled add-ons to the context packet:

```json
{
  "enabled_addons": [
    {
      "id": "devs-display-new",
      "kind": "repo_tool",
      "name": "DEVS Display Generator",
      "summary": "Use for DEVS/xDEVS simulator generation.",
      "available_actions": ["inspect", "run:generate simulator", "open-workspace"],
      "safety": {"write_policy": "read_only", "allowed_roots": ["resource/devs_display_new"]}
    }
  ]
}
```

The packet should include only compact summaries by default. The agent should
call an add-on inspection or retrieval tool when it needs detailed docs.

### Registration Application

Registration should copy a manifest, not a folder.

Rules:

- Environment targets default to `user_catalog/environments/<slug>/`.
- Method targets default to `user_catalog/methods/<slug>/`.
- Study targets default to `user_catalog/studies/<slug>.yaml` or a study
  subfolder when assets are included.
- Exclude `.git`, `.venv`, caches, run outputs, temp files, and dependency
  directories unless explicitly included.
- Record registration provenance in `workspace.json` and optionally in a small
  `registration.json` beside the catalog entry.

Use physical copies by default, not symlinks. A symlink is a filesystem pointer:
for example, `user_catalog/environments/foo` could point to
`.optpilot-ui/workspaces/ws_123/workspace` instead of containing copied files.
That is convenient locally, but it makes catalog entries less portable and means
editing the workspace silently edits the catalog entry. Keep symlinks as an
advanced local-only option after the copy-based path is solid.

### Smoke Tests

Validation should remain config-focused. Smoke tests are optional but strongly
recommended before registration:

- Environment smoke: run a minimal evaluator call with an example candidate or
  generated default candidate.
- Method smoke: call the method on a small compatible contract and ensure it
  proposes valid candidates.
- Study smoke: launch with `maxTrials: 1` into a temporary run directory.

The registration menu should separate these states:

- config validation
- compatibility validation
- smoke-test result
- registration applied

## OpenHands Integration

Use OpenHands as the assistant runtime, but keep OptPilot Studio as the product
UI.

Do not iframe the OpenHands UI into OptPilot. Instead, use the OpenHands
Software Agent SDK / Agent Server as the backend that powers the OptPilot
assistant panel.

### Why This Fits

OpenHands now provides:

- Python and REST APIs for agents that work with code
- built-in tools for bash, file editing, and task tracking
- custom tools for specialized platform actions
- remote agent server support over HTTP/WebSocket
- local, Docker, and remote workspace execution modes
- model-provider flexibility through LiteLLM-compatible model names

This maps well to OptPilot because OptPilot needs a general coding agent, but
also needs platform-specific tools for catalog, registration, studies, and runs.

### Runtime Placement

Use three separate runtime layers:

| Layer | Responsibility |
| --- | --- |
| OptPilot platform runtime | UI server, catalog scanning, registration, study launch, run inspection, code-server orchestration |
| OpenHands agent runtime | assistant conversations, planning, code edits, dependency inspection, tool calls inside workspaces |
| OptPilot study runtimes | environment evaluator and method execution, using current host/container config semantics |

OpenHands should live in the platform layer. It should not replace OptPilot's
method/environment runtimes.

Local development:

```text
uv run optpilot ui --open-browser
python -m openhands.agent_server --host 127.0.0.1 --port 8771
code-server on 127.0.0.1:8766
```

Production:

- OptPilot API/UI service per user or tenant.
- code-server service per user.
- OpenHands agent server either shared behind OptPilot, per user, or per active
  conversation depending on the trust boundary.
- Shared workspace volume mounted into OptPilot, code-server, and OpenHands
  with role-appropriate permissions.

An agent server abstracts more than conversation history. It owns or brokers
agent execution, tool calls, shell/file operations, event streaming, workspace
access, model configuration, and sandbox policy. Conversation history should be
stored separately in OptPilot's agent-session store, keyed by user and session.

A single shared OpenHands agent server can be acceptable for local development
or a trusted single-tenant deployment if OptPilot sits in front of it and
enforces authentication, per-user session IDs, workspace-root allowlists, and
tool permissions. Do not expose a shared agent server directly to all users
unless it provides the required tenant isolation. For public multi-user service,
start with one OpenHands server per user or one pool with strict per-user
workspace and credential isolation; add per-conversation sandboxing for
untrusted code or regulated deployments.

### Docker And Isolation

OptPilot already supports host and container execution for environment/method
runtimes. Keep that separate:

- OpenHands may install packages and run commands in its own agent workspace.
- When the agent wants to validate or launch a study, it calls OptPilot APIs or
  structured tools.
- OptPilot then runs studies through the existing local, local subprocess, or
  Docker/Podman-compatible execution path.

Avoid making the OpenHands container directly own Docker unless needed. If a
containerized OpenHands agent must trigger Docker-based study runs, prefer this
flow:

```text
OpenHands tool call -> OptPilot platform API -> OptPilot host-side Docker/Podman execution
```

Only mount the Docker socket into OpenHands for trusted deployments where the
security implications are acceptable.

### OptPilot Tools For OpenHands

Create OptPilot-specific tools as OpenHands custom tools first. MCP can be added
later if we want the same tools available to other agents.

Initial tool set:

| Tool | Purpose |
| --- | --- |
| `optpilot_workspace_list` | list workspaces and attached sessions |
| `optpilot_workspace_create` | create blank/git/catalog/run workspace |
| `optpilot_workspace_focus` | select workspace and focus path in UI |
| `optpilot_catalog_list` | list environments, methods, studies |
| `optpilot_catalog_detail` | inspect a selected catalog config and contract |
| `optpilot_addon_list` | list enabled add-ons and their exposure mode |
| `optpilot_addon_detail` | inspect add-on manifest, usage docs, and safety policy |
| `optpilot_addon_run` | run a declared add-on command or tool action |
| `optpilot_addon_open_workspace` | inspect an add-on source in a read-only workspace |
| `optpilot_config_discover` | discover OptPilot config files in a workspace |
| `optpilot_config_validate` | validate environment/method/study YAML |
| `optpilot_registration_prepare` | create or update registration manifest |
| `optpilot_registration_apply` | apply approved registration to `user_catalog/` |
| `optpilot_study_draft` | draft a study from environment/method inputs |
| `optpilot_study_launch` | launch a validated study |
| `optpilot_run_list` | list runs and live jobs |
| `optpilot_run_detail` | inspect run summary, trials, candidates, events |
| `optpilot_run_open_workspace` | attach run directory as analysis workspace |
| `optpilot_smoke_test` | run environment/method/study smoke tests |
| `optpilot_special_tool_run` | invoke tools such as `resource/devs_display_new` |

Every tool result should return compact structured JSON for the agent and a
human-readable event for the assistant panel.

### Agent Context Packet

Before each assistant request, send a compact context packet:

```json
{
  "current_page": "catalog",
  "selected_catalog_entry": {"kind": "environment", "id": "...", "path": "..."},
  "selected_workspace": {"id": "ws_...", "root": "...", "focus_paths": [...]},
  "attached_workspaces": [{"id": "ws_...", "title": "..."}],
  "selected_run": {"id": "...", "path": "...", "status": "..."},
  "registration_menu": {"workspace_id": "ws_...", "status": "draft", "targets": [...]},
  "enabled_addons": [{"id": "devs-display-new", "kind": "repo_tool"}],
  "available_tools": ["optpilot_config_validate", "optpilot_registration_prepare", "optpilot_addon_run"]
}
```

This is how the agent becomes aware of the current tab and can answer
tab-specific questions.

### Agent Skills

Provide an OptPilot skill/instruction bundle to OpenHands:

- explain OptPilot's environment/method/study boundary
- keep environment-owned inputs in `evaluator.settings`
- use `methodContext.references` for files methods may read
- avoid adding new public concepts such as top-level `instances`
- prefer registration manifests over copying whole repositories
- use catalog/run/study tools instead of parsing all files blindly
- for GitHub integrations, inspect upstream code, choose the OptPilot boundary,
  write configs, validate, and smoke-test
- for DEVS generation, call the installed DEVS tool only when useful
- when using add-ons, read the add-on manifest first, respect declared allowed
  roots and commands, and prefer add-on wrappers over arbitrary shell commands

## Visualization Preview

Prefer web-native visualization paths:

- if the simulator or dashboard launches a URL, show it in the OptPilot preview
  pane and provide an "Open in code-server" action
- if it runs a local web server from the workspace, route it through the
  code-server or OptPilot preview proxy
- if it produces HTML, images, plots, notebooks, CSV, JSON, SQLite, logs, or
  videos, let VS Code/code-server preview those artifacts with built-in viewers
  or installed extensions

For non-web native visualizations such as a desktop Godot or Unity window, use
one of these fallbacks:

- prefer a WebGL/web export when the simulator supports it
- use noVNC/Xvfb or a remote-display preview tool for GUI applications
- record screenshots, videos, or structured trace artifacts into the run
  evidence when live rendering is too heavyweight

Do not force every visualization into core OptPilot UI. Treat preview launchers
as workspace tools declared by the registered environment or method.

## Frontend Implementation

Frontend behavior already present in the local UI:

- **Open Separate Window** is the separate-window action.
- **Register to Catalog** replaces earlier validate/promote wording.
- Assistant appears as a sidecar launcher in the left rail.
- Workspace cards use quieter typography and show registration state.
- New workspace creates a generic workspace, not a new environment.

Workspace UI:

- Left rail contains product nav, Assistant launcher, and workspace list.
- Workspace cards show source, mode, status, and registration badges.
- Selected workspace opens embedded code-server automatically.
- The selected workspace card shows:
  - `Register to Catalog`
  - `Open Separate Window`
  - `Close`
- Registration menu appears inside the assistant sidecar, not as a modal that
  hides the editor.

Catalog UI:

- `Open workspace` opens read-only inspection.
- `Edit copy` creates editable workspace from catalog entry.
- If a shared codebase has multiple registered entries, show "same workspace"
  relationship and entry-specific focus files.

Runs UI:

- Add `Open Run Workspace`.
- Run detail should keep the structured evidence browser, but the workspace
  gives the assistant full file-level access to artifacts.

## Migration From Current Local UI

Completed foundation:

- Labels, visual cleanup, sidecar assistant launcher, generic workspace
  creation, and catalog/run workspace open flows are implemented in the local
  UI.
- `.optpilot-ui/workspaces/index.json`, workspace CRUD endpoints, and
  front-end loading from `/api/workspaces` are implemented.
- Config discovery, registration manifests, validation, diff review, and
  registration apply endpoints are wired to `Register to Catalog`.
- Assistant sessions, messages, events, workspace attachments, and context
  packets are persisted under `.optpilot-ui/agent_sessions/`.
- `src/optpilot/agent.py` provides the OpenHands configuration/status boundary
  and the OptPilot tool list exposed to the assistant context.

Remaining migration work:

1. **OpenHands dispatch**
   Connect queued assistant messages to an OpenHands runtime, translate
   messages/events, and execute approved OptPilot tools through the platform
   boundary.

2. **Event streaming**
   Stream assistant model output, tool calls, tool results, and approval
   requests to the panel instead of relying on local queued transcripts.

3. **Specialized tools**
   Add the Add-ons page and registry. Register `resource/devs_display_new`,
   GitHub import helpers, dataset profiling, knowledge bases, MCP servers, and
   visualization launchers behind the same capability service.

4. **Production hardening**
   Add authentication, per-user workspace roots, quotas, secrets management,
   approval policies, and sandbox profiles.

## Validation Plan

Backend:

- unit tests for workspace registry read/write and path safety
- unit tests for config discovery and multi-target registration manifests
- unit tests for catalog scanner entries sharing one workspace root
- unit tests for run workspace creation
- smoke test: register one environment and one method from the same workspace
- smoke test: open a run as read-only workspace and inspect `summary.json`

Frontend:

- Playwright checks for Assistant opening on Catalog, Studies, Runs, and editor
- Playwright check for generic new workspace
- Playwright check for `Register to Catalog` opening registration menu
- Playwright check for `Open Separate Window` opening a new window URL
- responsive checks for assistant sidecar and workspace list

Agent:

- mocked OpenHands adapter test for message/event streaming
- mocked tool-call test for config discovery, validation, and registration
- mocked add-on test for context-only, MCP-style, and repository-backed add-ons
- add-on safety test: `optpilot_addon_run` rejects undeclared commands and
  paths outside the add-on allowlist
- local OpenHands smoke test against a temporary workspace
- approval test: registration cannot apply without explicit user approval

## Resolved Product Decisions

- The workspace card **Close** action means detach from the current assistant
  session. It never deletes files.
- Attaching a workspace means exposing workspace metadata and path allowlists to
  the assistant session.
- Built-in examples are read-only by default. User catalog entries can be
  editable by default, with edit-copy available when safer.
- Registration uses physical copies plus provenance by default. Symlinks are an
  advanced local option only.
- OptPilot should rely on code-server/VS Code for unsaved editor buffers instead
  of building iframe dirty-state detection.
- One shared OpenHands server is acceptable only behind OptPilot-controlled
  authentication and workspace/tool isolation. Public deployments should prefer
  per-user or stronger isolation.
- Visualizations should be launched as workspace preview tools, with web URLs as
  the preferred path and VS Code/code-server viewers or remote-display adapters
  for non-web outputs.
- Add-ons are assistant capabilities, not environment/method/study catalog
  entries. Formal MCP tools, OpenHands custom tools, skills, knowledge bases,
  and unofficial repository-backed tools should all be represented through the
  same add-on manifest and permission model.

## References

- OpenHands GitHub README: <https://github.com/OpenHands/openhands>
- OpenHands Software Agent SDK: <https://docs.openhands.dev/sdk>
- OpenHands custom tools: <https://docs.openhands.dev/sdk/guides/custom-tools>
- OpenHands local agent server: <https://docs.openhands.dev/sdk/guides/agent-server/local-server>
- OpenHands remote agent server overview: <https://docs.openhands.dev/sdk/guides/agent-server/overview>
- OpenHands Docker sandbox: <https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox>
- OptPilot current UI docs: `docs/ui.md`
- OptPilot user catalog docs: `docs/user-catalog.md`
- OptPilot evidence docs: `docs/evidence.md`
