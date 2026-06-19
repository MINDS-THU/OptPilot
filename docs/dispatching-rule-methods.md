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
| JobShopLib dispatching rule | `examples/methods/job_shop_lib_dispatching_rule/method.yaml` | `environment_schedule_solution.yaml` | `job-shop-lib` |

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

The JobShopLib method reads `study_state.instances`, calls JobShopLib for each instance, and emits a schedule-solution candidate:

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

The method owns the JobShopLib call:

```python
from job_shop_lib.dispatching.rules import DispatchingRuleSolver
```

JobShopLib also exposes individual rule functions and scorers such as shortest processing time, first-come first-served, most work remaining, most operations remaining, and random operation. To use a different built-in rule, change the method setting:

```yaml
settings:
  dispatchingRule: shortest_processing_time
```

The environment does not import JobShopLib. It validates the schedule and computes the same metrics used by every other job-shop method.

## Why This Method Is Included

This page shows two levels of integration:

- a tiny dependency-free method useful for first runs
- a real external-library wrapper that reuses JobShopLib while producing the same schedule-solution contract as any other external solver

That contrast is useful for users deciding whether to write a small native OptPilot method or wrap an existing method library.
