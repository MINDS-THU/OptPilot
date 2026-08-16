---
title: Configuration Reference
description: Field-by-field reference for OptPilot environment, method, study, and resource configs.
---

# Configuration Reference

!!! tip "New to OptPilot?"

    Start with [Getting Started](getting-started.md) if you have not run
    OptPilot yet. Use this page as a field reference once you have seen one
    successful run.

OptPilot public configs are YAML files validated by packaged JSON Schemas. In a
source checkout, those schemas live under `src/optpilot/schemas/`. They are used
by:

```bash
optpilot validate path/to/study.yaml
```

!!! warning "Schema surface is broader than the current runner"

    The Realm runner currently executes parameter and bounded file candidates
    with Python batch methods/evaluators and package-owned `trialWorkspace`
    seeds on the local process runtime, without setup/build, containers, host
    values for Environments/backends, or ambient inheritance. A process Method
    may explicitly declare `runtime.envFromHost`; only those named values are
    supplied to its worker. Studio Runs retain only opaque local value
    revisions; raw values are excluded from the durable process request and Run
    evidence.
    Command batch methods also execute on this slice: the command head must be
    the logical interpreter name `python`/`python3`, mapped to the prepared
    method runtime. Session methods, command evaluators, containers, and legacy
    path-backed output declarations may validate as authored schema but are not
    executable by this retained slice. Unsupported studies fail closed.

To validate a whole package folder, use:

```bash
optpilot package validate path/to/package
```

For package source paths, setup files, and Python import targets, run the
explicit deeper checks:

```bash
optpilot package validate path/to/package \
  --check-source \
  --check-setup-files \
  --check-imports
```

These checks are intentionally stricter than normal runtime path resolution:
package source paths must stay inside the package being validated. This is what
lets a package keep working after it is moved from an attached external project
into `catalog/my_package/` or published as an immutable Realm package revision.

To check or execute setup declarations:

```bash
optpilot package setup-check path/to/package
optpilot package setup-check path/to/package --run-setup
```

To smoke-run a package study:

```bash
optpilot package smoke path/to/package --study studies/smoke.yaml
```

It covers the three public experiment config roles: `environment`, `method`,
and `study`. Catalog packages may also include optional `resource` manifests
for support material and launchable helper interfaces. Resources are secondary
catalog entries, not part of the core environment-method-study experiment
contract.

For the conceptual model behind those roles, use [Concepts](concepts.md). For the runtime procedure after these files are loaded and validated, use [How a Run Works](how-it-works.md).

If you are deciding how to connect a new method to a new environment, read [Candidate Contracts](candidate-contracts.md) before using this field reference. The reference tells you which fields exist; the contract guide explains how the fields fit together.

Every public config starts with:

```yaml
apiVersion: optpilot.io/v1
config: environment   # enum: environment | method | study | resource
```

`config` selects the schema. At launch, OptPilot captures the explicit package
root and retains an exact path-free study definition in the Realm. Users do not
author or locate that internal definition as a run-directory file.

## Naming Rules

The public config uses concrete names for concrete jobs.

| Name | Use |
| --- | --- |
| `config` | Identifies the config file role: `environment`, `method`, `study`, or `resource`. |
| `format` | Identifies candidate representation: `parameters`, `files`, or `opaque`. |
| `valueType` | Identifies one parameter value shape inside `candidate.parameters.schema`. |
| `python`, `command`, `adapter` | Identify how evaluator or method code is invoked without a separate discriminator field. |
| `source` | Identifies where a value comes from for selector fields such as `metrics.source` and `records[].source`. |

Candidate compatibility is based on the candidate format plus required contract paths and capabilities. OptPilot does not require a separate candidate domain label.

## Reference Types

| Type | Meaning |
| --- | --- |
| Free string | You choose the value. Used for ids, names, labels, descriptions, tags, or method-specific settings. |
| Enum | Must be one of the listed values. JSON Schema validates it. |
| Path | A filesystem path. Relative paths are resolved from the YAML file that contains the path unless noted otherwise. |
| Python import | `module:function` or `module:Class`, resolved through the config's `pythonPath`. Config-local imports such as `evaluator:evaluate` are preferred for catalog packages. |
| Command | A list of strings passed to a subprocess, for example `[python, script.py, "{input_file}", "{output_file}"]`. |
| Object | JSON/YAML object. Some objects are passed through to user code as settings. |

## Validation Pipeline

`optpilot validate` is intended to check more than YAML syntax.

The validation pipeline is:

```text
parse YAML
validate each config against JSON Schema
resolve referenced configs and relative paths
run semantic compatibility checks
compile to internal StudySpec
validate internal StudySpec invariants
```

That is why validation is the recommended first command whenever you create or edit a study.

## Config Roles

This reference covers three authored experiment config roles:

- `config: environment` describes what can be evaluated and how
- `config: method` describes how candidates are proposed and what contracts the method accepts
- `config: study` binds one environment to one method and chooses one run policy

