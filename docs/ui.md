---
title: UI
description: The local OptPilot workbench for browsing catalogs, launching studies, and inspecting runs.
---

# UI

OptPilot includes a local web UI, called OptPilot Studio, for browsing reusable
components, drafting studies, launching runs, inspecting evidence, and working
with the assistant.

```bash
uv run optpilot ui --open-browser
```

This starts a local server and opens the browser. This basic mode is enough to
browse the example catalog, validate compatibility, launch studies, and inspect
run evidence. Stop the server with `Ctrl-C` in the terminal when you are done.

Studio scans packages under `catalog/` by default when launched from the repository root.
The repository ships one package:

- `catalog/example_package/`

Studio creates `catalog/local_package/` on demand when you register your own
reusable environments, methods, and resources. Study YAML files are saved run
plans; keep them where you draft or launch them.

## Assistant-Enabled Startup

For the full Studio experience with the OpenHands assistant and embedded Code
Server editor, run the OpenHands agent server and OptPilot Studio locally. The
workspace editor runs inside per-workspace Docker/Podman containers managed by
Studio, so Docker or Podman must also be available.

The OpenHands bridge has been checked with `openhands-agent-server==1.29.0`.
OpenHands currently requires Python 3.12. Install the runtime packages in the
environment you use to launch Studio:

```bash
uv pip install -U openhands-sdk openhands-tools openhands-workspace openhands-agent-server
```

```bash
# Terminal 1: OpenHands agent server
OPENHANDS_SUPPRESS_BANNER=1 uv run agent-server --host 127.0.0.1 --port 8781

# Terminal 2: OptPilot Studio
uv run optpilot ui --host 127.0.0.1 --port 8866
```

Configure the assistant from Studio's assistant settings panel, or use
environment variables:

```bash
OPTPILOT_OPENHANDS_URL=http://127.0.0.1:8781
OPTPILOT_OPENHANDS_SESSION_ENDPOINT=/api/conversations
OPTPILOT_OPENHANDS_MODEL=deepseek/deepseek-v4-flash
OPTPILOT_OPENHANDS_API_KEY=...
```

`OPTPILOT_OPENHANDS_API_KEY` can also fall back to `LLM_API_KEY` or
`OPENAI_API_KEY`. Studio treats API keys as write-only settings: it stores
whether a key is configured, but does not echo the secret back into the browser.

The embedded Code Server service is managed by OptPilot Studio. Start it from the
Editor page, or trigger it after the GUI is up:

```bash
curl -s -X POST http://127.0.0.1:8866/api/code-server/start \
  -H "Content-Type: application/json" \
  -d "{\"folder\":\"$PWD\"}" | uv run python -m json.tool
```

The Editor page also has a workspace Preview mode for frontend apps launched
inside the workspace terminal. Start the app on `0.0.0.0`, enter its port in
Preview, and Studio embeds it through the selected workspace's Code Server
proxy. Code Server uses the same compact OptPilot default layout for each
workspace runtime.

Catalog entries can automate that flow. If an environment, method, or resource
declares an `interface` block, the Catalog page shows **Launch Interface** next
to **Inspect** and **Edit Copy**. Studio creates an editable draft copy, starts
the declared command inside the workspace runtime, waits for the declared
readiness path to answer, and switches to Preview for the declared port. While
the launch is running, Studio shows the current preparation step and recent
stdout/stderr from the launch command.

Expected local ports:

- OptPilot Studio: `http://127.0.0.1:8866/`
- Code Server: per-workspace ports starting at `http://127.0.0.1:18766/`
- OpenHands agent server: `http://127.0.0.1:8781/`

The status area in Studio reports:

- `Studio`: the OptPilot UI server
- `Code Server`: the embedded editor for the selected workspace
- `OpenHands`: the assistant runtime
- `Sandbox`: the per-workspace container runtime used by Code Server and
  assistant shell/debug tools

