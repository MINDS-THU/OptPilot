# AutoIE Lab Config Assessment

This document assesses whether the OptPilot interface design can wrap
`resource/autoie-lab`, which contains simulation environments and LLM-assisted
optimization methods for factory scheduling and supply-chain strategy design.

## Conclusion

The current design can cover `autoie-lab` if we model both applications as
file-candidate optimization environments:

- the environment owns the simulator, evaluator, initial editable files,
  database fixtures, metrics, and method-visible context;
- the method owns the manager/editor strategy, LLM settings, retry policy, and
  iteration logic;
- the study owns objective, budget, repeat-run policy, and execution limits.

The main refinement needed is explicit method-visible data artifacts. AutoIE
methods inspect a SQLite history database while editing code. That database is
not a candidate and should not be hard-coded in method config. It should be
declared by the environment as exposed context, optionally paired with a
read-only query capability.

## What AutoIE Lab Contains

`autoie-lab` has two optimization applications.

The factory app optimizes Python scheduling logic for a multi-line factory
simulator:

- initial candidate files:
  - `optimization_app/factory_optimization_app/initial_scheduler.py`
  - `optimization_app/factory_optimization_app/initial_param_estimator.py`
- initial data fixture:
  - `optimization_app/factory_optimization_app/initial_database.db`
- evaluator wrapper:
  - `optimization_app/factory_optimization_app/simulation_runner.py`
- objective metric:
  - maximize `total_score`
- secondary metrics:
  - `efficiency_score`
  - `quality_cost_score`
  - `agv_score`

The supply-chain app optimizes Python strategy logic for a miniSCOT-style
supply-chain simulator:

- initial candidate files:
  - `optimization_app/supplychain_optimization_app/initial_strategy.py`
  - `optimization_app/supplychain_optimization_app/initial_param_estimator.py`
- initial data fixture:
  - `optimization_app/supplychain_optimization_app/initial_database.db`
- evaluator wrapper:
  - `optimization_app/supplychain_optimization_app/simulation_runner.py`
- objective metric:
  - maximize `profit`
- secondary metrics:
  - `revenue`
  - `vendor_cost`
  - `transfer_cost`
  - `holding_cost`
  - `delay_penalty`
  - `terminal_asset_value`

Both applications use a two-level LLM method:

- a manager agent proposes improvement plans;
- an editing agent modifies the declared Python files;
- the simulation runner evaluates each modified workspace;
- the manager selects the best candidate and repeats.

In OptPilot terms, the manager is a controller and the editing agent is a
file-candidate engine.

## Factory Environment Mapping

The factory simulator can be wrapped as an `EnvironmentConfig` with
`candidate.type: files` and `artifactKind: code_bundle`.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: autoie-factory-scheduling
description: Optimizes scheduler and estimator Python files for the AutoIE multi-line factory simulator.
tags: [autoie, factory, scheduling, code-editing]

evaluate:
  type: python
  callable: optpilot_autoie.factory:evaluate
  timeoutSeconds: 600
  config:
    simulationSeconds: 500
    simTimeStep: 0.5
    repeatRuns: 5
    disableFaults: true

workspace:
  copy:
    - from: optimization_app/factory_optimization_app/initial_scheduler.py
      to: scheduler.py
      role: source
    - from: optimization_app/factory_optimization_app/initial_param_estimator.py
      to: param_estimator.py
      role: source
    - from: optimization_app/factory_optimization_app/initial_database.db
      to: database.db
      role: data
  readonly:
    - database.db

interfaces:
  - id: historical_db_query
    capability: optpilot.sqlite_query.v1
    description: Read-only SQL access to the historical factory database exposed to candidate-generation methods.
    adapter:
      implementation: builtin.sqlite_query
      config:
        path: database.db

candidate:
  type: files
  artifactKind: code_bundle
  description: Factory scheduling policy and parameter-estimation source files.
  files:
    root: .
    source:
      type: workspace_copy
      root: .
    editable:
      - path: scheduler.py
        language: python
        role: scheduling_policy
        description: Must expose create_scheduler(); scheduler.run(snapshot) emits factory commands.
      - path: param_estimator.py
        language: python
        role: parameter_estimation
        description: Estimates scheduler constants from database.db.
    required:
      - scheduler.py
      - param_estimator.py
    allow:
      - scheduler.py
      - param_estimator.py
    deny:
      - database.db
      - "**/__pycache__/**"
      - "**/*.pyc"
  exposure:
    instructions:
      - optimization_app/factory_optimization_app/prompts/database_description.md
    contextArtifacts:
      - id: historical_database
        path: database.db
        role: historical_data
        mediaType: application/vnd.sqlite3
        readonly: true

