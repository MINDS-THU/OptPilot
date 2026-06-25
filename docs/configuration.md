# Configuration Reference

<!-- This page is a field reference. Start with [Getting Started](getting-started.md) if you have not run the job-shop baseline yet. -->

OptPilot public configs are YAML files validated by JSON Schema. The schemas are packaged in `src/optpilot/schemas/` and are used by:

```bash
uv run optpilot validate path/to/study.yaml
```

It covers the three public config roles: `environment`, `method`, and `study`.

For the conceptual model behind those roles, use [Concepts](concepts.md). For the runtime procedure after these files are loaded and validated, use [How A Run Works](how-it-works.md).

If you are deciding how to connect a new method to a new environment, read [Candidate Contracts](candidate-contracts.md) before using this field reference. The reference tells you which fields exist; the contract guide explains how the fields fit together.

Every public config starts with:

```yaml
apiVersion: optpilot.io/v1
config: environment   # enum: environment | method | study
```

`config` selects the schema. OptPilot also writes an internal compiled run spec into run directories; users do not author that file directly.

## Naming Rules

The public config uses concrete names for concrete jobs.

| Name | Use |
| --- | --- |
| `config` | Identifies the config file role: `environment`, `method`, or `study`. |
| `format` | Identifies candidate representation: `parameters`, `files`, or `opaque`. |
| `valueType` | Identifies one parameter value shape inside `candidate.parameters.schema`. |
| `python`, `command`, `adapter` | Identify how evaluator or method code is invoked without a separate discriminator field. |
| `source` | Identifies where a value comes from for selector fields such as `metrics.source` and `records[].source`. |
| `backend` | Identifies how trials are scheduled. |

Candidate compatibility is based on the candidate format plus required contract paths and capabilities. OptPilot does not require a separate candidate domain label.

## Reference Types

| Type | Meaning |
| --- | --- |
| Free string | You choose the value. Used for ids, names, labels, descriptions, tags, or method-specific settings. |
| Enum | Must be one of the listed values. JSON Schema validates it. |
| Path | A filesystem path. Relative paths are resolved from the YAML file that contains the path unless noted otherwise. |
| Python import | `package.module:function` or `package.module:Class`. |
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

This reference covers three authored config roles:

- `config: environment` describes what can be evaluated and how
- `config: method` describes how candidates are proposed and what contracts the method accepts
- `config: study` binds one environment to one method and chooses one run policy

## Directory Layout

The same organization is used for built-in examples and user-owned code:

```text
examples/
  environments/
    strategic_airlift_devs/
      environment.yaml
      evaluator.py
      assets/
      prompts/
  methods/
    baseline_file_copy/
      method.yaml
      method.py
  studies/
    sa_baseline.yaml

user_catalog/
  environments/
    my_environment/
      environment.yaml
      evaluator.py
      assets/
  methods/
    my_method/
      method.yaml
      method.py
      assets/
  resources/
    my_reference_project/
      README.md
```

Environment and method configs are reusable. A single environment
implementation can have multiple environment YAML files for different datasets,
fidelity levels, metrics, or runtime settings. A single method implementation
can have multiple method YAML files for different prompts, models,
hyperparameters, or runtime settings.

Study configs are concrete project runs. Keep them with the project or
workspace where they are drafted and launched rather than registering them as
catalog entries.

## Path Resolution

| Field | Relative to |
| --- | --- |
| `study.environmentConfig`, `study.methodConfig`, `evidence.outputDir` | The study config file. |
| `environment.evaluator.pythonPath`, `environment.trialWorkspace[].from`, `environment.methodContext.instructions`, `environment.methodContext.references[].path` | The environment config file. |
| `method.entrypoint.pythonPath`, `method.runtime.container.build.context` | The method config file. |
| `study.execution.runtime.container.build.context` | The study config file. |
| `environment.evaluator.cwd` | The trial workspace created for the candidate evaluation. |
| `environment.outputFiles[].path` and string `environment.outputFiles` entries | The trial workspace after evaluator execution. |
| `environment.records[].path` for file-backed records | The trial workspace after evaluator execution. |

Python import strings are resolved by normal Python import rules after any declared `pythonPath` entries are prepended.

