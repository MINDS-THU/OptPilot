---
title: OR-Tools CP-SAT Methods
description: How the JobShopLib OR-Tools CP-SAT example connects to OptPilot.
---

# OR-Tools CP-SAT Methods

Constraint programming is a natural fit for job-shop scheduling. The example
reuses JobShopLib's OR-Tools CP-SAT wrapper instead of building a new model in
OptPilot.

The solver is method-owned. The job-shop environment only receives and scores
the resulting schedules.

## Contract

The included method is:

```text
catalog/example_package/methods/ortools_cpsat_solver/
```

It targets:

```text
catalog/example_package/environments/job_shop_scheduling/environment_schedule_solution.yaml
```

The method reads validation cases from `methodContext.references`, runs
JobShopLib's `ORToolsSolver` for each case, and returns schedule-solution
parameters:

```yaml
solutions:
  ft06_small:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 3
```

## Run It

Install optional example dependencies:

```bash
uv sync --all-packages --group examples
```

Run the study:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_ortools_cpsat.yaml
uv run optpilot run catalog/example_package/studies/job_shop_ortools_cpsat.yaml
```

## Method Settings

The bundled method exposes a time limit:

```yaml
settings:
  timeLimitSeconds: 10.0
```

The method owns the CP-SAT call:

```python
from job_shop_lib.constraint_programming import ORToolsSolver
```

The evaluator independently validates the returned schedules. This keeps the
environment reusable for any solver that can produce the same
schedule-solution shape.

## Why OR-Tools, Not Gurobi

Gurobi can model job-shop scheduling, but it adds licensing and installation
friction. OR-Tools CP-SAT is open, widely used for scheduling, and already
exposed by JobShopLib. That makes it a better default tutorial example.

Users who already have a Gurobi implementation can connect it the same way:
wrap it as a method that emits schedule-solution parameters. If the candidate
should be generated source code instead, use the `solver.py` file-candidate
contract described in [LLM Code-Writing Methods](llm-code-methods.md).
