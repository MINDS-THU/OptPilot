# OptPilot Example: LLM-Guided Optimization of the SA Simulator

This example shows how to use OptPilot as the orchestration layer around an
external simulator codebase and a user-owned LLM file-edit engine.

In this workflow, OptPilot does not own the search algorithm or the simulator.
It does four narrower things:

1. stores the candidate code snapshots proposed by the engine
2. materializes each candidate into a clean per-trial workspace
3. runs the evaluator against the copied simulator
4. records prompts, artifacts, observations, and run metadata in one run directory

The target simulator is the `SA` airfreight model from
`MINDS-THU/devs_gen_gallery`:

- Upstream gallery: <https://github.com/MINDS-THU/devs_gen_gallery>
- Simulator folder: <https://github.com/MINDS-THU/devs_gen_gallery/tree/main/simulators/SA>

What you own in this example:

- the prompt in `prompts/sa_file_edit_system_prompt.md`
- the LLM-backed engine in `user_engines/openai_file_edit_engine.py`
- the evaluator and metric definition in `sa_eval.py`

What OptPilot owns:

- study execution and per-trial isolation
- candidate storage and lineage
- prompt and model provenance capture
- the run directory structure under `runs/`

## What This Example Optimizes

After reading the SA implementation, the clean observable metrics are the event
types already emitted by the simulator:

- `pallet_delivered`
- `pallet_expired`
- `pallet_generated`
- delivery `latency`

This example uses a single scalar objective:

```text
service_score = delivered_count - expired_count - mean_latency / 100.0
```

That keeps the study easy to reason about:

- more completed deliveries is better
- queue expirations are bad
- lower latency is better, but it does not dominate the score

The evaluator also records `delivered_count`, `expired_count`, `generated_count`,
`mean_latency`, `max_latency`, `delivery_ratio`, and `expiration_ratio` as
secondary metrics.

## Why This Uses An Engine, Not A Controller

In OptPilot, the controller decides which engine to call and how many
candidates to request. The engine is the component that actually proposes new
candidate artifacts.

For this example, the LLM is editing source files and returning a new candidate
code bundle. That is engine behavior, so the study uses:

- `builtin.single_engine_controller`
- `OpenAIFileEditEngine` in this example folder

## Which Files To Edit

The SA simulator exposes several possible edit points, but they are not equally
useful.

- `devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py`
  is the highest-leverage file. It controls aircraft readiness, maintenance,
  delivery timing, and the phase transitions that directly affect throughput and
  latency.
- `devs_project/StrategicAirlift_D0_libs/FleetCoordinator.py` is a secondary
  edit target. It controls when pallets are requested and assigned.
- `devs_project/StrategicAirlift_D0_libs/PalletQueue.py` is available, but it
  is often a weaker target in this specific simulator because pallets are
  generated in FIFO order and their deadlines are a constant offset from
  generation time.

The shipped example now narrows the editable surface to
`MissionController.py` only. That makes the example cheaper to run, reduces
prompt size, and makes it less likely that an LLM rewrite will destabilize the
simulator. If you want to broaden the search space later, edit the `targetFiles`
list in `examples/opt_devs_gen_sims/methods/openai_file_editor.yaml`.

## 1. Clone The Repositories

Clone OptPilot and install its environment:

```bash
git clone https://github.com/cyrilli/OptPilot.git
cd OptPilot
uv sync
```

Clone the external simulator gallery under `resource/` so the example config can
refer to it without absolute paths:

```bash
mkdir -p resource
git clone https://github.com/MINDS-THU/devs_gen_gallery.git resource/devs_gen_gallery
```

## 2. Install The SA Runtime Dependency

The generated SA simulator depends on `xdevs.py`. Install it into the same `uv`
environment used for OptPilot:

```bash
uv pip install git+https://github.com/iscar-ucm/xdevs.py.git
```

## 3. Set Your LLM Credentials

This example engine uses the OpenRouter Chat Completions HTTP API through the
Python standard library, so it only needs an API key in the environment:

```bash
export OPENROUTER_API_KEY=your_key_here
```

