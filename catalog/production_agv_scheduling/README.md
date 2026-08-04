# Dynamic Production and AGV Scheduling

This OptPilot package turns the paper's research prototype into a set of
reusable, inspectable components. It contains one method-agnostic discrete-event
simulation environment, the proposed process-aware LLM heuristic-design method,
and every baseline family used in the paper.

## Design boundary

Every scheduling approach is an executable **file candidate**. A method emits a
bundle containing `scheduler.py`, `param_estimator.py`, and, when needed,
candidate-owned `policy/` modules. The environment loads that bundle in an
isolated module context, runs common stochastic replications, and returns the
same KPI schema for every method.

This means the rule implementations do **not** live inside the environment. The
rule-grid method owns them and generates 135 concrete policy bundles. OptPilot
then evaluates each bundle as a separate trial; the method does not run a
private simulator loop. The first grid point is
`DEFAULT/DEFAULT/DEFAULT`, which is the initial policy, so there is no duplicate
initial-policy method.

```text
method                         candidate boundary                  environment
LLM / rule grid / GA / MILP -> scheduler.py + policy modules -> common simulator
                                                               -> common metrics
                                                               -> run artifacts
```

The simulator accepts two policy entry points without knowing which method
created them:

- `create_scheduler()` for snapshot-to-command dispatch policies.
- `create_controller(simulation, settings)` for event-driven controllers such
  as the rolling MILP.

## Included methods

| Method | What it proposes | Paper-scale study |
| --- | --- | --- |
| Process-aware LLM heuristic design | Initial policy, then 3–4 parallel manager-directed code revisions per iteration; the manager can inspect a deterministic replay of the incumbent's worst replication | `studies/process_aware_llm.yaml` |
| Exhaustive rule grid | All 5 line × 9 task × 3 AGV rules, including the initial policy | `studies/rule_grid.yaml` |
| Genetic algorithm | 14 rule weights, initialized with all 64 non-random one-hot combinations, followed by ten generations | `studies/ga_weighted_rules.yaml` |
| Differential evolution | The same 14-weight space with DE/best/1/bin | `studies/de_weighted_rules.yaml` |
| Particle swarm optimization | The same 14-weight space with the paper's PSO parameters | `studies/pso_weighted_rules.yaml` |
| Rolling MILP | Original monolithic and two-stage controller bundles | `studies/rolling_milp.yaml` |

The evolutionary methods rank candidates by
`mean_total_score - 0.35 × std_total_score`. Candidate evaluation remains in
OptPilot, so trial metrics, source bundles, failures, and lineage are retained
uniformly.

## Environment configurations

All files under `environments/production_agv_scheduling/environment_*.yaml`
use the same evaluator and candidate contract. The configurations only change
evaluation conditions:

- `environment_smoke.yaml`: one short replication for integration checks.
- `environment_llm.yaml`: ten default-setting replications per candidate.
- `environment_meta.yaml`: twenty default-setting replications per candidate.
- `environment_baselines.yaml`: one hundred common-seed replications per fixed
  baseline policy.
- `environment_long_horizon.yaml`, `environment_variable_arrivals.yaml`, and
  `environment_faults.yaml`: robustness conditions for re-evaluating a frozen
  policy.

`studies/parallel_smoke.yaml` is a release check rather than a paper
experiment: it evaluates four deterministic GA candidates with capacity three
using the paper-default ten-replication environment. The longer real evaluator
workload makes all three running-attempt intervals overlap, so three-way
execution can be verified without an LLM call. Use `studies/smoke.yaml` for the
short single-replication integration check.

Each successful evaluation reports mean, sample standard deviation, minimum,
and maximum total score; mean score components; stability fitness; and the seed
and score of the worst replication. `metrics.json` and `worst_run.db` are
declared run artifacts. Evaluation or policy errors fail the trial rather than
being converted into a zero score.

OptPilot runs each retained evaluator attempt in its configured isolated
process runtime. Replications within one attempt are deliberately sequential;
the evaluator removes candidate-owned Python modules between seeds, verifies
the Candidate bundle digest after every replication and replay, and fails if
the executable bundle changes. This resets Python import state, but it does not
turn in-process policy execution into a hostile-code sandbox or prevent a
policy from using other process-global or writable external state.

## Optional 3D interface

Every environment variant declares the same optional factory interface:

- From Catalog, choose **Open Interface** and use **Factory Simulation** to
  explore the compiled Unity WebGL factory with the package's initial policy.
- From a retained file Candidate, choose **Open interactive interface** and use
  **Try Candidate in 3D** to replay that exact immutable policy.
- **Run headless** remains the noninteractive evaluator path and does not start the
  visualization.

The interface server, simulation worker, MQTT broker, and Unity client share
one launch-scoped runtime. MQTT-over-WebSocket traffic stays on the interface's
same-origin `/mqtt` endpoint; no public broker, `.env` file, host credential, or
external network access is required. Catalog launch uses a local process
profile inside Studio's launch-scoped isolated workspace runtime. Candidate
preview uses the package's pinned container profile so the Candidate and
interface source are projected read-only.

Visual replay runs at 1× simulation time. The supplied compiled viewer owns
its movement, turning, and product-transfer animation clocks and has no
playback-rate input, so accelerating only MQTT delivery would make animation
overtake or lag the recorded simulation. The interface also normalizes product
handoffs so an AGV releases a package before its destination claims it. These
visual-only records do not change candidate decisions, KPIs, or evaluator
traces.

