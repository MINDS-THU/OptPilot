---
sidebar_position: 5
slug: /research/trace-guided-heuristic-design
title: LLM-guided heuristic design from simulation traces
description: Improve executable decision policies through repeated simulation, exact-seed replay, and retained file Candidates.
featured: true
preview_image: /img/case-studies/llm-heuristics.svg
preview_metric: Policy files + exact replay
preview_tags: [LLM method, heuristics, simulation]
preview_visual_title: Trace-Guided Policy Design
preview_visual_description: Diagnose the incumbent on its hardest replay, propose complete policy revisions, validate them, and retain every Candidate.
preview_summary: A feedback-driven Generator improves executable policy Candidates from repeated simulation scores and event-level traces, with production and AGV scheduling as the reference Task.
preview_tone: slate
---

import Link from '@docusaurus/Link';

<p className="articleReturnLink">
  <Link to="/docs/use-cases">Back to all research packages</Link>
</p>

# LLM-guided heuristic design from simulation traces

The `catalog/production_agv_scheduling` project accompanies [*LLM-Guided Heuristic Design from Simulation Traces*](https://arxiv.org/abs/2608.09343). It packages a feedback-driven Generator—`process-aware-llm-heuristic-design`—and a dynamic production and AGV scheduling Task used as the reference application.

## How the Generator works

1. Evaluate the Task's baseline Candidate over repeated seeded Instances.
2. Replay the incumbent's worst seed into a bounded SQLite event trace.
3. Ask a manager model to diagnose bottlenecks and propose revision plans.
4. Let parallel editor models produce complete policy files.
5. Validate, evaluate, and retain only improvements.

LLM revision happens between evaluation batches. Every proposed policy is therefore a program Candidate with immutable files and evaluation evidence, not an untracked edit to the simulator.

## A genuinely reusable boundary

The Generator contains no production-specific imports. Everything domain-specific comes from the selected Task and Environment: editable policy files, instructions, baseline content, trace schema, replay settings, validation rules and feedback metrics.

Study review validates that the Generator's declared output and feedback requirements match the selected Task contract. This includes the production/AGV reference Task and compatible generated dispatch-policy Tasks from DEVS-Gen.

## Run the no-key smoke path

```bash
uv run optpilot experiment review --project catalog/production_agv_scheduling --study smoke
# Use the id returned by review in the next command.
uv run optpilot experiment run --project catalog/production_agv_scheduling --review REVIEW_ID
```

The paper-style Generator uses `OPENROUTER_API_KEY`. The project also includes exhaustive rule-grid, genetic-algorithm, differential-evolution and particle-swarm baselines evaluated by the same Task and metric contract.

[Read the complete heuristic-design package guide](https://minds-thu.github.io/OptPilot/reference/llm-policy-search/)
