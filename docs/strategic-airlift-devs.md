---
title: Strategic Airlift DEVS
description: How to connect a devs_gen_code generated simulator to OptPilot.
---

# Strategic Airlift DEVS

This is an advanced example, not the recommended first OptPilot run. Start with [Getting Started](getting-started.md) unless you specifically want to evaluate generated simulator code.

The Strategic Airlift example demonstrates a generated-simulator workflow:

1. generate a discrete-event simulator using `devs_gen_code`
2. treat the generated simulator as user-owned upstream code
3. configure OptPilot to copy and evaluate that generated simulator
4. apply candidate edits only inside disposable trial workspaces

This track is separate from the main job-shop tutorial environment. Its purpose is to show how OptPilot connects to generated simulation projects.

This page assumes that the generated Strategic Airlift simulator tree is already present in this repository checkout at `resource/devs_gen_gallery/simulators/SA/simulator`.

## Prerequisites

- Python 3.10+
- `uv`
- run commands from the repository root
- the Strategic Airlift simulator directory exists at `resource/devs_gen_gallery/simulators/SA/simulator`
- for the file-editing study only: `OPENROUTER_API_KEY` is set in the shell

`devs_gen_code` is an external simulator-generation workflow. In this example, OptPilot does not generate the simulator itself. OptPilot assumes the generated simulator already exists and copies it into disposable trial workspaces.

## Current Files

```text
examples/environments/strategic_airlift_devs/
  environment.yaml
  evaluator.py
  instances/sa_default.yaml
  prompts/sa_file_edit_system_prompt.md

examples/studies/
  sa_baseline.yaml
  sa_openai_file_editor.yaml
```

The generated simulator is expected at:

```text
resource/devs_gen_gallery/simulators/SA/simulator
```

## Environment Config

The environment config copies the generated simulator into each trial workspace:

```yaml
trialWorkspace:
  - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
    to: simulator
```

The original generated simulator is not modified by OptPilot. The copy inside each trial workspace is disposable runtime state.

The candidate contract exposes one generated simulator file:

```yaml
candidate:
  format: files
  materialize:
    root: simulator
  files:
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
```

## Evaluator

The evaluator runs the generated simulator module from the copied trial workspace:

```yaml
evaluator:
  python: examples.environments.strategic_airlift_devs.evaluator:evaluate
```

It collects simulator events and returns metrics such as:

- `service_score`
- `delivered_count`
- `expired_count`
- `generated_count`
- `mean_latency`

## Run

### Verify Setup

Before running either study, confirm:

1. `resource/devs_gen_gallery/simulators/SA/simulator` exists.
2. `resource/devs_gen_gallery/simulators/SA/simulator/devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py` exists.
3. `OPENROUTER_API_KEY` is set if you plan to run `sa_openai_file_editor.yaml`.

### Baseline Study

Validate and run the baseline:

```bash
uv run optpilot validate examples/studies/sa_baseline.yaml
uv run optpilot run examples/studies/sa_baseline.yaml
```

This baseline uses the checked-in OptPilot wrapper and the generated simulator copy. It does not require API keys or provider credentials.

### OpenAI-Compatible File Editing Study

The OpenAI-compatible file-editing method requires provider credentials:

```bash
export OPENROUTER_API_KEY=...
uv run optpilot run examples/studies/sa_openai_file_editor.yaml
```

Use this second study only after the baseline path works.

## Adapting To Another Generated Simulator

For another `devs_gen_code` simulator:

1. generate the simulator outside OptPilot
2. point `trialWorkspace[].from` to the generated simulator directory
3. choose the generated file or files that methods may edit
4. write an evaluator wrapper that runs the generated simulator command/module
5. return metrics through `metrics.source: return`
