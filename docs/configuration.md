# Configuration Reference

OptPilot public configs are YAML files validated by JSON Schema. The schemas are packaged in `src/optpilot/schemas/` and are used by:

```bash
uv run optpilot validate path/to/study.yaml
```

This page is the field reference for the three public config roles: `environment`, `method`, and `study`.

For the conceptual model behind those roles, use [Concepts](concepts.md). For the runtime procedure after these files are loaded and validated, use [How A Run Works](how-it-works.md).

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
| `source` | Identifies where a value comes from for selector fields such as `metrics.source`, `records[].source`, and `instances.source`. |
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
      instances/
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
      instances/
      assets/
  methods/
    my_method/
      method.yaml
      method.py
      assets/
  studies/
    my_study.yaml
```

Environment and method configs are reusable. A single environment implementation can have multiple environment YAML files for different datasets, fidelity levels, metrics, or runtime settings. A single method implementation can have multiple method YAML files for different prompts, models, hyperparameters, or runtime settings. Study configs are concrete project runs.

## Path Resolution

| Field | Relative to |
| --- | --- |
| `study.environmentConfig`, `study.methodConfig`, `instances.paths`, `evidence.outputDir` | The study config file. |
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

## Environment Config

An environment config describes what can be evaluated and how the evaluation happens.

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
  # evaluate(candidate_runtime, instance, context) -> dict
  python: user_catalog.environments.my_environment.evaluator:evaluate

  # Alternative command evaluator.
  # command: [python, run_eval.py, "{candidate_json}", "{metrics_file}"]

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
  settings: {}

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

### Evaluator Return

Python evaluators normally return:

```python
def evaluate(candidate_runtime, instance, context):
    return {
        "status": "success",
        "metric_values": {"score": 0.9},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

For parameter candidates, `candidate_runtime` is the candidate parameter dictionary. For file candidates, it contains the trial workspace, candidate root, manifest path, and candidate file records.

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
| `{metrics_file}` | Expected metrics file path. |
| `{instance_file}` | JSON file containing the current instance. |
| `{trial_id}` | Trial id. |
| `{study_id}` | Study id. |
| `{instance_index}` | Zero-based instance index. |

### Candidate Formats

`parameters` candidates are JSON-like assignments validated against a schema:

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

`files` candidates are generated file sets. `trialWorkspace` seeds the workspace; the method returns references to generated files; the materializer copies those files into `candidate.materialize.root`.

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

```yaml
candidate:
  format: opaque
  opaque:
    family: my-custom-payload
```

## Method Config

A method config describes candidate proposal code and declares which environment contracts it accepts.

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

# Optional method runtime. Useful for command methods with their own dependencies.
runtime:
  sandbox: host          # enum: host | container
```

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

instances:
  source: files          # enum: none | inline | files | sampler
  paths:
    - ../environments/my_environment/instances/default.yaml

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

Instance alternatives:

```yaml
instances:
  source: inline
  value:
    target_x: 4.0
```

```yaml
instances:
  source: sampler
  sampler:
    python: user_catalog.environments.my_environment.instances:Sampler
    count: 5
    settings:
      base: 4.0
```

Containerized environment execution:

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

## JSON Schema Files

The canonical schemas live in:

```text
src/optpilot/schemas/environment.schema.json
src/optpilot/schemas/method.schema.json
src/optpilot/schemas/study.schema.json
src/optpilot/schemas/defs/
```

The Python validator loads these packaged files, so schema validation is the same in the CLI, UI, and tests.