Most setup paths resolve from the config file that owns the field. Runtime
paths that describe what the evaluator should read or produce resolve inside
the trial workspace, because that workspace is the directory OptPilot prepares
and evaluates for each candidate.

Example:

- `examples/studies/job_shop_rule_parameters_baseline.yaml` resolves `environmentConfig` relative to the study file
- `examples/environments/job_shop_scheduling/environment_rule_parameters.yaml` resolves evaluator `pythonPath`, `trialWorkspace`, and `methodContext` paths relative to the environment file
- `examples/methods/fixed_rule_parameters/method.yaml` resolves any `pythonPath` entries relative to the method file

## Environment Config

An environment config describes what can be evaluated and how the evaluation happens.

The block below is an annotated field template, not a runnable example file. It intentionally shows alternatives such as Python, command, and adapter evaluators in one place. For complete runnable configs, see [Getting Started](getting-started.md) and the files under `examples/`.

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
  python: user_catalog.environments.my_environment.evaluator:evaluate

  # Alternative command evaluator.
  # command: [python, run_eval.py, "{candidate_json}", "{settings_file}", "{metrics_file}"]

  # Alternative custom adapter class.
  # Use only when a direct Python function or command is not enough.
  # adapter: user_catalog.environments.my_environment.adapter:MyAdapter

  # Optional evaluator controls.
  timeoutSeconds: 600
  pythonPath: [.]
  # Runtime working directory inside the trial workspace, not relative to this YAML file.
  cwd: .
  env:
    MY_ENV: value
  # Free object passed to the evaluator in context["settings"].
  # Use it for environment-owned scenario, dataset, query, case-list, or simulator arguments.
  settings:
    target_x: 4.0

# Optional runtime for the environment evaluator.
runtime:
  sandbox: host          # enum: host | container
  # network: disabled    # enum: enabled | disabled
  # container:
  #   image: python:3.11-slim
  #   executable: docker

# Optional files copied into each trial workspace before evaluation.
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

# Optional evidence streams extracted after evaluation.
records:
  - name: events
    source: jsonl        # enum: jsonl | csv | sqlite_table | sqlite_query | custom
    path: events.jsonl

