---
title: Job-Shop Environment
description: The main OptPilot example environment used to demonstrate multiple method families.
---

# Job-Shop Environment

The job-shop scheduling example is the main cross-method tutorial environment.

It demonstrates the core OptPilot idea: keep the environment boundary stable, then connect very different methods through candidate contracts.

## What It Evaluates

A job-shop instance contains jobs, operations, machine assignments, and processing times. A candidate produces either:

- weighted dispatch-rule parameters
- complete schedule solutions
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

The same environment implementation has four reusable config variants.

| Config | Candidate format | Intended methods |
| --- | --- | --- |
| `environment_rule_parameters.yaml` | `parameters` | Dependency-free weighted [dispatching rules](dispatching-rule-methods.md) |
| `environment_schedule_solution.yaml` | `parameters` with `solutions` | External solvers and policies, including JobShopLib [dispatching rules](dispatching-rule-methods.md), [simulated annealing](simulated-annealing-methods.md), and [OR-Tools CP-SAT](cp-sat-methods.md) |
| `environment_dispatch_rule.yaml` | `files` with `dispatch_rule.py` | [Dispatching rules](dispatching-rule-methods.md), [LLM code-writing methods](llm-code-methods.md), [LLM heuristic repositories](llm-heuristic-methods.md) |
| `environment_solver_code.yaml` | `files` with `solver.py` | [LLM code-writing methods](llm-code-methods.md) and user-provided solver-code methods |

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

Simulated annealing:

```bash
uv sync --extra examples
uv run optpilot validate examples/studies/job_shop_simulated_annealing.yaml
uv run optpilot run examples/studies/job_shop_simulated_annealing.yaml
```

OR-Tools CP-SAT:

```bash
uv sync --extra examples
uv run optpilot validate examples/studies/job_shop_ortools_cpsat.yaml
uv run optpilot run examples/studies/job_shop_ortools_cpsat.yaml
```

The baseline studies run from a fresh checkout without API keys or provider credentials. The JobShopLib dispatching-rule, simulated annealing, and CP-SAT studies additionally require the optional `examples` dependency.

## Weighted-Rule Parameter Contract

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

## Schedule-Solution Contract

`environment_schedule_solution.yaml` accepts complete schedules keyed by OptPilot study instance id:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      solutions:
        valueType: object
        properties: {}
```

The method produces:

```yaml
solutions:
  ft06_small:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 3
  la01_tiny:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 2
```

The keys `ft06_small` and `la01_tiny` come from the study instance file names. OptPilot exposes the same ids and instance payloads to methods through `study_state.instances`, so a solver method can solve the exact benchmark set used by the evaluator.

This contract is suitable for any method that produces finished schedules: JobShopLib, OR-Tools, Gurobi, a trained RL policy, or an internal company solver. The environment only validates and scores schedules. It does not know which method or library produced them.

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

Use this file contract when the candidate itself is code. For example, an LLM code-writing method may produce a new `solver.py`; a JobShopLib wrapper should normally produce schedule solutions instead.

## Wrapper Principle

The job-shop example is written as a thin wrapper. `simulator.py` represents the environment-facing scheduling API; `evaluator.py` is the OptPilot boundary.

For your own environment, follow the same pattern:

1. use the existing Python API, CLI, output files, or database
2. write a small evaluator wrapper beside it
3. define a candidate contract
4. keep method code outside the environment

## Next: Choose A Method Track

After you understand the environment configs, choose the method page that matches the optimizer you want to connect:

- [Dispatching Rule Methods](dispatching-rule-methods.md)
- [Simulated Annealing Methods](simulated-annealing-methods.md)
- [OR-Tools CP-SAT Methods](cp-sat-methods.md)
- [LLM Code-Writing Methods](llm-code-methods.md)
- [LLM Heuristic Repositories](llm-heuristic-methods.md)
