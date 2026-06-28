---
title: Simulated Annealing Methods
description: How the JobShopLib simulated annealing example connects to OptPilot.
---

# Simulated Annealing Methods

JobShopLib includes a simulated annealing solver for job-shop scheduling. The OptPilot example does not reimplement that solver and does not generate solver code. It wraps JobShopLib as a method that emits complete schedule solutions.

In upstream JobShopLib, simulated annealing lives under `job_shop_lib.metaheuristics` alongside the annealer, neighbor generators, and objective helpers. The bundled OptPilot wrapper uses `SimulatedAnnealingSolver` directly and keeps all of that dependency on the method side.

The included method is:

```text
catalog/example_package/methods/job_shop_lib_simulated_annealing/
```

It targets:

```text
catalog/example_package/environments/job_shop_scheduling/environment_schedule_solution.yaml
```

## Install Optional Dependency

JobShopLib is intentionally an example dependency, not a core OptPilot dependency:

```bash
uv sync --extra examples
```

## Run It

```bash
uv sync --extra examples
uv run optpilot validate catalog/example_package/studies/job_shop_simulated_annealing.yaml
uv run optpilot run catalog/example_package/studies/job_shop_simulated_annealing.yaml
```

## What The Method Produces

The method reads the shared validation case references from `methodContext.references`, runs `SimulatedAnnealingSolver` for each case, and emits:

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

The method settings control the annealer:

Method `settings` fragment:

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
- exposing the validation case references to the method
- storing the schedule-solution candidate
- calling the environment evaluator
- recording evidence

This demonstrates the intended integration pattern for existing optimization libraries: keep the external method intact, let the method produce a candidate shape the environment accepts, and keep the evaluator independent from the solver library.
