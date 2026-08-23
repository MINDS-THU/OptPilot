---
title: "Generate and Optimize: DEVS-Gen × Heuristic Design"
description: Generate a discrete-event simulator from text, then improve its declared decision policy using simulation traces.
---

# Generate and Optimize

This workflow composes two research packages through public OptPilot contracts:

1. **DEVS-Gen** turns a natural-language specification into a runnable
   discrete-event simulator with declared metrics and, when requested, an
   editable decision-policy hook.
2. **LLM-Guided Heuristic Design** consumes that hook, repeated-simulation
   metrics, and exact-replay traces to improve the executable policy.

Neither package imports the other. The generated Environment describes what a
Candidate may edit and how it can be replayed; the Method declares which pieces
of that contract it requires. Studio enables the pairing only when they match.

## Which route to use

Use **LLM-Guided Heuristic Design** when the decision is most naturally an
executable rule that can improve through repeated simulation and trace review.
Use **COOPA** when the decision is most naturally a mathematical program with
explicit variables, constraints, and an objective. A project can use both:
COOPA can propose a plan, while a DEVS-Gen Environment evaluates that plan
under operational uncertainty.

For the solver route, open the `solve-or-problem` Run setup and fill its
required `problem` input. A usable statement names what to decide, what to
optimize, and every constraint, for example:

```text
Minimise the total distance travelled by two AGVs. Decide which AGV serves
each transport request and in what order. Each request must be served once;
an AGV may carry only one load at a time; pickup must precede delivery; and
the schedule must satisfy all battery and shift-length constraints.
```

## 1. Generate the simulator

Open **DEVS-Gen** in Catalog and launch **DEVS Simulation Generator Interface**,
or run its headless `generate` Resource action. Describe both the system and the
decision that should be optimizable—for example:

> Jobs arrive at one machine. Whenever the machine becomes idle, a dispatch
> policy chooses one waiting job. Expose that decision as an editable policy.

The generated `devs.simulation.v2` manifest declares metrics and the policy
entrypoint. A system without an optimizable decision remains a valid simulation
Environment but will not match policy-design Methods.

## 2. Register the Environment

From the generated Workspace, choose **Set up for Catalog**. For a declared
policy hook, Studio creates:

- a file-Candidate Environment;
- baseline candidate files and policy instructions;
- `policyValidation` rules;
- seeded evaluator settings and metrics;
- an `exact_seed_replay` capability that produces a bounded SQLite trace.

DEVS-Gen includes two reviewable reference outputs:

| Environment | Policy-hook style | Location |
| --- | --- | --- |
| Dispatch station | `policy.py` factory function | `catalog/devs_gallery/environments/dispatch_station/` |
| Triage clinic | Editable DEVS component class | `catalog/devs_gallery/environments/triage_clinic/` |

## 3. Pair the Method

In Studio's Run setup flow:

1. Select the generated Environment—or one of the reference Environments.
2. Select **Trace-guided policy design (language model)** from the
   **LLM-Guided Heuristic Design** package.
3. Review the compatibility checks, objective, budget, and seed.
4. Save the Run setup and launch it.

The Method evaluates the baseline, replays the worst seed, lets a manager query
the event trace, asks parallel editors for complete policy revisions, validates
them, and keeps only improvements. The source Environment and prior results are
never modified.

## 4. Inspect and reuse the result

The Run retains every Candidate, trial, observation, trace, and generated policy
file. Open the best Candidate to replay it, compare it, save it to the Shortlist,
or create an editable Workspace.

The same boundary supports other combinations. A COOPA-produced formulation or
solver policy can be retained as a Candidate and evaluated by a compatible
DEVS-Gen Environment; translating a mathematical solution into a simulator
decision hook remains an explicit modeling step rather than hidden glue.

See [LLM-Guided Heuristic Design](llm-policy-search.md) for the Method contract,
[DEVS-Gen](devs-gallery.md) for generation, and [COOPA](or-solving.md) for
provenance-aware OR formulation and solver routing.
