---
title: Job-Shop Tutorial
description: Built-in OptPilot examples and the integration patterns they teach.
---

# Job-Shop Tutorial

`catalog/example_package/` is the built-in tutorial package. It is a normal
OptPilot package: it contains reusable environment configs, method configs,
resources, and study files.

The package teaches one idea: keep the environment boundary clear, then connect
different method families through explicit candidate contracts.

Start with [First Job-Shop Run](getting-started.md) for the first successful
run. Use this page to choose the next tutorial track.

## Shared Job-Shop Comparison Set

The main example is job-shop scheduling. Most studies reuse the same small
validation cases and objective:

- validation cases: `ft06_small.yaml`, `la01_tiny.yaml`, and `ft06_standard.yaml`
- objective: minimize `normalized_makespan`
- secondary metrics: `makespan`, `tardiness`, and `utilization`

The studies differ in candidate contract and method implementation. That lets
you compare a parameter tuner, generated file candidates, JobShopLib solver
wrappers, OR-Tools CP-SAT, simulated annealing, reinforcement learning, and an
OpenAI-compatible file editor without changing the evaluation problem.

## What Each Page Teaches

| Page | Main lesson | Start here when |
| --- | --- | --- |
| [Job-Shop Environment](job-shop-environment.md) | One environment can expose several candidate contracts for the same metrics. | You want the full example map. |
| [Dispatching Rule Methods](dispatching-rule-methods.md) | Baselines, schema-driven parameter tuning, file candidates, and a JobShopLib rule wrapper. | You want a dependency-free optimizer first. |
| [Simulated Annealing Methods](simulated-annealing-methods.md) | Wrap an existing metaheuristic as a method that returns schedule solutions. | You have an external search library. |
| [OR-Tools CP-SAT Methods](cp-sat-methods.md) | Wrap a constraint solver without coupling the evaluator to the solver. | You have a solver implementation. |
| [Reinforcement Learning Methods](reinforcement-learning-methods.md) | Train or load a policy inside the method and return schedules for validation cases. | You need method-side training or policy rollout. |
| [LLM Code-Writing Methods](llm-code-methods.md) | Use file candidates when the candidate itself is source code. | You want an agent to write `dispatch_rule.py` or `solver.py`. |

## Readiness

| Track | Runs from fresh checkout? | Extra setup |
| --- | --- | --- |
| Fixed weighted rule | Yes | None |
| Tune weighted rule parameters | Yes | None |
| Baseline file candidates | Yes | None |
| OpenAI-compatible file editor baseline path | Yes | Provider key only for real LLM edits |
| JobShopLib dispatching rule | No | `uv sync --all-packages --group examples` |
| Simulated annealing | No | `uv sync --all-packages --group examples` |
| OR-Tools CP-SAT | No | `uv sync --all-packages --group examples` |
| Stable-Baselines3 RL | No | `uv sync --all-packages --group examples` and a working PyTorch stack |

## Built-In Studies

Dependency-free studies:

```text
catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
catalog/example_package/studies/job_shop_tune_dispatch_weights.yaml
catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
catalog/example_package/studies/job_shop_solver_code_baseline.yaml
catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml
```

Optional-dependency studies:

```text
catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml
catalog/example_package/studies/job_shop_simulated_annealing.yaml
catalog/example_package/studies/job_shop_ortools_cpsat.yaml
catalog/example_package/studies/job_shop_rl_stable_baselines.yaml
```

## Package Layout

The tutorial uses the same package layout recommended for user packages:

```text
catalog/example_package/
  environments/
    job_shop_scheduling/
  methods/
    baseline_file_copy/
    fixed_rule_parameters/
    job_shop_lib_dispatching_rule/
    job_shop_lib_simulated_annealing/
    job_shop_rl_stable_baselines/
    openai_file_editor/
    ortools_cpsat_solver/
    tune_dispatch_weights/
  studies/
    job_shop_*.yaml
```

Environment and method directories own reusable implementation code and config
variants. Study files are concrete run plans: each study chooses one
environment config, one method config, objective, budget, and execution policy.
See [Packages and Catalogs](catalog.md) for the general package model.

## Adapting An Example

When adapting an example to your own project:

1. Copy the relevant pattern into `catalog/local_package/` or another package
   under `catalog/`.
2. Keep evaluator inputs in `environment.evaluator.settings`.
3. Expose files the method must read through `environment.methodContext`.
4. Keep algorithm knobs in `method.settings`.
5. Run `uv run optpilot validate path/to/study.yaml`.
6. Inspect `candidates.jsonl`, `observations.jsonl`, and `method_calls.jsonl`
   after the first run.

For package layout guidance, see [Packages and Catalogs](catalog.md). For field-level
details, see [Configuration](configuration.md). For runtime storage and
evidence, see [How A Run Works](how-it-works.md) and [Evidence](evidence.md).
