# Example Package

This package contains runnable OptPilot example environments, methods, case
data, and studies. It is both a tutorial package and a reference for how new
packages should be organized under `catalog/`.

For explanations, use the public docs:

- [First Job-Shop Run](../../docs/getting-started.md) for the first local run
- [Packages and Catalogs](../../docs/catalog.md) for the package layout and
  local package model
- [Candidate Contracts](../../docs/candidate-contracts.md) for the
  environment/method boundary
- [Job-Shop Tutorial Map](../../docs/examples.md) for the full example package
- [Job-Shop Environment](../../docs/job-shop-environment.md) for the main
  tutorial environment

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
  studies/
    job_shop_rule_parameters_baseline.yaml
    job_shop_tune_dispatch_weights.yaml
```

Environment and method folders own reusable config variants and implementation
code. Study files are concrete run plans that bind one environment, one method,
objective, budget, and execution policy. Other packages may also include
resource folders for reusable reference material, datasets, or launchable apps.

Python import strings should be local to the config folder, with `pythonPath`
pointing at that folder. For this package, imports look like:

```yaml
evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
```

For user-owned registrations, Studio creates `catalog/local_package/` on
demand. Registered configs should use the same local-import pattern.

## Current Retained Studies

These four dependency-free studies compile and run through the retained
process-study path. The first two use parameter candidates; the latter two use
file candidates:

```bash
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_tune_dispatch_weights.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_solver_code_baseline.yaml \
  --package-root catalog/example_package
```

After installing the optional example dependencies, the three JobShopLib solver
studies and the Stable-Baselines study are retained-launchable too:

```bash
uv sync --all-packages --group examples
uv run optpilot run catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_simulated_annealing.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_ortools_cpsat.yaml \
  --package-root catalog/example_package
uv run optpilot run catalog/example_package/studies/job_shop_rl_stable_baselines.yaml \
  --package-root catalog/example_package
```

Those methods receive environment-owned, package-backed
`methodContext.references` as a retained read-only projection. This capability
is also how the file baselines read their environment-owned starting templates.
File methods publish candidates through a runtime-private staging authority;
OptPilot captures each admitted bundle and materializes it into an isolated
attempt workspace for evaluation.

All nine studies are eligible for retained execution. The OpenAI editor has one
additional local setup requirement:

| Study | Retained capability | Local setup |
| --- | --- | --- |
| `job_shop_openai_dispatch_rule.yaml` | `ready` | Add `OPENROUTER_API_KEY` under Studio Settings → Local environment variables, or export it before a CLI launch. |

Package validation and retained launch capability are deliberately separate.
Use `optpilot package validate ... --check-imports` to validate all authored
configs and inspect each study's `retained_execution` capability. The solver and
RL examples additionally need their optional dependencies. For the
OpenAI-compatible file editor, OptPilot retains the declared variable name but
resolves its value separately for each Run and supplies it only to that Run's
Method process. The value is not copied into Run evidence.
