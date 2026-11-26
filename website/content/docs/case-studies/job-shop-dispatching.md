---
sidebar_position: 6
slug: /research/coopa
title: "COOPA: natural-language operations-research solving"
description: Solve one natural-language OR problem and retain the formulation, provenance, routing decision, generated solver, and result as Run evidence.
featured: true
preview_metric: One retained solve
preview_tags: [operations research, solver routing, provenance]
preview_visual_title: COOPA Solve Pipeline
preview_visual_description: Turn a plain-language optimization problem into a reviewable formulation, generated solver, numeric result, and complete audit trail.
preview_summary: A one-shot OptPilot workflow for provenance-aware OR formulation and solver routing, demonstrating that the same Run contract also supports methods that solve once rather than search repeatedly.
preview_tone: amber
---

import Link from '@docusaurus/Link';

<p className="articleReturnLink">
  <Link to="/docs/use-cases">Back to all research packages</Link>
</p>

# COOPA: natural-language operations-research solving

The `catalog/or_solving` project integrates [COOPA](https://arxiv.org/abs/2606.27611) as an OptPilot Generator. COOPA receives an operations-research problem from the Task context, proposes one program Candidate, and OptPilot retains the formulation, source provenance, confidence scores, routing decision, generated solver code and numeric result.

## A one-trial Run

COOPA is intentionally different from an iterative search. Its Study sets a generation budget of one and the Task context records the problem statement. That input is captured in the reviewed versions and Run evidence.

The Task grader does not claim to prove a general natural-language answer optimal. It checks that the retained report is complete and structurally valid and records the reported objective value. Human review remains part of the decision.

## The solve pipeline

1. Extract an objective, variables, constraints, and parameters with source provenance and confidence.
2. Refine the formulation when required.
3. Route the problem to a mathematical, combinatorial, metaheuristic, or general optimizer agent.
4. Generate and execute solver code locally.
5. Return one structured Candidate and preserve the complete report as evidence.

## Desktop experience

The project also declares a COOPA Solve Console interface. Desktop presents **Formulate → Review → Solve → Result**, including source quotes and confidence bars. Interactive mode lets a user request a revision before approving the solve; mock mode provides a no-key walkthrough with canned data.

Real Runs require a configured `OPENROUTER_API_KEY` and the documented native solver dependencies. Generated solver code executes locally and should be treated as trusted code.

[Read the complete COOPA package guide](https://minds-thu.github.io/OptPilot/reference/or-solving/)
