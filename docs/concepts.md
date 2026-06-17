---
title: Concepts
description: Core OptPilot concepts and how studies move from configs to evidence.
---

# Concepts

OptPilot exists to make optimization studies repeatable without forcing users to rewrite their environments or methods.

## Environment

An environment is anything that can evaluate a candidate and produce metrics. It may be a Python callable, a command-line simulator, a dataset evaluator, or a custom adapter.

The environment config declares:

- evaluator entrypoint
- candidate contract
- metrics
- workspace files to copy into each trial
- files and record streams to save as evidence

## Method

A method proposes candidates. It may be a random search, Bayesian optimizer, RL trainer, meta-heuristic, LLM workflow, or an existing agent.

The method config declares:

- implementation type: `python` or `command`
- protocol: batch or Python session
- compatibility with environment candidate contracts
- optional runtime isolation for command methods

## Study

A study binds one environment config to one method config. The study owns the project-specific choices:

- objective metric and direction
- instances
- trial budget
- execution backend
- evidence level
- reproducibility seed

## Candidate

A candidate is the object being evaluated. OptPilot supports:

- `parameters`: structured parameter dictionaries
- `files`: code or file bundles
- `opaque`: custom candidate payloads interpreted by a custom environment/method pair

## Evidence

Each run directory records what happened:

- compiled `study_spec.json`
- observations and trials
- candidate records
- method calls and method events
- scheduler events
- runtime policy and environment snapshot

Methods can inspect prior evidence through `EvidenceView`, which lets iterative methods learn from earlier observations without scraping raw files.