If you want to use a different model, edit
`examples/opt_devs_gen_sims/methods/openai_file_editor.yaml`.

## 4. Inspect The Three Config Files

This example follows the normal OptPilot split:

### EnvironmentConfig

File: `examples/opt_devs_gen_sims/environments/sa_simulator.yaml`

What it does:

- copies `resource/devs_gen_gallery/simulators/SA/simulator` into each trial
  workspace as `simulator/`
- expects file candidates that overlay selected Python files inside that copied
  workspace
- calls `examples.opt_devs_gen_sims.sa_eval:evaluate`
- computes metrics from the simulator's JSONL event stream

### MethodConfig

File: `examples/opt_devs_gen_sims/methods/openai_file_editor.yaml`

What it does:

- uses `builtin.single_engine_controller`
- uses `OpenAIFileEditEngine` to read the current source files, summarize prior
  observations, call the LLM, and return a code artifact manifest
- emits one baseline candidate before the first LLM-edited candidate so the run
  has a measured starting point
- limits the editable file set to `MissionController.py` in the shipped example

### StudyConfig

File: `examples/opt_devs_gen_sims/studies/sa_code_optimization.yaml`

What it does:

- binds the method and environment together
- maximizes `service_score`
- uses one fixed simulation instance from
  `examples/opt_devs_gen_sims/instances/sa_default.yaml`
- runs sequentially with full evidence capture

## 5. Understand The Instance File

The instance file is the simulation scenario passed to `python run.py`:

```yaml
duration: 120.0
num_aircraft: 2
pallet_interval: 20.0
pallet_expiration_time: 120.0
flight_time: 30.0
unload_time: 2.0
return_time: 30.0
maintenance_time: 10.0
```

To change the workload or time horizon, edit
`examples/opt_devs_gen_sims/instances/sa_default.yaml`.

## 6. Run The Study

Run the full example:

```bash
uv run optpilot run examples/opt_devs_gen_sims/studies/sa_code_optimization.yaml
```

The first trial is the unmodified baseline candidate. Later trials are created
by the LLM engine.

The CLI prints a JSON summary at the end of the run. That summary includes a
`run_dir` field with the exact output path for this run.

If you want a shorter first pass, lower `budget.maxTrials` in the study config.

## 7. Where The Run Directory Goes

If you do not pass `--output-root`, OptPilot creates the run under:

```text
examples/opt_devs_gen_sims/runs/
```

with a timestamped child directory such as:

```text
examples/opt_devs_gen_sims/runs/sa-code-optimization-2026-05-31T14-57-43.253686+00-00/
```

These run directories are generated output. They are useful for inspection, but
they are not part of the curated example and are ignored by git.

Not every file under `runs/` is equally important for day-to-day use. This
example keeps full evidence capture on purpose because LLM-driven code editing
is much easier to trust when you can inspect the whole chain from prompt, to
candidate code, to measured outcome. In practice:

- most users mainly need `summary.json`, `observations.jsonl`, `prompts/`,
  `artifacts/`, and the relevant `trials/trial-<id>/` directory
- the remaining JSON and JSONL files are primarily reproducibility,
  provenance, and debugging records

If you want the output somewhere else, pass `--output-root` explicitly:

```bash
uv run optpilot run examples/opt_devs_gen_sims/studies/sa_code_optimization.yaml \
  --output-root .runs
```

## 8. Inspect The Run Contents

The run directory contains both the normal OptPilot evidence files and the
SA-specific files emitted by this example.

For this example, the extra files are intentional rather than accidental. They
exist because the study is configured with full evidence retention. They are not
all required to judge whether a run improved the metric, but they are useful for
auditing what the LLM changed and why a trial succeeded, regressed, or timed
out.

A typical layout looks like this:

