# DEVS Simulation Interface

This OptPilot resource is a compact workspace for building discrete-event
simulation projects from natural-language descriptions.

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

The backend model can be customized with environment variables before launch:

```bash
export DEVS_INTERFACE_MODEL_ID="openrouter/openai/gpt-5.4"
export DEVS_INTERFACE_STRONG_MODEL_ID="openrouter/openai/gpt-5.4"
export DEVS_INTERFACE_CONCURRENCY="8"
```

## Launching Manually

From this directory:

```bash
./_optpilot_launch_interface.sh
```

Then open the frontend at `http://127.0.0.1:3000`.

## Scope

This package intentionally excludes benchmark suites, prior experiment logs,
paper artifacts, and alternative baseline-agent runners. It is meant to be a
clean example resource: launch the GUI, ask it to generate a DEVS simulator,
inspect the generated project, and iterate.
