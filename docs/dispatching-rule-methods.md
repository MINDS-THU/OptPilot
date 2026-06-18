---
title: Dispatching Rule Methods
description: How dispatching-rule methods connect to the job-shop example.
---

# Dispatching Rule Methods

Dispatching rules are the simplest natural method family for job-shop scheduling. The examples include both a dependency-free OptPilot baseline and a wrapper around JobShopLib's built-in dispatching-rule solver.

| Example | Method config | Environment config | Dependency |
| --- | --- | --- | --- |
| Fixed weighted rule | `examples/methods/fixed_rule_parameters/method.yaml` | `environment_rule_parameters.yaml` | none |
| Baseline Python rule file | `examples/methods/baseline_file_copy/method.yaml` | `environment_dispatch_rule.yaml` | none |
| JobShopLib dispatching rule | `examples/methods/job_shop_lib_dispatching_rule/method.yaml` | `environment_solver_code.yaml` | `job-shop-lib` |

## Dependency-Free Baselines

Run the parameter baseline:

```bash
uv run optpilot validate examples/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
```

Run the file-candidate baseline:

```bash
uv run optpilot validate examples/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run examples/studies/job_shop_dispatch_rule_baseline.yaml
```

These two studies are useful sanity checks before adding external method dependencies.

## JobShopLib Dispatching Rule

Install the examples extra:

```bash
uv sync --extra examples
```

Then run:

```bash
uv run optpilot validate examples/studies/job_shop_lib_dispatching_rule.yaml
uv run optpilot run examples/studies/job_shop_lib_dispatching_rule.yaml
```

The OptPilot method emits a generated `solver.py` candidate that imports:

```python
from job_shop_lib.dispatching.rules import DispatchingRuleSolver
```

and calls JobShopLib's solver with the configured rule:

```yaml
settings:
  dispatchingRule: most_work_remaining
```

The environment still sees only the file-candidate contract: `solver.py` must define `solve(instance, time_limit_seconds, context)`.

## Why This Method Is Included

This page shows two levels of integration:

- a tiny dependency-free method useful for first runs
- a real external-library wrapper that reuses JobShopLib rather than recreating its dispatching implementation

That contrast is useful for users deciding whether to write a small native OptPilot method or wrap an existing method library.
