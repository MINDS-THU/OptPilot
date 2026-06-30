---
title: Installation
description: Choose between the core CLI/SDK install and the full OptPilot Studio source checkout.
---

# Installation

OptPilot has two intended installation modes.

Use the **Core CLI/SDK** when you want to integrate your own environment and
method code with OptPilot's public YAML schema and run studies from a terminal.

Use the **full OptPilot Studio source checkout** when you want the local GUI,
workspace editor, assistant, and bundled tutorial package.

## Which Install Should I Use?

| Install mode | Best for | Includes | Does not include |
| --- | --- | --- | --- |
| Core CLI/SDK | Users building or running OptPilot packages in their own project. | Python package, JSON Schema validation, `optpilot run`, `optpilot validate`, `optpilot package validate`, local and container runtimes declared in configs. | Studio UI, OpenHands assistant, embedded Code Server, bundled `catalog/example_package/`. |
| Full Studio source checkout | Users self-hosting OptPilot Studio or exploring the built-in tutorial. | Everything in core, the bundled job-shop example package, Studio UI, workspace management, assistant integration, docs and contributor workflow. | A production multi-user deployment. |

Both modes use the same public config model. A package that validates and runs
with the core CLI should also be browsable in Studio when you put it under a
Studio catalog root.

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

## Full OptPilot Studio

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
uv run optpilot validate catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
```

Launch Studio:

```bash
uv run optpilot ui --open-browser
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