Each **Run candidate** or **Replay last run** starts a fresh viewer generation.
The interface unloads the previous Unity scene, rotates its launch-local MQTT
identity, and waits for the replacement viewer to subscribe before publishing
event zero. This prevents objects or in-progress movement from a previous
visual run from leaking into the next one.

That outer launch-scoped workspace or container is the security boundary.
Within it, candidate code runs as a same-user child process with conservative
CPU, memory, process-count, file-size, and descriptor limits. Those child
limits are defense in depth for a failed policy; they are not a separate
security sandbox.

The interface intentionally does not change the evaluator. Ordinary studies
continue to use deterministic in-process simulation without MQTT, browser
rendering, or real-time sleeps. This first interface slice is view-only:
temporary replay telemetry and its trace are removed with the interface
session and are not reported as saveable interface outputs.

Candidate preview also requires an operator-approved local container image.
Approve the exact package-declared digest once in the local Realm, then restart
Studio so it loads the updated trust snapshot:

```bash
uv run optpilot environment-preview trust approve \
  python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
```

For a deliberately temporary session, the legacy startup option remains
available:

```bash
uv run optpilot ui \
  --environment-preview-trusted-image \
  python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
```

That option selects the exact trust set for only that Studio process; it does
not extend or update persistent Realm approvals.

OptPilot does not trust an image merely because a package declares it; **Try
interactively** fails closed until the operator has approved the exact digest.

## Quick check

From the repository root:

```bash
uv run optpilot package validate catalog/production_agv_scheduling \
  --check-imports --check-source --check-setup-files

uv run optpilot run catalog/production_agv_scheduling/studies/smoke.yaml \
  --package-root catalog/production_agv_scheduling

uv run python \
  catalog/production_agv_scheduling/scripts/run_package_tests.py

cd catalog/production_agv_scheduling
shasum -a 256 --check UNITY_BUILD.sha256
```

The smoke study deliberately uses the initial `DEFAULT/DEFAULT/DEFAULT` rule
bundle and a short horizon. Paper-scale studies are substantially more
expensive: the rule grid has 135 trials, each evolutionary study can admit 704
trials, and the default LLM study admits one baseline plus twenty revisions.

For the LLM method, a trial budget and an improvement iteration are different
things. Each iteration proposes four candidates, while the initial policy uses
one trial before those iterations begin:

```text
maxTrials = 1 initial policy + candidatesPerIteration × desired iterations
```

For example, five complete four-candidate improvement iterations require
`maxIterations: 5` in the Method and `maxTrials: 21` in the Study. Setting only
`maxTrials: 5` admits the initial policy and one four-candidate iteration.

## Optional setup

The LLM study uses an OpenAI-compatible chat-completions endpoint through
OpenRouter by default. Add `OPENROUTER_API_KEY` in Studio's local environment
variables or export it before a CLI run. Provider URL, model, token limit, and
temperature are ordinary settings in
`methods/process_aware_llm/method.yaml`.

Launching that method sends bounded manager/editor prompts—including policy
source, metrics, and selected trace evidence—to OpenRouter and the inference
provider it routes to. The default permits OpenRouter provider fallbacks for
the configured model. Review those providers' data-retention terms and account
spending limits before a paper-scale run. OptPilot records bounded request,
response, token, cost, and provider metadata as Candidate provenance; it never
stores the API key in that provenance.

One manager/query/editor round can legitimately outlast the CLI's conservative
10-second method-callback default. Launch the paper-scale LLM study with a
5,000-second retained callback timeout. This covers the configured worst-case
proposal envelope (about 4,344 seconds when every HTTP and semantic retry times
out) as well as the 1,800-second exact-seed replay limit:

```bash
uv run optpilot run catalog/production_agv_scheduling/studies/process_aware_llm.yaml \
  --package-root catalog/production_agv_scheduling \
  --method-request-timeout 5000
```

This launch-time value is separate from `execution.timeoutSeconds`, which
limits each environment evaluation attempt.

The paper-scale Study permits three concurrent evaluations. The LLM designs
four sibling revisions together as one parallel-editor round; three begin at
once and the fourth begins when one evaluator slot becomes available. Reduce
`execution.parallelism` for a smaller machine without changing the candidates
or their retained evaluation settings.

Rolling-MILP evaluation requires `gurobipy` and a valid Gurobi license in the
environment worker's Python runtime. The package can still be validated and the
MILP candidates can still be generated without Gurobi. An actual MILP trial
fails with a clear dependency or license error when the solver is unavailable.
For ordinary solve failures, infeasible statuses, or time-limit exits, the
paper configuration uses its explicit heuristic fallback. Those replans are
labelled as fallbacks—not successful MILP solves—and the evaluator reports both
their mean count and the number of affected replications.

The production simulator includes a vendored, pure-Python SimPy runtime and its
license. It does not require a message broker, network access, or real-time
sleeps.

## Layout

```text
production_agv_scheduling/
  environments/production_agv_scheduling/  # simulator, evaluator, 3D interface, templates
  methods/process_aware_llm/                # manager/query/parallel-editor loop
  methods/rule_grid/                        # 135 fixed rule combinations
  methods/evolutionary_rule_search/         # GA, DE, and PSO
  methods/rolling_milp/                     # original and two-stage MILP
  studies/                                  # runnable bindings and budgets
```

See `SOURCE_PROVENANCE.md` for the extraction map, deliberate cleanup, and
reproducibility boundaries. `RELEASE_READINESS.md` records the verified release
gates and remaining blockers. See `NOTICE.md` before redistributing extracted
research code, and `SIMPY_PROVENANCE.md` for the exact vendored dependency.
