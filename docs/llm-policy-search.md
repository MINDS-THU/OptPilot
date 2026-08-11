---
title: LLM Policy Search
description: An LLM writes and revises an executable policy file, judged by seeded replications of your own simulator.
---

# LLM Policy Search

`catalog/llm_policy_search` ships one method and three environments. The method
`llm-policy-search` holds no simulator knowledge: the editable file set, the
policy interface, the validation rules, the domain wording and the replay
capability all arrive from whichever environment it is bound to. The loop:

1. **Baseline first.** Trial 1 always stages the environment's own policy
   template unchanged — no model call — so the search starts from a real score
   and a real process trace.
2. **Replay the worst seed.** The incumbent's `worst_seed` is re-run through the
   environment's `exact_seed_replay` capability into a bounded SQLite trace, in
   an isolated subprocess with the API key scrubbed from its environment. The
   replay must reproduce the observed score or the round fails.
3. **Manager.** One model call sees the policy, the metrics and a trace summary,
   and may issue bounded read-only SQL rounds against the trace
   (`managerQueryRounds: 2`, `maxQueryRows: 20`) before returning plans.
4. **Parallel editors.** One model call per plan returns *complete* files, each
   checked by OptPilot's core policy validator against the environment's
   `policyValidation` block; a rejected file is fed its own error and retried
   (`editorRetries: 3`).
5. **Keep only improvements.** An edit becomes the incumbent only when it
   strictly improves the incumbent's mean score.

## The contract an environment must publish

| The method reads | From |
| --- | --- |
| Editable policy files | `candidate.files.editable` |
| Where it may write | `candidate.files.allow` — must permit the editable files **and** `provenance/**`, where the bounded `provenance/llm_exchanges.json` sidecar lands |
| Policy interface and snapshot contract | `methodContext.instructions` |
| Starting file, domain wording, trace schema, replay settings | `methodContext.references` (a `candidate_template` entry per editable file, plus a `replay_settings` JSON equal to the evaluator settings) |
| Entrypoint shape, forbidden imports and names, string lints | `policyValidation` |
| Exact-seed replay | the `exact_seed_replay` capability's declared `module:function` callable |

The method's `accepts.requires` declares exactly these, so a binding that misses
one fails validation instead of failing mid-run.

## Bring your own simulator

`environments/queue_demo/` is the executable template — a deterministic
single-server job queue with three job classes, class-fixed service times and
weighted lateness penalties. It is the thing to copy:

| File | Role |
| --- | --- |
| `des_replication.py` | Domain-independent: seeded replications, worst-run selection, deterministic replay verification, trace dump, aggregation. Standard library only — copy it as-is. |
| `simulator.py` | The domain. Exposes the single hook `run_once(*, candidate_dir, settings, seed, database_path)`. |
| `evaluator.py` | Wires the two together and exposes `replay_candidate` as the capability. |
| `templates/scheduler.py` | The baseline policy (FCFS) and the file the first trial stages. |
| `prompts/policy_system.md`, `prompts/domain_description.md`, `prompts/trace_schema.md` | The three documents the prompts consume. |
| `trace_adapter.py` | Optional JSONL → SQLite converter for DEVS-Gen generated event traces. |

`run_once` must run one deterministic replication under the candidate policy and
return `{"total_score": <finite float>, "kpis": {<name>: <finite float>}}`; when
`database_path` is not `None` it must also write that replication's bounded
SQLite trace. `des_replication.py` re-runs the worst seed and **fails the trial**
if the replay score differs — a simulator that is not seed-deterministic is
rejected rather than silently searched against. It reports `mean_total_score`
(the objective), `std_total_score`, `min_total_score`, `max_total_score`,
`worst_seed`, `worst_total_score`, and a `mean_`-prefixed aggregate of every KPI
`run_once` returns — here `mean_served`, `mean_mean_wait`,
`mean_weighted_lateness`.

!!! note "Declare a runtime block only if you need one"
    `queue_demo` needs nothing beyond the standard library and therefore
    declares no `runtime:` block. When your simulator does need dependencies,
    copy the shape used by `environments/dispatch_station/`: `sandbox: process`
    plus a `runtime.setup` step over a vendored, hash-locked `requirements.lock`.

## Run the zero-LLM smoke

Validate the package first — this is read-only and needs nothing:

```bash
uv run optpilot package validate catalog/llm_policy_search
```

Then run the one-trial contract smoke. It stages the baseline policy, runs the
five declared replications, and exercises the worst-seed replay — without a
single model call:

```bash
export OPENROUTER_API_KEY=...   # required at launch, not spent by this study
uv run optpilot run catalog/llm_policy_search/studies/queue_demo_baseline_smoke.yaml \
  --package-root catalog/llm_policy_search
```

