---
title: Examples
description: Built-in OptPilot examples and the integration patterns they teach.
---

# Examples

`catalog/example_package/` contains curated integrations that teach how to connect external environments and methods to OptPilot. The UI scans packages under `catalog/` by default.

Use this page as a catalog, not as a step-by-step walkthrough. Start with [Getting Started](getting-started.md) for the first successful run, then use this page to choose a tutorial or integration track.

The examples are organized in the same order as the docs navigation:

1. the shared job-shop tutorial environment
2. runnable job-shop method tracks
3. advanced package and simulator integration patterns

## Shared Job-Shop Comparison Set

The job-shop studies are the main runnable comparison set. They intentionally reuse the same small validation cases and objective whenever the candidate contract allows it:

- validation cases: `catalog/example_package/environments/job_shop_scheduling/cases/ft06_small.yaml` and `catalog/example_package/environments/job_shop_scheduling/cases/la01_tiny.yaml`
- objective: minimize `normalized_makespan`
- secondary metrics: `makespan`, `tardiness`, and `utilization`
- budget: one trial per bundled method example

This lets users compare dependency-free rules, generated file candidates, JobShopLib dispatching, simulated annealing, OR-Tools CP-SAT, and a Stable-Baselines3 rollout without changing the benchmark set.

## Readiness Overview

| Track | Status | External setup | Best for |
| --- | --- | --- | --- |
| Job-shop environment | Turnkey tutorial | None | First run and shared environment boundary |
| Dispatching rules | Turnkey code | None for baselines; `uv sync --extra examples` for JobShopLib wrapper | Native and JobShopLib dispatching rules |
| Simulated annealing | JobShopLib wrapper | `uv sync --extra examples` | Reusing JobShopLib's metaheuristic solver |
| OR-Tools CP-SAT | JobShopLib wrapper | `uv sync --extra examples` | Reusing JobShopLib's constraint-programming solver |
| Reinforcement learning | Runnable Stable-Baselines3 code | `uv sync --extra examples` | Training on method-owned samples and rolling out schedules on shared validation cases |
| LLM code-writing methods | Runnable file-editor code | Provider credentials only for real LLM edits; bundled baseline path runs without credentials | Agents that write `dispatch_rule.py` or `solver.py` |
| DEVS-Gen simulation environments | Advanced pattern | Generated simulator tree under `resource/`; `uv sync --extra sa` for the bundled Strategic Airlift simulator; provider credentials only for real LLM edits | Wrapping generated simulation projects as OptPilot environments |

## Main Tutorial Environment

The primary tutorial environment is job-shop scheduling:

- environment configs: `catalog/example_package/environments/job_shop_scheduling/`
- baseline method configs: `catalog/example_package/methods/fixed_rule_parameters/` and `catalog/example_package/methods/baseline_file_copy/`
- runnable studies:
  - `catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml`
  - `catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml`
  - `catalog/example_package/studies/job_shop_solver_code_baseline.yaml`
  - `catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml`
  - `catalog/example_package/studies/job_shop_simulated_annealing.yaml`
  - `catalog/example_package/studies/job_shop_ortools_cpsat.yaml`
  - `catalog/example_package/studies/job_shop_rl_stable_baselines.yaml`
  - `catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml`

This environment is useful because the same problem can be optimized in several ways:

| Method family | Page | Candidate contract |
| --- | --- | --- |
| Dispatching rules | [Dispatching Rule Methods](dispatching-rule-methods.md) | weighted `parameters`, `dispatch_rule.py`, or schedule-solution `parameters` |
| Simulated annealing | [Simulated Annealing Methods](simulated-annealing-methods.md) | schedule-solution `parameters` |
| OR-Tools CP-SAT | [OR-Tools CP-SAT Methods](cp-sat-methods.md) | schedule-solution `parameters` |
| Reinforcement learning | [Reinforcement Learning Methods](reinforcement-learning-methods.md) | schedule-solution `parameters` from policy rollout |
| LLM agents that write code | [LLM Code-Writing Methods](llm-code-methods.md) | `files` containing `dispatch_rule.py` or `solver.py` |

Start with [Job-Shop Environment](job-shop-environment.md).

## JobShopLib Coverage

