# Getting Started

This guide runs the built-in strategic-airlift example and shows how the three config files work together.

## Install

```bash
uv sync
uv run optpilot --help
```

Useful commands:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
uv run optpilot ui --open-browser
```

## What The Example Contains

The example wraps a strategic-airlift DEVS simulator generated outside OptPilot. OptPilot does not own that simulator; it copies the declared simulator files into each trial workspace, lets a method propose file edits, and calls the evaluator to measure the result.

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

The simulator source tree is expected at the path declared in:

```yaml
workspace:
  copy:
    - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
      to: simulator
      role: source
```

If that path does not exist, the baseline run will fail during file materialization. That is intentional: the environment config explicitly declares the external simulator files it needs.

## Run The Baseline

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
```

This study uses:

- Environment: `examples/environments/strategic_airlift_devs/environment.yaml`
- Method: `examples/methods/baseline_file_copy/method.yaml`
- Study binding: `examples/studies/sa_baseline.yaml`

The baseline method emits the unmodified editable simulator file. This is the first sanity check to run before trying an LLM or search method.

## Inspect The Three Configs

### 1. Environment

`examples/environments/strategic_airlift_devs/environment.yaml` defines the candidate contract and evaluator.

```yaml
evaluate:
  type: python
  callable: examples.environments.strategic_airlift_devs.evaluator:evaluate
```

This is implementation-bound. The callable must exist, and it must accept:

```python
evaluate(artifact_spec, instance, context)
```

The same environment declares file candidates:

```yaml
candidate:
  type: files
  artifactKind: code_bundle
  files:
    root: simulator
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
```

This tells compatible methods:

- They are producing file artifacts, not plain parameter vectors.
- The artifact kind is `code_bundle`.
- The editable file is `MissionController.py`.
- The source tree is copied into the trial workspace under `simulator`.

The evaluator reports metrics by returning them directly:

```yaml
metrics:
  source: return
  keys: [service_score, delivered_count, expired_count, generated_count, mean_latency, max_latency, delivery_ratio, expiration_ratio]
```

The study objective must use one of these keys.

### 2. Method

`examples/methods/baseline_file_copy/method.yaml` points to method code:

```yaml
implementation:
  type: python
  callable: python:examples.methods.baseline_file_copy.method:BaselineFileCopyMethod
  protocol: optpilot.method.batch.v1
```

This is implementation-bound. OptPilot imports the class and constructs it with:

```python
BaselineFileCopyMethod(definition, study_spec, rng)
```

The method declares compatibility:

```yaml
compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext:
    - files.source
    - files.editable
```

This is how OptPilot knows the method can run on the strategic-airlift environment. It checks compatibility before launching a study.

### 3. Study

`examples/studies/sa_baseline.yaml` binds the environment and method:

```yaml
environment: ../environments/strategic_airlift_devs/environment.yaml
method: ../methods/baseline_file_copy/method.yaml
```

Then it chooses the objective, instance set, budget, backend, and evidence level:

```yaml
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

## What The Run Produces

By default, runs are written under a `runs/` directory near the study config. Each run contains:

```text
summary.json
study_spec.json
observations.jsonl
trials.jsonl
artifacts.jsonl
method_calls.jsonl
scheduler_events.jsonl
environment_snapshot.json
run_policy.json
run_lineage.json
```

Important first files to inspect:

| File | What to look for |
| --- | --- |
| `summary.json` | Best metric, best trial, failure count, run directory. |
| `study_spec.json` | The compiled form of your three configs. |
| `observations.jsonl` | Trial statuses and metric values. |
| `artifacts.jsonl` | Candidate validation and materialization details. |
| `method_calls.jsonl` | What the method was asked to propose and what it returned. |

## Run The UI

```bash
uv run optpilot ui --open-browser
```

The UI scans `examples/` and `user_catalog/` by default. It lets you:

- Browse available environments and methods.
- See which methods are compatible with an environment.
- Create and launch a study without hand-editing every field.
- Inspect previous run evidence.

## Add Your Own Environment And Method

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

Environment callable example:

```yaml
evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate
```

Method callable example:

```yaml
implementation:
  type: python
  callable: python:user_catalog.methods.my_method.method:MyMethod
  protocol: optpilot.method.batch.v1
```

Then create a study config that points to those two YAML files.

## Next Reading

- [How A Run Works](how-it-works.md) explains the runtime flow.
- [Configuration](configuration.md) is the detailed schema reference.
- [User Catalog](user-catalog.md) explains how to organize user-owned integrations.
