# User Catalog

This folder is for user-owned OptPilot integrations. Put both configs and implementation code here.

The UI scans this folder automatically when launched from the repository root:

```bash
uv run optpilot ui --open-browser
```

## Recommended Layout

```text
user_catalog/
  environments/
    my_environment/
      environment.yaml       # default EnvironmentConfig
      evaluator.py           # Python evaluator or adapter code
      instances/
        default.yaml
      assets/                # environment-specific prompts, schemas, fixtures
  methods/
    my_method/
      method.yaml            # default MethodConfig
      method.py              # Python method implementation
      assets/                # method-specific prompts/templates
  studies/
    my_study.yaml            # project-specific binding of env + method + objective
```

This mirrors the built-in examples: an environment or method directory owns the reusable implementation and one or more reusable config variants, while studies remain project-centric.

## Referencing Environment Code

If your evaluator lives at `user_catalog/environments/my_environment/evaluator.py`:

```yaml
evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate
```

Minimal evaluator:

```python
def evaluate(candidate, instance, context):
    score = 1.0
    return {
        "status": "success",
        "metric_values": {"score": score},
        "artifacts": [],
        "event_summary": {},
    }
```

## Referencing Method Code

If your method lives at `user_catalog/methods/my_method/method.py`:

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

## Multiple Configs For One Environment Or Method

Yes, this can happen. Keep the implementation once, and add config variants in the same directory:

```text
user_catalog/environments/my_environment/
  evaluator.py
  environment_fast.yaml
  environment_high_fidelity.yaml
```

Use this when the same simulator/evaluator has different runtime settings, metric extraction, exposed files, datasets, or fidelity levels. Do the same for methods when the same method implementation has different models, hyperparameters, prompts, or runtime containers.

## Notes

- Keep large simulators or external projects in their own repository when practical, then point configs at their Python modules, command entrypoints, Dockerfiles, or workspace files.
- Use local `assets/` folders for small support files that belong with a specific environment or method.
- Generated run directories should stay outside this folder or under an ignored `runs/` directory.
