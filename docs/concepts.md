---
title: Concepts
description: Core OptPilot concepts and boundaries.
---

# Concepts

This page gives the mental model behind OptPilot. For a runnable first example, use [Getting Started](getting-started.md).

OptPilot has one optimization abstraction:

```text
method proposes candidate -> environment evaluates candidate -> OptPilot records evidence
```

The important design choice is that methods and environments remain user-owned. OptPilot owns the boundary between them.

## Environment

An environment is anything that can evaluate a candidate and produce metrics. It may be:

- a Python evaluator
- a command-line simulator
- a dataset benchmark
- a custom adapter around a service or external runtime

The environment config declares the candidate contract, evaluator entrypoint, metric sources, trial workspace seed files, output files, record streams, and optional method-visible context.

## Method

A method proposes candidates. It may be:

- random search, Bayesian optimization, or a metaheuristic
- an RL training loop or policy-improvement workflow
- an LLM code editor or AlphaEvolve-style agent
- an existing heuristic-search repository with its own internal loop

OptPilot does not split methods into separate controller and engine concepts. A method is the user-owned optimization process, however simple or complex that process is.

## Study

A study binds one environment config to one method config and chooses the run policy:

- objective metric and direction
- instances
- trial budget
- execution backend and runtime
- evidence level and reproducibility seed

Environment and method configs should be reusable. Study configs should be concrete.

## Candidate

A candidate is what the method proposes and the environment evaluates.

OptPilot supports three candidate formats:

| Format | Use it for |
| --- | --- |
| `parameters` | JSON-like decisions: numeric parameters, discrete actions, schedules, simulator controls, BO search spaces, many RL action spaces. |
| `files` | Generated or edited files: source code, policy scripts, config files, heuristic programs, data files. |
| `opaque` | A custom payload convention understood by a matching method and environment. |

The full candidate contract is more than the format. For example, a file-editing environment is defined by `format: files`, editable paths, materialization root, allow/deny rules, method context, and evaluator behavior.

## Compatibility

Method/environment compatibility is explicit:

```yaml
accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions
    capabilities: []
```

OptPilot checks that:

- the environment candidate format is listed in `method.accepts.formats`
- required context paths exist in the compiled environment candidate context
- required capabilities are declared by the environment

This keeps compatibility tied to the actual contract instead of vague domain tags.

## Authoring Versus Runtime

Users author configs. OptPilot creates runtime folders.

| User-authored concept | Runtime-created storage |
| --- | --- |
| `candidate` says what a method must return. | Candidate store holds durable files for file candidates. |
| `trialWorkspace` says what each trial starts with. | Trial workspace is created fresh for each trial. |
| `methodContext` says which instructions and references are shown to the method. | Method workspace may hold per-call scratch files. |
| `metrics`, `records`, and `outputFiles` say what to collect. | Evidence store records observations, metadata, and file references. |

This distinction keeps the public schema focused on user intent while still supporting retries, parallel trials, container runtimes, and evidence inspection.

## Trial Workspace

`trialWorkspace` entries are copied once per trial before evaluation. They are for files the evaluator needs inside that trial workspace.

`trialWorkspace` is not:

- a dependency manager
- a permission boundary for what a method can read
- a semantic classification of files

A copied directory may contain runnable source, fixtures, datasets, config templates, and support scripts at the same time. The evaluator and candidate materialization rules determine how those files are used.

Seed a complete source tree when evaluation intentionally runs workspace-local code after candidate edits are applied. Do not seed the full implementation when the evaluator uses an installed package, a prebuilt container image, an external service, or only parameter/action payloads.

## Evidence

Each run records what happened:

- compiled `study_spec.json`
- `summary.json`
- observations and trials
- candidate records
- method calls and method events
- scheduler events
- runtime policy and environment snapshot

Methods can inspect prior evidence through `EvidenceView`, so iterative methods can learn from earlier observations without scraping raw files.