Packages can also include `config: resource` manifests for optional support
material or launchable helper interfaces. Resources are not part of the core
environment-method-study experiment contract.

## Directory Layout

The same organization is used for built-in examples, local user-owned code, and
additional packages:

```text
catalog/
  my_package/
    environments/
      my_environment/
        environment.yaml
        evaluator.py
        assets/
        prompts/
    methods/
      my_method/
        method.yaml
        method.py
    studies/
      my_study.yaml
```

Environment and method configs are reusable. A single environment
implementation can have multiple environment YAML files for different datasets,
fidelity levels, metrics, or runtime settings. A single method implementation
can have multiple method YAML files for different prompts, models,
hyperparameters, or runtime settings.

Study configs are concrete run plans. Keep authored studies with their package
or project. Studio Study Builder stores a new study in a Realm-managed workspace
assembled from the exact selected package roots, so edits and launch can be
fenced by an exact workspace revision.

## Path Resolution

| Field | Relative to |
| --- | --- |
| `study.environmentConfig`, `study.methodConfig` | The study config file. |
| `environment.evaluator.pythonPath`, `environment.trialWorkspace[].from`, `environment.methodContext.instructions`, `environment.methodContext.references[].path` | The environment config file. |
| `method.entrypoint.pythonPath`, `method.runtime.container.build.context` | The method config file. |
| `environment.evaluator.cwd` | The trial workspace created for the candidate evaluation. |
| `environment.outputFiles[].path` and string `environment.outputFiles` entries | The trial workspace after evaluator execution. |
| `environment.records[].path` for file-backed records | The trial workspace after evaluator execution. |

Python import strings are resolved by normal Python import rules after any declared `pythonPath` entries are prepended.

These are authoring-path rules inside a package; they are not the browser API.
Realm-backed Studio rows use exact catalog entry refs, and Study Builder accepts
those refs rather than host config paths. The generated YAML remains portable
because it writes ordinary relative
`environmentConfig` and `methodConfig` strings.

Most setup paths resolve from the config file that owns the field. Runtime
paths that describe what the evaluator should read or produce resolve inside
the trial workspace, because that workspace is the directory OptPilot prepares
and evaluates for each candidate.

Example:

- `catalog/my_package/studies/my_study.yaml` resolves `environmentConfig`
  relative to the study file
- `catalog/my_package/environments/my_environment/environment.yaml` resolves
  evaluator `pythonPath`, `trialWorkspace`, and `methodContext` paths relative
  to the environment file
- `catalog/my_package/methods/my_method/method.yaml` resolves any `pythonPath`
  entries relative to the method file

## Environment Config

An environment config describes what can be evaluated and how the evaluation happens.

The block below is an annotated schema template, not a runnable retained-study
example. It intentionally shows alternatives; the current runner accepts only
the Python/parameters/process subset described above.

```yaml
apiVersion: optpilot.io/v1
config: environment

# Free string. Stable id shown in the UI and run evidence.
id: my-environment

# Optional free text and tags.
description: My simulator or evaluator.
tags: [tutorial]

# Required. Exactly one of python, command, or adapter.
evaluator:
  # Python import. Function signature:
  # evaluate(candidate_runtime, context) -> dict
  python: evaluator:evaluate

  # Alternative command evaluator.
  # command: [python, run_eval.py, "{candidate_json}", "{settings_file}", "{metrics_file}"]

  # Alternative custom adapter class.
  # Use only when a direct Python function or command is not enough.
  # adapter: adapter:MyAdapter

  # Optional evaluator controls.
  # timeoutSeconds bounds each evaluation by wall clock. A slower evaluation
  # ends as a typed "timeout" trial result with its logs; a worker that
  # cannot be interrupted is stopped shortly after the limit. The effective
  # limit is the smaller of this value and the study's execution
  # timeoutSeconds; 600 when neither is declared.
  timeoutSeconds: 600
  pythonPath: [.]
  # Runtime working directory inside the trial workspace, not relative to this YAML file.
  cwd: .
  # NOTE: `env:` is NOT supported by the retained runner. Declaring it fails
  # the Run at attempt binding with `evaluator_environment_unsupported`,
  # because evaluator paths and environment must come from typed runtime
  # scopes. Put scenario values in `settings:` below, and use a method's
  # `runtime.envFromHost` when a secret is genuinely needed.
  # Free object passed to the evaluator in context["settings"].
  # Use it for environment-owned scenario, dataset, query, case-list, or simulator arguments.
  settings:
    target_x: 4.0

# Optional runtime for the environment evaluator.
runtime:
  sandbox: process       # enum: process | container
  # container:
  #   image: python:3.11-slim
  #   executable: docker
  #   network: disabled  # enum: enabled | disabled

# Optional package files/directories mapped into each fresh trial workspace.
trialWorkspace:
  - from: assets/input_data
    to: input_data

# Required. Defines what methods must produce.
candidate:
  format: parameters     # enum: parameters | files | opaque
  description: Parameters accepted by the evaluator.
  parameters:
    schema:
      x:
        valueType: float # enum: float | int | bool | string | categorical | array | object
        min: 0.0
        max: 1.0

# Optional method-visible context resolved from this environment config.
methodContext:
  instructions:
    - prompts/system_prompt.md
  references:
    - name: dataset_notes
      path: assets/notes.md
      type: markdown
      description: Natural-language dataset notes for the method.
      mimeType: text/markdown
    - name: historical_results
      path: assets/results.sqlite
      type: sqlite
      description: Read-only historical evaluation database.
      mimeType: application/vnd.sqlite3

# Required. Declares where metrics come from.
metrics:
  source: return         # enum: return | file | stdout | sqlite | custom
  keys: [score]

# Legacy authoring fields. The retained runner rejects these until the strict
# artifact/record contract lands.
records:
  - name: events
    source: jsonl        # enum: jsonl | csv | sqlite_table | sqlite_query | custom
    path: events.jsonl

outputFiles:
  - metrics.json
  - path: logs/*.txt
    name: logs
    required: false

# Optional capability ids exposed by this environment.
capabilities:
  - id: historical_db_query
    description: Read-only access to a historical SQLite database.
  - id: exact_seed_replay
    description: Replays a candidate on an exact evaluation seed.
    callable: evaluator:replay_candidate

# Optional static policy contract for generated candidate code.
policyValidation:
  entrypoint:
    file: scheduler.py
    callable: create_scheduler
    maxArguments: 0
  forbiddenImports: [os, sys, subprocess]
  forbiddenNames: [create_controller]
  lints:
    - id: battery-field
      forbiddenConstant: battery
      message: use 'battery_level' for AGV records.
```

