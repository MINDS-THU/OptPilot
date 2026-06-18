---
title: LLM Code-Writing Methods
description: How LLM agents that write dispatch rules or solver code connect to OptPilot.
---

# LLM Code-Writing Methods

LLM code-writing methods produce file candidates. The method may be a small prompt wrapper, a multi-step agent, or an existing workflow that writes code. OptPilot does not need to know the internal prompting strategy. It only needs the generated files and the environment contract they target.

The job-shop example exposes two file-candidate targets:

| Target | Environment config | Required function |
| --- | --- | --- |
| Dispatch rule | `environment_dispatch_rule.yaml` | `score(operation, machine, state)` |
| Solver script | `environment_solver_code.yaml` | `solve(instance, time_limit_seconds, context)` |

## Dispatch-Rule Editing

Use this when the LLM should generate or revise a priority rule:

```yaml
environmentConfig: ../environments/job_shop_scheduling/environment_dispatch_rule.yaml
```

The generated file must be named:

```text
dispatch_rule.py
```

The evaluator imports that file from the trial workspace and schedules operations by repeatedly calling:

```python
def score(operation, machine, state):
    ...
```

Run the baseline before connecting an LLM:

```bash
uv run optpilot validate examples/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run examples/studies/job_shop_dispatch_rule_baseline.yaml
```

## Solver-Code Writing

Use this when the LLM should write a complete solver wrapper, for example a heuristic solver or an OR-Tools script:

```yaml
environmentConfig: ../environments/job_shop_scheduling/environment_solver_code.yaml
```

The generated file must be named:

```text
solver.py
```

and define:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

The evaluator independently checks schedule feasibility. Invalid solver output fails the trial instead of producing a misleading score.

Run the baseline first:

```bash
uv run optpilot validate examples/studies/job_shop_solver_code_baseline.yaml
uv run optpilot run examples/studies/job_shop_solver_code_baseline.yaml
```

## Method Shape

A native LLM method can be a normal batch method:

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-llm-code-writer

entrypoint:
  python: user_catalog.methods.my_llm_code_writer.method:MyLLMCodeWriter
  protocol: batch

settings:
  batchSize: 1
  model: gpt-4.1-mini
  temperature: 0.2

accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions
```

The method can read:

- `study_state["candidate_context"]` for editable files and method instructions
- `evidence_view` or previous observations for feedback
- files listed by the environment's `methodContext`

It returns file candidates through `CandidateFileStore`.

## Difference From LLM Heuristic Repositories

Use this page when you are writing the OptPilot method yourself. The repository includes a generic OpenAI-compatible file-editing method under:

```text
examples/methods/openai_file_editor/
```

That method accepts file-candidate environments with `methodContext.instructions`, so it can be bound to the job-shop dispatch-rule or solver-code environment after you configure provider credentials.

Use [LLM Heuristic Repositories](llm-heuristic-methods.md) when you already have a larger upstream repository that owns its own search loop and only needs OptPilot to launch a command and collect one generated file.
