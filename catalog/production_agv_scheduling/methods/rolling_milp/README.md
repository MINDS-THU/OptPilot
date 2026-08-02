# Rolling-horizon MILP baselines

This method deterministically stages two executable policy candidates:

- `rolling-milp-monolithic`: the original single-model rolling MILP with a static task cap.
- `rolling-milp-two-stage`: raw-line selection followed by per-line sequencing MILPs, with the upstream adaptive task cap.

Both candidates use the paper defaults of a 2-second wall-clock Gurobi limit per solve and an 8-simulation-minute minimum replanning interval. Replanning is triggered by new-order and rework events. The extracted source historically used `*_sec` for simulator-time variables; the public method settings use minute-labelled names to remove that ambiguity.

The candidate exposes `scheduler.create_controller(simulation, settings)`. It owns state extraction, the Gurobi model, replanning, and command dispatch; the environment owns simulation and scoring.

## Gurobi gate

Candidate staging, package validation, syntax checks, and imports do not require `gurobipy`. A real evaluation does require an importable `gurobipy` and a working license. Missing imports and license failures always raise clear runtime errors; they never fall back to a heuristic policy.

The paper-comparison configuration uses `fallbackMode: heuristic`. Ordinary solve failures, time limits without an accepted solution, and infeasible or rejected statuses therefore use the explicit greedy fallback. A fallback plan is reported as `fallback_heuristic`, sets `all_replans_milp` to false, and is never reported as a successful MILP solve. The environment surfaces the policy diagnostics and fallback counts so these trials remain distinguishable from successful MILP replans. Set `fallbackMode: error` when strict failure behavior is desired for debugging.

Run the dependency-free method tests with:

```bash
uv run python -m unittest discover \
  -s catalog/production_agv_scheduling/methods/rolling_milp/tests \
  -p 'test_*.py'
```
