---
title: Examples
description: Built-in OptPilot examples and how they are organized.
---

# Examples

`examples/` contains curated integrations that are useful for learning the project. The UI scans this folder by default together with `user_catalog/`.

## Strategic Airlift DEVS

The current example wraps a strategic-airlift DEVS simulator generated outside OptPilot. It demonstrates a file-candidate environment and methods that can target it.

```text
examples/
  environments/
    strategic_airlift_devs/
      environment.yaml
      evaluator.py
      instances/sa_default.yaml
      prompts/sa_file_edit_system_prompt.md
  methods/
    baseline_file_copy/
      method.yaml
      method.py
    openai_file_editor/
      method.yaml
      method.py
  studies/
    sa_baseline.yaml
    sa_openai_file_editor.yaml
```

The environment config:

- copies the generated simulator from `resource/devs_gen_gallery/simulators/SA/simulator`
- exposes `MissionController.py` as the editable candidate file
- runs `examples.environments.strategic_airlift_devs.evaluator:evaluate`
- records simulator events and summary metrics

## Run The Baseline

```bash
uv run optpilot run examples/studies/sa_baseline.yaml
```

Run the baseline first to confirm the simulator can be copied and evaluated.

## Run The File Editor

```bash
export OPENROUTER_API_KEY=...
uv run optpilot run examples/studies/sa_openai_file_editor.yaml
```

The OpenAI-compatible method is user-owned example code. It demonstrates how an LLM-style workflow can propose file candidates while OptPilot owns evaluation and evidence capture.