The upstream [JobShopLib package](https://github.com/Pabloo22/job_shop_lib/tree/main/job_shop_lib) includes dispatching, constraint-programming, metaheuristic, and reinforcement-learning components. The bundled OptPilot examples make all four method families runnable:

- `job_shop_lib.dispatching.rules.DispatchingRuleSolver`
- `job_shop_lib.metaheuristics.SimulatedAnnealingSolver`
- `job_shop_lib.constraint_programming.ORToolsSolver`
- `job_shop_lib.reinforcement_learning.SingleJobShopGraphEnv`

The bundled reinforcement-learning study trains a small Stable-Baselines3 policy on separate training cases, then rolls it out on the shared validation cases. A user-owned RL method can load a checkpoint, change the policy class, or train for longer while emitting the same schedule-solution candidate used by the solver wrappers.

Support modules such as JobShopLib generation, graphs, benchmarking, and visualization are useful around experiments, but they are not method wrappers by themselves. Keep those dependencies in method code or analysis tooling unless your environment genuinely evaluates them.

## Method Tracks

After the job-shop environment page, read the method page that matches what you want to connect:

- [Dispatching Rule Methods](dispatching-rule-methods.md): fixed weighted rules, baseline Python rule files, and JobShopLib dispatching rules that emit schedule solutions.
- [Simulated Annealing Methods](simulated-annealing-methods.md): JobShopLib's simulated annealing solver producing schedule solutions.
- [OR-Tools CP-SAT Methods](cp-sat-methods.md): JobShopLib's OR-Tools CP-SAT solver producing schedule solutions.
- [Reinforcement Learning Methods](reinforcement-learning-methods.md): JobShopLib Gymnasium policy training and rollouts that produce schedule solutions.
- [LLM Code-Writing Methods](llm-code-methods.md): the OpenAI-compatible file editor bound by study config to the job-shop `dispatch_rule.py` environment.

Each method page explains which job-shop config to use, what the method should produce, and what remains user-owned.

## DEVS-Gen Generated Simulator Track

This advanced pattern starts with a simulator generated outside OptPilot by [DEVS-Gen](https://minds-thu.github.io/devs_gen/). OptPilot then copies that generated simulator into trial workspaces, applies candidate files to the copy, and runs an evaluator wrapper that returns metrics.

The bundled concrete sample uses a Strategic Airlift generated simulator at `resource/devs_gen_gallery/simulators/SA/simulator`. The baseline study does not need an API key. The OpenAI-compatible editing study needs provider credentials only for real LLM edits.

- environment: `catalog/example_package/environments/strategic_airlift_devs/environment.yaml`
- studies:
  - `catalog/example_package/studies/sa_baseline.yaml`
  - `catalog/example_package/studies/sa_openai_file_editor.yaml`

This track teaches how to use a DEVS-Gen simulator with OptPilot while keeping simulator generation outside OptPilot. See [DEVS-Gen Simulation Environments](devs-gen-simulation-environments.md).

## Curated Package Track

Reusable application packages can carry their own environments, methods, resources, adapters, examples, and tests, then be placed under `catalog/` or passed to Studio with `--catalog`. This keeps the default checkout runnable while still letting case studies and blog posts publish richer application code. See [Curated Packages](curated-packages.md).

## Layout

Built-in examples use the same layout recommended for every catalog package:

```text
catalog/example_package/
  environments/
    job_shop_scheduling/
    strategic_airlift_devs/
  methods/
    baseline_file_copy/
    fixed_rule_parameters/
    job_shop_lib_dispatching_rule/
    job_shop_lib_simulated_annealing/
    job_shop_rl_stable_baselines/
    openai_file_editor/
    ortools_cpsat_solver/
  studies/
    job_shop_*.yaml
    sa_*.yaml
```

Environment and method directories own reusable implementation code plus reusable config variants. Study files are concrete run plans: each study chooses one environment config, one method config, objective, budget, and runtime policy.

## Adapting An Example

When adapting an example to your own project:

1. Copy the relevant pattern into `catalog/local_package/` or another package under `catalog/`.
2. Update imports and paths relative to the config file that owns each field.
3. Run `optpilot validate` before running a study.
4. Use the UI compatibility view to confirm which methods match which environments.
5. Inspect `candidates.jsonl`, `observations.jsonl`, and `method_calls.jsonl` after the first run.

For field-level details, see [Configuration](configuration.md). For runtime storage and evidence, see [How A Run Works](how-it-works.md) and [Evidence](evidence.md).