A capability may declare an environment-owned `callable` (`module:object`)
that implements it. The module resolves against the environment's Python
import roots; when a method's `accepts.requires.capabilities` names such a
capability, the retained runner adds those environment import roots to the
method's runtime, so the method can resolve the declared entry without a
`pythonPath` that reaches across the package. Retained compilation verifies
the declared callable module is present under the retained environment roots.

`policyValidation` declares the static contract that generated candidate code
must satisfy: a required entrypoint (one synchronous top-level function with a
bounded signature that nothing rebinds), forbidden import roots, forbidden
identifiers, and string-constant lints with authored messages. The block
travels in the candidate context (`context.policyValidation`), and any
code-editing method can apply it generically with
`optpilot.policy_validation.validate_policy_sources(sources, policy)` instead
of hardcoding per-environment AST checks. These lints give code-generating
methods early, high-quality feedback; they are not a security boundary —
candidate code always runs under the evaluator's own isolation.

For the retained local-process slice, each `trialWorkspace.from`
must resolve to a regular file or directory inside the explicit package root.
OptPilot seals those bytes with the package, compiles ordered portable input
layers, and gives each attempt a fresh writable trial volume initialized from
them. The evaluator may modify that trial volume without changing the retained
package. Shared directories and identical files may overlap; conflicting or
case-colliding destinations fail closed. The config does not select copying,
overlay, reflink, or another provider realization strategy.

For a current runnable environment, use a Python evaluator, a parameter or
bounded file candidate contract, process runtime, and no legacy
`records`/`outputFiles`.

### Evaluator Return

Python evaluators normally return:

```python
def evaluate(candidate_runtime, context):
    settings = context["settings"]
    return {
        "status": "success",
        "metric_values": {"score": 0.9},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

For parameter candidates, `candidate_runtime` is the candidate parameter
dictionary. For file candidates, it contains the fresh trial-workspace path,
the environment-owned candidate root, the validated path-free file declaration,
and optional entrypoint/options. It never contains a Realm content ref,
immutable-store path, or staging token.

`evaluator.settings` is intentionally a plain object. OptPilot does not define
domain-specific concepts such as scenarios, datasets, queries, or benchmark
cases. If an environment needs those inputs, put them in
`evaluator.settings` and let the evaluator or custom adapter interpret them.
For example:

```yaml
evaluator:
  python: evaluator:evaluate
  settings:
    dataset: data/train.csv
    split: validation
    simulation:
      duration: 1000
      num_aircraft: 4
```

For multi-case benchmarks, keep the same pattern:

```yaml
evaluator:
  adapter: adapter:BenchmarkAdapter
  pythonPath: [.]
  settings:
    cases:
      - id: small
        path: assets/cases/small.yaml
      - id: medium
        path: assets/cases/medium.yaml
