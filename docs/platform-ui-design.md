---
title: Platform UI Design
description: Target product design for a full OptPilot workbench that creates, adapts, runs, and inspects environments, methods, and studies.
---

# Platform UI Design

This page sketches a target design for a more complete OptPilot GUI. It is based
on the current OptPilot model:

```text
method proposes candidate -> environment evaluates candidate -> OptPilot records evidence
```

The UI should make that model easier to use, not replace it. Users should still
end up with normal OptPilot assets:

- environment configs and environment-owned evaluator code
- method configs and method-owned optimizer or agent code
- study configs that bind one environment to one method
- run directories with evidence, artifacts, logs, and compiled specs

The product should feel like a local research and engineering workbench: part
catalog manager, part IDE, part agent console, and part experiment monitor.

## Product Goal

OptPilot Studio should let students, researchers, and industrial practitioners
move from an idea to a reproducible optimization study without first becoming
experts in the YAML schema.

The main user journeys are:

1. Add or create an environment from local files, a GitHub repository, an
   uploaded dataset, a generated simulator, a Gymnasium environment, a service,
   or a natural-language description.
2. Inspect, edit, run, and visualize that environment before using it in a
   study.
3. Add, select, modify, or generate methods, including wrappers around existing
   optimizer, RL, LLM, and heuristic-search code.
4. Configure studies by choosing compatible environment and method contracts,
   objective metrics, budgets, runtimes, and evidence policies.
5. Launch, monitor, compare, resume, and inspect studies, including generated
   code, candidate files, trial workspaces, metrics, visual outputs, and logs.

## Design Principles

- Keep the environment and method boundary explicit. The environment declares
  what it can evaluate; the method declares what it can produce and what context
  it needs.
- Make candidate contracts visual. Users should see parameters, editable files,
  required context, metrics, and compatibility checks as structured UI, with YAML
  as an inspectable source of truth.
- Treat generation as a workflow with checkpoints. An agent may download,
  generate, repair, and adapt code, but the UI should show plan, file changes,
  validation, smoke tests, and final catalog registration into `user_catalog/`.
- Keep environment execution separate from study execution. A user should be
  able to smoke-test an environment, run example candidates, and open a
  visualization before launching a full optimization loop.
- Keep advanced tools pluggable. DEVS generation, repository inspection,
  adapter writing, dataset profiling, and future simulator-specific tools should
  all appear as agent-callable tools behind a common workflow.
- Preserve reproducibility. Generated code, prompts, tool settings, external
  repo URLs, commits, validation output, and smoke-test evidence should be saved.

## Information Architecture

The first screen should be the workbench itself, not a landing page.

```text
OptPilot Studio
  Catalog
    Reusable environments and methods; contract inspection; attach workspaces;
    edit-copy workspaces; create-plan actions
  Studies
    Assistant-proposed and saved study plans; compatibility review; suite
    planning; YAML preview; launch approval
  Runs
    Live jobs, evidence browser, trial timeline, metrics, candidate diffs,
    artifacts, logs, comparisons
  Add-ons
    MCP servers, skills, knowledge bases, repository-backed utilities, preview
    adapters, local runtimes, Docker/Podman, LLM credentials
  Assistant control
    Cross-page collapsible session panel; conversation history; current-page
    context; agent actions
  Editor canvas
    Persistent workspace list in the sidebar; one embedded code editor;
    validation, preview, and registration for the selected workspace
```

The existing UI already covers catalog browsing, compatibility, study drafting,
launching, job tracking, and run inspection. The next design should keep those
pieces but reorganize them around richer creation and inspection workflows.

## Current UX Model

The product should use four user-facing concepts:

- Editor canvas: the main operating surface. It shows the workspaces attached
  to the current assistant session in the sidebar and opens the selected
  workspace in one embedded code editor. It does not need a separate Workbench
  navigation item; selecting a workspace returns the user to this canvas.
- Agent sessions: resumable assistant conversations, similar to a GitHub
  Copilot chat session. Assistant is a cross-page control, visually separated
  from Catalog, Studies, and Runs. It expands or collapses the assistant panel
  next to the active page, and the agent should receive the current visible page
  as context. The session list belongs inside that panel, with a back action
  from the current conversation to the list. Switching sessions changes
  assistant history, attached context, and the visible workspace set. The
  assistant panel should be horizontally resizable so users can trade off chat
  context against editor width.
