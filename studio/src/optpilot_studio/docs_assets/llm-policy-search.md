---
title: LLM-Guided Heuristic Design
description: Improve executable decision policies using repeated simulation and event-level traces.
---

# LLM-Guided Heuristic Design

`catalog/production_agv_scheduling` is the companion package for
[*LLM-Guided Heuristic Design from Simulation Traces*](https://arxiv.org/abs/2608.09343).
It owns one general Method—`process-aware-llm-heuristic-design`—and uses dynamic
production and AGV scheduling as its full reference application.

The Method contains no production-specific imports. Everything domain-specific
arrives from the selected Environment:

| The Method needs | Environment declaration |
| --- | --- |
| Editable policy files | `candidate.files.editable` and `candidate.files.allow` |
| Policy and snapshot contract | `methodContext.instructions` |
| Baseline files, domain description, trace schema, replay settings | `methodContext.references` |
| Entrypoint and safety checks | `policyValidation` |
| Deterministic diagnostic replay | `exact_seed_replay` capability |

This boundary is what lets the same Method optimize the AGV scheduler, a
DEVS-Gen generated dispatch policy, or another compatible executable policy.

## How the search works

1. Evaluate the Environment's baseline policy over repeated seeded simulations.
2. Replay the incumbent's worst seed into a bounded SQLite event trace.
3. Ask a manager model to diagnose bottlenecks and propose revision plans.
4. Let parallel editor models produce complete policy files.
5. Validate, evaluate, and retain only improvements.

The simulator remains fixed during each trial. LLM revision happens between
evaluation batches, so every Candidate is a retained executable policy that can
be inspected and replayed.

## Run the paper application

The quick smoke uses the initial policy and requires no model call:

```bash
uv run optpilot package validate catalog/production_agv_scheduling --check-source
uv run optpilot run catalog/production_agv_scheduling/studies/smoke.yaml \
  --package-root catalog/production_agv_scheduling
```

The paper-style LLM study needs `OPENROUTER_API_KEY`:

```bash
uv run optpilot run \
  catalog/production_agv_scheduling/studies/process_aware_llm.yaml \
  --package-root catalog/production_agv_scheduling
```

The package also includes exhaustive rule-grid, genetic-algorithm,
differential-evolution, and particle-swarm baselines evaluated by the same
Environment and metric contract.

## Pair it with another package

In Studio, select a compatible file-Candidate Environment—such as DEVS-Gen's
`dispatch-station`—then select **Trace-guided policy design (language model)**.
Compatibility is decided from declarations, not package names. Save the pairing
as a Run setup and launch it normally.

See [Generate and Optimize](generate-and-optimize.md) for the end-to-end DEVS-Gen
composition and [Candidate Contracts](candidate-contracts.md) when adapting your
own simulator.
