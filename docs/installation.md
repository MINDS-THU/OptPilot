---
title: Installation
description: Choose between the core CLI/SDK and a source checkout with Studio.
---

# Installation

OptPilot has two installation modes.

| Install | Best for | Includes |
| --- | --- | --- |
| Core CLI/SDK | Building, validating, and running your own OptPilot package. | Public schemas, package validation, Realm-backed `optpilot run`, and the Python SDK. |
| Source checkout | Developing OptPilot or using local Studio/docs/examples. | Core plus Studio, the tutorial catalog, assistant integration, docs, and contributor tooling. |

## Prerequisites

- Python 3.10 or newer
- `uv` for the source-checkout workflow
- Docker/Podman only for Studio workspace/assistant features that explicitly
  require it; the current retained study runner executes its bounded local
  process slice and does not execute container study configs

## Core CLI/SDK

```bash
python -m pip install optpilot
optpilot --help
```

Validate a package and study:

```bash
optpilot package validate path/to/package
optpilot validate path/to/package/studies/my_study.yaml
```

Run a supported retained study:

```bash
optpilot run path/to/package/studies/my_study.yaml \
  --package-root path/to/package
```

`--package-root` is required. It is the complete source authority OptPilot
captures before compilation; referenced configs, Python roots, and source-backed
callables must stay inside it.

Without `--realm-root`, the command uses OptPilot's private per-user Realm in
the OS user-data location. Use an explicit Realm root only for deliberate local
isolation/testing. It is not a Workspace or generated-output directory.

The current retained execution slice supports parameter and bounded file
candidates, Python batch methods/evaluators, local process runtime, and package-backed,
environment-owned `methodContext.references`. Those references are captured
with the package and projected read-only into the method worker. Package-owned
`trialWorkspace` files/directories are also supported as retained seed layers
for attempts; every attempt receives a fresh writable trial volume. File
candidates are frozen, sealed, and atomically admitted before their immutable
tree is projected into that volume. The slice does not yet support setup/build,
Environment/backend host-derived values, containers, or hostile native code.
A process Method may declare `runtime.envFromHost`; the launcher selects only
those names. Studio binds their current saved revisions to the new Run and
sends the values transiently to its Method worker, leaving them out of the
durable process request and Run evidence. A direct CLI launch instead uses its
exported process environment as a process-lifetime binding. Unsupported
authoring configs fail during retained compilation.

A package normally contains:

```text
my_package/
  environments/
  methods/
  resources/
  studies/
```

See [Packages and Catalogs](catalog.md).

## Source checkout and Studio

```bash
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync --all-packages --group examples --group docs
uv run optpilot --help
uv run optpilot package validate catalog/example_package --check-source
```

Four bundled studies run without optional dependencies: the fixed weighted-rule
baseline, deterministic tuner, dispatch-rule file baseline, and solver-code file
baseline. With the example dependency group installed, the three JobShopLib
solver studies and the Stable-Baselines study are retained-launchable too. Only
the OpenAI editor needs additional local setup: add `OPENROUTER_API_KEY` under
Studio Settings → Local environment variables, or export it before a CLI
launch. OptPilot supplies the value only to the Method process for that Run and
does not copy it into Run evidence.

Launch Studio:

```bash
uv run optpilot ui --open-browser
```

The default URL is `http://127.0.0.1:8765/`. To choose another port:

```bash
uv run optpilot ui --host 127.0.0.1 --port 8866 --open-browser
```

Studio scans `catalog/` by default and reads runs from the same default Realm.
See [Studio UI](ui.md), [Workspace Management](studio-workspaces.md), and
[OptPilot Assistant](assistant.md).

## Optional example dependencies

```bash
uv sync --all-packages --group examples
```

These dependencies make the following package-backed `methodContext` studies
retained-launchable:

```text
job_shop_lib_dispatching_rule.yaml
job_shop_simulated_annealing.yaml
job_shop_ortools_cpsat.yaml
job_shop_rl_stable_baselines.yaml
```

They do not enable the separate file-candidate path; parameter
`trialWorkspace` seeds need no optional runtime dependency.

## Documentation server

```bash
uv run --group docs mkdocs serve
```

The local docs URL is usually `http://127.0.0.1:8000/OptPilot/`.
