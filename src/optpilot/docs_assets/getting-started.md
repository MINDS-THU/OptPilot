# Getting Started

This guide gets you to a successful first OptPilot run with the job-shop scheduling example.

Use this page to validate the toolchain and understand one concrete study layout. It is the recommended first walkthrough. Use [Examples](examples.md) for the full built-in example catalog and [Configuration](configuration.md) for the YAML reference.

## Mental Model

Every OptPilot run follows the same loop:

```text
method proposes candidate
environment evaluates candidate
OptPilot records evidence
```

The three public configs map directly onto that loop:

- the environment config says what can be evaluated and how
- the method config says how candidates are proposed
- the study config binds them into one concrete run

## Install

Prerequisites:

- Python 3.10+
- `uv`
- run the commands below from the repository root

This walkthrough is fully local and does not require API keys or provider credentials.

```bash
uv sync
uv run optpilot --help
```

Useful first commands:

```bash
uv run optpilot validate examples/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot ui --open-browser
```

`optpilot validate` checks YAML structure, path resolution, and method/environment compatibility before a study runs.

## Validate And Run

The first example evaluates weighted dispatch-rule parameters on two small job-shop scheduling cases.

Validate it:

```bash
uv run optpilot validate examples/studies/job_shop_rule_parameters_baseline.yaml
```

Run it:

```bash
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
```

This baseline does not require an API key or external solver. It emits one parameter candidate, evaluates it, and writes run evidence under `examples/runs/` unless you pass `--output-root`.

## Expected Output

The run command prints a JSON summary at the end. A successful first run should show:

- `completed_trials: 1`
- `failure_count: 0`
- a non-empty `run_dir`
- `best_metric` and `best_trial_id`

If the command fails before printing a run summary, return to the install step and re-run `optpilot validate` first.

## The Three Configs In This Example

The study uses:

- `examples/environments/job_shop_scheduling/environment_rule_parameters.yaml`
- `examples/methods/fixed_rule_parameters/method.yaml`
- `examples/studies/job_shop_rule_parameters_baseline.yaml`

## Environment Config

The full environment config is:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: job-shop-rule-parameters
description: Evaluate weighted dispatch-rule parameters on small job-shop scheduling cases.
tags: [job-shop, scheduling, parameters, tutorial]

evaluator:
  python: examples.environments.job_shop_scheduling.evaluator:evaluate
  timeoutSeconds: 60
  settings:
    cases:
      - id: ft06_small
        path: cases/ft06_small.yaml
      - id: la01_tiny
        path: cases/la01_tiny.yaml

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
        description: Weight for remaining work in the current job.
      processing_time_weight:
        valueType: float
        min: -5.0
        max: 5.0
        default: -1.0
        description: Weight for the candidate operation duration.
      machine_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
        default: -0.1
        description: Weight for the selected machine ready time.
      job_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
        default: -0.1
        description: Weight for the selected job ready time.

metrics:
  source: return
  keys: [makespan, normalized_makespan, tardiness, utilization, feasible, operation_count]

outputFiles:
  - schedule_*.json
  - job_shop_metrics*.json
```

For `format: parameters`, `parameters.schema` is required. It is owned by the environment because the environment decides which parameter names, types, ranges, and defaults it knows how to evaluate.

Parameter schemas can also include cross-parameter constraints, written as a small YAML expression tree. This first example does not need constraints, but they are still supported. See [Parameter Constraints](configuration.md#parameter-constraints) for the supported nodes such as `compare`, `all`, `any`, `not`, `param`, `const`, `add`, `sub`, `mul`, and `div`.

The evaluator converts the parameter candidate into a dispatching rule, simulates a schedule, validates feasibility, and returns metrics. The environment advertises the metric keys it expects from that evaluator, including `normalized_makespan`.

The `evaluator.settings` block is normal environment-owned input to the evaluator. In this example it lists two validation case files. OptPilot passes that object to the Python evaluator as `context["settings"]`; the evaluator decides how to load the files and aggregate results.

## Method Config

The full method config is:

```yaml
apiVersion: optpilot.io/v1
config: method
id: fixed-rule-parameters
description: Emits one fixed weighted dispatch-rule parameter candidate.
tags: [baseline, parameters, job-shop, no-api]

entrypoint:
  python: examples.methods.fixed_rule_parameters.method:FixedRuleParametersMethod
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

produces:
  format: parameters
  parameters:
    schema:
      remaining_work_weight:
        valueType: float
        min: -5.0
        max: 5.0
      processing_time_weight:
        valueType: float
        min: -5.0
        max: 5.0
      machine_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
      job_ready_weight:
        valueType: float
        min: -2.0
        max: 2.0
