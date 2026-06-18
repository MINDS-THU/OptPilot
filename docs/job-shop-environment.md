---
title: Job-Shop Environment
description: The main OptPilot example environment used to demonstrate multiple method families.
---

# Job-Shop Environment

The job-shop scheduling example is the main cross-method tutorial environment.

It demonstrates the core OptPilot idea: keep the environment boundary stable, then connect very different methods through candidate contracts.

## What It Evaluates

A job-shop instance contains jobs, operations, machine assignments, and processing times. A candidate produces either:

- dispatch-rule parameters
- a generated `dispatch_rule.py`
- a generated `solver.py`

The evaluator simulates or validates the resulting schedule and returns:

- `makespan`
- `normalized_makespan`
- `tardiness`
- `utilization`
- `feasible`
- `operation_count`

The primary tutorial objective is:

```yaml
objective:
  metric: normalized_makespan
  direction: minimize
```

## Environment Config Variants

The same environment implementation has three reusable config variants.

| Config | Candidate format | Intended methods |
| --- | --- | --- |
| `environment_rule_parameters.yaml` | `parameters` | BO, random search, Pymoo-style optimizers |
| `environment_dispatch_rule.yaml` | `files` with `dispatch_rule.py` | LLM code editing, LLM heuristic search |
| `environment_solver_code.yaml` | `files` with `solver.py` | LLM solver-code writing, OR-Tools/Gurobi-style solver scripts |

This is intentional: the problem and metrics stay the same, while the candidate contract changes.

## Run The Baselines

Parameter baseline:

```bash
uv run optpilot validate examples/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
```

File dispatch-rule baseline:

```bash
uv run optpilot validate examples/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run examples/studies/job_shop_dispatch_rule_baseline.yaml
```

Solver-code baseline:

```bash
uv run optpilot validate examples/studies/job_shop_solver_code_baseline.yaml
uv run optpilot run examples/studies/job_shop_solver_code_baseline.yaml
```

All three studies are API-free and should run from a fresh checkout.

## Parameter Contract

`environment_rule_parameters.yaml` accepts a parameter candidate:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      remaining_work_weight:
        valueType: float
        min: -5.0
        max: 5.0
      processing_time_weight:
        valueType: float
        min: -5.0
        max: 5.0
```

The evaluator converts these weights into a priority dispatching rule.

## Dispatch-Rule File Contract

`environment_dispatch_rule.yaml` accepts one editable file:

```yaml
candidate:
  format: files
  materialize:
    root: candidate
  files:
    editable:
      - path: dispatch_rule.py
```

The generated file must define:

```python
def score(operation, machine, state):
    ...
```

Higher scores are scheduled first.

## Solver-Code File Contract

`environment_solver_code.yaml` accepts one editable file:

```yaml
candidate:
  format: files
  materialize:
    root: candidate
  files:
    editable:
      - path: solver.py
```

The generated file must define:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

The evaluator independently validates the returned schedule. A generated solver does not get credit for an infeasible schedule.

## Wrapper Principle

The job-shop example is written as a thin wrapper. `simulator.py` represents the environment-facing scheduling API; `evaluator.py` is the OptPilot boundary.

For your own environment, follow the same pattern:

1. use the existing Python API, CLI, output files, or database
2. write a small evaluator wrapper beside it
3. define a candidate contract
4. keep method code outside the environment
