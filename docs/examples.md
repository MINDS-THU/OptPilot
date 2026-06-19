---
title: Examples
description: Built-in OptPilot examples and the integration patterns they teach.
---

# Examples

`examples/` contains curated integrations that teach how to connect external environments and methods to OptPilot. The UI scans this folder by default together with `user_catalog/`.

Use this page as a catalog, not as a step-by-step walkthrough. Start with [Getting Started](getting-started.md) for the first successful run.

The examples are organized around one main tutorial environment plus separate method-family tracks.

## Readiness Overview

| Track | Status | External setup | Best for |
| --- | --- | --- | --- |
| Job-shop environment | Turnkey tutorial | None | First run and shared environment boundary |
| Dispatching rules | Turnkey code | None for baselines; `uv sync --extra examples` for JobShopLib wrapper | Native and JobShopLib dispatching rules |
| Simulated annealing | JobShopLib wrapper | `uv sync --extra examples` | Reusing JobShopLib's metaheuristic solver |
| OR-Tools CP-SAT | JobShopLib wrapper | `uv sync --extra examples` | Reusing JobShopLib's constraint-programming solver |
| Reinforcement learning | Integration pattern | JobShopLib plus a user-owned trained policy | Rolling out policies that emit schedules |
| LLM code-writing methods | Repo code with provider setup | Provider credentials for LLM methods | Agents that write `dispatch_rule.py` or `solver.py` |
| LLM heuristic repositories | Integration templates | Upstream repository clone, dependency install, command wiring, often provider credentials | Wrapping existing LLM search repositories |
| Strategic Airlift DEVS | Advanced example | Generated simulator tree under `resource/devs_gen_gallery/simulators/SA/simulator`; provider credentials only for the LLM editing study | File-candidate simulator workflows |

## Main Tutorial Environment

The primary tutorial environment is job-shop scheduling:

- environment configs: `examples/environments/job_shop_scheduling/`
- baseline method configs: `examples/methods/fixed_rule_parameters/` and `examples/methods/baseline_file_copy/`
- runnable studies:
  - `examples/studies/job_shop_rule_parameters_baseline.yaml`
  - `examples/studies/job_shop_dispatch_rule_baseline.yaml`
  - `examples/studies/job_shop_solver_code_baseline.yaml`
  - `examples/studies/job_shop_lib_dispatching_rule.yaml`
  - `examples/studies/job_shop_simulated_annealing.yaml`
  - `examples/studies/job_shop_ortools_cpsat.yaml`

This environment is useful because the same problem can be optimized in several ways:

| Method family | Page | Candidate contract |
| --- | --- | --- |
| Dispatching rules | [Dispatching Rule Methods](dispatching-rule-methods.md) | `parameters` or `files` |
| Simulated annealing | [Simulated Annealing Methods](simulated-annealing-methods.md) | schedule-solution `parameters` |
| OR-Tools CP-SAT | [OR-Tools CP-SAT Methods](cp-sat-methods.md) | schedule-solution `parameters` |
| Reinforcement learning | [Reinforcement Learning Methods](reinforcement-learning-methods.md) | schedule-solution `parameters` |
| LLM agents that write code | [LLM Code-Writing Methods](llm-code-methods.md) | `files` containing `dispatch_rule.py` or `solver.py` |
| Existing LLM heuristic-search repositories | [LLM Heuristic Repositories](llm-heuristic-methods.md) | generated file from upstream command |

Start with [Job-Shop Environment](job-shop-environment.md).

## Method Tracks

After the job-shop environment page, read the method page that matches what you want to connect:

- [Dispatching Rule Methods](dispatching-rule-methods.md): fixed weighted rules, baseline Python rule files, and JobShopLib dispatching rules that emit schedule solutions.
- [Simulated Annealing Methods](simulated-annealing-methods.md): JobShopLib's simulated annealing solver producing schedule solutions.
- [OR-Tools CP-SAT Methods](cp-sat-methods.md): JobShopLib's OR-Tools CP-SAT solver producing schedule solutions.
- [Reinforcement Learning Methods](reinforcement-learning-methods.md): JobShopLib Gymnasium policy rollouts that produce schedule solutions.
- [LLM Code-Writing Methods](llm-code-methods.md): LLM agents or workflows that generate `dispatch_rule.py` or `solver.py`.
- [LLM Heuristic Repositories](llm-heuristic-methods.md): existing repositories such as FunSearch, EoH, ReEvo, HeurAgenix, and EoH-S.

Each method page explains which job-shop config to use, what the method should produce, and what remains user-owned.

## Generated Simulator Track

This advanced example requires the generated Strategic Airlift simulator tree at `resource/devs_gen_gallery/simulators/SA/simulator`. The baseline study does not need an API key. The OpenAI-compatible editing study does.

The Strategic Airlift DEVS example demonstrates a different use case: generating a discrete-event simulator outside OptPilot and then configuring OptPilot to evaluate it.

- environment: `examples/environments/strategic_airlift_devs/environment.yaml`
- studies:
  - `examples/studies/sa_baseline.yaml`
  - `examples/studies/sa_openai_file_editor.yaml`

This track teaches how to use a simulator generated by `devs_gen_code`, copy that generated simulator into trial workspaces, and apply candidate edits only to disposable trial copies. See [Strategic Airlift DEVS](strategic-airlift-devs.md).

## Existing Method Repository Track

These are integration templates: clone the upstream repository, install its dependencies, identify the generated file, and wire the command into the OptPilot method config.

The `llm_heuristic_search/` method directory demonstrates how to wrap existing LLM-based heuristic-search repositories as coarse-grained OptPilot methods.

Included method templates:

- `examples/methods/llm_heuristic_search/funsearch_command.yaml`
- `examples/methods/llm_heuristic_search/eoh_command.yaml`
- `examples/methods/llm_heuristic_search/reevo_command.yaml`
- `examples/methods/llm_heuristic_search/heuragenix_command.yaml`
- `examples/methods/llm_heuristic_search/eohs_command.yaml`

These templates are not turnkey studies. They show how to point OptPilot at one upstream command and one generated file. See [LLM Heuristic Repositories](llm-heuristic-methods.md).

## Layout

Built-in examples use the same layout recommended for `user_catalog/`:

```text
examples/
  environments/
    job_shop_scheduling/
    strategic_airlift_devs/
  methods/
    baseline_file_copy/
    fixed_rule_parameters/
    job_shop_lib_dispatching_rule/
    job_shop_lib_simulated_annealing/
    llm_heuristic_search/
    openai_file_editor/
    ortools_cpsat_solver/
  studies/
    job_shop_*.yaml
    sa_*.yaml
```

Environment and method directories own reusable implementation code plus reusable config variants. Study files are concrete run plans: each study chooses one environment config, one method config, objective, instances, budget, and runtime policy.

## Adapting An Example

When adapting an example to your own project:

1. Copy the relevant pattern into `user_catalog/`.
2. Update imports and paths relative to the config file that owns each field.
3. Run `optpilot validate` before running a study.
4. Use the UI compatibility view to confirm which methods match which environments.
5. Inspect `candidates.jsonl`, `observations.jsonl`, and `method_calls.jsonl` after the first run.

For field-level details, see [Configuration](configuration.md). For runtime storage and evidence, see [How A Run Works](how-it-works.md) and [Evidence](evidence.md).
