# Studio Folder and Runtime Management

This is an internal Studio design note. Public user documentation belongs under
`docs/`; Studio product design, implementation notes, and local planning belong
under `resource/`.

Terminology:

- Repo-root `resource/` is internal project material. Studio does not scan it as
  a user catalog. Root-level Markdown files there may be tracked as design
  notes; local external projects under subdirectories should stay uncommitted.
- `user_catalog/resources/` is the user-facing resource catalog managed by
  Studio.

The goal is to keep the product model small:

1. Runs store evidence.
2. The user catalog stores durable reusable assets.
3. Draft workspaces store editable work in progress.
4. Each workspace runs in one default per-workspace container runtime.

Assistant sessions can attach workspaces as context, but sessions do not own
workspace files or runtime state.

## Folder Classes

### Run Artifacts

Run artifacts are produced by study execution.

Typical location:

```text
runs/
```

Rules:

- Run artifact folders are evidence, not editable project folders.
- Opening a run creates a read-only analysis view over the run directory.
- The assistant may inspect run files through run tools.
- The assistant must not modify run artifacts.
- Detaching a run view from an assistant session never deletes the run directory.

If a user wants to write notes, derived reports, or analysis scripts from a run,
Studio should create a normal editable draft workspace that references the run
instead of writing into the run directory.

### User Catalog

The user catalog is the durable reusable library.

Typical layout:

```text
user_catalog/
  environments/
  methods/
  resources/
```

Rules:

- Environments and methods are OptPilot study components.
- Resources are reusable user-facing codebases, documents, datasets, examples,
  templates, or reference folders.
- Catalog folders are durable assets and should be opened read-only by default.
- To modify a catalog asset, create an editable draft copy first.
- Registration copies selected validated files from a draft workspace into
  `user_catalog/`; it should not copy a whole workspace blindly.

Resources are not the same thing as assistant capabilities. A resource may be
helper code, a simulation toolkit, an unfinished future environment, or an
unfinished future method. It can be inspected, opened read-only, or copied into
an editable draft workspace. It is not directly callable by OptPilot or by the
assistant unless a separate tool wrapper, MCP server, AgentSkill, or method
config is created.

Example:

```text
user_catalog/resources/devs_display_new/
```

This resource can be reused by users and by assistant workflows, but the folder
itself remains a catalog asset.

### Draft Workspaces

Draft workspaces are editable folders that are not necessarily registered.

Current local storage:

```text
.optpilot-ui/
  workspaces/
    index.json
    ws_<id>/
      workspace/
      registrations/
```

There are two draft workspace ownership types:

- Studio-owned drafts live under `.optpilot-ui/workspaces/ws_<id>/workspace`.
  Studio owns their files and may delete them after explicit confirmation.
- External draft references point at an existing user folder inside an allowed
  OptPilot root. Studio can attach, inspect, edit, and register them, but it
  does not own the underlying files and must not delete them.

Rules:

- Draft workspaces are where users and the assistant edit code, configs, notes,
  tests, adapters, and copied catalog assets.
- Draft workspaces may be created from scratch, from a catalog edit copy, from a
  local folder, from a generated project, from a Git repository, or from an
  assistant-created task.
- A draft workspace may later be registered as an environment, method, or
  resource.
- Studies are not registered. A study YAML file is already saved when drafted or
  launched, and run evidence records what executed.
- A draft workspace can be attached to multiple assistant sessions.
- Detaching removes the workspace from the current session context only.

Studio should show all draft workspaces in the workspace list, not only the
ones attached to the current assistant session. For the selected session, each
row shows an `attached` or `unattached` badge.

Ordering:

1. Workspaces attached to the selected session.
2. Workspaces not attached to the selected session.
3. Within each group, newest modified first.

When the user switches assistant sessions, the draft workspace list stays
visible, but the badges and ordering update for that session.

When a user detaches a draft workspace from its last attached session, Studio
should show a cleanup dialog:

```text
This draft workspace is no longer attached to any assistant session.

[Keep Draft] [Register...] [Delete Draft | Remove From Studio]
```

`Keep Draft` keeps the workspace visible as an unattached draft. `Register...`
starts environment, method, or resource registration.

The destructive action must be labeled by ownership:

- For a Studio-owned draft, use `Delete Draft`. It removes the workspace files,
  the workspace record, and runtime state after explicit confirmation.
- For an external draft reference, use `Remove From Studio`. It removes only the
  workspace record and Studio runtime state. It never deletes the referenced
  folder.

This avoids a separate draft-workspace manager while still preventing hidden
unreachable folders.

## Session Attachment

Assistant sessions attach workspaces as context.

