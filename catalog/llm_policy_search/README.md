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

## Mirroring

`methods/llm_policy_search/` is the source of truth for the shared
implementation; `production_agv_scheduling/methods/process_aware_llm`
mirrors it byte-identically (package boundaries forbid cross-package
imports in retained runs). `tests/core/test_llm_policy_search_mirror.py`
enforces the mirror.
