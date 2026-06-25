---
title: User Catalog
description: Where to put user-owned OptPilot environments, methods, resources, and reusable assets.
---

# User Catalog

`user_catalog/` is the recommended place for your own environments, methods,
resources, prompts, fixtures, datasets, and assets.

Use this page after you complete the built-in job-shop tutorial. The easiest first custom integration is usually a copy of one working example with your own names and paths.

The UI scans `user_catalog/` automatically when launched from the repository root:

```bash
uv run optpilot ui --open-browser
```

Stop the UI server with `Ctrl-C` in the terminal when you are done.

The catalog is for durable reusable assets:

- environments you can evaluate again in many studies
- methods you can pair with compatible environments
- resources you want to inspect, copy, or reuse across sessions

Study YAML files are different. A study is a concrete run plan, so it is saved
where you draft or launch it instead of being registered as a catalog entry.

## First Move After The Tutorial

The shortest path from the built-in examples to your own study is:

1. copy one working example into a draft workspace or local folder
2. rename imports, ids, and local paths
3. run `uv run optpilot validate` on the new study before running it

The first paths that usually need changing are `study.environmentConfig`, `study.methodConfig`, and any environment-side `evaluator.settings`, `trialWorkspace`, or `methodContext` file references.

## Recommended Layout

```text
user_catalog/
  environments/
    my_environment/
      environment.yaml
      evaluator.py
      prompts/
      assets/
  methods/
    my_method/
      method.yaml
      method.py
      prompts/
      assets/
  resources/
    my_resource/
      README.md
      optpilot.resource.yaml
```

Environment and method directories own reusable implementation and reusable
config variants. Resources are reusable reference folders. Study YAML files are
project/run plans; they are saved where you draft or launch them, not registered
as catalog entries.

If you are experimenting, start in a draft workspace. Register only the files
you want to make durable and reusable.

## Optional Graphical Interfaces

Some reusable components include a small web UI, simulator display, dashboard,
or demo app. Add an `interface` block to an environment or method config, or add
`optpilot.resource.yaml` to a resource folder.

For a resource:

```yaml
apiVersion: optpilot.io/v1
config: resource
id: my-resource
name: My Resource
tags: [frontend]

interface:
  label: Demo UI
  command: [python, -m, http.server, "5173", --bind, 0.0.0.0]
  port: 5173
  readyPath: /
  readyTimeoutSeconds: 60
```

Studio shows **Launch Interface** for catalog entries with this block. Clicking
it creates an editable draft copy, starts the command inside that workspace's
container runtime, shows preparation steps and recent command output while it
waits for the configured readiness path, and opens the port in the Preview
panel. Keep the catalog source read-only; make changes in the launched copy and
register them when they should become reusable.

## Referencing Environment Code

Minimal complete environment config:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: my-environment

evaluator:
  python: user_catalog.environments.my_environment.evaluator:evaluate
  settings:
    target: 0.5

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
def evaluate(candidate_runtime, context):
    target = context["settings"]["target"]
    return {
        "status": "success",
        "metric_values": {"score": 1.0 - abs(candidate_runtime["x"] - target)},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

## Referencing Method Code

Minimal complete method config:

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