- Workspaces: persistent code/project folders such as a catalog environment, an
  edit copy, a cloned repository, a generated environment, or a study-plan
  workspace. Workspaces can exist independently from sessions and may be
  attached to one or more sessions.
- Code editor: one embedded code-server surface that follows the selected
  workspace. Opening an additional IDE window is an advanced escape hatch, not
  a second first-class OptPilot workspace.

The editor canvas should be spatially dense. It should not use a hero-style page
title, and it should not place a large floating action bar over the code editor.
Workspace workflow actions such as registration belong with the selected
workspace. The embedded editor should remain the main canvas, and selecting a
workspace should focus that workspace in code-server.

Validation is workspace-scoped, but it should not blindly validate every file in
the folder. OptPilot should first use a workspace manifest or registration manifest
when one exists, then fall back to discovering OptPilot config files in the
workspace. If discovery finds multiple candidate environment, method, or study
configs, the UI or assistant should pick the active target explicitly before
running checks.

Registration should never copy the whole workspace into the catalog. It should
copy only the validated files declared by the workspace or registration manifest
into the correct `user_catalog/` folder, after diff review.
Temporary files, cloned upstream repositories, virtual environments, caches,
logs, and scratch outputs should stay in the workspace unless the manifest
explicitly includes a required asset.

This avoids nesting workspaces under sessions in the data model while still
letting a session remember which workspaces it is using. Closing a workspace in
the sidebar should detach it from the current session after warning the user to
save editor changes; files remain on disk and can be reattached later.

The assistant is session-scoped, not workspace-scoped. It should see the same
high-level platform context the user sees: selected session, attached
workspaces, catalog, study plans, runs, code editor target, validation output,
and artifacts. It should not be a log of tool events from the selected
workspace.

The standalone config editor should not be part of the primary navigation once
code-server is embedded. YAML and Python are edited in the code editor; OptPilot
owns validation, compatibility checks, catalog registration, study launch, and
run evidence.

The assistant should eventually be backed by an existing coding-agent runtime
rather than a hand-rolled chat box. OptPilot should provide that runtime with a
tool adapter for catalog inspection, code edits, validation, smoke tests, study
drafting, launch, and run inspection. The platform UI should render the agent's
plan, approvals, tool calls, diffs, logs, and artifacts in OptPilot terms.

## Environment Studio

Environment Studio is the most important new surface.

### Request Composer

The user should not have to choose from a closed list of environment source
types. The main creation surface is a general request composer attached to a coding
workspace. The user can describe the desired environment, attach local files,
paste a repository URL, upload data, reference an existing simulator, or combine
those inputs in one request.

The composer sends the request to a general coding agent. The agent works in a
workspace, inspects the available context, chooses tools from an
installed tool registry, writes code/config files, runs validation, and asks
for clarification only when the OptPilot contract is ambiguous.

The UI can still provide structured fields, but they should be editable contract
lenses rather than fixed source-mode buttons. The minimum information to extract
is:

- What should a method propose: parameters, files, or opaque payloads?
- What metrics define success?
- What does one evaluation run do?
- What files or context may a method read?
- What dependencies, toolchains, and runtime isolation are needed?
- What is the smallest smoke test that proves the environment works?

### Workspace

After intake or catalog inspection, the UI opens a VS Code plus Copilot style
workspace:

- sidebar: attached workspaces, generated artifacts, config variants, and
  validation state for the current assistant session
- editor canvas: embedded code editor plus OptPilot-owned validation and registration panels
- assistant panel: collapsible session list, agent chat, plan, tool calls,
  progress events, validation results
- bottom pane: terminal/logs, smoke-test output, environment preview

The agent should operate inside a controlled workspace, not directly inside the
final catalog path. Workspaces are used for creating new components, inspecting
existing components, editing safe copies, and drafting experiment plans. A
typical internal workspace directory can look like:

```text
.optpilot-ui/sessions/<session_id>/
  session.json
  messages.jsonl
  events.jsonl
  plan.md
  workspace/
  generated/
  validation/
  registration_manifest.json
```

