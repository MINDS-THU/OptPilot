# DEVS Simulation Generator Interface

This OptPilot resource launches a browser interface for generating xDEVS
discrete-event simulation projects from natural-language descriptions. It is a
resource, not an optimization environment or method: users open it as an
editable workspace, interact with the GUI, and inspect the generated simulator
code.

OptPilot Studio launches it from the Catalog by copying this resource into an
editable workspace, starting the backend API on port `8000`, starting the Vite
frontend on port `3000`, and opening the frontend in Studio Preview.

## What Is Included

- `optpilot.resource.yaml`: Catalog metadata and launch declaration.
- `_optpilot_launch_interface.sh`: Studio-facing launcher.
- `_start_backend.sh`: Starts the DEVS generation backend.
- `_start_frontend.sh`: Starts the Vite frontend.
- `devs_app/run.py`: Backend agent entry point.
- `devs_display/backend/`: FastAPI session, project, chat, and graph APIs.
- `devs_display/frontend/`: Browser UI for sessions, generated projects, and visualizations.
- `devs_tools/devs_construct_recon/`: Active DEVS project construction engine.
- `default_tools/file_editing/`: Minimal file operations used by the generation agent.
- `devs_settings.py`: Shared defaults for model ids, graph parsing, and concurrency.
- `src/monitoring.py`: Lightweight logger used by the backend agent.

## Runtime Data

The launcher creates runtime-only folders as needed:

- `.venv/`
- `devs_app/working_dirs/`
- `devs_app/persistent_storage/`
- `devs_app/index_dir/`
- `devs_display/.storage/`
- `devs_display/frontend/node_modules/`
- `backend.run.log`

These folders and logs are not part of the curated package.

## Launching From Studio

Use the resource action in OptPilot Studio. The first launch in a fresh copied
workspace can take a few minutes because it creates a local Python environment
and installs frontend dependencies. Later launches reuse those dependencies.

Studio must provide `OPENROUTER_API_KEY` because the generation backend uses an
OpenRouter-compatible LiteLLM model. Configure it in Studio Settings under
Environment & Secrets, or export it before starting Studio.

The resource config declares one required host variable:

```yaml
interface:
  envFromHost:
    - OPENROUTER_API_KEY
```

The public defaults are ordinary interface environment values:

```yaml
interface:
  env:
    DEVS_INTERFACE_MODEL_ID: openrouter/openai/gpt-5.4
    DEVS_INTERFACE_CONCURRENCY: "8"
    DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS: "240"
    DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS: "6"
```

Advanced users can override these values in an editable copy before launching.
`DEVS_INTERFACE_STRONG_MODEL_ID` is also supported; when omitted, it uses the
same model as `DEVS_INTERFACE_MODEL_ID`.

Optional local-auth variables are intentionally not declared in `envFromHost`.
If `DEVS_DISPLAY_PASSWORD` is set manually, the backend enables a lightweight
single-password gate. When it is not set, authentication is disabled for local
development.

## Launching Manually

From this directory:

```bash
export OPENROUTER_API_KEY="..."
./_optpilot_launch_interface.sh
```

Then open the frontend at `http://127.0.0.1:3000`.

## Runtime Write Policy

The interface writes generated projects, logs, and dependency installs into the
editable copy. These folders are runtime data and should not be committed back
to the curated catalog package.

## Scope

This resource intentionally excludes benchmark suites, prior experiment logs,
paper artifacts, and alternative baseline-agent runners. It is meant to be a
clean example resource and a useful tool: launch the GUI, describe a simulation,
inspect the generated xDEVS project, run it, visualize its structure, and
iterate.