```

The adapter can loop over `cases`, call domain code, aggregate metrics, and
return one OptPilot evaluator result. If a method must read the same case files
before proposing a candidate, expose those environment-owned files through
`methodContext.references`. Keep method `settings` for method-owned knobs,
model choices, prompts, or assets. This keeps case handling out of OptPilot
core while preserving a clear environment/method boundary.

### Typed Settings With settingsSchema

`evaluator.settings` stays a free object by default, but an environment may
declare an optional `evaluator.settingsSchema` that types it. Each entry uses
the same parameter definition as `candidate.parameters.schema` (`valueType`,
`min`/`max`, `values`, `default`, `description`, `unit`, `pattern`, and nested
`items`/`properties`):

```yaml
evaluator:
  python: evaluator:evaluate
  settingsSchema:
    scenario:
      valueType: categorical
      values: [baseline, faults, long_horizon]
      default: baseline
      description: Which bundled scenario to simulate.
    replications:
      valueType: int
      min: 1
      max: 50
      default: 5
  settings:
    scenario: baseline
```

When `settingsSchema` is declared, validation enforces it: every declared
setting without a `default` must be present in `settings`, undeclared keys are
rejected, and each value must match its declared type, bounds, membership, and
pattern. A config without `settingsSchema` keeps the untyped behavior
unchanged. Declaring a schema is what lets Studio render a real input form for
the component instead of falling back to YAML inspection, so prefer it for any
setting a user is expected to change.

### Command Placeholders

Command evaluators can use these placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{python}` | Current Python executable. |
| `{workspace}` | Trial workspace path. |
| `{candidate_root}` | Root directory containing the materialized candidate. |
| `{candidate_file}` | Single candidate file path when unambiguous, otherwise candidate root. |
| `{candidate}` | Alias for `{candidate_file}`. |
| `{candidate_json}` | JSON file containing the candidate runtime payload. |
| `{settings_file}` | JSON file containing `evaluator.settings`. |
| `{metrics_file}` | Expected metrics file path. |
| `{trial_id}` | Trial id. |
| `{study_id}` | Study id. |

### Candidate Formats

`parameters` candidates are JSON-like assignments validated against a schema:

Candidate field fragment:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      rate:
        valueType: float
        min: 0.0
        max: 8.0
        default: 4.0
      mode:
        valueType: categorical
        values: [balanced, aggressive, conservative]
    constraints:
      - id: aggressive-rate
        description: Aggressive mode requires rate at least 2.
        expr:
          any:
            - compare:
                left: {param: mode}
                op: "!="
                right: {const: aggressive}
            - compare:
                left: {param: rate}
                op: ">="
                right: {const: 2.0}
```

#### Parameter Constraints

`candidate.parameters.constraints` is optional. Use it when individual parameter bounds are not enough and a valid candidate must satisfy relationships among fields.

Each constraint has an `id`, an optional `description`, and an `expr`. The expression is a small YAML/JSON tree. It is intentionally simple so OptPilot can validate it before a run and evaluate it during candidate materialization.

Boolean expression nodes:

| Node | Meaning |
| --- | --- |
| `compare` | Compare two scalar expressions. |
| `all` | Every child expression must be true. |
| `any` | At least one child expression must be true. |
| `not` | Negates one child expression. |

Comparison operators:

| Operator | Meaning |
| --- | --- |
| `<`, `<=`, `>`, `>=` | Numeric or ordered comparison. |
| `==`, `!=` | Equality comparison. |
| `in`, `not_in` | Membership comparison. |

Scalar expression nodes:

| Node | Meaning |
| --- | --- |
| `{param: name}` | Read a candidate value from `spec.name`. |
| `{const: value}` | Use a literal value. |
| `{op: add, args: [...]}` | Add scalar expressions. |
| `{op: sub, args: [...]}` | Subtract scalar expressions from the first argument. |
| `{op: mul, args: [...]}` | Multiply scalar expressions. |
| `{op: div, args: [...]}` | Divide the first argument by each following argument. |

For example, this constraint requires `batch_size * workers <= 256`:

```yaml
constraints:
  - id: total-worker-batch-limit
    description: Total worker batch must fit the memory budget.
    expr:
      compare:
        left:
          op: mul
          args:
            - {param: batch_size}
            - {param: workers}
        op: "<="
        right: {const: 256}
```

If a candidate violates a constraint, OptPilot rejects it before calling the evaluator and records the failed constraint id in candidate evidence.

`files` candidates are generated file sets. `trialWorkspace` optionally seeds
the workspace, and the method stages generated files through
`runtime_context.candidate_staging_dir`. OptPilot freezes the complete proposal,
seals each tree, atomically admits it, and projects the selected immutable tree
under `candidate.materialize.root` as the final input layer of every fresh
attempt. The config describes semantics only; it does not select copying,
overlay, reflink, or another provider realization.

Environment field fragments:

```yaml
trialWorkspace:
  - from: assets/template_project
    to: project

candidate:
  format: files
  description: Editable source file.
  materialize:
    root: project
  files:
    editable:
      - path: solver.py
    required:
      - solver.py
    allow:
      - solver.py
    deny: []
```

`opaque` candidates are for custom method/environment pairs that share their own payload semantics:

Candidate field fragment:

```yaml
candidate:
  format: opaque
  opaque:
    family: my-custom-payload