The canonical attachment state lives on the assistant session record as
`attached_workspace_ids`. A workspace-level `attached_sessions` field may exist
as a denormalized cache for list rendering and cleanup checks, but it must be
recomputed or updated from session records. Do not let the two records become
independent sources of truth.

Attaching a workspace means:

- add the workspace id to the current assistant session
- include the workspace summary in the assistant context packet
- allow assistant tools to read files under the attached root
- allow writes only when the workspace mode is editable
- make the workspace runtime available for shell commands and debugging

Detaching a workspace means:

- remove it from that assistant session
- stop exposing it to the assistant through that session
- keep it visible in the draft workspace list
- leave files on disk unless the user explicitly deletes the draft
- release or stop the workspace runtime only according to the lifecycle rules
  below

Sessions are conversation and context objects. They are not storage lifetimes.

## Registration

Registration makes reusable implementation or reference assets durable.

Environment and method registration copies selected files into:

```text
user_catalog/environments/<id>/
user_catalog/methods/<id>/
```

Resource registration copies selected files under:

```text
user_catalog/resources/<id>/
```

Registration may record provenance metadata beside the copied files, but the
default durable artifact is a physical copy. Symlinks or reference-only
registrations are advanced local-only behavior and should not be the default
because they make catalog entries less portable and can make later edits mutate
catalog assets silently.

Registration should be manifest based:

- discover candidate files in the draft workspace
- let the user choose what should be registered
- validate environment or method configs when applicable
- preview destination paths and copied files
- apply only after user approval

For resources, validation can be lightweight at first: check that the folder
exists and has a short description, README, or user-provided note.

## Runtime Model

Studio should use one default runtime model for user-facing workspaces:

```text
one workspace -> one container runtime
```

The same workspace runtime should be used by:

- the embedded Code Server terminal
- assistant shell/debug tools such as `optpilot_shell_run`
- validation commands against files in that workspace
- smoke tests for draft environments, methods, and resources
- dependency installation while developing that workspace

This gives users one understandable answer to "where did this command run?": it
ran inside the selected workspace runtime.

Current implementation uses `WorkspaceRuntimeManager` for user-facing workspace
execution. Code Server is launched inside the selected workspace container, and
assistant shell/debug commands run through the same container with `exec`.
Studio status should report the container engine, image, container name,
mounted workspace root, and Code Server port. If Docker or Podman is not
available, Studio must say the workspace runtime is unavailable instead of
falling back to host execution.

### Runtime Classes

There are three related runtimes, but only one should be visible as the normal
development runtime:

1. Workspace runtime: the per-workspace container used by Code Server and the
   assistant when editing, running, and debugging files.
2. Study runtime: the runtime declared by environment or method configs and
   used by OptPilot study execution.
3. Assistant runtime: the OpenHands agent runtime used to reason, call tools,
   and coordinate actions.

The workspace runtime is user-facing. The study runtime is reproducibility
metadata for launched experiments. The assistant runtime is an implementation
detail of the assistant bridge.

OpenHands MCP servers and custom tools should run in the assistant runtime when
they are assistant capabilities. Commands that act on a workspace should route
through Studio's workspace runtime manager instead of directly using the
OpenHands process environment.

### Resource Codebases

Resource codebases are user-facing, so they use the workspace runtime when they
are opened or copied into a draft workspace.

This matters because a resource may start as helper code and later become an
environment or method. While it is a draft resource, users should be able to
open a terminal, install dependencies, run tests, and ask the assistant to debug
inside the same per-workspace container.

When the resource is promoted to a formal environment or method, its study
runtime should be declared explicitly in the environment or method config.

### Container Ownership

Each workspace has a runtime record owned by the workspace runtime manager.

Current local state:

```text
.optpilot-ui/
  workspaces/
    index.json
    ws_<id>/
      workspace/
      registrations/
  runtime/
    ws_<id>/
      logs/
      code-server/
        user-data/
        extensions/
      runtime.json
```

The runtime record should store only operational metadata:

- image or build recipe
- container name or id
- status: `stopped`, `running`, `failed`, `unavailable`
- created, started, updated, and last-used timestamps
- exposed ports such as Code Server
- log paths
- last error summary

Avoid adding a second persistent metadata system. The workspace index remains
the source of truth for workspace identity, roots, source type, mode, and
registration state. Assistant session records remain the source of truth for
workspace attachment.

### Mounts And Permissions

Default mounts:

- editable draft workspace: workspace root mounted read-write
- external draft reference: referenced root mounted read-write only if the user
  explicitly created or opened it as editable
- catalog asset inspection: catalog folder mounted read-only
- run artifact inspection: run directory mounted read-only
- runtime home/cache: mounted under `.optpilot-ui/runtime/ws_<id>/`

