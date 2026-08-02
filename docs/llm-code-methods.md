---
title: LLM Code-Writing Methods
description: How LLM agents that write dispatch rules or solver code connect to OptPilot.
---

# LLM Code-Writing Methods

!!! note "OpenAI editor setup"

    The dependency-free dispatch-rule and solver-code baselines are launchable
    through the retained local-process file-candidate slice. The
    OpenAI-compatible editor is also launchable after `OPENROUTER_API_KEY` is
    added under Studio Settings → Local environment variables, or exported for
    a CLI launch.


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

Validate the baseline file-copy fixture:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_dispatch_rule_baseline.yaml
```

Then validate the OpenAI-compatible file-editor binding:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_openai_dispatch_rule.yaml
```

Both studies are retained-launchable. The OpenAI-compatible Method declares
`runtime.envFromHost: [OPENROUTER_API_KEY]`. Studio shows that name as a local
setup requirement and supplies the configured value only to the Method process
for the Run being launched. The Run records the requirement name and an opaque
local Settings revision, not the credential value. Changing the setting creates
a revision for later Runs; it does not silently change an existing Run.

The included OpenAI study has `budget.maxTrials: 1` and
`includeBaselineCandidate: true`, so its method can produce a baseline without
calling a provider. The declared key is still required at launch because
OptPilot prepares the complete Method runtime rather than guessing which branch
the Method will take. To request a real LLM edit, configure
`OPENROUTER_API_KEY`, increase the study budget, or set
`includeBaselineCandidate: false`.

For the retained baseline run, the expected result is:

- a supported baseline file-copy run should complete one trial with `failure_count: 0`
- the Workbench Candidates page should contain a `files` candidate with `dispatch_rule.py`
- the template is supplied through a package-backed
  `methodContext.references` entry rather than a copied trial seed
- a real LLM edit requires the declared local provider key and enough budget to
  propose the edited candidate

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

Validate the baseline fixture:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_solver_code_baseline.yaml
```

This dependency-free study is a retained launch smoke. The expected result is:

- a supported run should complete one trial with `failure_count: 0`
- the Workbench Candidates page should contain a `files` candidate with `solver.py`
- evaluator failures usually mean the generated solver returned an infeasible
  or malformed schedule

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

It returns provisional file candidates through `CandidateBundleStager`, using
the generation-bound `runtime_context.candidate_staging_dir` supplied to each
proposal call. OptPilot freezes and seals the proposal before it becomes a
durable candidate; methods never choose immutable-store paths or copy modes.

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