```

## Method Config

A method config describes candidate proposal code and declares which environment contracts it accepts.

The block below is an annotated field template, not a runnable example file. A real method config should choose one entrypoint style and only include the fields it uses.

```yaml
apiVersion: optpilot.io/v1
config: method

id: my-method
description: My optimizer.

entrypoint:
  # Python import. Class constructed as MyMethod(definition, study_spec, rng).
  python: method:MyMethod
  protocol: batch        # enum: batch | session
  pythonPath: [.]
  # Maximum duration of one propose/observe exchange. Defaults to 10 seconds.
  # This is not a whole-Run or internal HTTP-client timeout.
  exchangeTimeoutSeconds: 60

  # Alternative command entrypoint. The retained runner executes command
  # BATCH METHODS: one bounded subprocess per proposal exchange, with
  # `command[0]` restricted to python/python3 (it is mapped to the worker's
  # prepared interpreter). Command *evaluators* remain validate-only.
  # See methods.md for the JSON stdin/stdout and {input_file}/{output_file}
  # protocols.
  # command: [python, method.py, "{input_file}", "{output_file}"]

# Free object passed to the method as method settings.
settings:
  batchSize: 4

# Optional typed declaration for settings. Uses the same parameter
# definition as candidate.parameters.schema. When declared, settings are
# validated against it: declared settings without a default are required,
# undeclared keys are rejected, and values must match types and bounds.
# Typed settings also let Studio render an input form for this method.
settingsSchema:
  batchSize:
    valueType: int
    min: 1
    max: 64
    default: 4
    description: Candidates proposed per exchange.

# Required compatibility declaration.
accepts:
  formats: [parameters]  # list of parameters | files | opaque
  requires:
    context:
      - candidate.parameters.schema
    capabilities: []

# Optional method runtime. Useful for methods with their own dependencies.
runtime:
  sandbox: process       # enum: process | container
```

For a current runnable method, use a Python `batch` entrypoint accepting
`parameters` with process runtime. The environment owns the candidate contract;
OptPilot validates every submitted candidate against that environment contract
during the run.

### Exact Python Dependencies For Retained Runs

An Environment or Method in the current process runtime can declare Python
dependencies without relying on packages installed in Studio's host Python.
Vendor pure-Python wheels in the same package, record each wheel's SHA-256 in a
lock file, and use the same narrow `runtime.setup` declaration on whichever
component needs those imports:

```yaml
runtime:
  sandbox: process
  setup:
    cache: prepared
    timeoutSeconds: 300
    steps:
      - uses: python-venv
        cwd: ../..
        requirements: [requirements.lock]
```

Paths are resolved from `cwd`, which is itself relative to the component YAML.
Each non-comment lock-file line has exactly this form:

```text
vendor/example_dependency-1.2.3-py3-none-any.whl --hash=sha256:<64 lowercase hex characters>
```

This first retained dependency slice intentionally accepts only vendored,
hash-locked `py3-none-any` (or `py2.py3-none-any`) wheels. It does not run shell
commands, contact a package index, install the project itself, inherit host
secrets, or accept native extensions. During Workspace Setup, **Check** validates
the declaration and paths; **Test** verifies the hashes, prepares the exact
read-only dependency layers, and executes the Study through the ordinary Run
path. A successful preparation is cached locally for speed, while every Run
retains the exact dependency trees it used.

A method that names an environment capability under
`accepts.requires.capabilities` runs that environment's own code inside the
method process, so it also receives the environment's prepared dependency
layer, read-only. The layer is placed after the method's own imports, which
means a method that locks its own dependencies keeps them: the environment's
layer only supplies imports the method does not provide itself. Declaring the
capability is what grants this — a method that reaches into an environment
directory through `entrypoint.pythonPath` alone sees that source but none of
its locked dependencies.

Batch Python methods can implement:

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng=None):
        ...

    def propose(self, n_candidates, study_state):
        return [
            {
                "candidate_id": "candidate-001",
                "format": "parameters",
                "spec": {"x": 0.5},
                "generator": {"method_id": self.definition["id"]},
            }
        ]

    def observe(self, observations):
        ...
```

`protocol: session` is reserved in the authoring schema, but the public Realm
runner rejects it. Live submit/wait/poll semantics are not implemented; OptPilot
does not execute a session config with degraded batch timing.

Command batch methods (`entrypoint.command`) execute on the retained slice as
one bounded subprocess per proposal exchange — see the Methods guide for the
exact request/response contract and its interpreter constraint. Command
*evaluators* remain an authoring target, not a current retained execution
capability.

## Study Config

A study config binds one environment config to one method config.

The block below is an annotated field template for the current retained study
shape.

