---
title: Simulated Annealing Methods
description: How the JobShopLib simulated annealing example connects to OptPilot.
---

# Simulated Annealing Methods

!!! warning "Current runtime status"

    This study is retained-launchable after the optional example dependencies
    are installed. Its environment-owned case references are captured from the
    package and projected read-only into the retained method worker.


Simulated annealing is a method-side search algorithm. In this example,
OptPilot does not implement the annealer and the environment does not import
it. The method wraps JobShopLib's `SimulatedAnnealingSolver` and returns
complete schedules to the shared job-shop evaluator.

## Contract

This method targets:

```text
catalog/example_package/environments/job_shop_scheduling/environment_schedule_solution.yaml
```

It accepts the `schedule-solution-candidate` capability, reads validation cases
from `methodContext.references`, and returns:

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

The candidate is the finished schedule bundle. The annealing trajectory,
temperature schedule, neighbors, and objective helpers stay inside the method.

## Run It

Install optional example dependencies:

```bash
uv sync --all-packages --group examples
```

Validate and run the study:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_simulated_annealing.yaml
uv run optpilot run catalog/example_package/studies/job_shop_simulated_annealing.yaml \
  --package-root catalog/example_package
```

The retained runner resolves `methodContext.references` from the captured
package snapshot. This study uses only that read-only method context; it does
not require the supported parameter `trialWorkspace` seed path, and it does not
depend on the still-pending file-candidate path.

Expected result:

- the run should complete one trial with `failure_count: 0`
- the Workbench Candidates page should contain a `parameters` candidate whose `spec`
  contains `solutions`
- the Workbench Observations page should contain the shared job-shop metrics, including
  `normalized_makespan`
- import failures usually mean the optional examples dependency group has not
  been installed in the current source checkout

## Method Settings

The method settings control the annealer:

```yaml
settings:
  initialTemperature: 2500.0
  endingTemperature: 2.5
  steps: 1000
  updates: 0
  seed: 0
```

OptPilot passes these settings to the method. JobShopLib interprets them.

## Boundary

JobShopLib owns:

- the simulated annealing implementation
- the schedule representation used internally by the solver
- the neighborhood and objective behavior

OptPilot owns:

- validating method/environment compatibility
- exposing environment-owned case references to the method
- storing the schedule-solution candidate
- calling the environment evaluator
- recording observations and artifacts

This is the pattern for existing metaheuristic libraries: keep the library
intact, write a small method wrapper, and return the candidate shape the
environment already accepts.
