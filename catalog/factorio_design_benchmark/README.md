# factorio_design_benchmark

The Factorio Design Benchmark as an OptPilot package: an LLM proposes a
complete factory as JSON, and 22 executable static checks plus a recipe-based
cost model score it. Static validation is the shipped default — it runs with
no Factorio game, no network, and no proprietary software.

## What ships

| Config | Id | Purpose |
| --- | --- | --- |
| Environment | `factory-design` | Scores one `production_line.json` against a selected benchmark task |
| Method | `direct-designer` | The paper's Direct baseline: propose a design per trial, revise from the previous verdict |
| Method | `direct-designer-seed` | Deterministic twin (no model, no API key, no network) — the CI smoke and the fixed reference point |
| Study | `factory-design-smoke` | One-trial zero-LLM run through the retained pipeline |

## Metrics

`failed_check_count` is the objective: it degrades smoothly as a design gets
closer to valid, where `static_valid` is all-or-nothing. Six family counters
(`failed_schema`, `failed_recipe`, `failed_geometry`, `failed_logistics`,
`failed_power`, `failed_terrain`) localise the remaining work, and
`total_entity_cost` / `entity_count` describe the build.

Treat `total_entity_cost` as a tie-breaker among *valid* designs only. The
design self-declares `logistic_robot_count`, which dominates the cost model
(about 84% of the bundled example), so minimising cost alone drives robots to
zero without making the factory better.

**No production rate is reported.** The static checks cannot derive throughput.
Rate achievement (`actual_rate`, `target_achieved`) exists only in the
benchmark's Factorio execution mode, which is deliberately not part of this
package — see "Execution mode" below.

## Tasks

32 tasks: 8 product families x {low, high} target rate x {easy, hard} map.
Select one with the environment's `task_id` setting, e.g. `iron_gear_low_easy`
(the default), `electronic_circuit_high_hard`, `military_science_pack_low_easy`.
The full list is `fd_core/tasks/configs/`.

> **Open question for the owner — canonical target rates.** The release plan
> records that repo configs disagree with the paper's Table 1 for four product
> families. That cannot be checked from the research tree: it contains no
> paper artifact of any kind. What *is* verifiable is that the 32 JSON configs
> disagree with the programmatic `rates = {...}` fallbacks in
> `tasks/task_config.py` for **all 8 families**, and one upstream doc line
> quotes `inserter_high_hard` at 15/min against the config's 60. The JSON
> configs are the source of truth here. Resolve the rates with the authors
> before publishing any comparative numbers.

## Running it

```bash
uv run optpilot run catalog/factorio_design_benchmark/studies/factory_design_smoke.yaml \
  --package-root catalog/factorio_design_benchmark
```

To use a model, point a study at `direct-designer` (not the seed twin) and
grant `OPENROUTER_API_KEY`; set `mode: llm` in the method settings.

## Execution mode (not shipped, deliberately)

The upstream benchmark can instantiate a design in a real Factorio server and
measure `actual_rate`, `placement_success_rate` and per-item production. That
path needs a **user-provisioned, proprietary** Factorio headless server
(pinned to 1.1.110) driven over RCON, ~58 Lua assets pushed at connect, and
the `factorio-rcon-py`, `lupa`, `slpp` and `pillow` packages — none of which
can be locked into an OptPilot process runtime, and none of which may be
redistributed here. Each design costs roughly 1.5–3 minutes of wall clock.

If you want it, run it out-of-band with the upstream tooling and treat the
result as an external artifact. Static validation is what this package
promises, and it is what catches instantiation failures cheaply.

## Vendored code

`environments/factory_design/fd_core/` is the benchmark's evaluation subset
(schemas, validation, tasks — MIT), edited only for pydantic-v1 compatibility
so it runs on a pure-Python locked runtime. `tests/core/test_factorio_vendored_core.py`
pins that the copy returns identical verdicts to upstream across 288 cases.
See `environments/factory_design/runtime_dependencies/THIRD_PARTY_NOTICES.md`.
