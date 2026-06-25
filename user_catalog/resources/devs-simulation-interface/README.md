# DEVS Simulation Interface

This resource contains the DEVS project workspace used to generate, inspect,
and visualize discrete-event simulation projects from natural-language
descriptions.

OptPilot Studio can launch its graphical interface directly from the Catalog.
The launch flow copies this resource into an editable workspace, starts the
backend API on port `8000`, starts the Vite frontend on port `3000`, and opens
the frontend in the Studio Preview panel.

The first launch in a fresh copied workspace may take a few minutes because it
creates a local `.venv`, installs the Python runtime dependencies, and runs
`npm install` for the frontend. Later launches from the same copied workspace
reuse those installed dependencies.

Useful entry points:

- `_optpilot_launch_interface.sh`: Studio-facing one-click launcher.
- `_start_backend.sh`: starts the DEVS backend API.
- `_start_frontend.sh`: starts the Vite frontend.
- `devs_app/run.py`: backend agent/server entry point.
- `devs_display/frontend/`: frontend application.
- `devs_display/backend/`: FastAPI service used by the frontend.
