# Getting Started With OptPilot

OptPilot orchestrates iterative optimization studies. You provide an environment evaluator and a user-owned method. OptPilot runs candidates, records observations, and keeps the evidence trail reproducible.

## Install

```bash
uv sync
uv run optpilot --help
```

## Built-In Example

The built-in example under `examples/` wraps a strategic-airlift DEVS simulator generated outside OptPilot. It demonstrates a realistic file-candidate environment and two methods that can target it.

```text
examples/
  environments/strategic_airlift_devs/
    environment.yaml
    evaluator.py
    instances/sa_default.yaml
    prompts/sa_file_edit_system_prompt.md
  methods/baseline_file_copy/
    method.yaml
    method.py
  methods/openai_file_editor/
    method.yaml
    method.py
  studies/
    sa_baseline.yaml
    sa_openai_file_editor.yaml
```

The environment expects the generated simulator to exist at the `workspace.copy.from` path declared in `examples/environments/strategic_airlift_devs/environment.yaml`.

Run the baseline method first:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
```

Then run the OpenAI-compatible file editor after setting an API key:

```bash
export OPENROUTER_API_KEY=...
uv run optpilot run examples/studies/sa_openai_file_editor.yaml
```

Each run creates a directory containing `study_spec.json`, `summary.json`, `observations.jsonl`, `trials.jsonl`, `artifacts.jsonl`, `method_calls.jsonl`, `scheduler_events.jsonl`, and environment snapshot files.

## The Three Configs

`EnvironmentConfig` defines the evaluator and candidate contract.

```yaml
apiVersion: optpilot.io/v1
kind: EnvironmentConfig
id: sa-simulator-code-edit
evaluate:
  type: python
  callable: examples.environments.strategic_airlift_devs.evaluator:evaluate
candidate:
  type: files
  artifactKind: code_bundle
metrics:
  source: return
  keys: [service_score]
```

`MethodConfig` defines the optimization method.

```yaml
apiVersion: optpilot.io/v1
kind: MethodConfig
id: baseline-file-copy
implementation:
  type: python
  callable: python:examples.methods.baseline_file_copy.method:BaselineFileCopyMethod
  protocol: optpilot.method.batch.v1
compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
```

`StudyConfig` binds one environment config to one method config for a particular study.

```yaml
apiVersion: optpilot.io/v1
kind: StudyConfig
name: sa-baseline
environment: ../environments/strategic_airlift_devs/environment.yaml
method: ../methods/baseline_file_copy/method.yaml
objective:
  metric: service_score
  direction: maximize
budget:
  maxTrials: 1
```

## User Catalog

Put your own configs and implementation code under `user_catalog/` using the same structure as `examples/`:

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

Reference environment code from an `EnvironmentConfig`:

```yaml
evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate
```

Reference method code from a `MethodConfig`:

```yaml
implementation:
  type: python
  callable: python:user_catalog.methods.my_method.method:MyMethod
  protocol: optpilot.method.batch.v1
```

For the same environment or method implementation, you may keep multiple config variants in the same directory, for example `environment_fast.yaml` and `environment_high_fidelity.yaml`.

## UI

```bash
uv run optpilot ui --open-browser
```

By default, the UI scans `examples/` and `user_catalog/`. It launches studies, shows running jobs, and inspects previous run evidence.

See [configuration.md](configuration.md) for schema details.