# Optional files to save from each trial workspace after evaluation.
outputFiles:
  - metrics.json
  - path: logs/*.txt
    name: logs
    required: false

# Optional capability ids exposed by this environment.
capabilities:
  - id: historical_db_query
    description: Read-only access to a historical SQLite database.
```

For a first runnable environment config, the minimum important fields are usually `id`, `evaluator`, `candidate`, and `metrics`.

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

For parameter candidates, `candidate_runtime` is the candidate parameter dictionary. For file candidates, it contains the trial workspace, candidate root, manifest path, and candidate file records.

`evaluator.settings` is intentionally a plain object. OptPilot does not define
domain-specific concepts such as scenarios, datasets, queries, or benchmark
cases. If an environment needs those inputs, put them in
`evaluator.settings` and let the evaluator or custom adapter interpret them.
For example:

```yaml
evaluator:
  python: user_catalog.environments.my_environment.evaluator:evaluate
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
  adapter: user_catalog.environments.my_environment.adapter:BenchmarkAdapter
  settings:
    cases:
      - id: small
        path: assets/cases/small.yaml
      - id: medium
        path: assets/cases/medium.yaml
```

The adapter can loop over `cases`, call domain code, aggregate metrics, and
return one OptPilot evaluator result. If a method must read the same case files
before proposing a candidate, expose those files through `methodContext.references`
or method `settings`. That keeps case handling owned by the environment/method
integration instead of making it a built-in OptPilot abstraction.

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

`files` candidates are generated file sets. `trialWorkspace` seeds the workspace; the method returns references to generated files; the materializer copies those files into `candidate.materialize.root`.

Environment field fragments:

```yaml
trialWorkspace:
  - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
    to: simulator

candidate:
  format: files
  description: Editable simulator control logic.
  materialize:
    root: simulator
  files:
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    required:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    allow:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
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
  python: user_catalog.methods.my_method.method:MyMethod
  protocol: batch        # enum: batch | session
  pythonPath: [.]

  # Alternative command entrypoint. Command methods currently use batch protocol.
  # command: [python, method.py, "{input_file}", "{output_file}"]

# Free object passed to the method as method settings.
settings:
  batchSize: 4

# Required compatibility declaration.
accepts:
  formats: [parameters]  # list of parameters | files | opaque
  requires:
    context:
      - candidate.parameters.schema
    capabilities: []

# Optional: use only when this method submits a known candidate shape.
produces:
  format: parameters
  parameters:
    schema:
      x:
        valueType: float

# Optional method runtime. Useful for command methods with their own dependencies.
runtime:
  sandbox: host          # enum: host | container
```

For a first runnable method config, the minimum important fields are usually `id`, `entrypoint`, and `accepts`. Add `produces` when the method always submits a known candidate shape; OptPilot compares it structurally with the environment candidate contract. Omit `produces` for schema-general methods that read `candidate.parameters.schema` and generate candidates for whatever parameter schema the environment declares.

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

Session Python methods implement `run(session)` and actively submit candidates through the session object. Batch and session protocols have the same candidate/evidence capability; the difference is passive request/response versus active tool-like interaction.

Command methods receive a JSON request on stdin unless `{input_file}` is present. If `{output_file}` is present, they write the response there; otherwise OptPilot reads stdout.

## Study Config

A study config binds one environment config to one method config.

The block below is an annotated field template. [Getting Started](getting-started.md) shows a complete runnable study config from `examples/studies/`.

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
  backend: local         # enum: local | local_subprocess
  parallelism: 2
  timeoutSeconds: 600

evidence:
  level: standard        # enum: minimal | standard | full
  outputFileStorage: reference # enum: reference | copy

reproducibility:
  seed: 0
```

A study config does not describe domain inputs directly. If the selected
environment needs a scenario, dataset, query, simulator argument set, or
benchmark case list, put that in the environment config's `evaluator.settings`
or create another environment config variant. This keeps studies small: they
choose the environment, method, objective, budget, runtime, evidence policy,
and seed.

Containerized environment execution:

Study `execution.runtime` fragment:

```yaml
execution:
  backend: local
  runtime:
    sandbox: container
    network: disabled
    container:
      image: python:3.11-slim
      executable: docker
      build:
        context: .
        dockerfile: Dockerfile.environment
        tag: my-env:latest
```

### Environment Variants And Inputs

Environment configs are the reusable place to bind evaluator-specific inputs.
Use multiple environment YAML files when the same evaluator should be run with
different datasets, fidelity levels, simulator arguments, case suites, or
metric extraction settings.

For example, these are separate environment configs rather than different
OptPilot study concepts:

```text
user_catalog/environments/my_benchmark/
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

## Launchable Interfaces

Reusable environments, methods, and resources can optionally declare a small
frontend or graphical helper with an `interface` block. Studio shows **Launch
Interface** for catalog entries that include this block.

When launched, Studio copies the catalog folder into an editable draft
workspace, starts the command inside that workspace's container runtime, and
opens the configured port in the Preview panel.

```yaml
interface:
  label: Demo UI
  description: Optional short note shown in Studio.
  command: [python, -m, http.server, "5173", --bind, 0.0.0.0]
  port: 5173
  cwd: .
  env:
    APP_MODE: demo
  extraPorts: [8000]
  readyPath: /
  readyTimeoutSeconds: 60
```

Use `command` for the long-running frontend process and `port` for the main
browser port. The command should bind to `0.0.0.0` inside the workspace runtime
so Studio can proxy it. `cwd` is relative to the copied workspace root.
`extraPorts` is only needed when the frontend calls another local backend port
through the same Preview session. `readyPath` is the HTTP path Studio probes
before showing the preview, and `readyTimeoutSeconds` controls how long launch
waits for first-time installs or builds.

Resources can declare the same block in an optional
`optpilot.resource.yaml` file at the resource root:

```yaml
apiVersion: optpilot.io/v1
config: resource
id: devs-display-generator
name: DEVS Display Generator
tags: [simulation, frontend]

interface:
  command: [python, -m, http.server, "5173", --bind, 0.0.0.0]
  port: 5173
```

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
