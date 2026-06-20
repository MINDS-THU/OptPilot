---
title: UI
description: The local OptPilot workbench for browsing catalogs, launching studies, and inspecting runs.
---

# UI

OptPilot includes a lightweight local web UI.

```bash
uv run optpilot ui --open-browser
```

This starts a local server and opens the browser. Stop the server with `Ctrl-C`
in the terminal when you are done.

By default, the UI scans:

- `examples/`
- `user_catalog/`

You can pass explicit roots:

```bash
uv run optpilot ui --catalog user_catalog --runs runs
```

## What The UI Does

- Browse environments, methods, and studies.
- Inspect method/environment compatibility.
- Draft and validate study configs.
- Launch studies.
- Track UI-launched jobs.
- Inspect previous run directories, trials, candidate records, events, and files.

## Screenshots

Home:

![OptPilot UI home](assets/ui-home.png)

Study Builder:

![OptPilot study builder](assets/ui-study-builder.png)

Config Editor:

![OptPilot config editor](assets/ui-config-editor.png)

## Design Boundary

The UI is a local-first workbench. It does not replace a full IDE, and it does not embed simulator-specific visualizations into the core platform. Environment-specific assets and frontends can still live beside the environment implementation.