!!! warning "The key must be set even for the zero-LLM smoke"
    The method declares `runtime.envFromHost: [OPENROUTER_API_KEY]`, and OptPilot
    prepares the complete method runtime before the method chooses a branch, so a
    launch with the variable unset stops with
    `Missing Method environment variable: OPENROUTER_API_KEY`. A one-trial smoke
    never reaches the provider, so any placeholder value satisfies the check. In
    Studio, save the variable under Settings → local environment variables; the
    Run records the requirement name and a Settings revision, never the value.

A local run of the FCFS baseline over seeds 101–105 scored
`mean_total_score = -15.74`, with seed 101 the worst replication at
`worst_total_score = -84.67`. That is the number the search has to beat.

## Run the real search

```bash
uv run optpilot run catalog/llm_policy_search/studies/queue_demo_policy_search.yaml \
  --package-root catalog/llm_policy_search
```

This one spends tokens: `budget.maxTrials: 13` is one baseline plus three
iterations of four candidates — one editor call per candidate, one manager call
plus its query rounds per iteration. The shipped provider settings:

| Setting | Value | Note |
| --- | --- | --- |
| `provider` / `apiBase` | `openrouter` / `https://openrouter.ai/api/v1/chat/completions` | Any OpenAI-compatible chat-completions URL works |
| `model` | `deepseek/deepseek-v4-flash` | |
| `apiKeyEnvVar` | `OPENROUTER_API_KEY` | Must match the `runtime.envFromHost` entry |
| `temperature` / `maxTokens` | `0.2` / `10000` | |
| `candidatesPerIteration` / `maxIterations` / `patience` | `4` / `5` / `3` | `candidatesPerIteration` is capped at 4 |
| `targetScore` | `80` | Absolute stop threshold on the primary metric |
| `requestTimeoutSeconds` / `requestRetries` | `180` / `2` | Per model call, with bounded backoff |
| `replayTimeoutSeconds` | `1800` | Per isolated worst-seed replay |
| `entrypoint.exchangeTimeoutSeconds` | `5000` | One exchange spans manager + parallel editors + replay |

`targetScore` is read in the objective's own units — `80` is reachable only in
`queue-demo`'s 100-based score, so the generated compositions below stop on the
study budget (or `maxIterations`/`patience`) instead. An explicit
`--method-request-timeout` overrides `exchangeTimeoutSeconds`; leave it unset.

!!! note "No SDK: plain `urllib` over an OpenAI-compatible endpoint"
    The method POSTs the chat-completions payload itself with `urllib.request`
    from the standard library, with bounded response and error reads and
    provider text redacted before anything is retained. That is not minimalism
    for its own sake: OptPilot's process runtime accepts only vendored,
    hash-locked pure-Python `py3-none-any` wheels, and the usual provider SDK
    closures pull in compiled dependencies (for example `pydantic-core`, which
    publishes no pure wheel), so they cannot be locked into a retained method
    runtime at all. Stdlib HTTP keeps the method's dependency closure empty.

## Generated simulators

Two DEVS-Gen generated simulators are included as reference compositions, one per
policy-hook style — see [Generate and Optimize](generate-and-optimize.md) for how
the generator declares the hook and the Studio wizard emits these environments.

| Environment | Editable candidate | `policyValidation` entrypoint |
| --- | --- | --- |
| `dispatch-station` | `policy.py` (function style) | `create_policy`, 0 arguments |
| `triage-clinic` | `TriagePolicy.py`, the deciding DEVS component itself (class style) | none — the editing contract lives in `policy_instructions.md`, and a rewrite that breaks the class name or protocol fails its own replications |

Each of the three environments has a 1-trial smoke with no model calls
(`queue_demo_baseline_smoke.yaml`, `dispatch_baseline_smoke.yaml`,
`clinic_baseline_smoke.yaml`) and a 13-trial search that needs the key
(`..._policy_search.yaml`). The two generated environments prepare a vendored
xdevs runtime on first launch, so their smokes take longer than `queue-demo`'s
but still make no network call.

The package README and the Generate and Optimize page record reference search
results for them — dispatch station `-7.81 → -4.53`, triage clinic
`-3.75 → -2.26` `mean_total_score`, both with `deepseek/deepseek-v4-flash` via
OpenRouter. This page does not reproduce those runs, and LLM search is not
deterministic: treat them as recorded observations, not expected output.

!!! note "The method is mirrored, not shared"
    `methods/llm_policy_search/` is the source of truth;
    `catalog/production_agv_scheduling/methods/process_aware_llm/` is a
    byte-identical mirror, because package boundaries forbid cross-package
    imports in retained runs. `tests/core/test_llm_policy_search_mirror.py`
    enforces it — edit the source, then copy.
