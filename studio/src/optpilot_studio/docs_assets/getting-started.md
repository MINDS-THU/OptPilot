# First Job-Shop Run

This page walks through the smallest runnable study in the bundled job-shop
tutorial package.

Before starting, follow the **Full OptPilot Studio** source-checkout install in
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
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
```

The command prints a JSON summary. A successful first run should show:

- `completed_trials: 1`
- `failure_count: 0`
- a non-empty `run_dir`
- `best_metric` and `best_trial_id`

Run evidence is written under `runs/` unless you pass `--output-root` or set
`evidence.outputDir` in the study.

## Environment Config

The environment config says what OptPilot can evaluate:

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
      processing_time_weight:
        valueType: float
        min: -5.0
        max: 5.0
        default: -1.0
      machine_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
        default: -0.1
      job_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
        default: -0.1

methodContext:
  references:
    - name: ft06_small
      path: cases/ft06_small.yaml
      type: job_shop_case
    - name: la01_tiny
      path: cases/la01_tiny.yaml
      type: job_shop_case
    - name: ft06_standard
      path: cases/ft06_standard.yaml
      type: job_shop_case

metrics:
  source: return
  keys: [makespan, normalized_makespan, tardiness, utilization, feasible, operation_count]

outputFiles:
  - schedule_*.json
  - job_shop_metrics*.json
```

Important details:

- `evaluator.settings.cases` are environment-owned evaluator inputs.
- `candidate.parameters.schema` defines the parameter names and bounds.
- `methodContext.references` exposes read-only case files to methods that ask
  for method context.
- `metrics.keys` names the metrics that a study may choose as objective or
  secondary metrics.

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

Useful files in the run directory:

| File | What it tells you |
| --- | --- |
| `summary.json` | Best metric, best trial, failure count, and run status. |
| `study_spec.json` | Compiled environment, method, objective, runtime, and execution policy. |
| `candidates.jsonl` | Candidate validation and materialization records. |
| `observations.jsonl` | Trial outcomes and metric values. |
| `trials.jsonl` | Terminal trial records and execution metadata. |
| `method_calls.jsonl` | Method requests, responses, and errors. |

See [Evidence](evidence.md) for the full file layout.

## Next Steps

After this run:

1. Read [Job-Shop Tutorial](examples.md) for the full package map.
2. Run the tuner in [Dispatching Rule Methods](dispatching-rule-methods.md).
3. Read [Candidate Contracts](candidate-contracts.md) before adding your own
   method or environment.
4. Open [OptPilot Studio](ui.md) if you want to browse the package and inspect
   runs in the GUI.