```

`accepts` is the method-side compatibility declaration. It tells OptPilot which environment candidate formats the method can target. If a method lists `candidate.parameters.schema` under `accepts.requires.context`, it means the method wants OptPilot to provide the environment's parameter schema in the method request context.

This baseline method is not schema-general. Its method settings contain four fixed values, and the method always returns those four parameter names. Its `produces` block is therefore a method output promise: this method returns a parameter candidate with these fields. It is not an environment-specific block; it is useful for any environment whose candidate contract accepts the same four fields.

A schema-general method would look different: it would request `candidate.parameters.schema`, inspect whatever fields the selected environment declares, and generate values for those fields at runtime. That kind of method usually omits `produces` because its output shape depends on the environment schema it receives.

## Study Config

The full study config is:

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
  backend: local
  parallelism: 1
  timeoutSeconds: 60

evidence:
  level: full

reproducibility:
  seed: 0
```

The study binds one environment and one method. The objective metric name, `normalized_makespan`, comes from the environment config's `metrics.keys`. The evaluator returns those metric keys after each trial. In the job-shop example, `normalized_makespan` is computed as `makespan / lower_bound`, where `lower_bound` comes from each case file. The study chooses which returned metric is primary and whether lower or higher is better.

The job-shop cases belong to the environment config because they are evaluator inputs. OptPilot does not know job-shop semantics or run a special case loop. It calls the evaluator once per trial:

```python
evaluate(candidate_runtime, context)
```

The selected environment decides how to interpret `context["settings"]`. In this job-shop environment, each case file contains machine count, jobs, operations, durations, and optional metadata such as `lower_bound` and `due_date`; the evaluator converts those dictionaries into job-shop problems and averages numeric metrics across the configured cases. A different environment could use `settings` for simulator scenarios, dataset slices, SQL query specs, benchmark cases, or any other environment-owned input shape.

Methods that need to read the same case files can get them through `methodContext.references` in the environment config. That keeps method-visible data explicit without adding a separate OptPilot concept for benchmark cases.

Study paths are resolved from the study file. Environment paths are resolved from the environment file. Method paths are resolved from the method file.

When you run this study, OptPilot compiles the three public YAML files into `study_spec.json` inside the run directory. That compiled spec is evidence of the exact environment, method, objective, evaluator settings, and runtime that were executed.

## Try File Candidates

The same job-shop evaluator also has file-candidate variants.

Dispatch-rule file:

```bash
uv run optpilot run examples/studies/job_shop_dispatch_rule_baseline.yaml
```

Solver-code file:

```bash
uv run optpilot run examples/studies/job_shop_solver_code_baseline.yaml
```

See [Job-Shop Environment](job-shop-environment.md) for the candidate contracts.

## Inspect The Run

Important files:

| File | What to inspect |
| --- | --- |
| `summary.json` | Best metric, best trial, failure count, run directory. |
| `study_spec.json` | Compiled run spec generated from the three YAML files. |
| `observations.jsonl` | Trial statuses and metric values. |
| `trials.jsonl` | Trial inputs and backend metadata. |
| `candidates.jsonl` | Candidate validation and materialization details. |
| `method_calls.jsonl` | Method requests and responses. |

See [How A Run Works](how-it-works.md) and [Evidence](evidence.md) for the full runtime sequence.

## Use The UI

```bash
uv run optpilot ui --open-browser
```

The UI scans `examples/` and `user_catalog/` by default. It lets you browse
environments and methods, check compatibility, draft studies, launch runs, and
inspect previous run evidence. The command starts a local server; stop it with
`Ctrl-C` in the terminal when you are done.

For the assistant-enabled Studio workflow with OpenHands, Code Server, and
per-workspace containers, see [UI](ui.md).

## Add Your Own Code

Put user-owned integrations under `user_catalog/`:

```text
user_catalog/
  environments/my_environment/
    environment.yaml
    evaluator.py
  methods/my_method/
    method.yaml
    method.py
  resources/my_reference_project/
    README.md
```

Environment and method folders are reusable catalog entries. Resource folders
are reusable reference projects or assets. Study YAML files are concrete run
plans; save them where you draft or launch them instead of registering them as
catalog entries.

Use [User Catalog](user-catalog.md) for layout guidance and
[Configuration](configuration.md) for the full schema.

## Next Steps

After this first run:

1. Read [Concepts](concepts.md) and [Methods](methods.md) to understand the environment/method/study boundary.
2. Read [How A Run Works](how-it-works.md) and [Evidence](evidence.md) to understand trial workspaces, candidate materialization, and recorded outputs.
3. Read [Examples](examples.md) and [Job-Shop Environment](job-shop-environment.md) to choose between the shared job-shop method tracks.
4. Copy the pattern into `user_catalog/` when you are ready to bind your own environment and method.