Useful workspace runtime options:

```bash
uv run optpilot ui \
  --workspace-runtime-bin docker \
  --workspace-runtime-image optpilot/workspace-dev:latest \
  --workspace-runtime-port-start 18766
```

When no image is specified, Studio builds and uses
`optpilot/workspace-dev:latest` from its packaged runtime Dockerfile. The image
includes Code Server, Python, `uv`, Node.js, npm, git, ripgrep, and common build
tools. Set `OPTPILOT_WORKSPACE_RUNTIME_IMAGE` or pass
`--workspace-runtime-image` to use a deployment-managed image instead.

Workspace containers default to `2` CPUs, `4g` memory, a `1024` process limit,
and Docker/Podman `no-new-privileges`. Override these with:

```bash
OPTPILOT_WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS=3600
OPTPILOT_WORKSPACE_RUNTIME_CPUS=4
OPTPILOT_WORKSPACE_RUNTIME_MEMORY=8g
OPTPILOT_WORKSPACE_RUNTIME_PIDS_LIMIT=2048
OPTPILOT_WORKSPACE_RUNTIME_NO_NEW_PRIVILEGES=true
```

Studio reserves per-workspace Code Server ports from existing runtime records,
so a container can safely hold a port even before Code Server is reachable.
Studio also stops idle workspace containers after the configured idle timeout
when no assistant session is attached, no selected editor workspace is using the
container, and Code Server is not reachable. Idle cleanup stops the container;
it does not delete workspace files or runtime cache.

Hosted deployments can restrict workspace images with a comma-separated
allowlist:

```bash
OPTPILOT_WORKSPACE_RUNTIME_IMAGE_ALLOWLIST="optpilot/workspace-dev:*,ghcr.io/coder/code-server:*,registry.example.com/optpilot/*"
```

When an allowlist is configured, Studio refuses to build, pull, or start a
workspace runtime image outside the allowed patterns. If Studio builds the
default image locally, the configured base image must also match the allowlist.

## Assistant Behavior

The assistant panel is local-first. Conversations, visible page context,
workspace attachments, and pending approvals are stored under `.optpilot-ui/`.
When OpenHands is configured, Studio sends the current request, the visible page
context, and the allowed OptPilot tool manifest to the OpenHands agent server.

If OpenHands is not configured, Studio still keeps the conversation locally and
shows a clear disabled or misconfigured status instead of pretending the request
ran. If only a model and API key are configured, Studio may use its lightweight
model-chat fallback for simple answers, but file edits, shell commands, study
launches, and catalog registration require the OpenHands-backed tool path.

The assistant can inspect attached workspaces, read or write files inside
editable attached roots, run shell/debug commands inside the workspace
container, inspect catalog entries, validate configs, draft study YAML, inspect
run evidence, and search curated OptPilot documentation. Higher-impact actions,
including launching studies, applying catalog registration, stopping jobs, and
writing files, are approval-gated in the Studio UI.

## What The UI Does

Studio has three main project views:

- **Catalog**: reusable environments, methods, and resources.
- **Studies**: saved or drafted study YAML files.
- **Runs**: completed and running study evidence.

The workspace list shows draft workspaces, catalog edit copies, local folders,
and run-analysis workspaces. Workspaces can be attached to assistant sessions,
opened in the embedded editor, or registered as reusable catalog entries when
appropriate.

The assistant can use the visible page context plus attached workspaces. It can
inspect files, validate configs, draft study YAML, run workspace shell commands
inside the workspace container, inspect run evidence, and request approval
before actions such as launching studies or applying catalog registration.

## Custom Roots

You can pass explicit catalog or run roots:

```bash
uv run optpilot ui --catalog catalog/local_package --runs runs
```

## Design Boundary

The UI is a local-first workbench. It does not replace a full IDE, and it does not embed simulator-specific visualizations into the core platform. Environment-specific assets and frontends can still live beside the environment implementation.
