---
title: First Job-Shop Run
description: Run the smallest bundled job-shop study and inspect the evidence it creates.
---

# First Job-Shop Run

This page walks through the smallest runnable study in the bundled job-shop
tutorial package.

Before starting, follow the **Source Checkout: Tutorial and Studio** install in
[Installation](installation.md). The PyPI core package does not ship the
bundled `catalog/example_package/`.

## What This Run Shows

The first study evaluates one fixed set of dispatch-rule parameters on the
job-shop environment.

It uses three config files:

- environment:
  `catalog/example_package/environments/job_shop_scheduling/environment_rule_parameters.yaml`
- method:
  `catalog/example_package/methods/fixed_rule_parameters/method.yaml`
- study:
  `catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml`

The environment owns the job-shop cases, candidate schema, evaluator, and
metrics. The method owns how it proposes parameter values. The study binds them
and chooses the objective and budget.

## Validate And Run

Validate the study:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
```

Run it:

```bash
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml \
  --package-root catalog/example_package
```

The command prints a JSON summary. A successful first run should show:

- `run_status: succeeded`
- an explicit `stop_code`, normally `max_trials` for this study
- `counts.logical_trials.terminal: 1` and `successful: 1`
- `counts.logical_trials.final_failures: 0`
- `counts.attempts.total: 1` and `counts.observations.total: 1`
- a non-empty `run_id`
- `best.metric` plus correlated Candidate, logical-trial, attempt, and
  observation ids (this is a best single observation, not complete-Candidate
  ranking)

Example excerpt:

```json
{
  "schema": "optpilot.run-summary-projection.v1",
  "run_id": "run-…",
  "run_status": "succeeded",
  "stop_code": "max_trials",
  "counts": {
    "logical_trials": {
      "terminal": 1,
      "successful": 1,
      "final_failures": 0
    },
    "attempts": {"total": 1, "retries": 0},
    "observations": {"total": 1}
  },
  "best": {
    "candidate_id": "fixed-rule-parameters-0000",
    "metric": 1.2009657009657009
  }
}
```

Run, trial, attempt, and observation ids will differ on your machine. The key
first-run checks are a succeeded Run, one successful terminal logical trial,
one attempt and observation, and zero final failures.

The Run is retained in OptPilot's private local Realm; it is not written as a
mutable `runs/` directory. Treat the printed summary as a read model, not as a
resume file or the canonical evidence store.

If you want copy-pasteable inspection commands, save the command output first:

```bash
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml \
  --package-root catalog/example_package \
  | tee /tmp/optpilot-first-run.json
```

Then print the canonical Run id:

```bash
export RUN_ID=$(uv run python - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("/tmp/optpilot-first-run.json").read_text())["run_id"])
PY
)
echo "$RUN_ID"
```

Start Studio and select that id on **Runs** to inspect its Overview,
Candidates, trials, attempts, observations, artifacts, and exact-head timeline:

```bash
uv run optpilot ui --open-browser
```

## Environment Config

The environment config says what OptPilot can evaluate. This abridged excerpt
shows the reusable evaluator, Candidate contract, and metric contract:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: job-shop-rule-parameters
description: Evaluate weighted dispatch-rule parameters on small job-shop scheduling cases.
tags: [job-shop, scheduling, parameters, tutorial]

evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
  timeoutSeconds: 60
  settings:
    cases:
      - id: ft06_small
        path: cases/ft06_small.yaml
      - id: la01_tiny
        path: cases/la01_tiny.yaml
      - id: ft06_standard
        path: cases/ft06_standard.yaml

candidate:
  format: parameters
  description: Numeric weights for a priority dispatching rule.
  parameters:
    schema:
      remaining_work_weight:
        valueType: float
        min: -5.0
        max: 5.0
        default: 1.0
      # The source file defines three more bounded numeric weights.

metrics:
  source: return
  keys: [makespan, normalized_makespan, tardiness, utilization, feasible, operation_count]
```