metrics:
  source: return
  keys: [total_score, efficiency_score, quality_cost_score, agv_score]

filesToSave:
  - metadata.json
  - database.db
  - scheduler.py
  - param_estimator.py

recordsToExtract:
  - name: factory_kpi
    source: sqlite_table
    path: database.db
    table: kpi
  - name: factory_orders
    source: sqlite_table
    path: database.db
    table: order
```

The callable `optpilot_autoie.factory:evaluate` would be a thin adapter around
`factory_optimization_app.simulation_runner.SimulationRunner`. It should create
the trial workspace, run the simulator, and return a flat metric dictionary.

The AutoIE runner expects the workspace files to be named `scheduler.py`,
`param_estimator.py`, and `database.db`. That is an environment fact, so it
belongs in `workspace.copy`, `candidate.files`, and evaluator materialization,
not in the method config.

## Supply-Chain Environment Mapping

The supply-chain app uses the same boundary, with different editable files and
metrics.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: autoie-supplychain-strategy
description: Optimizes strategy and estimator Python files for the AutoIE supply-chain simulator.
tags: [autoie, supply-chain, strategy, code-editing]

evaluate:
  type: python
  callable: optpilot_autoie.supplychain:evaluate
  timeoutSeconds: 600
  config:
    simulationDuration: 100
    repeatRuns: 5

workspace:
  copy:
    - from: optimization_app/supplychain_optimization_app/initial_strategy.py
      to: strategy.py
      role: source
    - from: optimization_app/supplychain_optimization_app/initial_param_estimator.py
      to: param_estimator.py
      role: source
    - from: optimization_app/supplychain_optimization_app/initial_database.db
      to: database.db
      role: data
  readonly:
    - database.db

interfaces:
  - id: historical_db_query
    capability: optpilot.sqlite_query.v1
    description: Read-only SQL access to the historical supply-chain database exposed to candidate-generation methods.
    adapter:
      implementation: builtin.sqlite_query
      config:
        path: database.db

candidate:
  type: files
  artifactKind: code_bundle
  description: Supply-chain strategy and parameter-estimation source files.
  files:
    root: .
    source:
      type: workspace_copy
      root: .
    editable:
      - path: strategy.py
        language: python
        role: strategy_policy
        description: Defines purchase_strategy, fulfillment_strategy, and placement_strategy.
      - path: param_estimator.py
        language: python
        role: parameter_estimation
        description: Estimates demand parameters from database.db.
    required:
      - strategy.py
      - param_estimator.py
    allow:
      - strategy.py
      - param_estimator.py
    deny:
      - database.db
      - "**/__pycache__/**"
      - "**/*.pyc"
  exposure:
    instructions:
      - optimization_app/supplychain_optimization_app/prompts/database_description.md
      - optimization_app/supplychain_optimization_app/prompts/supplychain_description.md
    contextArtifacts:
      - id: historical_database
        path: database.db
        role: historical_data
        mediaType: application/vnd.sqlite3
        readonly: true

metrics:
  source: return
  keys:
    - profit
    - revenue
    - vendor_cost
    - transfer_cost
    - holding_cost
    - delay_penalty
    - terminal_asset_value

filesToSave:
  - metadata.json
  - database.db
  - strategy.py
  - param_estimator.py

recordsToExtract:
  - name: customer_orders
    source: sqlite_table
    path: database.db
    table: customer_orders
  - name: supplychain_metrics
    source: sqlite_table
    path: database.db
    table: metrics
```

The callable `optpilot_autoie.supplychain:evaluate` would be a thin adapter
around `supplychain_optimization_app.simulation_runner.SimulationRunner`.

## AutoIE Method Mapping

The existing AutoIE method should be represented as a method that consumes a
file-candidate context and optionally requires read-only SQLite query access.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: autoie-manager-editor
description: LLM manager and code-editing engine for file-candidate optimization.
tags: [llm, code-editing, manager-editor]

controller:
  id: manager
  implementation: python:optpilot_autoie.methods:PlanManagerController
  config:
    modelId: openai/gpt-4.1
    maxIterations: 10
    earlyStopPatience: 3
    targetMetricValue: null

