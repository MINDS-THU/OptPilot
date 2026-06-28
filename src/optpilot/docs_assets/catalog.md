---
title: Catalog
description: How OptPilot organizes packages of environments, methods, resources, and studies.
---

# Catalog

`catalog/` is the local shelf of OptPilot packages. Each direct child is a
package that can contain environments, methods, resources, studies, code,
prompts, fixtures, and package-specific docs.

The repository ships one package:

```text
catalog/
  example_package/
```

Studio scans packages under `catalog/` when launched from the repository root:

```bash
uv run optpilot ui --open-browser
```

## Catalog vs Package

A catalog is the collection of packages available to OptPilot. A package is one
curated folder inside that collection.

Adding a package should add a new sibling under `catalog/`; it should not
overwrite `example_package` or any user-created package:

```text
catalog/
  example_package/       # bundled runnable examples
  local_package/         # created on demand for user registrations
  job_shop_case_study/   # future curated package
  my_lab_project/        # user-owned package
```

This keeps packages removable, reviewable, and easy to update. If two packages
contain similar ids, keep both folders and resolve the conflict in the UI or by
renaming the entry inside one package.

## Package Layout

```text
catalog/my_package/
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
  studies/
    my_study.yaml
```

Environment and method directories own reusable implementation and reusable
config variants. Resources are reusable reference folders, simulator
interfaces, datasets, or launchable apps. Study YAML files are concrete run
plans that bind one environment, one method, objective, budget, and runtime.

## Local Package

The repo does not ship an empty user package. When Studio registers user-owned
files, it creates `catalog/local_package/` on demand and copies selected files
there.

If you create files manually, use normal Python import strings that match the
folder path. For example, if your evaluator lives at
`catalog/local_package/environments/my_environment/evaluator.py`, reference it
as:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: my-environment

evaluator:
  python: catalog.local_package.environments.my_environment.evaluator:evaluate
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

If your method lives at `catalog/local_package/methods/my_method/method.py`,
reference it as:

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method

entrypoint:
  python: catalog.local_package.methods.my_method.method:MyMethod
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

## Optional Interfaces

Some reusable components include a small web UI, simulator display, dashboard,
or demo app. Add an `interface` block to an environment or method config, or
add `optpilot.resource.yaml` to a resource folder.

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
panel.

For the complete field-by-field schema, see [Configuration](configuration.md).
For the runtime sequence from candidate proposal to evidence files, see
[How A Run Works](how-it-works.md).
