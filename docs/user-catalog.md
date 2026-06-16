---
title: User Catalog
description: Where to put user-owned OptPilot environments, methods, configs, and study files.
---

# User Catalog

`user_catalog/` is the recommended place for your own environments, methods, configs, prompts, fixtures, and study files.

The UI scans `user_catalog/` automatically when launched from the repository root.

```bash
uv run optpilot ui --open-browser
```

## Recommended Layout

```text
user_catalog/
  environments/
    my_environment/
      environment.yaml
      evaluator.py
      instances/
        default.yaml
      assets/
  methods/
    my_method/
      method.yaml
      method.py
      assets/
  studies/
    my_study.yaml
```

Environment and method directories own reusable implementation and reusable config variants. Studies remain project-centric bindings.

## Referencing Environment Code

```yaml
evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate
```

Minimal evaluator:

```python
def evaluate(candidate, instance, context):
    return {
        "status": "success",
        "metric_values": {"score": 1.0},
        "artifacts": [],
        "event_summary": {},
    }
```

## Referencing Method Code

```yaml
implementation:
  type: python
  callable: python:user_catalog.methods.my_method.method:MyMethod
  protocol: optpilot.method.batch.v1
```

Minimal method:

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng):
        self.definition = definition

    def propose(self, n_candidates, study_state):
        return [
            {
                "artifact_kind": "parameter_spec",
                "spec": {"x": 1.0},
                "generator_record": {"method_id": self.definition["id"]},
            }
            for _ in range(n_candidates)
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
```

Use variants when the same evaluator has different fidelity levels, datasets, exposed files, metrics, or runtime settings. Do the same for methods when changing models, prompts, hyperparameters, or containers.
