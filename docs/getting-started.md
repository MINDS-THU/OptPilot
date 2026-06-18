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

The first example evaluates weighted dispatch-rule parameters on two small job-shop scheduling instances.

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

The environment config declares a parameter candidate contract:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: job-shop-rule-parameters

candidate:
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
```

It points to a Python evaluator:

```yaml
evaluator:
  python: examples.environments.job_shop_scheduling.evaluator:evaluate
```

The evaluator converts the parameter candidate into a dispatching rule, simulates a schedule, validates feasibility, and returns metrics.

## Method Config

The matching method emits one fixed parameter candidate:

```yaml
apiVersion: optpilot.io/v1
config: method
id: fixed-rule-parameters

entrypoint:
  python: examples.methods.fixed_rule_parameters.method:FixedRuleParametersMethod
  protocol: batch

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

`accepts` is the compatibility declaration. It tells OptPilot that this method can work with environments whose candidate format is `parameters` and whose context includes a parameter schema.

## Study Config

The study binds the environment and method:

```yaml
apiVersion: optpilot.io/v1
config: study
name: job-shop-rule-parameters-baseline

environmentConfig: ../environments/job_shop_scheduling/environment_rule_parameters.yaml
methodConfig: ../methods/fixed_rule_parameters/method.yaml

objective:
  metric: normalized_makespan
  direction: minimize

instances:
  source: files
  paths:
    - ../environments/job_shop_scheduling/instances/ft06_small.yaml
    - ../environments/job_shop_scheduling/instances/la01_tiny.yaml

budget:
  maxTrials: 1
```

Study paths are resolved from the study file. Environment paths are resolved from the environment file. Method paths are resolved from the method file.

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

The UI scans `examples/` and `user_catalog/` by default. It lets you browse environments and methods, check compatibility, draft studies, launch runs, and inspect previous run evidence.

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
  studies/my_study.yaml
```

Use [Configuration](configuration.md) for the full schema and [User Catalog](user-catalog.md) for layout guidance.

## Next Steps

After this first run:

1. Read [How A Run Works](how-it-works.md) to understand trial workspaces, candidate materialization, and evidence.
2. Read [Examples](examples.md) to choose between turnkey tutorials, advanced examples, and integration templates.
3. Copy the pattern into `user_catalog/` when you are ready to bind your own environment and method.
