---
title: Simulated Annealing Methods
description: How the JobShopLib simulated annealing example connects to OptPilot.
---

# Simulated Annealing Methods

JobShopLib includes a simulated annealing solver for job-shop scheduling. The OptPilot example does not reimplement that solver and does not generate solver code. It wraps JobShopLib as a method that emits complete schedule solutions.

The included method is:

```text
examples/methods/job_shop_lib_simulated_annealing/
```

It targets:

```text
examples/environments/job_shop_scheduling/environment_schedule_solution.yaml
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

## What The Method Produces

The method reads the shared study instances from `study_state.instances`, runs `SimulatedAnnealingSolver` for each instance, and emits:

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

The method settings control the annealer:

```yaml
settings:
  initialTemperature: 2500.0
  endingTemperature: 2.5
  steps: 1000
  updates: 0
  seed: 0
```

The environment validates the returned schedules and records the metrics.

## Boundary

JobShopLib owns:

- the simulated annealing implementation
- the schedule representation used by the solver
- the neighborhood and objective behavior

OptPilot owns:

- launching the method
- exposing the study instances to the method
- storing the schedule-solution candidate
- calling the environment evaluator
- recording evidence

This demonstrates the intended integration pattern for existing optimization libraries: keep the external method intact, let the method produce a general candidate contract, and keep the evaluator independent from the solver library.
