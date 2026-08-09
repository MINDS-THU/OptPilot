---
title: "Generate and Optimize: DEVS-Gen x Policy Search"
description: Generate a simulator from a text spec, get a declared policy hook, and improve the policy with trace-aware LLM search.
---

# Generate and Optimize: DEVS-Gen x Policy Search

This walkthrough closes the loop between the two flagship integrations:

1. **DEVS-Gen** turns a natural-language specification into a runnable
   discrete-event simulator that *declares its own optimization contract* —
   result metrics and, when the spec names an optimizable decision, an
   editable **policy hook**.
2. **llm-policy-search** takes any environment that publishes the policy
   contracts and improves the policy with a manager/editor LLM loop driven
   by aggregate KPIs and the worst replication's event trace.

No hand-written glue code connects them: the generated simulator declares
the contract, the Studio wizard emits the environment composition, and the
method reads everything from the environment's declarations.

The checked-in reference composition is
`catalog/llm_policy_search/environments/dispatch_station/` — a DEVS-Gen
generated dispatch station whose decision ("which waiting job runs next")
delegates to an editable `policy.py`.

## Step 1 — Generate a simulator with a policy hook

Use the DEVS-Gen interface (Catalog → `devs-gen-interface` → Open
interface) or the headless `generate` resource action:

```bash
optpilot resource run \
  catalog/example_package/resources/devs-gen-interface/optpilot.resource.yaml \
  generate \
  --inputs-file spec.yaml --output-dir generated/
```

Write the specification so it **names one optimizable decision** — for the
dispatch station: "when the machine becomes idle, a dispatch decision picks
which waiting job to serve next; this decision should be optimizable."
The generator builds the model as it normally would — the decision lives
inside a deciding component — and then **declares where it lives**: the
runner's `OPTPILOT_POLICY` literal names the component file and its
top-level class (or, for delegated designs, a `policy.py` module with a
zero-argument factory function), alongside the result metrics
(`OPTPILOT_METRICS`). The manifest builder statically verifies that the
declared file exists and defines the declared entrypoint before emitting
the `devs.simulation.v2` `policy` block — a declaration pointing at
nothing is discarded whole.

A spec without an optimizable decision produces a plain simulator with
metrics only — the policy block is optional.

## Step 2 — Register: the wizard emits the policy-search variant

When a Studio workspace hands off a v2 bundle **with** a policy block, the
environment starter emits the file-candidate policy variant instead of the
plain parameter template:

- `environment.yaml` — the declared file as the editable candidate,
  `policyValidation` (forbidden imports, plus the entrypoint pin for the
  function-style contract), an `exact_seed_replay` capability,
  seeded-replication evaluator settings, and declared metric keys;
- `optpilot_adapter_policy.py` — seeded replications of the generated
  simulator (overlaying the candidate file at its declared path),
  worst-run selection, JSONL→SQLite trace conversion, and the replay
  callable;
- `policy_instructions.md` — the method-facing contract. For a
  component-class hook this is an **editing contract**: preserve the
  class name, ports, protocol, and DEVS lifecycle verbatim; change only
  the selection logic. For a `policy.py` function hook it is the snapshot
  interface contract;
- `settings/replay.json` — seeds and score metric.

The `dispatch_station` environment in `catalog/llm_policy_search/` is
exactly this output, kept as a reviewable reference.

## Step 3 — Baseline

`studies/dispatch_baseline_smoke.yaml` runs the generated FCFS-style
template policy once through the retained runner:

```bash
optpilot run catalog/llm_policy_search/studies/dispatch_baseline_smoke.yaml \
  --package-root catalog/llm_policy_search
```

The baseline scores `mean_total_score = -7.81` (score is the negated
average waiting time in hours, so higher is better).

## Step 4 — Search

`studies/dispatch_policy_search.yaml` runs the LLM search (13 trials:
baseline + 3 iterations of 4 candidates; requires `OPENROUTER_API_KEY`):

```bash
optpilot run catalog/llm_policy_search/studies/dispatch_policy_search.yaml \
  --package-root catalog/llm_policy_search
```

Each iteration the manager inspects the aggregate metrics and queries the
worst seed's SQLite trace, proposes improvement plans, parallel editors
write complete `policy.py` candidates, the core policy validator enforces
the environment's declared rules, and improvements are verified by
exact-seed replay through the environment's declared capability.

One proposal exchange spans several model calls, so the method declares
`entrypoint.exchangeTimeoutSeconds: 5000`; the retained runner honors the
declaration (an explicit `--method-request-timeout` remains a launch-time
override).

## Results

A reference run (13/13 trials succeeded, seeds 7/11/23, deepseek-v4-flash
via OpenRouter):

| Candidate | mean_total_score | worst_total_score |
|---|---|---|
| baseline (generated FCFS template) | −7.81 | −10.11 |
| best (iteration 1, plan 1) | **−4.53** | **−5.27** |

The best candidate cut the average waiting time by **42%**. Its policy is
the textbook answer for this system — Shortest Processing Time first with
arrival-time tiebreaking — discovered in the first iteration from the
baseline's worst-seed trace; iterations 2 and 3 explored priority and
aging variants without beating it (one rediscovered the same rule). Every
one of the 12 LLM candidates outperformed the baseline.

A second reference run exercises the **class-style** contract on the
generated triage clinic (`clinic-policy-search`), where the whole
`TriagePolicy` DEVS component file is the editable candidate under a
generated editing contract:

| Candidate | mean_total_score | worst_total_score |
|---|---|---|
| baseline (generated FIFO component) | −3.75 | −6.63 |
| best (iteration 2) | **−2.26** | **−3.32** |

All 12 whole-component rewrites were valid — every candidate preserved
the class name, ports, protocol, and DEVS lifecycle and changed only the
selection logic; the winner implemented Weighted Shortest Processing
Time (`exam_duration/urgency`) with a waiting-time aging penalty, a 40%
reduction in urgency-weighted waiting over FIFO.

## Where the pieces live

| Piece | Location |
|---|---|
| Generator prompts + policy-hook contract | `catalog/example_package/resources/devs-gen-interface/` |
| Manifest v2 metrics/policy extraction | `devs_tools/.../result_summary_contract.py` |
| Generic method | `catalog/llm_policy_search/methods/llm_policy_search/` |
| Reference composition | `catalog/llm_policy_search/environments/dispatch_station/` |
| BYO-simulator template | `catalog/llm_policy_search/environments/queue_demo/` |

To adopt the loop for your own simulator — generated or hand-written —
follow the "Bring your own simulator" section of
`catalog/llm_policy_search/README.md`, or simply generate with a
decision-naming spec and let the wizard emit the composition.
