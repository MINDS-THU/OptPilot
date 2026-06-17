# Getting Started

This guide runs the strategic-airlift example and explains the three YAML files that make it work.

## Install

```bash
uv sync
uv run optpilot --help
```

Useful commands:

```bash
uv run optpilot validate examples/studies/sa_baseline.yaml
uv run optpilot run examples/studies/sa_baseline.yaml
uv run optpilot ui --open-browser
```

## Example Layout

The example wraps a strategic-airlift DEVS simulator generated outside OptPilot. OptPilot does not own the simulator. The environment config declares which simulator files are copied into each trial workspace and which file a method can edit.

```text
examples/
  environments/
    strategic_airlift_devs/
      environment.yaml
      evaluator.py
      instances/sa_default.yaml
      prompts/sa_file_edit_system_prompt.md
  methods/
    baseline_file_copy/
      method.yaml
      method.py
    openai_file_editor/
      method.yaml
      method.py
  studies/
    sa_baseline.yaml
    sa_openai_file_editor.yaml
```

The simulator source tree is declared in `environment.yaml`:

```yaml
trialWorkspace:
  - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
    to: simulator
```

That copy happens once for each trial workspace. If the source path does not exist, file candidate materialization fails before evaluation.

## Run The Baseline

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
```

The baseline method emits the unmodified editable simulator file. Run it first to confirm the simulator can be copied and evaluated.

## Environment Config

`examples/environments/strategic_airlift_devs/environment.yaml` is a public environment config:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: sa-simulator-code-edit
```

It points to a Python evaluator:

```yaml
evaluator:
  python: examples.environments.strategic_airlift_devs.evaluator:evaluate
```

The callable must exist and accept:

```python
evaluate(artifact_spec, instance, context)
```

The environment declares a file candidate contract:

```yaml
candidate:
  format: files
  materialize:
    root: simulator
  files:
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
```

This tells compatible methods that they must produce a file candidate that edits `MissionController.py`. OptPilot copies the simulator into the trial workspace, applies the candidate file into `simulator`, then calls the evaluator.

Metrics are returned by the evaluator:

```yaml
metrics:
  source: return
  keys:
    - service_score
    - delivered_count
    - expired_count
    - generated_count
```

The study objective must use one of these metric keys.

## Method Config

`examples/methods/baseline_file_copy/method.yaml` is a public method config:

```yaml
apiVersion: optpilot.io/v1
config: method
id: baseline-file-copy

entrypoint:
  python: examples.methods.baseline_file_copy.method:BaselineFileCopyMethod
  protocol: batch

accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
```

OptPilot imports the class and constructs it with:

```python
BaselineFileCopyMethod(definition, study_spec, rng)
```

`accepts` is the compatibility declaration. It tells OptPilot that this method can work with environments whose candidate format is `files` and whose context includes editable files.

## Study Config

`examples/studies/sa_baseline.yaml` binds one environment to one method:

```yaml
apiVersion: optpilot.io/v1
config: study
name: sa-baseline

environmentConfig: ../environments/strategic_airlift_devs/environment.yaml
methodConfig: ../methods/baseline_file_copy/method.yaml

objective:
  metric: service_score
  direction: maximize

instances:
  source: files
  paths:
    - ../environments/strategic_airlift_devs/instances/sa_default.yaml

budget:
  maxTrials: 1

execution:
  backend: local
  parallelism: 1
```

Study paths are resolved from the study file. Environment paths are resolved from the environment file. Method paths are resolved from the method file.

## Inspect The Run

Runs are written under `runs/` unless an output directory is provided.

Important files:

| File | What to inspect |
| --- | --- |
| `summary.json` | Best metric, best trial, failure count, run directory. |
| `study_spec.json` | Compiled internal spec generated from the three YAML files. |
| `observations.jsonl` | Trial statuses and metric values. |
| `trials.jsonl` | Trial inputs and backend metadata. |
| `artifacts.jsonl` | Candidate validation and materialization details. |
| `method_calls.jsonl` | Method requests and responses. |

## Use The UI

```bash
uv run optpilot ui --open-browser
```

The UI scans `examples/` and `user_catalog/` by default. It lets you browse environments and methods, check compatibility, draft studies, launch runs, and inspect previous run evidence.

## Add Your Own Code

Put user-owned integrations under `user_catalog/`:

```text
user_catalog/
  environments/my_environment/
    environment.yaml
    evaluator.py
  methods/my_method/
    method.yaml
    method.py
  studies/my_study.yaml
```

Use [Configuration](configuration.md) for the full schema and [User Catalog](user-catalog.md) for layout guidance.
