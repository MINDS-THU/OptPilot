# DEVS Backend Agent

`devs_app/run.py` starts the backend agent used by the DEVS Simulation
Interface. The shipped resource uses server mode:

```bash
python -m devs_app.run --mode server
```

The server creates per-session workspaces under `devs_app/working_dirs/`,
builds DEVS/xDEVS projects from natural-language requests, and exposes them to
the frontend through the FastAPI service in `devs_display/backend/`.

The active generation path is intentionally small:

- file tools in `default_tools/file_editing/`
- the `devs_construct_recon` constructor in `devs_tools/`
- the project visualizer/session API in `devs_display/backend/`

Generated workspaces, indexes, logs, and session registries are runtime data and
are created when the interface launches.
