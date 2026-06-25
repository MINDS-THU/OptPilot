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

Study config fragment:

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

Then run the OpenAI-compatible file editor binding:

```bash
uv run optpilot validate examples/studies/job_shop_openai_dispatch_rule.yaml
uv run optpilot run examples/studies/job_shop_openai_dispatch_rule.yaml
```

That study uses:

```text
examples/methods/openai_file_editor/method.yaml
examples.methods.openai_file_editor.method:OpenAIFileEditMethod
```

The included study has `budget.maxTrials: 1` and `includeBaselineCandidate: true`, so it is executable without provider credentials and exercises the actual file-candidate method path. To request a real LLM edit, set a provider key such as `OPENROUTER_API_KEY`, increase the study budget, or set `includeBaselineCandidate: false`.

The same method config is used by the Strategic Airlift editing study. The method stays generic: it accepts `files`, reads the environment-provided editable-file contract and `methodContext.instructions`, and returns a file candidate. The study is where one environment is bound to one method for a concrete run.

## Solver-Code Writing

Use this when the LLM should write a complete solver wrapper, for example a heuristic solver or an OR-Tools script:

Study config fragment:

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

A native LLM method can be a normal batch method. This is a minimal complete method config template for a user-owned file-writing method:

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
- files listed by the environment's `methodContext`, including natural-language notes, CSV files, SQLite databases, or other reference material
- `evidence_view.records(...)` for structured rows extracted after evaluation
- `evidence_view.artifacts(...)` for evaluator output files such as logs, JSON reports, plots, CSV files, or SQLite databases

It returns file candidates through `CandidateFileStore`.

## Difference From LLM Heuristic Repositories

Use this page when you are writing the OptPilot method yourself. The repository includes a generic OpenAI-compatible file-editing method under:

```text
examples/methods/openai_file_editor/
```

That method accepts file-candidate environments with `methodContext.instructions`, and the job-shop dispatch-rule binding above is a concrete runnable example.

Use [LLM Heuristic Repositories](llm-heuristic-methods.md) when you already have a larger upstream repository that owns its own search loop and only needs OptPilot to launch a command and collect one generated file.
