---
title: First Job-Shop Run
description: Run the smallest bundled job-shop study, inspect its evidence, then route to the other bundled packages.
---

# First Job-Shop Run

This page walks through the smallest runnable study in the bundled job-shop
tutorial package, then routes you to the other bundled packages in
[Where To Go Next](#where-to-go-next).

Before starting, follow the **Source checkout and Studio** install in
[Installation](installation.md). The PyPI core package does not ship the
bundled `catalog/` packages.

!!! note "Every `optpilot run` needs its own `--package-root`"

    `--package-root` is the folder that contains the study, not `catalog/`.
    For this page that is `catalog/example_package`; the gallery, OR-solving,
    and Factorio commands below each use their own package root.

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

## Run It In Studio

Start Studio from the source checkout:

```bash
uv run optpilot ui --open-browser
```

The opening **Conversation** explains the available kinds of work. Ask to run
the fixed job-shop baseline, or open **Catalog** and find the registered
job-shop Environment and fixed-parameter Method yourself. Catalog, Run setups,
Runs, and Workspaces remain direct destinations; none depends on an Assistant
recommendation.

Studio presents the binding as a **Run setup**. Review the Environment, Method,
`normalized_makespan` objective, `minimize` direction, and one-trial budget,
then choose **Launch run** explicitly. Internally, the setup remains the Study
configuration listed above.

While active, the new Run appears in **Open work** and in the originating Conversation. Open
it to inspect progress and evidence. A simulator or other interactive component
opens in the full main area; **Ask from this page** reveals the same Conversation as
an overlay without recreating the interface. Returning to Conversation does not
stop the Run or interface.

When the Run finishes, find it under **Runs**. Saved Run setups remain under
**Run setups**, and editable projects remain under **Workspaces**. Existing
refresh-safe links to Studies, Runs, Candidates, Workspaces, and interfaces
remain valid.

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

If a study declares per-launch `inputs`, supply them with repeatable
`--input key=value` flags (or `--inputs-file inputs.yaml`); this baseline study
declares none.

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

Start Studio and find that id under **Runs**, or ask the Assistant to open the
Run by id, to inspect its Overview, Candidates, trials, attempts, observations,
artifacts, and exact-head timeline:

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

If compilation rejects a referenced config or Python root, check
`--package-root`: it must be the package folder that owns the study
(`catalog/example_package`, `catalog/devs_gallery`, `catalog/or_solving`,
`catalog/factorio_design_benchmark`, `catalog/llm_policy_search`), not
`catalog/` itself. To confirm a package is intact before running anything in
it:

```bash
uv run optpilot package validate catalog/devs_gallery
```

If an optional-dependency study fails to import JobShopLib, OR-Tools,
Stable-Baselines3, or PyTorch, run:

```bash
uv sync --all-packages --group examples
```

The first baseline on this page should not need those optional dependencies.

## Where To Go Next

The source checkout ships five more packages under `catalog/`. Each is a
normal OptPilot package with its own package root, so every one of them is
one `optpilot run` away. This table is the routing map:

| Goal | Package root | Needs |
| --- | --- | --- |
| Open a gallery simulator | `catalog/devs_gallery` | Nothing extra |
| Run the job-shop baseline | `catalog/example_package` | Nothing extra (this page) |
| Solve one OR problem from text | `catalog/or_solving` | Mock: nothing. Real: `OPENROUTER_API_KEY` **and** a user-provisioned COOPA checkout |
| Run one Factorio static-validation study | `catalog/factorio_design_benchmark` | Nothing extra for the smoke study; `OPENROUTER_API_KEY` for the design study |
| Improve a policy with LLM search | `catalog/llm_policy_search` | Baselines: nothing. Search: `OPENROUTER_API_KEY` |

### Open a gallery simulator

`catalog/devs_gallery` holds two simulators generated by the DEVS Simulation
Generator and packaged as ordinary Environments: `seird-epidemic` (an SEIRD
epidemic model) and `abp-protocol` (the Alternating Bit Protocol). Both are
deterministic and need no API key — the `xdevs` wheel they depend on is
vendored in the package and installed into an isolated prepared runtime, so
the first launch spends extra time on that setup and later launches reuse it.

```bash
uv run optpilot run catalog/devs_gallery/studies/seird_minimize_deaths.yaml \
  --package-root catalog/devs_gallery
```

Five random-search trials minimize the final `deceased` count.
`studies/abp_tune_timeout.yaml` is the same shape over the protocol model,
minimizing `retransmissions`. In Studio, both appear under **Catalog** as
Environments; neither declares an interactive interface, so you read them in
Catalog and watch behavior through Run evidence. See
[DEVS Gallery](devs-gallery.md).

### Solve one OR problem from text

`catalog/or_solving` takes a plain-language problem statement as a per-launch
input and returns a retained solution artifact — formulation, routing decision,
generated solver code, and the numeric answer:

```bash
uv run optpilot run catalog/or_solving/studies/solve_or_problem.yaml \
  --package-root catalog/or_solving \
  --method-request-timeout 900 \
  --input problem="A factory makes two products. Product A yields 40 profit and takes 2 hours of labor; product B yields 30 and takes 1 hour. With 100 labor hours available, maximize profit."
```

!!! warning "This study needs more than an API key"

    `studies/solve_or_problem.yaml` drives the COOPA multi-agent pipeline.
    COOPA is Apache-2.0 licensed but is **not vendored** into OptPilot,
    because its solver backends are native and cannot be locked into a
    process runtime. Before that study can run you must obtain a COOPA
    checkout, point `COOPA_HOME` at it, install
    `catalog/or_solving/methods/coopa_solver/requirements-pruned.txt` into the
    Python environment that executes the method, and supply
    `OPENROUTER_API_KEY`. The exact steps are in
    `catalog/or_solving/README.md` and [OR Solving](or-solving.md).

    To check the package wiring without any of that, run its validation:
    `uv run optpilot package validate catalog/or_solving`.

With those prerequisites in place the real study is launched the same way:

```bash
uv run optpilot run catalog/or_solving/studies/solve_or_problem.yaml \
  --package-root catalog/or_solving \
  --method-request-timeout 900 \
  --input problem="<your problem in plain language>"
```

In Studio the `problem` input appears as a **Launch inputs** field on the
`solve-or-problem` Run setup. The `coopa-solver` method also declares an
interface, the **COOPA Solve Console**, opened from its Catalog page; it needs
the same user-provisioned checkout.

### Run one Factorio static-validation study

`catalog/factorio_design_benchmark` scores a factory design JSON against the
benchmark's static checks. The smoke study calls no model:

```bash
uv run optpilot run catalog/factorio_design_benchmark/studies/factory_design_smoke.yaml \
  --package-root catalog/factorio_design_benchmark
```

It succeeds with `failed_check_count = 5` on the default task
(`iron_gear_low_easy`): the bundled template is a schema illustration, not a
solution, so a non-zero score is the expected baseline. Pass
`--input task_id=<id>` to score a different one of the 32 tasks. The 25-trial
`factory_design_task.yaml` study calls a model and needs `OPENROUTER_API_KEY`.
See [Factorio Design Benchmark](factorio-design-benchmark.md).

### Where the keys go

Studies that call a model read `OPENROUTER_API_KEY` through their method's
declared `runtime.envFromHost`. For a CLI launch, export it in the shell that
runs `optpilot run`. For a Studio launch, save it under **Settings → Local
environment variables**; Studio binds the current value to the Run and keeps
it out of the durable request and the Run evidence. Studio Settings is not a
secret vault.

## Next Steps

To go deeper on the job-shop tutorial itself:

1. Read [Job-Shop Tutorial](examples.md) for the full package map.
2. Run the tuner in [Dispatching Rule Methods](dispatching-rule-methods.md).
3. Read [Candidate Contracts](candidate-contracts.md) before adding your own
   method or environment.
4. Open [OptPilot Studio](ui.md) if you want to browse the package and inspect
   runs in the GUI.

For the end-to-end story that generates a simulator from a text specification
and then optimizes its policy, read
[Generate and Optimize](generate-and-optimize.md) and
[LLM Policy Search](llm-policy-search.md).