Important details:

- `evaluator.settings.cases` are environment-owned evaluator inputs.
- `candidate.parameters.schema` defines the parameter names and bounds.
- `metrics.keys` names the metrics that a study may choose as objective or
  secondary metrics.
- The evaluator returns typed artifact declarations alongside its metrics;
  output placement is not configured with legacy path globs.

In these tutorial studies, `normalized_makespan` is the main score:

```text
normalized_makespan = makespan / reference bound
```

The evaluator computes it per case and reports the average. Lower is better.

## Method Config

The baseline method emits one fixed candidate:

```yaml
apiVersion: optpilot.io/v1
config: method
id: fixed-rule-parameters
description: Emits one fixed weighted dispatch-rule parameter candidate.
tags: [baseline, parameters, job-shop, no-api]

entrypoint:
  python: method:FixedRuleParametersMethod
  pythonPath: [.]
  protocol: batch

settings:
  batchSize: 1
  values:
    remaining_work_weight: 1.0
    processing_time_weight: -1.0
    machine_ready_weight: -0.1
    job_ready_weight: -0.1

accepts:
  formats: [parameters]
  requires:
    context: []
```

`accepts.formats` says this method can submit parameter candidates. OptPilot
checks that against the selected environment before the study runs.

This method is intentionally simple. The next tutorial method,
`tune-dispatch-weights`, reads the environment's parameter schema and proposes
several candidate values over multiple trials.

## Study Config

The study binds the reusable environment and method:

```yaml
apiVersion: optpilot.io/v1
config: study
name: job-shop-rule-parameters-baseline
description: Evaluate a fixed weighted dispatching rule on the job-shop parameter environment.
tags: [job-shop, baseline, parameters]

environmentConfig: ../environments/job_shop_scheduling/environment_rule_parameters.yaml
methodConfig: ../methods/fixed_rule_parameters/method.yaml

objective:
  metric: normalized_makespan
  direction: minimize
  secondaryMetrics: [makespan, tardiness, utilization]

budget:
  maxTrials: 1

execution:
  parallelism: 1
  timeoutSeconds: 60

evidence:
  level: full

reproducibility:
  seed: 0
```

The objective metric must be returned by the environment evaluator. The
direction tells OptPilot how to rank trials and write the run summary.

## Inspect The Run

After the first Run, use Studio's bounded Run views:

| View | What it tells you |
| --- | --- |
| Overview | Status, stop reason, budget, counts, objective, and best eligible Candidate. |
| Candidates | Proposed inputs, complete-plan outcomes, ranks, inspection actions, and comparisons. |
| Trials and attempts | Logical budget use, retries, execution state, and terminal outcomes. |
| Observations and artifacts | Evaluator metrics, constraints, retained schedules, and per-case job-shop results. |
| Timeline | Ordered lifecycle and Method-exchange evidence at one exact Realm head. |

See [Runs and Evidence](evidence.md) for the canonical evidence model.

## Troubleshooting

If `counts.logical_trials.final_failures` is greater than zero, open the Run in
Studio, then inspect its terminal logical trials, attempts, observations,
timeline, and bounded Method/runtime logs.

If the command cannot find `catalog/example_package/`, make sure you are in a
source checkout of the repository. The PyPI core package does not include the
bundled tutorial package.

If an optional-dependency study fails to import JobShopLib, OR-Tools,
Stable-Baselines3, or PyTorch, run:

```bash
uv sync --all-packages --group examples
```

The first baseline on this page should not need those optional dependencies.

## Next Steps

After this run:

1. Read [Job-Shop Tutorial](examples.md) for the full package map.
2. Run the tuner in [Dispatching Rule Methods](dispatching-rule-methods.md).
3. Read [Candidate Contracts](candidate-contracts.md) before adding your own
   method or environment.
4. Open [OptPilot Studio](ui.md) if you want to browse the package and inspect
   runs in the GUI.
