---
sidebar_position: 4
slug: /research/devs-gen
title: "DEVS-Gen: simulation generation and optimization"
description: Generate discrete-event simulations from natural language, then evaluate or optimize them through ordinary OptPilot contracts.
featured: true
preview_image: /img/case-studies/simulation-loop.svg
preview_metric: 4 shipped environments
preview_tags: [DEVS, simulation, generation]
preview_visual_title: Generated Simulation Loop
preview_visual_description: Move from a natural-language system description to a runnable simulation Task with declared metrics and decision parameters.
preview_summary: DEVS-Gen packages an interactive generator and generated simulators as versioned OptPilot experiment artifacts for parameter tuning and policy design.
preview_tone: teal
---

import Link from '@docusaurus/Link';

<p className="articleReturnLink">
  <Link to="/docs/use-cases">Back to all research packages</Link>
</p>

# DEVS-Gen: simulation generation and optimization

[DEVS-Gen](https://arxiv.org/abs/2603.03784) turns natural-language system descriptions into runnable discrete-event simulations. Its OptPilot project, `catalog/devs_gallery`, includes the interactive generator and generated simulators described by versioned Environment and Task artifacts.

## What ships

| Task | Candidate | Decision |
| --- | --- | --- |
| SEIRD epidemic | Parameters | Tune epidemiological parameters to minimize final deaths. |
| Alternating Bit Protocol | Parameters | Tune sender and channel timing under deterministic loss. |
| Dispatch station | Policy files | Choose the next queued production job. |
| Triage clinic | Policy files | Choose the next patient for service. |

The pre-generated examples run locally without a model key. Creating a new simulator through the DEVS-Gen interface uses a configured `OPENROUTER_API_KEY`.

## Why the package matters

The generated simulator remains user-owned Environment code. OptPilot records its contract, Candidate shape, metrics, frozen source and evaluation evidence. The policy Tasks also publish instructions, baseline files, validation rules and exact-seed replay context that a compatible Generator can consume.

That makes cross-project composition possible. DEVS-Gen can supply the evaluation project while a Generator from the LLM-Guided Heuristic Design package supplies policy Candidates through the declared experiment contracts.

## Start with a no-key Run

```bash
uv run optpilot experiment review --project catalog/devs_gallery --study seird-search
# Use the id returned by review in the next command.
uv run optpilot experiment run --project catalog/devs_gallery --review REVIEW_ID
```

The authored Study uses a small seeded random search over the declared parameter schema. These commands validate and start that Study. This is an executable smoke path, not a benchmark claim.

[Read the complete DEVS-Gen package guide](https://minds-thu.github.io/OptPilot/reference/devs-gallery/)
