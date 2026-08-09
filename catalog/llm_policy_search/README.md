# llm_policy_search — trace-aware LLM policy search, bring your own simulator

The general form of the `production_agv_scheduling` flagship method: a
manager LLM analyzes aggregate KPIs and the worst replication's SQLite
trace, proposes improvement plans, parallel editor LLMs implement them as
complete policy files, and every accepted improvement is verified by
exact-seed replay. **Everything domain-specific arrives through the
environment's declarations** — the method itself knows nothing about any
particular simulator:

| The method reads | From |
|---|---|
| Editable policy files | `candidate.files.editable` |
| Policy interface + snapshot contract | `methodContext.instructions` |
| Entrypoint shape, forbidden imports/names, lints | `policyValidation` (validated by the shared core checker) |
| Domain wording | environment `description` + instructions |
| Replay seed/score metric names | method settings (`replaySeedMetric`, `replayScoreMetric`, defaults `worst_seed`/`worst_total_score`) |
| Exact-seed replay | the `exact_seed_replay` capability's declared `module:function` callable |

## Bring your own simulator

`environments/queue_demo/` is the executable template — a deterministic
single-server job queue. To adopt the method for your own DES:

1. Copy `des_replication.py` (domain-independent: seeded replications,
   worst-run selection, deterministic replay verification, trace dump,
   aggregation) next to your evaluator.
2. Write `simulator.py`-equivalent code exposing one
   `run_once(candidate_dir, settings, seed, database_path)` hook that runs
   a deterministic replication of your simulator under the candidate
   policy and writes a bounded SQLite trace when asked.
3. Write the three documents the method's prompts consume: the policy
   contract (`prompts/policy_system.md`), a domain description, and the
   trace schema.
4. Declare in `environment.yaml`: the editable file set, the
   `methodContext` references (including a `replay_settings` JSON equal to
   your evaluator settings), the `exact_seed_replay` capability callable,
   and the `policyValidation` block.

`studies/queue_demo_baseline_smoke.yaml` (1 trial) exercises the whole
contract — staging, declared-contract validation, replications, worst-seed
replay — without any LLM calls. `studies/queue_demo_policy_search.yaml`
runs the real search and needs `OPENROUTER_API_KEY`.

## Generated-simulator compositions

Two DEVS-Gen generated simulators are included as reference
compositions, one per policy-hook style (see the "Generate and
Optimize" docs page):

- `environments/dispatch_station/` — **function style**: the generated
  simulator delegates its dispatch decision to `devs_project/policy.py`
  (`create_policy()` factory); the candidate is that 10-line module.
  Search result: FCFS baseline −7.81 → best −4.53 mean_total_score
  (SPT-first, found in iteration 1; 12/12 LLM candidates valid).
- `environments/triage_clinic/` — **class style (declare-don't-extract)**:
  the deciding DEVS component `TriagePolicy` is itself the editable
  candidate, guided by a generated editing contract
  (`policy_instructions.md`). Search result: FIFO baseline −3.75 → best
  −2.26 mean_total_score (WSPT with an aging penalty, iteration 2;
  12/12 whole-component rewrites valid, zero protocol breaks).

`studies/dispatch_baseline_smoke.yaml` / `studies/clinic_baseline_smoke.yaml`
(1 trial, no LLM calls) smoke each contract;
`studies/dispatch_policy_search.yaml` / `studies/clinic_policy_search.yaml`
run the real searches and need `OPENROUTER_API_KEY`.

## Mirroring

`methods/llm_policy_search/` is the source of truth for the shared
implementation; `production_agv_scheduling/methods/process_aware_llm`
mirrors it byte-identically (package boundaries forbid cross-package
imports in retained runs). `tests/core/test_llm_policy_search_mirror.py`
enforces the mirror.