Existing catalog components should open read-only first. If the user wants to
change a component, OptPilot should create an edit-copy workspace and register
only after validation and diff review pass.

Only after validation passes should the user register files into the catalog:

```text
user_catalog/environments/<environment_slug>/
  environment.yaml
  evaluator.py
  adapter.py
  assets/
  prompts/
  README.md
  ui.preview.yaml
```

### VS Code Server

The default model should be one code-server service per user with multiple
workspace folders under a mounted project home. This matches common containerized
code-server deployments: the container opens a stable workspace root, while
OptPilot creates or focuses folders below it for each workspace.

The UI should therefore treat code-server as an editor attached to workspaces, not
as the owner of OptPilot state:

- The sidebar shows attached workspaces and the code-server folder for each
  workspace. Agent sessions are managed in the assistant panel.
- Catalog actions can open a read-only inspection workspace or an edit-copy
  workspace in code-server.
- Multiple environments, methods, and plans can be open at once as separate
  folders in the same user code-server service.
- An isolated code-server container per workspace can be an advanced option for
  untrusted code, heavyweight dependencies, or conflicting runtimes.

### Contract Builder

The environment detail page should turn `candidate` into controls:

- parameter table with type, bounds, defaults, descriptions, and constraints
- file-candidate editor with editable, required, allow, deny, and materialize root
- opaque family declaration for advanced paired integrations
- method-visible context editor for instructions and references
- metrics editor with source, keys, output files, and record streams
- runtime editor for host/container execution

Every edit should regenerate YAML and run the same validation pipeline used by
`optpilot validate`.

### Environment Smoke Tests

Before an environment can be marked ready, the UI should run one or more smoke
tests:

- validate environment config schema and semantics
- execute evaluator with a baseline candidate
- confirm metrics keys are present and numeric where required
- confirm output files and records are collected
- confirm timeouts and container settings work if configured
- for file candidates, materialize an unchanged baseline file bundle

These smoke tests should create lightweight evidence under the integration
session, separate from full study runs.

### Visualization And Preview

Visualizations should be environment-owned sidecars. The core runner should not
know Unity, Godot, web canvases, or DEVS graphs. The UI can embed them when the
environment declares a preview manifest.

Use a sidecar first because the current public schemas reject unknown top-level
fields:

```yaml
# user_catalog/environments/my_env/ui.preview.yaml
preview:
  type: iframe        # iframe | command | static_file | image_sequence | custom
  title: Simulation Preview
  launch:
    command: [python, preview_server.py, "--port", "{port}"]
    cwd: .
    timeoutSeconds: 60
  url: "http://127.0.0.1:{port}/"
  inputs:
    scenarioConfig: assets/default_scenario.yaml
```

Later, if this becomes stable, OptPilot can add an official `ui` or
`extensions.ui` block to the environment schema.

## Agent Tool Registry

Specialized generators should be add-ons available to the general coding agent,
not top-level product choices. For example, `resource/devs_display_new` can be
preinstalled as a repository-backed add-on that the agent may call when the
request needs discrete-event simulator generation. The same mechanism can
support MCP tools, skills, knowledge bases, repo inspection, dependency
detection, adapter writing, dataset profiling, smoke-test execution, preview
launching, and future domain-specific generators.

The target flow is:

1. User gives the agent a request and optional attachments.
2. The agent decides whether an installed tool is useful.
3. OptPilot records tool calls, inputs, outputs, logs, and generated files in
   the integration session.
4. The user reviews code, contract lenses, tests, preview, and evidence.
5. OptPilot registers only the validated files into `user_catalog/`.

This keeps domain-specific generation outside the OptPilot runner while still
making it available from the GUI whenever the agent needs it.

An add-on does not need to be a formal MCP server or OpenHands custom tool on
day one. It can be a normal codebase with a manifest that explains when to use
it, which docs to read, which commands are safe to run, and which paths the
agent may access. Formal MCP/OpenHands wrappers can be added later without
changing the user-facing concept.

## Method Studio

Method Studio should mirror Environment Studio, but method-first.

Core functions:

- create or adapt a method through the same coding-agent workspace
- import local code, uploaded archives, or repository URLs as context for the
  agent rather than separate product modes
