---
title: Dispatching Rule Methods
description: How dispatching-rule methods connect to the job-shop example.
---

# Dispatching Rule Methods

Dispatching rules are the simplest useful job-shop method family. They choose
which available operation to schedule next. The examples show three OptPilot
patterns with increasing strength:

- emit one fixed parameter candidate
- tune bounded dispatch-rule weights over several trials
- wrap JobShopLib's dispatching-rule solver and return complete schedules

The file-copy baseline is also included as a smoke test for file candidates. It
does not optimize by itself.

## Which Contract To Use

| Example | Environment config | Candidate returned by method | Extra dependency |
| --- | --- | --- | --- |
| Fixed weighted rule | `environment_rule_parameters.yaml` | one parameter dictionary | none |
| Tune weighted rule parameters | `environment_rule_parameters.yaml` | several parameter dictionaries | none |
| Baseline Python rule file | `environment_dispatch_rule.yaml` | unmodified `dispatch_rule.py` | none |
| JobShopLib dispatching rule | `environment_schedule_solution.yaml` | `spec.solutions` schedules | `uv sync --all-packages --group examples` |

## Dependency-Free Parameter Methods

Run the fixed baseline:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
```

Run the deterministic tuner:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_tune_dispatch_weights.yaml
uv run optpilot run catalog/example_package/studies/job_shop_tune_dispatch_weights.yaml
```

Both studies use `environment_rule_parameters.yaml`. The environment exposes a
schema for four numeric weights. The fixed baseline submits one setting. The
tuner reads the schema from candidate context, proposes a bounded grid of
settings, and lets OptPilot evaluate each candidate against the same cases.

This is the smallest useful optimizer in the tutorial because improvement comes
from multiple OptPilot trials, not from a hidden solver loop.

Expected result:

- the fixed baseline should complete one trial with `failure_count: 0`
- the tuner should complete up to 12 trials and record one observation per
  evaluated parameter setting
- `candidates.jsonl` should show `parameters` candidates with the four weight
  fields
- `observations.jsonl` should show whether the tuned grid found a lower
  `normalized_makespan` than the first fixed setting

## File-Candidate Smoke Test

Run:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
```

This study uses `baseline-file-copy` with `environment_dispatch_rule.yaml`. The
method copies the environment's template `dispatch_rule.py` into the candidate
store, and OptPilot materializes it into the trial workspace before evaluation.

Use this to verify file-candidate materialization before trying an LLM or a
larger heuristic-code generator.

Expected result:

- the run should complete one trial with `failure_count: 0`
- `candidates.jsonl` should contain a `files` candidate with `dispatch_rule.py`
- the trial workspace should contain a materialized candidate file under the
  environment's configured candidate root
- this smoke test is not expected to optimize; it proves the file-candidate
  handoff works

## JobShopLib Dispatching Rule

Install the optional example dependencies:

```bash
uv sync --all-packages --group examples
```

Then run:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml
uv run optpilot run catalog/example_package/studies/job_shop_lib_dispatching_rule.yaml
```

This method uses `environment_schedule_solution.yaml`. It reads validation
cases from `methodContext.references`, calls JobShopLib's
`DispatchingRuleSolver`, and emits schedule-solution parameters:

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

The environment does not import JobShopLib. It validates the returned schedule
and computes the same metrics used by every other job-shop method.

Expected result:

- the run should complete one trial with `failure_count: 0`
- `candidates.jsonl` should contain a `parameters` candidate whose `spec`
  contains `solutions`
- `observations.jsonl` should report the same job-shop metrics as the
  dependency-free studies

To use a different built-in rule, change the method setting:

```yaml
settings:
  dispatchingRule: shortest_processing_time
```

## What This Page Teaches

Dispatching rules show three different OptPilot boundaries:

- parameter candidates when the environment can turn weights into behavior
- file candidates when the candidate itself is source code
- schedule-solution candidates when an external library produces complete
  schedules

Those boundaries are reusable beyond job-shop scheduling. Pick the one that
matches what your method naturally produces.