```text
runs/
  sa-code-optimization-<timestamp>/
    summary.json
    study_spec.json
    run_policy.json
    run_lineage.json
    environment_snapshot.json
    observations.jsonl
    trials.jsonl
    artifacts.jsonl
    controller_decisions.jsonl
    engine_snapshots.jsonl
    scheduler_events.jsonl
    prompts/
      prompt-<id>/
        prompt.json
    artifacts/
      sa-baseline-<id>/
        files/
          devs_project/.../MissionController.py
      sa-llm-<id>/
        files/
          devs_project/.../MissionController.py
    trials/
      trial-<id>/
        workspace_manifest.json
        sa_events.jsonl
        sa_metrics.json
        sa_stderr.log
        simulator/
          devs_project/...
```

What you will usually inspect first:

- `summary.json`: final run-level answer, including the best artifact, best
  metric, completed trial count, and `run_dir`
- `observations.jsonl`: one completed evaluation per line, including status,
  metrics, and error/timeout details
- `prompts/`: exact prompt payloads used for each LLM edit
- `artifacts/`: durable copies of the candidate code snapshots produced by the
  engine
- `trials/trial-<id>/`: the actual per-trial workspace and the SA-specific
  outputs for that trial

What the other top-level files mean:

- `study_spec.json`: the compiled internal `StudySpec` actually executed by the runner
- `run_policy.json`: the resolved execution and evidence settings for this run
- `run_lineage.json`: whether this run was new, resumed, or branched from another run
- `environment_snapshot.json`: machine and runtime metadata captured for reproducibility
- `trials.jsonl`: trial scheduling records and backend execution metadata
- `artifacts.jsonl`: normalized artifact records, validation results, and
  materialization metadata
- `controller_decisions.jsonl`: controller-level decisions about when and how to ask the engine for candidates
- `engine_snapshots.jsonl`: engine-side records of what was proposed at each step
- `scheduler_events.jsonl`: submission, completion, and retry events from the local scheduler

What the subdirectories mean:

- `prompts/`: exact prompt payloads sent to the LLM engine; baseline candidates
  do not create prompt records because no model call happens
- `artifacts/`: durable copies of each candidate code snapshot returned by the
  engine; these are the code snapshots OptPilot materializes into trial workspaces
- `trials/trial-<id>/`: the per-trial workspace used for evaluation

Inside each `trials/trial-<id>/` directory:

- `workspace_manifest.json`: how the candidate files were copied into the workspace
- `simulator/`: the copied SA simulator tree that was actually executed for this trial
- `sa_events.jsonl`: the raw JSONL event stream emitted by the simulator on a successful evaluation
- `sa_metrics.json`: the evaluator's derived metrics, including `service_score`
- `sa_stderr.log`: anything the simulator wrote to stderr

For successful trials, that directory contains the raw event stream and derived
metrics. For invalid or timed-out trials, you may only see the materialized
workspace plus error information in `observations.jsonl`, because the evaluator
never reached the point where it could write normal SA output files.

That gives you a full audit trail from prompt, to candidate code snapshot, to
materialized workspace, to measured simulator outcome.

## 9. Adjust The Example

Common changes:

### Change the model

Edit `examples/opt_devs_gen_sims/methods/openai_file_editor.yaml`:

- `model`
- `temperature`
- `maxTokens`

### Change which files the LLM may edit

Edit the `targetFiles` list in the same method config.

The default example keeps that list intentionally narrow for stability. Add
other files only after you have a stable baseline workflow.

### Change the optimization objective

Edit `examples/opt_devs_gen_sims/sa_eval.py` if you want a different scoring
formula, then update the study objective metric name if needed.

### Make the prompt stricter or broader

Edit `examples/opt_devs_gen_sims/prompts/sa_file_edit_system_prompt.md`.

## Notes

- This is intentionally a user-owned engine example. OptPilot does not own the
  LLM prompting logic.
- The example keeps the simulator repository external. OptPilot copies it into a
  clean workspace for each trial and overlays only the candidate files.
- The example is designed to show the orchestration pattern clearly, not to
  claim that the current SA simulator has a deeply rich policy search space in
  every file. `MissionController.py` is the strongest initial edit target.
- The evaluator owns timeout cleanup explicitly so a bad LLM edit does not
  leave a simulator subprocess running after a timed-out trial.