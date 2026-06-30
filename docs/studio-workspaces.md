---
title: Workspace Management
description: How OptPilot Studio creates editable copies, launches interfaces, and manages local workspace containers.
---

# Workspace Management

Studio keeps catalog source and execution work separate.

Package source is read-only by default. When a user wants to edit or execute a
component, Studio creates an editable workspace copy and works from that copy.
This keeps curated packages stable while still supporting codebases that write
logs, caches, generated files, or intermediate outputs during execution.

## Workspace Types

| Workspace type | How it is created | Typical use |
| --- | --- | --- |
| Catalog inspection | Inspect Read-Only Source Code | Read package source exactly as shipped. |
| Editable catalog copy | Create Editable Copy and Install | Modify or install a package component without changing the package source. |
| Interface workspace | Launch Interface | Run a package-provided GUI or helper service. |
| Study workspace | Open a study or save a study copy | Configure and launch a concrete run. |
| Run workspace | Open a run as workspace | Inspect run evidence, trial files, and artifacts. |

Editable workspaces are stored under `.optpilot-ui/`. They are local runtime
state and should not be committed.

## Setup And Runtime

Environment, method, and resource configs can declare setup steps through their
runtime or interface sections. Studio uses those declarations when it creates an
editable copy or launches an interface.

Typical setup work includes:

- syncing Python dependencies
- installing Node dependencies
- building a local helper app
- preparing a component-specific runtime directory

Studio does not infer dependencies automatically. The package author should
declare the setup commands needed for the component to run.

## Embedded Code Server

Studio can open a workspace in an embedded Code Server editor. The Code Server
process runs inside a per-workspace Docker/Podman-compatible container by
default.

Useful launch options:

```bash
uv run optpilot ui \
  --workspace-runtime-bin docker \
  --workspace-runtime-image optpilot/workspace-dev:latest \
  --workspace-runtime-port-start 18766
```

When no image is specified, Studio builds and uses
`optpilot/workspace-dev:latest` from the packaged runtime Dockerfile. The image
includes Code Server, Python, `uv`, Node.js, npm, git, ripgrep, and common build
tools.

## Preview Ports

If a workspace starts a web app, bind it to `0.0.0.0` inside the workspace
runtime. Studio can proxy that port back to the browser through the workspace
preview panel.

For catalog entries with an `interface` block, Studio automates this flow:

1. create an editable workspace copy
2. run declared setup steps
3. run the interface command
4. wait for the configured `readyPath`
5. open the configured port in Preview

## Runtime Defaults

Workspace containers default to:

- `2` CPUs
- `4g` memory
- process limit `1024`
- Docker/Podman `no-new-privileges`

Override with environment variables:

```bash
OPTPILOT_WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS=3600
OPTPILOT_WORKSPACE_RUNTIME_CPUS=4
OPTPILOT_WORKSPACE_RUNTIME_MEMORY=8g
OPTPILOT_WORKSPACE_RUNTIME_PIDS_LIMIT=2048
OPTPILOT_WORKSPACE_RUNTIME_NO_NEW_PRIVILEGES=true
```

Studio stops idle workspace containers after the configured idle timeout when no
assistant session, selected editor, or reachable Code Server is using them. It
does not delete workspace files or runtime cache.

## Image Allowlist

Hosted deployments can restrict workspace images:

```bash
OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST="optpilot/workspace-dev:*,ghcr.io/coder/code-server:*,registry.example.com/optpilot/*"
```

When an allowlist is configured, Studio refuses to build, pull, or start a
workspace runtime image outside the allowed patterns.