The assistant must still respect Studio permissions:

- only attached workspaces are visible to an assistant session
- file writes require an editable workspace
- shell commands run inside the workspace runtime
- risky commands still require user approval
- secrets should be passed through explicit allowlists, not inherited wholesale
- Docker or Podman socket access should be disabled by default

### Lifecycle

Runtime lifecycle should be simple:

- Create on first workspace open, terminal request, validation, or assistant
  shell command.
- Reuse while the workspace exists.
- Treat the runtime as active while any of these references exist: attached
  assistant session, selected/open editor, active Code Server terminal, active
  assistant turn, validation/smoke-test process, or user-started process.
- Stop only after no active references remain and the idle timeout expires.
- Restart from the same workspace files and runtime cache.
- Delete runtime state when a Studio-owned draft workspace is deleted.
- For an external draft reference, deleting the Studio record may delete Studio
  runtime/cache state after confirmation, but never the referenced folder.

For catalog and run read-only views, Studio may use a short-lived runtime or a
read-only attached runtime. The important rule is that writes cannot go back
into catalog or run artifact folders.

Current implementation creates the container on first Code Server open or
assistant shell command. It removes the container and runtime state when a draft
workspace record is deleted or removed from Studio. It also stops idle
containers after `OPTPILOT_WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS` when there
are no Studio-tracked active references. Idle cleanup is non-destructive: it
stops the container but keeps workspace files and runtime cache.

### Code Server

Code Server should be served from the workspace runtime, not from the host
project process.

The embedded editor should follow the selected workspace:

```text
selected workspace -> workspace runtime -> code-server port -> Studio iframe
```

This makes the Code Server terminal, assistant shell commands, dependency installs,
and test runs use the same filesystem and environment.

Workspace previews use the same path. A frontend server started inside the
workspace runtime is embedded through the selected workspace's Code Server proxy,
so preview, terminal, and assistant debugging all point at the same container.
Studio seeds each workspace Code Server profile with the shared OptPilot default
layout, while the profile remains stored under that workspace runtime.

The default workspace image is `optpilot/workspace-dev:latest`, built by Studio
from the packaged runtime Dockerfile when the image is not already available.
It includes Code Server, Python, `uv`, Node.js, npm, git, ripgrep, and common
build tools. Production deployments may replace it with an organization-curated
image through `OPTPILOT_WORKSPACE_RUNTIME_IMAGE` or
`--workspace-runtime-image`.

Hosted deployments should set
`OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST` to comma-separated Docker/Podman
image glob patterns. If configured, Studio refuses to build, pull, or start a
workspace runtime image outside the allowlist. When Studio builds the default
image locally, the base image is checked against the same allowlist.

When the desired image changes, Studio should recreate the affected workspace
container instead of silently continuing to run the old image. Runtime records
store both the desired/current image and the Dockerfile path used for local
builds.

### Assistant Tool Execution

Assistant tools should separate transport from execution semantics.

OpenHands remains the transport and agent runtime:

```text
OpenHands agent -> OptPilot tool call -> Studio permission layer -> workspace runtime
```

Examples:

- `optpilot_file_read`: read from attached workspace roots through Studio.
- `optpilot_file_write`: write only to editable attached workspace roots.
- `optpilot_shell_run`: execute inside the target workspace runtime.
- `optpilot_config_validate`: execute validation in the workspace runtime.
- `optpilot_study_launch`: launch through OptPilot after approval, using the
  study/runtime configs declared by the study assets.

This prevents the assistant from accidentally using the host machine or the
OpenHands service process as the workspace development environment.

## Assistant Settings

OpenHands AgentSkills, MCP servers, and custom tools are assistant capabilities.
They are not catalog resources and should not be managed through a broad
Add-Ons page.

Studio should expose an assistant gear button in the assistant panel header.
That modal should contain:

- OpenHands server URL and session endpoint
- LLM provider, model, and API-key configuration
- enabled AgentSkills
- enabled MCP servers
- enabled custom tools
- approval policy for writes, shell commands, registration, study launch, and
  job stop
- workspace runtime defaults such as base image, CPU/memory limits, network
  policy, and idle timeout

Runtime configuration is available through environment variables and UI server
CLI flags:

