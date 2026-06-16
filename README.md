# OptPilot

OptPilot is an orchestrator for AI-assisted iterative optimization over measured objectives. It standardizes the study loop around configuration, candidate handoff, evaluation against target environments or evaluation harnesses, evidence capture, lineage, and reproducibility.

OptPilot is intentionally not an optimizer, simulator, RL framework, or LLM agent framework. Those parts remain user-owned. OptPilot provides the protocol that lets those components run as repeatable studies against a shared execution and evidence model.

## Project Status

The intended user-facing surface is small and explicit:

- `EnvironmentConfig`: describes how an environment evaluates candidates and produces metrics and artifacts.
- `MethodConfig`: describes the user-owned optimization method.
- `StudyConfig`: binds one method to one environment with an objective, instances, and budget.
- CLI: `optpilot run`, `optpilot ui`, `optpilot import-frontier`
- Python entrypoint: `optpilot.runner.run_study`

OptPilot compiles the three authoring files above into an internal `StudySpec` that is recorded in the run evidence. Most users should author the public configs, not hand-write `StudySpec`.

Included in the current release:

- config loader and compiler
- `evaluate.type: python` and `evaluate.type: command`
- parameter and file candidate materialization
- candidate validation for parameter bounds and referenced file artifacts
- structured evidence capture into local JSONL and file-backed run directories
- pluggable methods through `python:module:Class` or the command method protocol
- optional Docker/Podman-compatible runtime isolation for command methods
- local process, local subprocess, and container-backed trial evaluation
- declared Docker/Podman-compatible image builds for method and environment containers
- curated examples for external simulator integration and file-edit methods
- resume and branch lineage metadata
- lightweight local UI for browsing catalog entries and run directories
- prompt and model provenance helpers for user-owned LLM-style methods

Optional integration helpers:

- Frontier-Engineering draft import support through `optpilot import-frontier`

Not included in the current release:

- built-in Bayesian optimization, RL, or LLM search algorithms
- remote execution backends
- automatic dependency inference or package installation for method and environment sandboxes
- multi-user UI auth and permissions

## Install With uv

OptPilot now uses `uv` as the recommended project manager.

Prerequisites:

- Python 3.10+
- `uv` 0.10+

Clone the repository, then create the local environment and install the package
in editable mode:

```bash
uv sync
```

Verify the CLI:

```bash
uv run optpilot --help
```

`uv sync` creates a local `.venv`, installs the package from `src/`, and uses
the checked-in `uv.lock` for reproducible dependency resolution.

## Quickstart

Run the strategic-airlift baseline study:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
```

The command prints a JSON summary with fields such as `study_id`, `run_dir`, `completed_trials`, and `best_metric`. The run directory contains the audit and evidence records for that study, including:

- `study_spec.json`
- `summary.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `method_calls.jsonl`
- `method_events.jsonl`
- `scheduler_events.jsonl`

The built-in example wraps a strategic-airlift DEVS simulator generated outside OptPilot and shows how an environment and different methods connect through configs:

- [examples/README.md](examples/README.md)
- `examples/environments/strategic_airlift_devs/environment.yaml`
- `examples/methods/baseline_file_copy/method.yaml`
- `examples/methods/openai_file_editor/method.yaml`
- `examples/studies/sa_baseline.yaml`
- `examples/studies/sa_openai_file_editor.yaml`

To browse local studies and run directories in the lightweight UI:

```bash
uv run optpilot ui --open-browser
```

## Authoring Model

Every OptPilot study is built from three small config files.

1. `EnvironmentConfig`
   Defines how OptPilot evaluates candidates. The current release supports Python
   callables and external commands.
2. `MethodConfig`
   Defines the optimization method. This can point at a built-in reference
   method, user-owned Python classes, or command methods. Command methods can
   optionally run inside a Docker/Podman-compatible container runtime.
3. `StudyConfig`
   Selects the environment and method, then adds the objective, instances,
   execution settings, and stopping budget.

An example study is exactly this pattern:

```yaml
apiVersion: optpilot.io/v1
kind: StudyConfig
name: sa-baseline

environment: ../environments/strategic_airlift_devs/environment.yaml
method: ../methods/baseline_file_copy/method.yaml

objective:
  metric: service_score
  direction: maximize

instances:
  source: files
  paths:
    - ../environments/strategic_airlift_devs/instances/sa_default.yaml

budget:
  maxTrials: 1
```

For a full walkthrough, see [docs/getting_started.md](docs/getting_started.md).
For the full schema, see [docs/config_files.md](docs/config_files.md).

## User-Owned Catalog And Code

OptPilot is designed so users own the search algorithm and the callable or command code used to evaluate an environment.

Put your own integration files under `user_catalog/`:

```text
user_catalog/
  environments/my_environment/
    environment.yaml
    evaluator.py
    instances/
    assets/
  methods/my_method/
    method.yaml
    method.py
    assets/
  studies/my_study.yaml
```

Environment callables use `module:function` for `evaluate.type: python`, for example:

```yaml
evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate
```

Methods use `python:module:Class`, for example:

```yaml
implementation:
  type: python
  callable: python:user_catalog.methods.my_method.method:MyMethod
  protocol: optpilot.method.batch.v1
```

That means you can keep your optimization logic outside the OptPilot package itself while still getting the standard study runtime and evidence model.

## Common Commands

Resume an existing run directory:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml \
  --resume-run-dir path/to/existing-run
```

Branch a new run from an earlier run:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml \
  --branch-from-run-dir path/to/existing-run
```

Start the lightweight local UI:

```bash
uv run optpilot ui --open-browser
uv run optpilot ui --catalog user_catalog --runs runs
```

Run trials through a Docker/Podman-compatible container image:

```yaml
execution:
  backend: container
  config:
    image: python:3.11
    containerExecutable: docker
    build:
      context: .
      dockerfile: Dockerfile.environment
      tag: python:3.11
```

Run a command method through its own Docker/Podman-compatible container image:

```yaml
implementation:
  type: command
  command: [python, my_agent.py, "{input_file}", "{output_file}"]
  protocol: optpilot.method.batch.v1
runtime:
  type: container
  image: my-agent-image:latest
  containerExecutable: docker
  networkPolicy: disabled
  build:
    context: .
    dockerfile: Dockerfile.agent
    tag: my-agent-image:latest
  envFromHost: [OPENAI_API_KEY]
```

Generate a Frontier-Engineering draft config when a local copy of that external project exists under `resource/`:

```bash
uv run optpilot import-frontier \
  resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning \
  --output frontier_pid_study.yaml
```

The `resource/` directory is intentionally ignored and is not part of the OptPilot release package.

`import-frontier` is an optional compatibility helper for the Frontier unified benchmark metadata layout. It writes a draft `StudyConfig`; users still provide the method implementation that proposes candidate file artifacts.

## Development Checks

Run the repository checks with `uv`:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall src/optpilot
./scripts/smoke_test.sh
```

The smoke script re-executes itself through `uv run` when needed.

## More Documentation

- [docs/getting_started.md](docs/getting_started.md)
- [docs/config_files.md](docs/config_files.md)
- [docs/ui_overhaul_design.md](docs/ui_overhaul_design.md)
- [docs/release_checklist.md](docs/release_checklist.md)

## Release Note

OptPilot is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
