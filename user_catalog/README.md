# User Catalog

This folder is for user-owned OptPilot integrations. Put your own environment
code, method code, configs, prompts, datasets, fixtures, and study files here.

The UI scans this folder automatically when launched from the repository root:

```bash
uv run optpilot ui --open-browser
```

## Recommended Layout

```text
user_catalog/
  environments/
    my_environment/
      environment.yaml       # reusable environment config
      evaluator.py           # Python evaluator, command helper, or adapter code
      instances/
        default.yaml
      prompts/
      assets/
  methods/
    my_method/
      method.yaml            # reusable method config
      method.py              # Python method implementation or command helper
      prompts/
      assets/
  studies/
    my_study.yaml            # project-specific binding of environment + method
```

Environment and method directories own reusable implementation and reusable
config variants. Study files are concrete run plans.

## Environment Example

If your evaluator lives at
`user_catalog/environments/my_environment/evaluator.py`, reference it with a
normal Python import string:

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

## Method Example

If your method lives at `user_catalog/methods/my_method/method.py`, reference it
with a normal Python import string:

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

Minimal batch method:

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

One implementation can have multiple configs:

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

Use variants when the same evaluator has different datasets, fidelity levels,
metrics, exposed files, or runtime settings. Use method variants for different
models, prompts, hyperparameters, candidate batch sizes, or containers.

For the complete field reference, see `docs/configuration.md`. For the runtime
sequence from candidate proposal to evidence files, see `docs/how-it-works.md`.
