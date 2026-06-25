---
title: DEVS-Gen Simulation Environments
description: How to use DEVS-Gen generated simulators as OptPilot evaluation environments.
---

# DEVS-Gen Simulation Environments

This is an advanced integration pattern, not the recommended first OptPilot run. Start with [Getting Started](getting-started.md) unless you specifically want to evaluate a generated simulator.

[DEVS-Gen](https://minds-thu.github.io/devs_gen/) generates discrete-event simulation projects from high-level process descriptions. OptPilot does not generate those simulators. OptPilot treats the generated simulator as user-owned environment code, copies it into each trial workspace, applies candidate files to that disposable copy, and runs an evaluator wrapper that returns metrics.

The integration flow is:

1. use DEVS-Gen to generate a simulator outside OptPilot
2. keep the generated simulator in a stable local path
3. write an OptPilot environment config that copies the generated simulator into each trial workspace
4. expose the generated file or files that methods may edit
5. write an evaluator wrapper that runs the generated simulator and returns metrics
6. bind that environment to any compatible method in a study

The bundled repository uses a Strategic Airlift simulator as the concrete sample, but the page is about the pattern. The same structure applies to any DEVS-Gen generated simulator.

## Bundled Sample

The sample OptPilot environment lives under:

```text
examples/environments/strategic_airlift_devs/
  environment.yaml
  evaluator.py
  assets/sa_default.yaml
  prompts/sa_file_edit_system_prompt.md
```

The sample studies are:

```text
examples/studies/
  sa_baseline.yaml
  sa_openai_file_editor.yaml
```

The generated simulator itself is expected at:

```text
resource/devs_gen_gallery/simulators/SA/simulator
```

`resource/` is intentionally local external material. A clean checkout can still validate the configs, but running this sample requires the generated simulator tree to exist at that path.

## Environment Boundary

The bundled sample uses this full environment config:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: sa-simulator-code-edit
description: Evaluate edited SA simulator source copied from the external devs_gen_gallery repository
tags: [external-project, llm-code-edit, simulator, files]

evaluator:
  python: examples.environments.strategic_airlift_devs.evaluator:evaluate
  timeoutSeconds: 180
  settings:
    duration: 120.0
    num_aircraft: 2
    pallet_interval: 20.0
    pallet_expiration_time: 120.0
    flight_time: 30.0
    unload_time: 2.0
    return_time: 30.0
    maintenance_time: 10.0

candidate:
  format: files
  description: SA simulator control logic files.
  materialize:
    root: simulator
  files:
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    required:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    allow:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py

methodContext:
  instructions:
    - prompts/sa_file_edit_system_prompt.md

trialWorkspace:
  - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
    to: simulator

metrics:
  source: return
  keys: [service_score, delivered_count, expired_count, generated_count, mean_latency, max_latency, delivery_ratio, expiration_ratio]

outputFiles:
  - sa_events.jsonl
  - sa_metrics.json
  - sa_stderr.log
```

The original generated simulator is not modified by OptPilot. `trialWorkspace` copies it into each trial workspace, and candidate edits are applied only to the disposable trial copy.

For another DEVS-Gen simulator, change `trialWorkspace[].from`, `candidate.files.editable`, `candidate.files.required`, and `candidate.files.allow` to match that generated project.

## Evaluator Wrapper

The evaluator is normal OptPilot environment code:

Evaluator field fragment from the environment config:

```yaml
evaluator:
  python: examples.environments.strategic_airlift_devs.evaluator:evaluate
```

It runs the generated simulator from the copied trial workspace, reads the simulator outputs, and returns metrics. In the bundled sample, those metrics include:

- `service_score`
- `delivered_count`
- `expired_count`
- `generated_count`
- `mean_latency`

The evaluator is the only place that needs to understand the generated simulator's runtime interface. Methods only see the file-candidate contract.

## Methods

The baseline study uses `baseline-file-copy`, which copies the generated file unchanged and verifies that the simulator wrapper works.

The file-editing study uses the generic OpenAI-compatible file editor. The full method config is:

```yaml
apiVersion: optpilot.io/v1
config: method
id: openai-file-editor
description: OpenAI-compatible method that edits environment-provided file candidates.
tags: [llm, code-edit, files]

entrypoint:
  python: examples.methods.openai_file_editor.method:OpenAIFileEditMethod
  protocol: batch

settings:
  batchSize: 1
  provider: openrouter
  model: openai/gpt-4.1-mini
  temperature: 0.2
  maxTokens: 4000
  apiKeyEnvVar: OPENROUTER_API_KEY
  apiBase: https://openrouter.ai/api/v1/chat/completions
  includeBaselineCandidate: true

accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions
```

That method is not Strategic-Airlift-specific. It accepts any `files` candidate contract with editable files and environment-provided `methodContext.instructions`.

## Run The Sample

Before running either sample study, confirm:

1. `resource/devs_gen_gallery/simulators/SA/simulator` exists.
2. `resource/devs_gen_gallery/simulators/SA/simulator/devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py` exists.
3. the generated simulator's Python dependencies are installed. The bundled Strategic Airlift sample imports `xdevs` from [`iscar-ucm/xdevs.py`](https://github.com/iscar-ucm/xdevs.py), so install the SA extra before running it:

```bash
uv sync --extra sa
```

If you are adapting a different generated simulator, install that simulator's own dependencies instead.

`OPENROUTER_API_KEY` is only needed if you plan to run real LLM edits in `sa_openai_file_editor.yaml`.

The full baseline study config is:

```yaml
apiVersion: optpilot.io/v1
config: study
name: sa-baseline
description: Evaluate the unmodified strategic-airlift simulator files as a baseline.
tags: [external-project, baseline, simulator]

environmentConfig: ../environments/strategic_airlift_devs/environment.yaml
methodConfig: ../methods/baseline_file_copy/method.yaml

objective:
  metric: service_score
  direction: maximize
  secondaryMetrics: [delivered_count, expired_count, mean_latency]

budget:
  maxTrials: 1

execution:
  backend: local
  parallelism: 1
  timeoutSeconds: 180

evidence:
  level: full

reproducibility:
  seed: 0
```

Validate and run the baseline:

```bash
uv sync --extra sa
uv run optpilot validate examples/studies/sa_baseline.yaml
uv run optpilot run examples/studies/sa_baseline.yaml
```

The baseline does not require API keys or provider credentials.

The full OpenAI-compatible file-editing study config is:

```yaml
apiVersion: optpilot.io/v1
config: study
name: sa-openai-file-editor
description: Use a user-owned LLM method to rewrite selected SA simulator files and optimize service_score
tags: [external-project, llm, code-edit, simulator]

environmentConfig: ../environments/strategic_airlift_devs/environment.yaml
methodConfig: ../methods/openai_file_editor/method.yaml

objective:
  metric: service_score
  direction: maximize
  secondaryMetrics: [delivered_count, expired_count, mean_latency]

budget:
  maxTrials: 4

execution:
  backend: local
  parallelism: 1
  timeoutSeconds: 180

evidence:
  level: full

reproducibility:
  seed: 0
```

Run the OpenAI-compatible file-editing study after the baseline path works:

```bash
export OPENROUTER_API_KEY=...
uv run optpilot run examples/studies/sa_openai_file_editor.yaml
```

## Adapting The Pattern

For a new DEVS-Gen simulator:

1. generate the simulator with DEVS-Gen
2. put the generated simulator under a stable local path such as `resource/my_simulator`
3. copy the bundled sample environment directory into `user_catalog/environments/my_simulator`
4. update `trialWorkspace[].from` to point at the generated simulator
5. expose the generated file or files that methods may edit
6. put simulator arguments, scenario knobs, or benchmark selections under `evaluator.settings`
7. write or adapt `evaluator.py` so it runs the generated simulator from the trial workspace
8. choose metrics and declare them in `metrics.keys`
9. bind the environment to a method in a study config

The resulting OptPilot boundary is the same as every other environment: the environment defines what it can evaluate, the method defines how candidates are produced, and the study binds one environment, one method, objective, budget, and runtime.
