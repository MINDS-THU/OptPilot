# DEVS Generator Frontend

This React/Vite app is the browser interface for the DEVS Simulation Generator
Interface resource. It is launched by `_start_frontend.sh` and talks to the
FastAPI backend through the Studio preview proxy.

The frontend provides:

- compact design history and conversation views
- chat with the backend DEVS generation agent
- generated simulation upload and selection
- interactive structure visualization
- bounded simulation runs and result inspection
- read-only browsing of generated files

Graph visualization runs a lightweight local parser first. If local parsing is
not enough, the frontend asks the backend to parse the model using the
configured OpenRouter-compatible model. Raw API keys are not compiled into the
frontend bundle.

## Development

From this directory:

```bash
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

For normal OptPilot use, launch the parent resource instead:

```bash
../../_optpilot_launch_interface.sh
```

## Source Layout

```text
components/
  ChatInterface.tsx
  FileTreeBrowser.tsx
  GraphVisualizer.tsx
  SimulationRunPanel.tsx
  SessionSelectorPanel.tsx
  SourcePreviewPanel.tsx
services/
  agentService.ts
  graphParseService.ts
  sourceFileService.ts
App.tsx
types.ts
```
