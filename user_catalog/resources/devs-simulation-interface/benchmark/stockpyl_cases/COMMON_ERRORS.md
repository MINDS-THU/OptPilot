# Stockpyl Cases: Common Failure Patterns

This file tracks recurring implementation failures observed across `stockpyl_cases`.

## 1) Required Event Name Drift
- Symptom: output uses names like `DAILY_STATE`, `summary_created`, `PERIOD_TICK` while checker expects a different required event name from `event_schema`.
- Effect: format may pass partially, but KPI extraction fails or undercounts.
- Check: compare emitted `event` values against `scenario_blueprint.event_schema` keys exactly (case-sensitive).

## 2) Horizon Mismatch
- Symptom: simulator emits only 14/30 days while golden/checker expects 100-day horizon.
- Effect: KPI distributions are far from golden even when per-step logic looks reasonable.
- Check: verify `--simulate_time` in `config.json -> cases[].sim_args` and ensure simulator uses it as full reporting horizon.

## 3) Partial Coverage of Required Events
- Symptom: only subset of nodes or subset of days emit required events.
- Effect: KPI is systematically biased and Wasserstein distance fails.
- Check: required event coverage should be complete across `(time, node)` for all applicable instances and all required periods.

## 4) Auxiliary Logs Polluting Required Namespace
- Symptom: non-evaluator logs use reserved required event names.
- Effect: checker interprets auxiliary records as required events and fails schema/logic checks.
- Check: non-required logs should avoid `event` key; if present, value must not be a required event name.

## 5) Time Semantics Off-by-One
- Symptom: day starts at 0 in one module but 1 in another; arrivals/order effects land one period early/late.
- Effect: large KPI drift despite seemingly correct local formulas.
- Check: enforce one semantic day index convention consistently for runtime signals, state updates, and outputs.

## 6) Inventory-Position Semantics Incomplete
- Symptom: policy decisions omit some open commitments (for example in-transit or outstanding orders).
- Effect: reordering rhythm deviates strongly, usually causing high stockout or excessive holding.
- Check: ensure the same derived-state definition is used consistently across policy and accounting.

## 7) Topology Initialization Not Applied Before First Step
- Symptom: defaults (inventory/cost/policy/lead time) are patched late or ignored at startup.
- Effect: trajectory diverges from golden from day 1.
- Check: initialization semantics must be fully active before first simulation transition.

## 8) Dynamic Count Expansion Errors
- Symptom: instance counts resolved incorrectly (`arg:*` not applied, wrong indexing, missing instances).
- Effect: required-event coverage gaps and wrong aggregate KPIs.
- Check: validate expanded IDs and edge expansion cardinality before day progression.

## Fast Triage Order
1. Required event names and top-level schema
2. Horizon and `(time, node)` coverage
3. Time-index consistency (0/1-based, arrival day semantics)
4. Policy derived-state semantics (inventory position/open commitments)
5. Initialization and dynamic topology expansion

## 9) Seed Not Applied to Simulation Engine
- Symptom: stochastic scenario declares `seed` in `cli_args_schema` but generated code ignores it or uses a hardcoded value.
- Effect: every run produces identical KPI → Tier 4 EMD fails because the distribution is degenerate (single point rather than spread).
- Check: verify the seed argument is propagated into the random number generator / demand function; for stockpyl-based scenarios, confirm `rand_seed` is set from `sim_args["--seed"]` on each run.
- Reference: `oracle_runner.py` passes `--seed` to `stockpyl.sim.simulation(rand_seed=seed)` and also injects it into `DYNAMIC_ARGS` for hook access.

## 10) Stochastic KPI Instability with Small Oracle Runs
- Symptom: self-consistency test (`master_pipeline.py`) fails on EMD check despite oracle code being correct.
- Effect: small `oracle_runs` (e.g., 12) produce noisy golden distributions; a fresh 6-run self-check sample may appear different enough to trigger Wasserstein distance failures.
- Mitigation: increase `oracle_runs` (e.g., 100+) OR reduce to a single stable KPI (e.g., `total_holding_cost` over `total_stockout_cost` which is often sparse).
- Reference: `stochastic_seeded_noise` blueprint uses single-KPI `total_holding_cost` to pass self-consistency at `oracle_runs=12`.

## 9) Seed Not Applied to Simulation Engine
- Symptom: stochastic scenario declares `seed` in `cli_args_schema` but generated code ignores it or uses a hardcoded value.
- Effect: every run produces identical KPI → Tier 4 EMD fails because the distribution is degenerate (single point rather than spread).
- Check: verify the seed argument is propagated into the random number generator / demand function; for stockpyl-based scenarios, confirm `rand_seed` is set from `sim_args["--seed"]` on each run.
- Reference: `oracle_runner.py` passes `--seed` to `stockpyl.sim.simulation(rand_seed=seed)` and also injects it into `DYNAMIC_ARGS` for hook access.

## 10) Stochastic KPI Instability with Small Oracle Runs
- Symptom: self-consistency test (`master_pipeline.py`) fails on EMD check despite oracle code being correct.
- Effect: small `oracle_runs` (e.g., 12) produce noisy golden distributions; a fresh 6-run self-check sample may appear different enough to trigger Wasserstein distance failures.
- Mitigation: increase `oracle_runs` (e.g., 100+) OR reduce to a single stable KPI (e.g., `total_holding_cost` over `total_stockout_cost` which is often sparse).
- Reference: `stochastic_seeded_noise` blueprint uses single-KPI `total_holding_cost` to pass self-consistency at `oracle_runs=12`.
