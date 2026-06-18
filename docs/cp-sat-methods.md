---
title: OR-Tools CP-SAT Methods
description: How the JobShopLib OR-Tools CP-SAT example connects to OptPilot.
---

# OR-Tools CP-SAT Methods

Constraint programming is a strong natural fit for job-shop scheduling. JobShopLib already includes an OR-Tools CP-SAT solver, so the OptPilot example reuses that implementation instead of building its own model.

The included method is:

```text
examples/methods/ortools_cpsat_solver/
```

It targets:

```text
examples/environments/job_shop_scheduling/environment_solver_code.yaml
```

## Install Optional Dependency

JobShopLib is intentionally not a core OptPilot dependency. Install the example extra before running this study:

```bash
uv sync --extra examples
```

## Run It

```bash
uv run optpilot validate examples/studies/job_shop_ortools_cpsat.yaml
uv run optpilot run examples/studies/job_shop_ortools_cpsat.yaml
```

## What The Method Produces

The method emits:

```text
solver.py
```

The solver file imports:

```python
from job_shop_lib.constraint_programming import ORToolsSolver
```

and defines:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

The job-shop evaluator imports that file inside the trial workspace, calls `solve(...)`, and independently validates the returned schedule.

## Why OR-Tools, Not Gurobi

Gurobi can model job-shop scheduling, but it is not the best default example for this release because it adds licensing and installation friction. OR-Tools CP-SAT is open, widely used for scheduling, and already exposed by JobShopLib. That makes it the better demonstration of OptPilot's ability to wrap a solver method while keeping the tutorial reproducible.

Users who already have a Gurobi implementation can still connect it through the same `solver.py` file-candidate contract.
