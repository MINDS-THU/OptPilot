---
title: User Catalog
description: Where to put user-owned OptPilot environments, methods, configs, and study files.
---

# User Catalog

`user_catalog/` is the recommended place for your own environments, methods, prompts, fixtures, datasets, assets, and study files.

Use this page after you complete the built-in job-shop tutorial. The easiest first custom integration is usually a copy of one working example with your own names and paths.

The UI scans `user_catalog/` automatically when launched from the repository root:

```bash
uv run optpilot ui --open-browser
```

## First Move After The Tutorial

The shortest path from the built-in examples to your own study is:

1. copy one working example into `user_catalog/`
2. rename imports, ids, and local paths
3. run `uv run optpilot validate` on the new study before running it

The first paths that usually need changing are `study.environmentConfig`, `study.methodConfig`, instance paths, and any environment-side `trialWorkspace` or `methodContext` file references.

## Recommended Layout

```text
user_catalog/
  environments/
    my_environment/
      environment.yaml
      evaluator.py
      instances/
        default.yaml
      prompts/
      assets/
  methods/
    my_method/
      method.yaml
      method.py
      prompts/
      assets/
  studies/
    my_study.yaml
```

Environment and method directories own reusable implementation and reusable config variants. Studies remain project-centric bindings.

## Referencing Environment Code

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

Minimal evaluator:

```python
def evaluate(candidate_runtime, instance, context):
    return {
        "status": "success",
        "metric_values": {"score": 1.0},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

## Referencing Method Code

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

Minimal method:

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng=None):
        self.definition = definition

    def propose(self, n_candidates, study_state):
        return [
            {
                "candidate_id": f"candidate-{index}",
                "format": "parameters",
                "spec": {"x": 1.0},
                "generator": {"method_id": self.definition["id"]},
            }
            for index in range(n_candidates)
        ]

    def observe(self, observations):
        return None
```

## Multiple Config Variants

The same implementation can have multiple configs:

```text
user_catalog/environments/my_environment/
  evaluator.py
  environment_fast.yaml
  environment_high_fidelity.yaml

user_catalog/methods/my_method/
  method.py
  method_small_model.yaml
  method_large_model.yaml
```

Use variants when the same evaluator has different fidelity levels, datasets, exposed files, metrics, or runtime settings. Use method variants for different models, prompts, hyperparameters, candidate batch sizes, or containers.

For the complete field-by-field schema, see [Configuration](configuration.md). For the runtime sequence from candidate proposal to evidence files, see [How A Run Works](how-it-works.md).
