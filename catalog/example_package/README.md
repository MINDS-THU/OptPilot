# Example Package

This package contains runnable OptPilot example environments, methods,
resources, and studies. It is both a tutorial package and a reference for how
new packages should be organized under `catalog/`.

For explanations, use the public docs:

- `docs/getting-started.md` for the first local run
- `docs/catalog.md` for the package layout and local package model
- `docs/candidate-contracts.md` for the environment/method boundary
- `docs/examples.md` for the full example catalog
- `docs/job-shop-environment.md` for the main tutorial environment

## Catalog Model

`catalog/` is the shelf; each direct child is a package:

```text
catalog/
  example_package/
  local_package/
  another_package/
```

Adding a new package should add another sibling folder. It should not overwrite
this package. That keeps example code, user-owned code, and future case study
packages easy to inspect, update, and remove.

## What A Package Can Contain

```text
catalog/example_package/
  environments/
    job_shop_scheduling/
      environment_rule_parameters.yaml
      evaluator.py
      cases/
      training_cases/
      rl_env_adapter.py
      prompts/
  methods/
    fixed_rule_parameters/
      method.yaml
      method.py
    tune_dispatch_weights/
      method.yaml
      method.py
  resources/
    devs-simulation-interface/
      README.md
      optpilot.resource.yaml
  studies/
    job_shop_rule_parameters_baseline.yaml
    job_shop_tune_dispatch_weights.yaml
```

Environment and method folders own reusable config variants and implementation
code. Resource folders hold reusable reference material, simulator interfaces,
datasets, or launchable apps. Study files are concrete run plans that bind one
environment, one method, objective, budget, and execution policy.

Python import strings should be local to the config folder, with `pythonPath`
pointing at that folder. For this package, imports look like:

```yaml
evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
```

For user-owned registrations, Studio creates `catalog/local_package/` on
demand. Registered configs should use the same local-import pattern.

## Quick Runs

Dependency-free job-shop baselines and parameter tuning:

```bash
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_tune_dispatch_weights.yaml
uv run optpilot run catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_solver_code_baseline.yaml
```

JobShopLib and Stable-Baselines examples:

```bash
uv sync --extra examples
uv run optpilot run catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml
uv run optpilot run catalog/example_package/studies/job_shop_simulated_annealing.yaml
uv run optpilot run catalog/example_package/studies/job_shop_ortools_cpsat.yaml
uv run optpilot run catalog/example_package/studies/job_shop_rl_stable_baselines.yaml
```

LLM code-editing example:

```bash
uv run optpilot run catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml
```

The OpenAI-compatible editing study runs its baseline candidate without
provider credentials. Real LLM edits require provider credentials and a larger
trial budget.
