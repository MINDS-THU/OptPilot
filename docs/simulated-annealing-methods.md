---
title: Simulated Annealing Methods
description: How the JobShopLib simulated annealing example connects to OptPilot.
---

# Simulated Annealing Methods

JobShopLib includes a simulated annealing solver for job-shop scheduling. The OptPilot example does not reimplement that solver. It wraps JobShopLib as a method that emits a `solver.py` file candidate.

The included method is:

```text
examples/methods/job_shop_lib_simulated_annealing/
```

It targets:

```text
examples/environments/job_shop_scheduling/environment_solver_code.yaml
```

## Install Optional Dependency

JobShopLib is intentionally an example dependency, not a core OptPilot dependency:

```bash
uv sync --extra examples
```

## Run It

```bash
uv sync --extra examples
uv run optpilot validate examples/studies/job_shop_simulated_annealing.yaml
uv run optpilot run examples/studies/job_shop_simulated_annealing.yaml
```

This study uses `execution.backend: local_subprocess`. JobShopLib's annealer is built on `simanneal`, which uses process-level signal handling; running the evaluator in a subprocess avoids thread-pool signal limitations.

## What The Method Produces

The OptPilot method writes a generated `solver.py` candidate. That file imports:

```python
from job_shop_lib.metaheuristics import SimulatedAnnealingSolver
```

and exposes the environment-facing function:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

The job-shop evaluator imports `solver.py`, calls `solve(...)`, validates the returned schedule, and records the metrics.

## Boundary

JobShopLib owns:

- the simulated annealing implementation
- the schedule representation used by the solver
- the neighborhood and objective behavior

OptPilot owns:

- launching the method
- storing the generated `solver.py` as a candidate
- materializing the candidate into a trial workspace
- calling the environment evaluator
- recording evidence

This demonstrates the intended integration pattern: keep the external method intact and write a thin OptPilot wrapper around it.
