---
sidebar_position: 5
slug: /research/trace-guided-heuristic-design
title: Evaluate a factory scheduling policy
description: Run an executable policy in a simulated factory and inspect the measurements for a short shift.
featured: true
preview_summary: Run the baseline dispatch policy on a short shift. Use the same simulator and scoring rule to test a revision.
---

A dispatch policy decides how a factory should use its machines and automated guided vehicles as orders arrive. The evaluator calls the policy with a public snapshot of the simulated factory and applies the commands it returns. This lets a project author replace the policy while reusing the model and grading code.

The public manifest supplies one baseline policy and a `smoke` study for a short shift. Run the baseline to see which indicators the grader measures. Add a second policy to compare throughput, quality or vehicle utilization under the same shift settings. The manifest also defines a longer instance with equipment faults for a separate comparison.

[*LLM-Guided Heuristic Design from Simulation Traces*](https://arxiv.org/abs/2608.09343) studies how simulation traces can guide policy revisions. The runnable study here measures a supplied policy. It takes a separately configured generator to conduct an automated search. [Run the scheduling study](https://minds-thu.github.io/OptPilot/reference/llm-policy-search/)
