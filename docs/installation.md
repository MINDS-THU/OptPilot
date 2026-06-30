---
title: Installation
description: Choose between the core CLI/SDK install and the full OptPilot Studio source checkout.
---

# Installation

OptPilot has two intended installation modes.

Most new users should start with the **source checkout for the tutorial and
Studio**. It includes the bundled tutorial package and the local GUI, so it is
the fastest way to see the complete workflow.

Use the **Core CLI/SDK** when you already have an OptPilot package or want to
integrate your own environment and method code with the public YAML schema from
a terminal.

## Which Install Should I Use?

| Install mode | Best for | Includes | Does not include |
| --- | --- | --- | --- |
| Core CLI/SDK | Users building or running OptPilot packages in their own project. | Python package, JSON Schema validation, `optpilot run`, `optpilot validate`, `optpilot package validate`, local and container runtimes declared in configs. | Studio UI, OpenHands assistant, embedded Code Server, bundled `catalog/example_package/`. |
| Source checkout: tutorial and Studio | Users exploring the built-in tutorial or self-hosting OptPilot Studio. | Everything in core, the bundled job-shop example package, Studio UI, workspace management, assistant integration, docs, and contributor workflow. | A production multi-user deployment. |

Both modes use the same public config model. A package that validates and runs
with the core CLI should also be browsable in Studio when you put it under a
Studio catalog root.

## Prerequisites

| Capability | Required for | Requirement |
| --- | --- | --- |
| Python | Core CLI/SDK and Studio | Python 3.10 or newer. |
| `uv` | Source checkout, examples, docs, and Studio | Recommended for the full local workflow. |
| Docker or Podman | Container runtimes, embedded Code Server, workspace previews, and assistant tools that execute in workspaces | Optional until you use those features. |
| Python 3.12 environment for OpenHands | OpenHands agent-server runtime | The local bridge has been checked with `openhands-agent-server==1.29.0`; OpenHands currently expects Python 3.12. |
| API key for a model provider | LLM methods or assistant model chat | Optional; configure only for workflows that declare or use it. |

## Core CLI/SDK

Install the core package from PyPI:

```bash
python -m pip install optpilot
```

Use it inside a project that already contains OptPilot configs:

```bash
optpilot package validate path/to/package
optpilot validate path/to/package/studies/my_study.yaml
optpilot run path/to/package/studies/my_study.yaml
```

The core install supports the public configuration schema, including component
`runtime` settings. If a config asks for a container runtime, Docker or Podman
must be available on the machine running the command. If a config asks for a
local process runtime, its dependencies must be installable in that local
workspace.

For direct CLI runs, `envFromHost` reads from the shell process environment.
Values saved in Studio settings are local to Studio-managed setup, interface,
assistant, and study-launch paths; they are not read by plain `optpilot run`.

The core install is the right distribution target for package authors. A package
can contain:

```text
my_package/
  environments/
  methods/
  resources/
  studies/
```

See [Packages and Catalogs](catalog.md) for the package layout.

## Source Checkout: Tutorial And Studio

Clone the repository when you want the full local Studio:

```bash
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync --all-packages --group examples --group docs
```

Verify the CLI and bundled example package:

```bash
uv run optpilot --help
uv run optpilot package validate catalog/example_package
```

Continue with [First Job-Shop Run](getting-started.md) for the first runnable
study, expected output, and evidence inspection commands.

Launch Studio:

```bash
uv run optpilot ui --open-browser
```

The default Studio URL is:

```text
http://127.0.0.1:8765/
```

Use `--port` when you want a specific port:

```bash
uv run optpilot ui --host 127.0.0.1 --port 8866 --open-browser
```

Studio scans packages under `catalog/` by default. The repository ships the
job-shop tutorial package at `catalog/example_package/`.

For the assistant-enabled Studio workflow, also run an OpenHands agent server
and make Docker or Podman available for workspace containers. See
[OptPilot Studio](ui.md), [Workspace Management](studio-workspaces.md), and
[OptPilot Assistant](assistant.md).

## Optional Example Dependencies

The full source sync above installs the optional example dependency group. If
you started from a smaller source environment and later want to run JobShopLib,
OR-Tools CP-SAT, simulated annealing, or Stable-Baselines examples, run:

```bash
uv sync --all-packages --group examples
```

The dependency-free job-shop baseline and tuner do not require external solver
or LLM dependencies.

## Documentation Server

From a source checkout:

```bash
uv run --group docs mkdocs serve
```

The local docs URL is usually `http://127.0.0.1:8000/OptPilot/`.