- edit `entrypoint`, `settings`, `runtime`, `accepts`, and `produces`
- run a dry proposal against a selected environment contract
- inspect method calls, stdout, stderr, and generated candidate manifests
- clone a method variant with different prompts, models, hyperparameters, or
  runtime images

The most important UI element is the compatibility inspector. It should explain
why a method can or cannot target an environment:

- candidate format mismatch
- missing required context path
- missing capability
- produced parameter/file schema mismatch
- missing runtime dependency or API key

## Study Plans

Users should not have to visit a manual designer every time they want to launch
a study. A study can be created from the editor canvas, Catalog, Runs, or the
Studies page itself:

- Editor canvas: ask the assistant to choose compatible methods and launch after
  validation.
- Catalog: select an environment or method and ask for a study plan.
- Runs: clone, rerun, or branch a previous study.
- Studies: review saved plans, proposed plans, and comparison suites.

The Studies page is the control room for study configs before execution. It
should support:

- assistant-proposed study plans with rationale and compatibility checks
- saved study YAML files under `user_catalog/studies/`
- compatibility matrix view
- manual override of environment, method, objective, budget, runtime, and
  evidence policy
- compare multiple methods against the same environment and shared cases
- batch launch of a comparison suite
- warnings when two studies do not use the same environment variant, cases, or
  metric policy
  metric
- one-click launch after review

The YAML preview remains important because `config: study` is the reproducible
source of truth, but it should not force a form-first workflow.

## Run Observatory

Run Observatory extends the current Runs page.

It should include:

- live job timeline with scheduler, method, evaluator, and artifact events
- metric charts with best-so-far and per-trial values
- trial table with status, candidate, runtime, failure reason, and artifacts
- candidate inspector:
  - parameter JSON
  - file bundle tree
  - diffs against baseline or previous best
  - validation/materialization report
- method-call inspector:
  - request, response, stdout, stderr, prompt files
- environment artifact viewer:
  - output files
  - records
  - simulator logs
  - images or charts
  - preview/replay sidecar when available
- run comparison:
  - same environment and metric checks
  - best trial comparison
  - artifact and candidate diffs
  - exportable report

## Backend Architecture

The current stdlib HTTP server is a good lightweight MVP. The full platform will
need uploads, long-running generation, event streaming, subprocess management,
and optional embedded visual servers. A FastAPI backend would fit that better,
but the transition can be incremental.

Recommended services:

| Service | Responsibility |
| --- | --- |
| Catalog service | scan, index, create, clone, inspect, and register environment/method/study configs |
| Validation service | schema validation, semantic validation, compatibility checks, smoke-test runs |
| File service | safe workspace file reads/writes, diffs, uploads, archive import/export |
| Workspace service | persistent workspaces, read-only inspection, edit copies, registration manifests |
| Code-server service | per-user IDE service, workspace-folder routing, optional isolated IDE workspaces |
| Agent service | session messages, events, plans, tool calls, and code edits inside attached workspaces |
| Tool service | installed tool registry, tool execution, logs, and generated artifacts |
| Preview service | launch local preview servers and return iframe URLs or static artifacts |
| Study job service | launch, stop, resume, branch, and monitor OptPilot study runs |
| Evidence service | read summaries, JSONL records, artifacts, candidate files, method calls |

The API should use the same shape for every long-running agent request:

```text
session -> workspace -> request -> events -> files/artifacts -> validate -> register or launch
```

This matches the DEVS Display session model internally and can also support generic coding
agents and GitHub importers.

## Suggested API Additions

Near-term additions to the existing UI server:

```text
GET  /api/files/tree?root=...
GET  /api/files/content?path=...
POST /api/files/content
POST /api/catalog/environments:create
POST /api/catalog/methods:create
POST /api/catalog/components/{component_id}/inspect
POST /api/catalog/components/{component_id}/edit-copy
POST /api/studies/save
POST /api/environment-smoke/run
GET  /api/agent-sessions
GET  /api/workspaces
GET  /api/agent-sessions/{session_id}/events
POST /api/agent-sessions/{session_id}/cancel
```

Full-platform API shape:

