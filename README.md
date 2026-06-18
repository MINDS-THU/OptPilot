# OptPilot

OptPilot is a lightweight orchestration layer for iterative optimization studies. It connects a user-owned method to a user-owned environment, runs candidate solutions, records objective metrics, and keeps an auditable evidence trail.

OptPilot is not an optimizer, simulator, RL framework, or LLM agent framework. Those pieces remain yours. OptPilot standardizes the loop around them:

1. A method proposes one or more candidates.
2. OptPilot validates and materializes each candidate.
3. An environment evaluates the candidate and reports metrics.
4. OptPilot records trials, observations, saved output files, method calls, and run metadata.
5. The method can use the accumulated evidence to propose the next candidates.

## Current Surface

Users author three public YAML config files:

- `config: environment`: candidate contract, evaluator, metrics, trial workspace, saved output-file rules, and optional records.
- `config: method`: method entrypoint, protocol, settings, compatibility requirements, and optional method runtime.
- `config: study`: the concrete run binding an environment config to a method config with objective, instances, budget, execution, and evidence settings.

OptPilot validates those YAML files with packaged JSON Schemas, compiles them into an internal `StudySpec`, and writes the compiled spec into every run directory.

Included in the current release:

- JSON Schema validation for public environment, method, and study configs
- parameter, file, and opaque candidate contracts
- Python and command environment evaluators
- Python and command methods with batch protocol, plus Python session protocol
- local thread, local subprocess, and Docker/Podman-compatible environment execution
- Docker/Podman-compatible command-method runtime isolation
- local JSONL evidence store with run summaries, trials, observations, candidate records, saved output files, method calls, and events
- curated job-shop scheduling tutorial environment with parameter and file-candidate variants
- strategic-airlift DEVS example using an external generated simulator
- local UI for browsing catalogs, checking compatibility, launching studies, and inspecting runs

Not included:

- built-in Bayesian optimization, RL, LLM, or metaheuristic algorithms
- remote execution backends
- automatic dependency inference or package installation
- multi-user UI authentication

## Prerequisites

OptPilot currently supports Python 3.10 and newer.

Before running the examples below, install:

- Python 3.10+
- `uv`

## Install

OptPilot uses `uv`.

```bash
uv sync
uv run optpilot --help
```

## Quickstart

Start with the job-shop parameter baseline. It is the recommended first run, works from a fresh checkout, and does not require API keys or external solvers.

Run the job-shop parameter baseline:

```bash
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
```

Validate a config without running it:

```bash
uv run optpilot validate examples/studies/job_shop_rule_parameters_baseline.yaml
```

Open the local UI:

```bash
uv run optpilot ui --open-browser
```

The UI scans `examples/` and `user_catalog/` by default.

Advanced examples and integration templates such as Strategic Airlift and `llm_heuristic_search` require extra setup. Use the job-shop example first, then continue with the example-specific docs.

## Minimal Config Shape

Study config:

```yaml
apiVersion: optpilot.io/v1
config: study
name: job-shop-rule-parameters-baseline

environmentConfig: ../environments/job_shop_scheduling/environment_rule_parameters.yaml
methodConfig: ../methods/fixed_rule_parameters/method.yaml

objective:
  metric: normalized_makespan
  direction: minimize

instances:
  source: files
  paths:
    - ../environments/job_shop_scheduling/instances/ft06_small.yaml

budget:
  maxTrials: 1

execution:
  backend: local
  parallelism: 1
```

Environment evaluator:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: my-environment

evaluator:
  python: user_catalog.environments.my_environment.evaluator:evaluate

candidate:
  format: parameters
  parameters:
    schema:
      x:
        valueType: float
        min: 0.0
        max: 1.0

metrics:
  source: return
  keys: [score]
```

Method entrypoint:

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method

entrypoint:
  python: user_catalog.methods.my_method.method:MyMethod
  protocol: batch

accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
```

Python evaluator references use `module:function`. Python method references use `module:Class`.

## User-Owned Catalog

Put your own environments, methods, and studies under `user_catalog/`:

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

Environment and method directories own reusable implementation code and reusable config variants. Study configs are project-specific bindings.

## Container Runtime Example

Run environment trials in a container:

```yaml
execution:
  backend: local
  runtime:
    sandbox: container
    network: disabled
    container:
      image: python:3.11-slim
      executable: docker
```

Run a command method in its own container:

```yaml
entrypoint:
  command: [python, my_agent.py, "{input_file}", "{output_file}"]
  protocol: batch

runtime:
  sandbox: container
  network: disabled
  container:
    image: my-agent-image:latest
    executable: docker
    build:
      context: .
      dockerfile: Dockerfile.agent
      tag: my-agent-image:latest
  envFromHost: [OPENAI_API_KEY]
```

## Documentation

- [Getting Started](docs/getting-started.md)
- [Configuration Reference](docs/configuration.md)
- [How A Run Works](docs/how-it-works.md)
- [User Catalog](docs/user-catalog.md)
- [UI](docs/ui.md)

Build the docs locally:

```bash
uv run --extra docs mkdocs serve
```

## Development Checks

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall src/optpilot
./scripts/smoke_test.sh
```

OptPilot is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