```yaml
apiVersion: optpilot.io/v1
config: study

name: my-study
description: Compare one method against one environment.
tags: [local]

# Paths resolved from this study file.
environmentConfig: ../environments/my_environment/environment.yaml
methodConfig: ../methods/my_method/method.yaml

objective:
  metric: score
  direction: maximize    # enum: maximize | minimize
  aggregation: mean      # enum: mean | median | min | max | sum | last | weighted_mean
  secondaryMetrics: []

budget:
  maxTrials: 10
  maxFailures: 5

execution:
  parallelism: 2
  timeoutSeconds: 600

evidence:
  level: standard        # enum: minimal | standard | full

reproducibility:
  seed: 0
```

`weighted_mean` currently uses uniform weights because the study schema does
not yet expose an explicit weight vector. The evaluator and the Workbench
candidate-result projection apply that same rule; use `mean` when the more
specific label is unnecessary.

A study config does not describe domain inputs directly. If the selected
environment needs a scenario, dataset, query, simulator argument set, or
benchmark case list, put that in the environment config's `evaluator.settings`
or create another environment config variant. This keeps studies small: they
choose the environment, method, objective, budget, evidence policy, and seed.
For values that legitimately change per launch rather than per environment
variant, declare per-launch `inputs` (see below).

Containerized environment runtime. The image must already be built and is
named by fingerprint — a tag can resolve to different bytes later, so a record
naming one would not describe what ran. There is deliberately no way to
declare a build: building fetches software from the network, and what it
fetches can differ between builds. The image must be approved for study
execution (`optpilot image approve`) before anything runs in it.

Environment `runtime` fragment:

```yaml
runtime:
  sandbox: container
  container:
    image: ghcr.io/example/pkg@sha256:<64 hex digits>
    platform: linux/amd64
    network: disabled
    # Optional: raise resource limits for this component's containers.
    # OptPilot applies defaults (2 cpus, 4g memory, 512 processes); a raise
    # is shown when the image is approved, because raising one is part of
    # what is being agreed to. The wall-clock limit per evaluation stays in
    # evaluator.timeoutSeconds above.
    limits:
      cpus: "4"
      memory: 8g
      pids: 1024
```

Methods declare the same `runtime.container` shape. Each candidate evaluates
in a fresh container from this image; a method gets one container for the
whole run.

### Per-Launch Study Inputs

A study may declare an optional `inputs` map of typed per-launch values. Each
entry uses the same parameter definition as `candidate.parameters.schema` and
`settingsSchema` (`valueType`, `min`/`max`, `values`, `default`, `description`,
`unit`, `pattern`, and nested `items`/`properties`):

```yaml
inputs:
  problem:
    valueType: string
    description: Natural-language problem statement.
  budget_hint:
    valueType: int
    min: 1
    default: 60
```

Values are supplied at launch time. In Studio, a Run setup that declares
inputs shows a **Launch inputs** form on its detail page — one typed field
per declared input (dropdowns for categorical/bool values, number fields with
declared bounds, JSON for nested values) — and the entered values are bound
into the Run when you launch. On the CLI, use repeatable `--input key=value`
flags and/or one `--inputs-file` YAML mapping (`--input` wins on conflicting
keys). Flag values are parsed as YAML scalars, so `30` becomes an int, `true`
a bool, and a quoted value stays a string:

```bash
uv run optpilot run studies/my_study.yaml \
  --package-root . \
  --input problem="maximize throughput on line 3" \
  --input budget_hint=30
```

Validation is fail-closed and happens before anything is retained: a declared
input without a `default` must be supplied, undeclared keys are rejected, each
value must match its declared type and bounds, and supplying any launch inputs
to a study that declares none is an error. A study that declares `inputs` can
be launched without flags only when every input has a `default`.

Each rejection carries a stable machine code so a UI can act on it rather than
match error text. The codes are:

| Code | Meaning |
| --- | --- |
| `study_inputs_required` | A declared input has no `default` and no supplied value. Carries the missing names and their declarations, so a caller can collect the values and retry. |
| `study_inputs_invalid` | A supplied value failed its declared type, bounds, or membership, or an undeclared key was passed. |
| `study_inputs_undeclared` | Launch inputs were supplied to a study that declares none. |
| `study_inputs_reserved_key` | The environment's `evaluator.settings` or the method's `settings` already declares the reserved top-level `inputs` key. |

These are raised as `ValueError` subclasses carrying a `code` attribute, so
existing `except ValueError` handlers keep working unchanged. Studio surfaces
the code on every launch route — the CLI path, the Studio launch form, and the
Assistant — and uses `study_inputs_required` to ask for the missing values
instead of reporting a generic failure.

The resolved mapping is delivered to authored code under a reserved `inputs`
settings key: the evaluator reads it as `context["settings"]["inputs"]` and
the method as `settings["inputs"]`. Because the key is reserved, declaring
study `inputs` while the environment's `evaluator.settings` or the method's
`settings` already contain a top-level `inputs` key fails validation.

Per-launch inputs are problem payloads, not secrets. They are compiled into
the retained Run definition and appear in evidence like any other config, and
they participate in the definition's content digest, so the same study with
the same inputs reproduces the identical retained definition while different
inputs produce a different one. This is the opposite of the method runtime
`envFromHost` pattern, which exists to keep secret host values out of retained
evidence; never pass credentials through `inputs`.