```text
POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/requests
GET  /api/sessions/{session_id}/events
GET  /api/sessions/{session_id}/files
POST /api/sessions/{session_id}/validate
POST /api/sessions/{session_id}/register
POST /api/code-server/open

GET  /api/tools
POST /api/tools/{tool_id}/run
GET  /api/tools/{tool_id}/events
POST /api/agent/run

POST /api/previews/launch
POST /api/previews/{preview_id}/stop
```

## Data Model

Keep public OptPilot configs stable. Add workbench metadata around them.

```text
.optpilot-ui/
  sessions/
  workspaces/
  previews/
  job_logs/
  tool_registry.json
  catalog_index.json
```

Promotion manifest:

```json
{
  "kind": "environment",
  "mode": "edit-copy",
  "target_path": "user_catalog/environments/my_env",
  "created_files": ["environment.yaml", "evaluator.py"],
  "context": ["request.md", "github_url.txt", "local_upload.zip"],
  "tools_used": [
    {"id": "devs_display_new", "run_id": "toolrun_..."},
    {"id": "adapter_writer", "run_id": "toolrun_..."}
  ],
  "validation": {
    "schema_valid": true,
    "smoke_passed": true
  }
}
```

## Phased Implementation

### Phase 1: Assistant Sessions And Workspace Shell

- Add assistant-owned session history with a session-list view in the assistant
  drawer.
- Add attached workspaces for read-only inspection, edit copies, new components,
  and study plans.
- Add a file tree and safe editor for workspaces.
- Add code-server open/focus actions for the selected workspace folder.
- Reuse current validation and compatibility checks continuously.

### Phase 2: Environment Smoke Test And Preview

- Add environment-only smoke-test API.
- Add baseline candidate generation for parameter and file candidates.
- Add sidecar `ui.preview.yaml` support.
- Embed static files, local preview servers, and image/log outputs in the UI.
- Record smoke-test evidence under `.optpilot-ui/sessions/<session_id>/validation/`.

### Phase 3: Agentic Environment Creation

- Add a general coding-agent service with a constrained workspace and diff
  review.
- Let users attach local code, archives, datasets, repository URLs, and notes as
  context rather than choosing fixed import modes.
- Let the agent register validated environment code.

### Phase 4: Tool Registry

- Add installed tool configuration for `resource/devs_display_new`.
- Add repository inspection, adapter-writing, smoke-test, and preview-launcher
  tools.
- Record tool calls, logs, generated files, and graph previews inside the
  active workspace.
- Let the agent decide when a specialized tool is relevant.

### Phase 5: Method Workspaces And Study Plans

- Add method inspection and edit-copy workspaces.
- Add method dry-run against selected environment contract.
- Add assistant-proposed study plans that write normal `config: study` YAML.
- Add batch study suite generation for multiple methods on one environment.
- Add richer run comparison reports and artifact diffing.

### Phase 6: Multi-User And Remote Execution

- Add authentication, user/project permissions, and server deployment mode.
- Add remote execution backends when the core runner supports them.
- Add shared tool credentials and runtime pools.

## First Implementation Slice

The smallest high-value slice is:

1. Add an agent workspace for environment creation.
2. Let users provide a natural-language request plus attachments such as local
   folders, archives, datasets, or repository URLs.
3. Let the agent write files under an integration-session workspace, with tool
   calls recorded as events.
4. Show contract lenses and YAML side by side.
5. Run environment-only smoke tests.
6. Register validated files into `user_catalog/environments/<slug>/`.
7. Show compatible methods and create a study from the new environment.

This slice does not require the full agentic stack, but it establishes the
product shape. Once users can create and validate environments in the UI, agentic
generation and DEVS Display integration can plug into the same registration path.

## Open Design Questions

- Should visual preview metadata remain sidecar-only, or should the public
  environment schema grow an official `ui` or `extensions` block?
- Should generated environments copy external repositories into `user_catalog/`,
  reference stable local paths, or support both with clear provenance?
- What is the default safety policy for agents that install dependencies or run
  imported code?
- Should study suites become a first-class config, or should the UI generate
  multiple ordinary study YAML files?
- What subset of GitHub import should be turnkey first: dataset benchmarks,
  Gymnasium environments, command simulators, or method repositories?
