---
title: Factorio Design Benchmark
description: Score LLM-designed factories against 22 executable static checks, with no game required.
---

# Factorio Design Benchmark

This tutorial runs the `factorio_design_benchmark` package: an LLM proposes a
complete factory as a single JSON file, and OptPilot scores it against the
benchmark's 22 static checks plus a recipe-based cost model. It is the
package that demonstrates *comparing methods on one environment repeatably*.

Everything here runs without the Factorio game and without a network, except
where a model is explicitly used.

## What the environment scores

`factory-design` takes one editable file, `production_line.json`, and returns
eleven metrics:

| Metric | Meaning |
| --- | --- |
| `failed_check_count` | How many of the 22 checks failed. **The objective.** |
| `static_valid` | 1.0 only when every check passed |
| `failed_schema`, `failed_recipe`, `failed_geometry`, `failed_logistics`, `failed_power`, `failed_terrain` | Which areas still fail |
| `total_entity_cost`, `entity_count` | Recipe-based cost of the build |
| `warning_count` | Non-fatal advisories |

`failed_check_count` is the objective because it degrades smoothly: a design
that goes from six failures to two has genuinely improved, while
`static_valid` would still read 0. The six family counters tell a method
*where* to look.

!!! warning "Cost is a tie-breaker, not an objective"
    A design declares its own `logistic_robot_count`, and that dominates the
    cost model — about 84% of the bundled example. Minimising
    `total_entity_cost` alone therefore drives robot count to zero without
    improving the factory. Rank by cost only among designs that already pass.

!!! note "No production rate is reported"
    The static checks cannot derive throughput. `actual_rate` and
    `target_achieved` exist only in the benchmark's Factorio execution mode,
    which needs a user-provisioned, proprietary game server and is not part of
    this package. Static validation is what catches instantiation failures
    cheaply, and it is what ships.

## Run the zero-LLM smoke

This needs no API key. It stages the environment's candidate template,
validates it, and records the verdict as a Run:

```bash
uv run optpilot run catalog/factorio_design_benchmark/studies/factory_design_smoke.yaml \
  --package-root catalog/factorio_design_benchmark
```

The Run succeeds with `failed_check_count = 5` for the default task — the
template is a schema illustration, not a solution, so a non-zero score is the
expected baseline.

## Choose a task

There are 32 tasks: eight product families x low/high target rate x easy/hard
map. The task is a launch input, so one Run setup covers all of them:

```bash
uv run optpilot run catalog/factorio_design_benchmark/studies/factory_design_smoke.yaml \
  --package-root catalog/factorio_design_benchmark \
  --input task_id=iron_plate_low_easy
```

In Studio the same choice appears as a **Launch inputs** field on the Run
setup. Changing the task changes the verdict: the template scores 5 on
`iron_gear_low_easy` but 4 on `iron_plate_low_easy`, because that task's
target product is one the template actually produces.

## Design with a model

`factory-design-task` uses `direct-designer-llm` — one design per trial,
revised from the previous trial's verdict, for 25 trials, matching the paper's
protocol. It needs a model.

!!! note "Three method configs, one implementation"
    A Study cannot override a method's settings, so the mode is fixed by which
    method config you point at: `direct-designer-seed` (deterministic, no key),
    `direct-designer-llm` (calls a model), and `direct-designer` (the
    documented default, seed mode). Pick the config, not a flag.

```bash
uv run optpilot run catalog/factorio_design_benchmark/studies/factory_design_task.yaml \
  --package-root catalog/factorio_design_benchmark \
  --input task_id=iron_gear_low_easy
```

Grant `OPENROUTER_API_KEY` through the method's `envFromHost` declaration (in
Studio, save it as a Settings variable). The method assembles its prompt from
the environment's own instructions, schema description, candidate template and
the 22 validation rules, so it carries no Factorio knowledge of its own — point
it at another file-candidate environment that publishes those references and it
works there too.

## What the model is told

Each prompt carries the environment's instructions, schema description and the
22 validation rules, plus two things resolved at launch:

- **the task** — id, target product, target rate, map bounds, ore patches and
  water patches for the `task_id` you launched, read from the environment's
  `task_specs` reference. Without this a model would design against the
  template's task and fail the map-bounds, ore and water checks on the other
  31 tasks.
- **the previous attempt** — the design you submitted last trial and its
  per-family failure counts, so the model revises rather than restarts.

!!! warning "Coarser feedback than the paper"
    The environment reports numeric metrics, so the model sees *how many*
    checks failed in each family, not the individual check ids and detail
    strings the upstream harness renders. Steering is therefore coarser than
    the published Direct baseline, and results are not directly comparable.

## Sweep every task

```bash
uv run python catalog/factorio_design_benchmark/scripts/run_task_sweep.py
```

Sweeps all 32 tasks with the zero-LLM study and prints a per-task table plus
min/max/mean. Pass `--study studies/factory_design_task.yaml` to sweep with a
model instead, and `--tasks <id> <id>` to restrict the set.

## Comparing methods

Because the environment and the method are separate contracts, a second method
over the same environment is an ordinary comparison: point a new study at
`factory-design` with your own method, keep `task_id` and the objective fixed,
and the Runs are directly comparable. `direct-designer-seed` is a useful fixed
reference point — it is deterministic, so any difference is attributable to the
method under test.
