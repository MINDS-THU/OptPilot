---
title: LLM Code-Writing Methods
description: How LLM agents that write dispatch rules or solver code connect to OptPilot.
---

# LLM Code-Writing Methods

LLM code-writing methods produce file candidates. OptPilot does not need to
know the prompting strategy or agent loop. It only needs a file manifest that
matches the environment's file-candidate contract.

The job-shop example exposes two file-candidate targets:

| Target | Environment config | Required function |
| --- | --- | --- |
| Priority rule | `environment_dispatch_rule.yaml` | `score(operation, machine, state)` |
| Complete solver | `environment_solver_code.yaml` | `solve(instance, time_limit_seconds, context)` |

## Dispatch-Rule Editing

Use this contract when the method should write a priority rule:

```yaml
environmentConfig: ../environments/job_shop_scheduling/environment_dispatch_rule.yaml
```

The generated file must be named `dispatch_rule.py` and define:

```python
def score(operation, machine, state):
    ...
```

Higher scores are scheduled first.

Run the baseline file-copy study before connecting an LLM:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
```

Then run the OpenAI-compatible file editor binding:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml
uv run optpilot run catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml
```

The included study has `budget.maxTrials: 1` and
`includeBaselineCandidate: true`, so it is executable without provider
credentials. To request a real LLM edit, set a provider key such as
`OPENROUTER_API_KEY`, increase the study budget, or set
`includeBaselineCandidate: false`.

## Solver-Code Writing

Use this contract when the method should write a complete solver wrapper:

```yaml
environmentConfig: ../environments/job_shop_scheduling/environment_solver_code.yaml
```

The generated file must be named `solver.py` and define:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

The evaluator independently checks schedule feasibility. Invalid solver output
fails the trial instead of producing a misleading score.

Run the baseline first:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_solver_code_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_solver_code_baseline.yaml
```

## What The Method Can See

File-candidate environments expose editable paths and prompt instructions:

```yaml
accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions
```

The method can read:

- `study_state["candidate_context"]` for editable paths and method
  instructions
- previous observations through `evidence_view`
- files listed by the environment's `methodContext.references`
- evaluator artifacts such as logs, JSON reports, plots, CSV files, or SQLite
  databases when they are recorded as evidence

It returns file candidates through `CandidateFileStore`.

## OpenAI-Compatible Editor

The repository includes a generic file-editing method:

```text
catalog/example_package/methods/openai_file_editor/
```

The method accepts any file-candidate environment with editable paths and
instructions. If the environment exposes `methodContext.references`, the editor
adds readable referenced files to the prompt with a bounded context budget. The
job-shop dispatch-rule study is one binding of that generic method to one
environment.

## When To Use A Separate Package

Use this page when the OptPilot method itself owns the code-writing loop. If you
already have a larger upstream repository with its own search loop, adapters,
dependencies, and smoke tests, add it as a separate package instead. See
[Packages and Catalogs](catalog.md).