### Environment Variants And Inputs

Environment configs are the reusable place to bind evaluator-specific inputs.
Use multiple environment YAML files when the same evaluator should be run with
different datasets, fidelity levels, simulator arguments, case suites, or
metric extraction settings.

For example, these are separate environment configs rather than different
OptPilot study concepts:

```text
catalog/my_package/environments/my_benchmark/
  environment_small.yaml
  environment_large.yaml
  evaluator.py
  assets/
```

Both files can point to the same evaluator but use different `evaluator.settings`.
The study then chooses which environment variant to run.

When an environment needs to evaluate several internal cases for one candidate,
implement that loop inside the evaluator or a custom adapter. The evaluator
still returns one OptPilot result with metric values, output files, records,
and event summary. If per-case details matter, write them as configured
`records` or `outputFiles` so they appear in evidence.

### Studio Study Builder Boundary

Study Builder accepts an exact environment and method from one immutable Realm
package revision or from non-conflicting package roots in different Realm
revisions in the same content store. Saving supplies the complete exact package
selections and component focus paths to one actor-bound Create Workspace
command. One distinct root is adopted directly. Multiple roots use one
whole-tree manifest union without copying file blobs: directories can merge, but
any file overlap, file/directory conflict, or case-fold collision rejects.

The command binds and checks recovery before source reads; multi-root assembly
uses a leased internal attempt, retained proof, atomic final workspace commit,
and bounded startup cleanup. Cross-store transfer remains a future explicit
import, never an implicit copy. Updates include the expected workspace revision.
Launch commits that checkout and validates and plans from a read-only projection
of the exact committed workspace revision. The checkout's absolute path is never
part of the study's durable identity.

## Launchable Interfaces

Reusable environments, methods, and resources can optionally declare a small
frontend or graphical helper with an `interface` block. Studio shows **Launch
Interface** for catalog entries that include this block.

When launched from the Catalog, Studio keeps component source read-only and
creates private launch-scoped runtime, dependency, and frontend storage. An
interface that declares `outputs: true` (or the action form described below)
also receives private control and output storage. Studio starts the command in
that transient runtime and opens the configured port in Preview. Use **Edit in
Workspace** when the source itself must be changed; launching an interface does
not create a durable workspace.

```yaml
interface:
  label: Demo UI
  description: Optional short note shown in Studio.
  outputs: true
  command: [python, -m, http.server, "5173", --bind, 0.0.0.0]
  cwd: .
  env:
    APP_MODE: demo
  runtime:
    sandbox: process
  grants:
    network: disabled
    envFromHost: []
    secretsFromHost: []
  resources:
    cpu: 1
    memoryMiB: 2048
    gpus: 0
  timeoutSeconds: 3600
  presentation:
    kind: web
    port: 5173
    extraPorts: [8000]
    readyPath: /
    readyTimeoutSeconds: 60
  accepts:
    selectionKinds: [candidate, trial]
    mediaTypes: []
```

Use `command` for the long-running frontend process and `presentation.port` for
the main browser port. The command should bind to `0.0.0.0` inside its runtime
so Studio can proxy it. `cwd` is a portable path relative to the component
source. Fixed nonsecret values belong in `env`; required user- or machine-selected
values such as model ids belong in `grants.envFromHost`; secret names belong in
`grants.secretsFromHost`; and network authority belongs in `grants.network`.
Studio resolves both host-variable lists only from **Local environment variables** and
never exposes their values in catalog summaries. Capacity and duration belong
in `resources` and `timeoutSeconds`. `presentation.extraPorts` exposes
additional local service ports, while its readiness fields define the bounded
HTTP probe.

Catalog launch currently accepts process-declared profiles and runs them
through Studio's managed authoring-runtime provider. It does not yet
independently enforce every declared per-profile network, resource, or
long-running timeout field; unsupported runtime/profile combinations fail
closed. Contextual Environment Preview uses the narrower retained-profile and
trusted-container checks described in [Studio UI](ui.md).

Output reporting is part of this same optional `interface` contract for
Environments, Methods, and Resources. Set `outputs: true` on a producing launch
profile; omit it for a view-only interface. This flag declares the capability,
not a list of paths. For each opted-in launch, Studio creates a different private
output area and control file and injects their locations as
`OPTPILOT_INTERFACE_OUTPUT_ROOT` and `OPTPILOT_INTERFACE_OUTPUTS_FILE`.
These launch-owned runtime paths are chosen and set by Studio. A Catalog author
does not configure them, and they are not relative paths inside the Environment,
Method, or Resource source tree.

To report a result, the interface writes the complete file or folder below
`OPTPILOT_INTERFACE_OUTPUT_ROOT`, stops modifying it, and appends one
newline-terminated JSON object to `OPTPILOT_INTERFACE_OUTPUTS_FILE`:

```json
{"schema_version":"optpilot.interface.output.v1","id":"simulator-001","label":"Generated simulator","kind":"tree","root":"output","path":"results/simulator-001"}
```

`kind` is `file` or `tree`, `root` is always `output`, and `path` is a canonical
relative path below the supplied root. This is a language-neutral environment
variable and JSONL boundary: interface code does not import or depend on an
OptPilot module. Studio validates and seals the relinquished bytes, then shows
a read-only output card. Repeating the same record is idempotent; changed bytes
must use a new `id`. A ready folder can then be saved as a managed editable
Workspace through the same one-selection Create Workspace command used by
Study Builder, without trusting a mutable app path. **Output missing?** is a
manual recovery path for a completed folder that the interface failed to
report; it uses the same capture lifecycle.

These two handles are supplied only to an opted-in interface launch. The
Environment evaluator and Method entrypoint used by a Study keep their ordinary
execution contracts and do not need to implement this protocol.

If a reported folder has a useful bounded command—for example, running a
generated simulator—declare that command in the registered interface profile:

```yaml
interface:
  outputs:
    actions:
      - id: run-simulation
        label: Run simulation
        command:
          - bash
          - -lc
          - 'exec "$OPTPILOT_PREPARED_RUNTIME_ROOT/python-venv/bin/python" -u run.py "$@"'
          - output-action
        cwd: .
        timeoutSeconds: 120
        acceptsArguments: true
        runtime: originating-interface
        showInOutputCard: false
  # command, presentation, runtime, grants, and other profile fields follow
```

The command belongs to the exact registered Catalog profile. A launch-local
broker request—or an output-card browser request when the action is shown—may
select its `id`, but cannot supply another command, image, environment, mount,
or network policy. Studio snapshots the selected
folder and starts a fresh network-disabled sibling container from the same
immutable image and read-only prepared runtime as the originating interface.
It does not execute generated code inside the live interface container and
does not pass that interface's environment variables or secrets. `cwd` is
relative to the selected folder, `timeoutSeconds` is bounded to one hour, and
extra arguments are accepted only when `acceptsArguments` is true.
A request may omit `timeout_seconds` to use the authored maximum, or supply a
positive value no greater than that maximum to finish sooner; it can never
extend the registered action's lifetime.

By default, Studio also shows each registered action on every compatible
output card. Set `showInOutputCard: false` when the launched interface already
provides the student-facing controls and uses the action only through its
launch-local broker. The action remains available to that interface, while the
generic output card stays focused on viewing and saving the result.

An interface that needs the same action for its own verification UI also
receives `OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT`. Its language-neutral request,
response, cancellation, and result files let the interface select a declared
action for a tree staged below the broker's dedicated `inputs/` directory
without importing OptPilot. These transient execution inputs are separate from
`OPTPILOT_INTERFACE_OUTPUT_ROOT`, so they never appear as generated output.
Studio still chooses the runtime and command. This is the same execution path used by the
output card, so automatic checks and user-triggered runs do not require two
different sandbox mechanisms.

When a component genuinely needs several independent commands or runtime
policies, use `launchProfiles`. Each entry is complete and named; profiles do
not inherit from the surrounding interface or from one another:

```yaml
interface:
  launchProfiles:
    - id: inspect
      label: Inspect Candidate
      command: [python, -m, viewer]
      presentation: {kind: web, port: 5173}
      accepts: {selectionKinds: [candidate]}
    - id: replay
      label: Replay Trial
      command: [python, -m, replay]
      grants: {network: disabled, envFromHost: [], secretsFromHost: []}
      presentation: {kind: web, port: 6173, readyPath: /health}
      accepts: {selectionKinds: [trial]}
```

`launchProfiles` is mutually exclusive with all top-level profile fields. A
profile `runtime` may contain only `sandbox`, `setup`, and typed container
image/build/platform/engine settings. It cannot carry environment variables,
host-secret names, network authority, filesystem grants, resources, or launch
timeouts.

Resources can declare the same block in an optional
`optpilot.resource.yaml` file at the resource root:

```yaml
apiVersion: optpilot.io/v1
config: resource
id: case-browser
name: Case Browser
purpose: viewer
tags: [frontend]

interface:
  command: [python, -m, http.server, "5173", --bind, 0.0.0.0]
  presentation: {kind: web, port: 5173}
  accepts: {selectionKinds: [workspace]}
```

The optional `purpose` field is bounded to `generator`, `viewer`, `template`,
or `reference`. Studio shows the matching human-readable Catalog badge. If the
field is absent, Studio shows **Resource**; it does not infer a role from tags,
paths, files, or commands.

## JSON Schema Files

The canonical schemas live in:

```text
src/optpilot/schemas/environment.schema.json
src/optpilot/schemas/method.schema.json
src/optpilot/schemas/resource.schema.json
src/optpilot/schemas/study.schema.json
src/optpilot/schemas/defs/
```

The Python validator loads these packaged files, so schema validation is the same in the CLI, UI, and tests.
