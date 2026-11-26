# DEVS Display Service

`devs_display` contains the FastAPI backend and React frontend used by the
DEVS Simulation Generator Interface.

The backend owns:

- session and project storage
- chat requests sent to the DEVS generation agent
- generated project file reads and uploads
- graph parsing for generated xDEVS projects
- optional local password protection

The frontend owns:

- session selection and chat UI
- project selection and upload
- source preview
- graph visualization
- parameterized simulation execution with summaries and a readable event-trace
  table

The service is launched through the resource-level
`_optpilot_launch_interface.sh` script. Standalone execution stores runtime files under the resource’s `.runtime/` directory.
