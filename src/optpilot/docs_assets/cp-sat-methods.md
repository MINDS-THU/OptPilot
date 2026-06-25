---
title: OR-Tools CP-SAT Methods
description: How the JobShopLib OR-Tools CP-SAT example connects to OptPilot.
---

# OR-Tools CP-SAT Methods

Constraint programming is a strong natural fit for job-shop scheduling. JobShopLib already includes an OR-Tools CP-SAT solver, so the OptPilot example reuses that implementation instead of building its own model.

In upstream JobShopLib, this solver is exposed from `job_shop_lib.constraint_programming` as `ORToolsSolver`. The bundled OptPilot wrapper imports it in method code and emits the same schedule-solution contract used by the other solver methods.

The included method is:

```text
examples/methods/ortools_cpsat_solver/
```

It targets:

```text
examples/environments/job_shop_scheduling/environment_schedule_solution.yaml
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

The method reads the job-shop case references exposed through `methodContext.references`, runs JobShopLib's OR-Tools solver for each case, and emits schedule solutions:

Candidate `spec` payload fragment:

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

The method owns the CP-SAT call:

```python
from job_shop_lib.constraint_programming import ORToolsSolver
```

The evaluator independently validates the returned schedule. This keeps the environment reusable for any solver that can produce the same schedule-solution shape.

## Why OR-Tools, Not Gurobi

Gurobi can model job-shop scheduling, but it is not the best default example for this release because it adds licensing and installation friction. OR-Tools CP-SAT is open, widely used for scheduling, and already exposed by JobShopLib. That makes it the better demonstration of OptPilot's ability to wrap a solver method while keeping the tutorial reproducible.

Users who already have a Gurobi implementation can connect it either as a method that emits schedule-solution parameters or through the `solver.py` file-candidate contract. In both cases, the Gurobi dependency stays outside the environment.
