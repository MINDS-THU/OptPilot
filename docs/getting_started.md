# Getting Started With OptPilot

This guide shows the current alpha workflow for installing OptPilot, running a
study, and authoring your own configs.

## 1. What OptPilot Does

OptPilot orchestrates iterative optimization studies over measured objectives,
typically by evaluating candidates against external target environments or
evaluation harnesses. It does four main jobs:

1. Loads a small user-facing study definition.
2. Invokes a controller and engine to propose candidates.
3. Evaluates those candidates in a target environment.
4. Records normalized evidence, lineage, and reproducibility metadata.

OptPilot does not own your optimization algorithm. It owns the runtime and the
study protocol around that algorithm.

## 2. Prerequisites

- Python 3.10 or newer
- `uv`

From the repository root, create the local environment:

```bash
uv sync
```

Check that the CLI is available:

```bash
uv run optpilot --help
```

## 3. Run The First Example

The fastest way to understand the platform is to run the reference study:

```bash
uv run optpilot run examples/studies/toy_random_search.yaml
```

That study uses:

- a toy Python environment in `examples/environments/toy_factory.yaml`
- the built-in reference random-search engine in
  `examples/methods/reference_random_search.yaml`
- one fixed instance in `examples/instances/toy_factory_case.yaml`

The CLI prints a JSON summary like this shape:

```json
{
  "study_id": "study-...",
  "run_dir": "/tmp/...",
  "completed_trials": 12,
  "best_metric": 98.49,
  "best_trial_id": "trial-...",
  "best_artifact_id": "artifact-..."
}
```

The exact values vary by run, but the key fields are stable for the alpha CLI.

## 4. Understand The Three Config Files

OptPilot expects you to author three config kinds.

### EnvironmentConfig

`EnvironmentConfig` defines how the target environment is evaluated and what
kind of candidate it accepts.

Example:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: toy-factory

evaluate:
  type: python
  callable: optpilot.examples.toy_factory_env:evaluate

candidate:
  type: parameters
  schema:
    x:
      type: float
      min: 0.0
      max: 8.0
    y:
      type: int
      min: 1
      max: 10
    mode:
      type: categorical
      values: [balanced, aggressive, conservative]

metrics:
  source: return
  keys: [throughput, cycle_time]
```

Current alpha support:

- `evaluate.type: python`
- `evaluate.type: command`
- `evaluate.type: custom`
- `candidate.type: parameters`
- `candidate.type: files`
- `candidate.type: opaque`

### MethodConfig

`MethodConfig` defines the controller and engine used to propose candidates.

Built-in reference example:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: reference-random-search

engine:
  implementation: builtin.reference_random_search
  config:
    batchSize: 4
```

User-owned example:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: fixed-parameter-engine

engine:
  implementation: python:examples.user_engines.fixed_parameter_engine:FixedParameterEngine
  config:
    batchSize: 3
    candidates:
      - {x: 4.2, y: 7, mode: balanced}
      - {x: 2.0, y: 4, mode: conservative}
```

### StudyConfig

`StudyConfig` ties everything together.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: StudyConfig
name: toy-random-search

environment: ../environments/toy_factory.yaml
method: ../methods/reference_random_search.yaml

objective:
  metric: throughput
  direction: maximize

instances:
  source: files
  paths:
    - ../instances/toy_factory_case.yaml

budget:
  maxTrials: 12

execution:
  backend: local
  parallelism: 4
  timeoutSeconds: 120

reproducibility:
  seed: 7
```

OptPilot expands these authoring files into an internal `StudySpec` and stores
that expanded representation in the run evidence.

## 5. Write Your Own Study

The normal workflow is:

1. Define how the environment is evaluated in an `EnvironmentConfig`.
2. Define how candidates are proposed in a `MethodConfig`.
3. Bind them together in a `StudyConfig`.
4. Run the study with `uv run optpilot run path/to/study.yaml`.

When choosing the environment contract, start with the narrowest useful form:

- Use `evaluate.type: python` if you already have a Python callable.
- Use `evaluate.type: command` if your benchmark is a script or CLI tool.
- Use `candidate.type: parameters` if the search object is structured data.
- Use `candidate.type: files` if you are evolving source files or bundles.

## 6. Plug In User-Owned Python Code

Two extension forms matter for most users.

Environment callable:

```text
module:function
```

Controller or engine class:

```text
python:module:Class
```

The repository examples are intentionally outside `src/optpilot` to show the
ownership boundary. OptPilot owns the protocol. You own the optimization logic.

If you run from the repository root with `uv run`, the `examples` package is
importable for the bundled examples.

## 7. Inspect Run Outputs

Each study run creates a run directory that contains the normalized audit trail
for the study. Common files include:

- `study_spec.json`
- `summary.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `controller_decisions.jsonl`
- `engine_snapshots.jsonl`
- `scheduler_events.jsonl`
- `environment_snapshot.json`
- `run_policy.json`

These files are the main product of OptPilot. They let you inspect what was
proposed, what was evaluated, what succeeded or failed, and how the run can be
reproduced later.

## 8. Common CLI Workflows

Run a study:

```bash
uv run optpilot run path/to/study.yaml
```

Resume a run in place:

```bash
uv run optpilot run path/to/study.yaml \
  --resume-run-dir path/to/existing-run
```

Branch a new run from an earlier run:

```bash
uv run optpilot run path/to/study.yaml \
  --branch-from-run-dir path/to/existing-run
```

Generate a draft config from a Frontier-Engineering benchmark:

```bash
uv run optpilot import-frontier \
  resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning \
  --output frontier_pid_study.yaml
```

The importer is only useful when that external project exists locally under
`resource/`.

## 9. Validate The Repository

The repository checks are also `uv`-first:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall src/optpilot
./scripts/smoke_test.sh
```

The smoke script will re-run itself under `uv` if needed.

## 10. Current Boundaries

The intended stable alpha user surface is:

- `StudyConfig`, `EnvironmentConfig`, and `MethodConfig`
- `optpilot run`
- `optpilot import-frontier`
- `optpilot.runner.run_study`

Internal execution details may still change between alpha revisions. In
particular, treat the expanded `StudySpec`, low-level adapters, and the exact
local evidence-store layout as implementation details.

For the full schema, see [config_files_v3alpha.md](config_files_v3alpha.md).