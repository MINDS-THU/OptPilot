# Getting Started With OptPilot

OptPilot orchestrates iterative optimization studies. You provide an environment evaluator and a user-owned method. OptPilot runs candidates, records observations, and keeps the evidence trail reproducible.

## Install

```bash
uv sync
uv run optpilot --help
```

## Run A Study

```bash
uv run optpilot run examples/studies/toy_random_search.yaml
```

The run creates a directory containing `study_spec.json`, `summary.json`, `observations.jsonl`, `trials.jsonl`, `artifacts.jsonl`, `method_calls.jsonl`, `scheduler_events.jsonl`, and environment snapshot files.

Other examples:

```bash
uv run optpilot run examples/studies/toy_cli_random_search.yaml
uv run optpilot run examples/studies/toy_user_method.yaml
uv run optpilot run examples/studies/toy_lifecycle_method.yaml
uv run optpilot run examples/studies/toy_evidence_aware_method.yaml
```

## The Three Configs

`EnvironmentConfig` defines the evaluator and candidate contract.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: toy-factory
evaluate:
  type: python
  callable: optpilot.examples.toy_factory_env:evaluate
candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Toy factory parameters.
  parameters:
    schema:
      x: {type: float, min: 0.0, max: 8.0}
metrics:
  source: return
  keys: [throughput]
```

`MethodConfig` defines the optimization method.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: reference-random-search
implementation:
  type: python
  callable: builtin.reference_random_search
  protocol: optpilot.method.batch.v1
config:
  batchSize: 4
compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
```

`StudyConfig` binds one environment to one method.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: StudyConfig
name: toy-random-search
environment: ../environments/toy_factory.yaml
method: ../methods/reference_random_search.yaml
objective:
  metric: throughput
  direction: maximize
budget:
  maxTrials: 12
```

## User-Owned Methods

Python methods use `python:module:Class`. A simple method implements:

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng):
        self.definition = definition

    def propose(self, n_candidates, study_state):
        return [...]

    def observe(self, observations):
        pass
```

Longer-running methods can implement `start`, `poll`, `finalize`, and optionally `intervene`.

## UI

```bash
uv run optpilot ui --open-browser
```

The UI scans configs, launches studies, shows running jobs, and inspects previous run evidence.

## Frontier Draft Import

```bash
uv run optpilot import-frontier \
  resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning \
  --output frontier_pid_study.yaml
```

See [config_files_v3alpha.md](config_files_v3alpha.md) for schema details.