- `OPTPILOT_WORKSPACE_RUNTIME_EXECUTABLE` or `--workspace-runtime-bin`
- `OPTPILOT_WORKSPACE_RUNTIME_IMAGE` or `--workspace-runtime-image`
- `OPTPILOT_WORKSPACE_RUNTIME_BASE_IMAGE`
- `OPTPILOT_WORKSPACE_RUNTIME_BUILD`
- `OPTPILOT_WORKSPACE_RUNTIME_DOCKERFILE`
- `OPTPILOT_WORKSPACE_RUNTIME_NETWORK` or `--workspace-runtime-network`
- `OPTPILOT_WORKSPACE_RUNTIME_PORT_START` or `--workspace-runtime-port-start`
- `OPTPILOT_WORKSPACE_RUNTIME_CPUS`
- `OPTPILOT_WORKSPACE_RUNTIME_MEMORY`
- `OPTPILOT_WORKSPACE_RUNTIME_PIDS_LIMIT`
- `OPTPILOT_WORKSPACE_RUNTIME_NO_NEW_PRIVILEGES`
- `OPTPILOT_WORKSPACE_RUNTIME_IMAGE_PULL_TIMEOUT_SECONDS`
- `OPTPILOT_WORKSPACE_RUNTIME_IMAGE_BUILD_TIMEOUT_SECONDS`
- `OPTPILOT_WORKSPACE_RUNTIME_START_TIMEOUT_SECONDS`
- `OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST`
- `OPTPILOT_WORKSPACE_CODE_SERVER_AUTH`
- `OPTPILOT_WORKSPACE_CODE_SERVER_PASSWORD`

On first use, Studio inspects the configured workspace image. If the default
`optpilot/workspace-dev:latest` image is missing, Studio builds it from the
packaged Dockerfile. If a non-default image is configured, Studio pulls it when
missing. Production deployments should usually pre-pull or pre-build the
curated workspace image, but local Studio must still handle first-run image
preparation gracefully.

First-time build and pull progress is currently reported after the synchronous
operation completes. A production UI should stream build and pull output through
Server-Sent Events or the same event channel used for assistant steps, then
collapse the progress panel once the runtime is ready.

Default container limits:

- `OPTPILOT_WORKSPACE_RUNTIME_CPUS=2`
- `OPTPILOT_WORKSPACE_RUNTIME_MEMORY=4g`
- `OPTPILOT_WORKSPACE_RUNTIME_PIDS_LIMIT=1024`
- `OPTPILOT_WORKSPACE_RUNTIME_NO_NEW_PRIVILEGES=true`

Code Server host-port assignment must treat ports recorded by other workspace
runtime records as reserved even when no process is listening yet. Docker or
Podman can reserve a published port before Code Server is reachable inside the
container, so listening-socket checks alone are not sufficient.

The current global Settings page should be folded into this assistant settings
modal unless future platform-wide settings appear.

## Capability Loading

Assistant capabilities should be loaded in layers:

1. Core OptPilot tools are always available.
2. User-enabled AgentSkills, MCP servers, and custom tools are available to each
   assistant session.
3. Page-specific context tells the assistant which tools are likely relevant.
4. The assistant may inspect configured resources, skills, or tools on demand.

The assistant should not receive the full content of every skill, resource, or
tool manifest on every turn. It should receive compact summaries and call tools
for details when needed.

## Implementation Plan

1. Keep public documentation under `docs/` and internal Studio design notes
   under `resource/`.
2. Keep the current `.optpilot-ui/workspaces/` draft workspace store.
3. Show all draft workspaces with attached/unattached badges for the selected
   assistant session.
4. Allow a workspace to be attached to multiple sessions.
5. Treat assistant session records as the canonical attachment state.
6. Add `user_catalog/resources/` to catalog discovery and registration.
7. Keep studies as saved YAML and run evidence, not catalog registrations.
8. Distinguish Studio-owned draft deletion from external draft reference
   removal.
9. Keep `WorkspaceRuntimeManager` responsible for container creation, status,
   exec, logs, and cleanup.
10. Keep Code Server routed through the selected workspace runtime.
11. Keep assistant workspace shell/debug tools routed through the workspace
   runtime.
12. Keep OpenHands as the assistant transport/runtime, with Studio enforcing
    workspace permissions and runtime selection.
13. Add UI streaming for first-time workspace image build and pull progress.

## Verification Targets

Before calling this production ready, tests and browser checks should prove:

- public MkDocs navigation contains only user-facing pages
- internal Studio design files are under `resource/`
- multiple workspaces can be attached to one session
- one workspace can be attached to multiple sessions
- all draft workspaces remain visible after session switches
- last-detach cleanup offers Keep Draft, Register, and the ownership-specific
  destructive action
- Studio-owned draft deletion removes files and runtime state
- external draft removal leaves the referenced folder intact
- Code Server terminal runs inside the selected workspace runtime
- assistant shell commands run inside the same workspace runtime
- catalog and run folders are mounted read-only
- disallowed workspace runtime images are rejected before build, pull, or start
- idle workspace containers stop after timeout without deleting workspace files
- session records remain the canonical source for attachment state