engine:
  id: editor
  implementation: python:optpilot_autoie.methods:CodeEditingPlanEngine
  config:
    modelId: openai/gpt-4.1-mini
    maxRetries: 3
    maxSteps: 12
    tools:
      - read_files
      - search_files
      - edit_files
      - sqlite_query

compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext:
    - files.source
    - files.editable
  optionalContext:
    - exposure.instructions
    - exposure.contextArtifacts
  requiredCapabilities:
    - optpilot.sqlite_query.v1
```

This method config contains no AutoIE-specific environment paths or file names.
Those details come from the compiled `CandidateContext`.

The existing AutoIE `ExecutePlan` tool combines candidate generation and
evaluation in one call. In OptPilot it should be split:

- the controller asks the engine for candidate proposals;
- the engine edits only the files declared by `candidate.files.editable`;
- OptPilot validates and materializes each proposal;
- the evaluator runs the materialized trial workspace;
- the controller observes metrics and evidence before choosing the next plans.

This split is what makes the method reusable across both AutoIE environments
and other compatible file-editing environments.

## Data and Database Handling

AutoIE uses SQLite in three distinct ways.

First, the initial database is input/context data. The editable estimator files
read `database.db` to estimate constants before evaluation. This means the
database must be copied into the trial workspace.

Second, the database is method-visible context. The LLM editing agent can query
historical data while deciding how to rewrite the estimator or policy. This
means the environment should expose the database read-only through
`candidate.exposure.contextArtifacts` and, when the method wants SQL access,
through an `interfaces` capability such as `optpilot.sqlite_query.v1`.

Third, the simulator writes an evaluated run database. That database is
evidence. It should be saved through `filesToSave` and optionally converted
into structured records through `recordsToExtract`. Controllers and monitors can
then read it through `EvidenceView.records(...)` without knowing where trial
workspace files are stored.

These three roles should stay separate:

| Role | Config location | Example |
| --- | --- | --- |
| Evaluator input fixture | `workspace.copy` | `initial_database.db` copied to `database.db` |
| Method-visible context | `candidate.exposure.contextArtifacts` and `interfaces` | read-only SQL queries over `database.db` |
| Post-run evidence | `filesToSave` and `recordsToExtract` | final `database.db`, `kpi`, `customer_orders`, `metrics` |

## Compatibility With Other Method Families

The same design still covers other optimization methods.

RL methods usually do not edit `scheduler.py` or `strategy.py`; they would
typically produce `opaque / policy_checkpoint` candidates and require a rollout
capability such as `optpilot.rollout.v1`. AutoIE's file-editing environments
are not automatically compatible with those methods unless they are modeled as
policy-checkpoint environments.

Bayesian optimization and meta-heuristics over simulator knobs would use
`parameters / parameter_spec`, not the AutoIE file contract. If the same
simulator exposes both editable policy files and tunable numeric knobs, that
should be modeled as separate environment configs unless the project later adds
a fully implemented composed-candidate contract.

LLM-monitored versions of BO, RL, or meta-heuristics should be expressed with
`monitor`, not by changing the candidate contract. The monitor can inspect
observations, metrics, artifacts, logs, and exposed context, but it does not own
environment paths or evaluator details.

## Recommended Design Improvement

The core candidate boundary is good. The only design improvement prompted by
AutoIE is to explicitly document method-visible data artifacts:

```yaml
candidate:
  exposure:
    contextArtifacts:
      - id: historical_database
        path: database.db
        role: historical_data
        mediaType: application/vnd.sqlite3
        readonly: true
```

This is intentionally general. It can represent SQLite databases, CSV datasets,
JSONL traces, simulator profiles, trained reference policies, or any other
non-candidate artifact a method may inspect.

If the method needs a structured interaction protocol over that artifact, the
environment declares an open capability under `interfaces`. For AutoIE, a
read-only SQLite query capability is enough. For other projects, the capability
might be a rollout API, dataset stream, simulator query API, or custom lab-owned
adapter.

## Verdict

OptPilot can wrap `autoie-lab` cleanly. The wrapping should not put source
directories, target files, database paths, or simulator metrics in method
config. Those are environment-owned contract details. The method should consume
the normalized candidate context and declared capabilities.

With `contextArtifacts` plus optional query capabilities, the config design
covers both AutoIE applications without making the project less general.
