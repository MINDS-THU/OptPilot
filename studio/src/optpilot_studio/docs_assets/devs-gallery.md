---
title: DEVS-Gen
description: Generate discrete-event simulations from natural language, then evaluate or optimize them through ordinary OptPilot contracts.
---

# DEVS-Gen

`catalog/devs_gallery` is the release package for
[DEVS-Gen](https://arxiv.org/abs/2603.03784). It includes the interactive
generator and four generated simulators packaged as ordinary OptPilot
Environments. The two compact parameter-tuning examples wrap an
**unmodified** generated `devs_project/` with one small OptPilot-authored
evaluator and a vendored, hash-locked copy of the pure-Python `xdevs` wheel.

The pre-generated examples run locally with no API key or external software.
Creating a new simulator through the DEVS-Gen interface uses the configured
`OPENROUTER_API_KEY`. The complete workflow is covered in
[Generate and Optimize](generate-and-optimize.md).

## Included examples

| Environment | Models | Interesting decision | Shipped study objective |
| --- | --- | --- | --- |
| `seird-epidemic` | SEIRD compartmental epidemic, fixed-step Euler integration over a 30-day horizon | epidemiological parameters | minimize `deceased` |
| `abp-protocol` | Alternating Bit Protocol: sender and receiver exchanging 20 packets across two lossy subnets | sender retransmission `timeout` | minimize `retransmissions` |
| `dispatch-station` | Generated production dispatch simulation | choose the next queued job | evaluate a policy with `total_score` |
| `triage-clinic` | Generated clinic flow simulation | choose the next patient | evaluate a policy with `total_score` |

The first two take `format: parameters` candidates. The last two take
`format: files` policy candidates and expose the trace and validation context
needed by the general trace-guided method in
`catalog/production_agv_scheduling`. This is the intended cross-package
composition: DEVS-Gen supplies a simulator; another research package supplies
the optimization method.

## seird-epidemic

Candidate parameters (from `environments/seird/environment.yaml`):

| Parameter | Type | Range | Default | Meaning |
| --- | --- | --- | --- | --- |
| `transmission_rate` | float | 0.1–10.0 | 2.5 | Transmission rate beta per day |
| `mortality` | float | 0.0–100.0 | 10.0 | Percentage of infective individuals who die |
| `incubation_period` | float | 0.5–30.0 | 5.0 | Mean days from exposure to infectiousness |
| `infectivity_period` | float | 1.0–60.0 | 14.0 | Mean days an individual stays infective |
| `initial_infective` | int | 1–500 | 10 | Infective individuals at time zero |

Metrics are the final compartment populations: `deceased`, `recovered`,
`infective`, `exposed`, `susceptible`. The horizon and population are
evaluator settings, not candidate parameters — `simulationTime: 30.0`,
`totalPopulation: 1000`, `dt: 0.1` — so every trial is scored on the same
system.

## abp-protocol

| Parameter | Type | Range | Default | Meaning |
| --- | --- | --- | --- | --- |
| `timeout` | float | 5.0–200.0 | 20.0 | Sender retransmission timeout (ms) |
| `sender_delay` | float | 1.0–50.0 | 10.0 | Sender preparation delay per packet (ms) |
| `receiver_delay` | float | 1.0–50.0 | 10.0 | Receiver processing delay per packet (ms) |
| `channel_delay` | float | 0.5–20.0 | 3.0 | One-way subnet transmission delay (ms) |
| `seed` | int | 0–10000 | 42 | Deterministic noise seed for both lossy subnets |

Metrics: `packets_delivered`, `retransmissions`, `forward_dropped`,
`ack_dropped`. Evaluator settings fix the workload at `totalPackets: 20` and
`simulateTime: 5000.0`.

The channels are not random: each subnet advances an integer state with
`x = (17 * x + 11) mod 100` per arrival and drops the packet when `x < 10`
(`ABP_D1_libs/DeterministicLossChannel.py`). A given `seed` therefore replays
exactly the same loss pattern, which is what makes the study reproducible.

## Run the shipped studies

Both studies use `gallery-random-search`, a seeded uniform sampler over the
environment's declared parameter schema, for five trials each.

```bash
optpilot run catalog/devs_gallery/studies/seird_minimize_deaths.yaml \
  --package-root catalog/devs_gallery
```

```bash
optpilot run catalog/devs_gallery/studies/abp_tune_timeout.yaml \
  --package-root catalog/devs_gallery
```

Both completed 5/5 trials in about seven seconds each when this page was
written, including first-time preparation of the isolated dependency layer.
For reference, running each evaluator at its declared defaults gives:

| Run | Objective at declared defaults | Best of the 5-trial study |
| --- | --- | --- |
| `seird-minimize-deaths` (seed 7) | `deceased` = 72.10 | `deceased` = 41.02 |
| `abp-tune-timeout` (seed 11) | `retransmissions` = 8.0 | `retransmissions` = 7.0 |

These are baselines for a deliberately naive sampler, not benchmark results.

!!! warning "`seed` is inside the ABP search space"
    Random search samples `seed` along with the timing parameters, so the five
    ABP trials are scored under five different loss patterns and the reported
    minimum mixes noise realisations. Holding `seed = 42` and the other
    defaults fixed while sweeping `timeout` gives 26 retransmissions at 5.0
    and 8 at each of 10.0, 20.0, 50.0, 100.0 and 200.0 — the timeout bites
    only while it is shorter than the round trip. Pin `seed` (or average over
    several) before comparing timing parameters seriously.

To check the package without running anything — this is what CI does:

```bash
optpilot package validate catalog/devs_gallery --check-source
```

## How the locked xdevs runtime works

Each environment declares a process runtime whose setup builds an isolated
virtual environment from its own lock file:

```yaml
runtime:
  sandbox: process
  setup:
    cache: prepared
    timeoutSeconds: 300
    steps:
      - uses: python-venv
        cwd: "."
        requirements: [runtime_dependencies/requirements.lock]
```

The lock file has a single line naming a wheel that lives *inside the package*
together with its SHA-256 — the only form this dependency slice accepts:
vendored, hash-locked, pure-Python (`py3-none-any`) wheels, no package index,
no shell steps, no native extensions. See "Exact Python Dependencies For
Retained Runs" in the [Configuration Reference](configuration.md).

The wheel is vendored **per environment** rather than once for the package
because the declaration belongs to the component that needs the import: an
environment is the unit that gets retained, prepared and replayed, so each one
carries its own dependency closure and its own licence paperwork. `xdevs
3.0.0` is GPL-licensed; both folders ship
`runtime_dependencies/licenses/xdevs-3.0.0-LICENSE.txt` and a
`THIRD_PARTY_NOTICES.md`.

## Register your own generated simulator

The same shape works for any generated model that can be driven in-process:

1. Copy the generated project unchanged into `environments/<name>/devs_project/`.
2. Vendor the pure-Python wheels it imports into
   `runtime_dependencies/vendor/`, record each SHA-256 in
   `runtime_dependencies/requirements.lock`, and include the licence text.
3. Write one `evaluator.py` with an `evaluate(candidate_runtime, context)`
   function that builds the model, runs the xDEVS `Coordinator`, and returns
   `{"metric_values": {...}}` read from final component state.
4. Declare the parameters you want searched under `candidate.parameters.schema`,
   keep fixed workload knobs in `evaluator.settings`, and list the metric keys
   under `metrics.keys` with `source: return`.
5. Point a study at the environment and any parameters-format method.

The generated CLI runners (`devs_project/run_seird_d1.py`,
`devs_project/run_abp_d1.py`) are kept as-is and map neatly onto step 4: their
arguments are the constructor knobs worth exposing.

!!! note "These bundles predate the v2 manifest contract"
    As the package README states, these bundles were generated before the
    `devs.simulation.v2` manifest and the summary/trace contracts existed, so
    they wrap the raw models directly instead of going through Studio's **Set
    up for Catalog** registration wizard. Two consequences: the environments
    declare their metrics by hand rather than inheriting them from a generated
    manifest, and the evaluators disable the generated JSONL process logging
    (metrics come from final component state), so no event trace is retained.
    Register a newly generated simulator through the wizard instead — see
    [Generate and Optimize](generate-and-optimize.md).
