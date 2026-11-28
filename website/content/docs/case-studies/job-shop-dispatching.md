---
sidebar_position: 6
slug: /research/coopa
title: Inspect a solver’s answer with COOPA
description: Read the objective value, problem statement and report behind one generated operations-research solution.
featured: true
preview_summary: Give COOPA a decision problem in words. Inspect the reported objective and the record of how the solver arrived at it.
---

The [COOPA research project](https://arxiv.org/abs/2606.27611) investigates solving operations-research problems expressed in natural language. In the OptPilot package, the `solve` study starts with one such problem and asks a generator for one answer. The candidate contains a numeric value, a flag indicating whether an answer was found, and the solver report as JSON.

The grader checks the report format, required fields and the presence of a finite objective when the solver reports an answer. A `solved` score of one means the answer passed those checks. To establish that a proposed solution is feasible or optimal, compare it against the problem constraints and an independent bound or trusted solution.

Use the package guide to set up the COOPA runtime, choose the model and edit the problem before reviewing the study. [Run and interpret the COOPA example](https://minds-thu.github.io/OptPilot/reference/or-solving/)
