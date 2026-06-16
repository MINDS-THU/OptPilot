# Configuration Reference

OptPilot uses three public YAML config kinds:

- `EnvironmentConfig`: defines the environment, candidate contract, evaluator, metrics, and saved artifacts.
- `MethodConfig`: defines a method implementation and declares which environments it can target.
- `StudyConfig`: binds one environment to one method for a concrete run.

All public configs use:

```yaml
apiVersion: optpilot.io/v1
kind: EnvironmentConfig   # or MethodConfig, StudyConfig
```

## How To Read This Reference

Each field below is described using four categories:

| Category | Meaning |
| --- | --- |
| User value | You choose the value. It is used for labels, filtering, bookkeeping, or method-specific config. |
| Enum | Must be one of the listed allowed values. |
| Path | Must point to an existing file or directory when that feature runs. Most authoring paths are resolved from the config file that contains them; runtime container build and workdir paths are resolved from the compiled study base directory. |
| Implementation | Must point to code or a command with the expected interface. If this does not match your code, the run will fail. |

Component references use one of these formats:

| Format | Meaning |
| --- | --- |
| `python:package.module:ClassOrFunction` | Import a Python class or function through OptPilot's component registry. Used for methods, custom backends, custom metric extractors, and custom record extractors. |
| `package.module:function` | Import a Python evaluator function for `evaluate.type: python`. The `python:` prefix is not used for this field. |
| `builtin.name` | Use a built-in OptPilot component. Most users do not need to write these in public authoring configs. |

## Directory Layout

Use the same structure for built-in examples and your own catalog:

```text
examples/                  # curated examples shipped with the project
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

user_catalog/              # user-owned integrations
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
  studies/
    my_study.yaml
```

Environment and method directories own reusable implementation code plus one or more reusable config variants. Study configs are project-specific: each study chooses one environment config, one method config, instances, objective, and runtime policy.

Path resolution follows the config role:

| Field family | Relative to |
| --- | --- |
| `StudyConfig.environment`, `StudyConfig.method`, `instances.paths`, `evidence.outputDir` | The study config file. |
| `EnvironmentConfig.workspace.copy.from`, `evaluate.pythonPath`, `candidate.exposure.*`, interface adapter paths | The environment config file. |
| `MethodConfig.implementation.callable` | Python import path, resolved through normal Python import rules. |
| `MethodConfig.runtime.workdir`, `MethodConfig.runtime.build.context` | The compiled study base directory. |
| `StudyConfig.execution.config.build.context` | The compiled study base directory. |

## EnvironmentConfig

An environment config answers four questions:

1. What kind of candidate can be evaluated?
2. What files, schemas, prompts, or interfaces should be visible to methods?
3. What code actually evaluates a materialized candidate?
4. Where do metric values and saved evidence come from?

Minimal shape:

```yaml
apiVersion: optpilot.io/v1
kind: EnvironmentConfig
id: my-environment

evaluate:
  type: python
  callable: user_catalog.environments.my_environment.evaluator:evaluate

candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Parameters accepted by my evaluator.
  parameters:
    schema:
      x:
        type: float
        min: 0.0
        max: 1.0

metrics:
  source: return
  keys: [score]
```

### Top-Level Fields

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `apiVersion` | yes | string | Enum | Must be `optpilot.io/v1`. |
| `kind` | yes | string | Enum | Must be `EnvironmentConfig`. |
| `id` | yes | string | User value | Stable environment id shown in the UI and written to run evidence. |
| `description` | no | string | User value | Human-readable summary. |
| `tags` | no | list of strings | User value | UI/search metadata. |
| `evaluate` | yes | object | Implementation | How OptPilot runs the evaluator. |
| `candidate` | yes | object | Contract | Candidate type, artifact kind, schema/files, and method-visible context. |
| `workspace` | no | object | Path | Files copied into each trial workspace before evaluation. |
| `metrics` | yes | object | Contract | Where metric values come from and which metric keys exist. |
| `interfaces` | no | list of objects | Implementation | Optional environment capabilities exposed to methods. |
| `filesToSave` | no | list of strings | Path pattern | Files inside the trial workspace to attach as artifacts. |
| `recordsToExtract` | no | list of objects | Path/implementation | CSV, JSONL, SQLite, or custom records to convert to JSONL evidence streams. |

### `evaluate`

`evaluate` is environment-side implementation. It must match code you actually provide.

| Field | Required | Allowed values / type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `type` | yes | `python`, `command`, `custom` | Enum | Chooses evaluator adapter. |
| `callable` | for `python` | `module:function` | Implementation | Function imported and called as `evaluate(artifact_spec, instance, context)`. |
| `command` | for `command` | list of strings | Implementation | Subprocess command. Placeholders are formatted before launch. |
| `implementation` | for `custom` | `python:module:Class` | Implementation | Custom target adapter class implementing `evaluate(...)`. |
| `timeoutSeconds` | no | integer | User value | Per-instance evaluator timeout. Default is 600. |
| `pythonPath` | no | list of paths | Path | Extra import paths for `python` or `command` evaluator code. |
| `cwd` | no | path inside trial workspace | Path | Working directory for command evaluators. Default is the trial workspace. |
| `env` | no | object | User value | Literal environment variables for command evaluators. Values can use placeholders. |
| `config` | no | object | User value | Config passed to custom components. |

Python evaluator interface:

```python
def evaluate(artifact_spec, instance, context):
    return {
        "status": "success",
        "metric_values": {"score": 0.9},
        "constraint_results": {},
        "artifacts": [],
        "event_summary": {},
    }
```

`artifact_spec` is the materialized candidate runtime spec. For parameters, it is the parameter dict. For file candidates, it contains the trial `workspace`, `candidateRoot`, `manifestPath`, and candidate file records.

Command evaluator placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{python}` | Current Python executable. |
| `{workspace}` | Trial workspace path. |
| `{candidate_root}` | Root directory containing the materialized candidate. |
| `{candidate_file}` | Single candidate file path when unambiguous, otherwise candidate root. |
| `{candidate}` | Alias for `{candidate_file}`. |
| `{candidate_json}` | JSON file containing the candidate artifact payload. |
| `{metrics_file}` | Expected metrics file path. |
| `{instance_file}` | JSON file containing the current instance. |
| `{trial_id}` | Trial id. |
| `{study_id}` | Study id. |
| `{instance_index}` | Zero-based instance index. |

### `candidate`

`candidate` is the most important part of `EnvironmentConfig`. It defines what the method is allowed to produce.

Common fields:

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `type` | yes | `parameters`, `files`, `opaque` | Enum | Candidate family. |
| `artifactKind` | yes | string | Contract | More specific artifact label. Methods use this in compatibility checks. Examples: `parameter_spec`, `code_bundle`, `policy_weights`. |
| `description` | yes | string | Contract | Human explanation of what a valid candidate represents. |
| `tags` | no | list of strings | User value | Search/UI metadata. |
| `exposure` | no | object | Path/contract | Extra context visible to methods. |

#### Parameter Candidates

Use `candidate.type: parameters` when candidates are JSON-like parameter assignments.

```yaml
candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Continuous and categorical simulator controls.
  parameters:
    schema:
      rate:
        type: float
        min: 0.0
        max: 8.0
        default: 4.0
        description: Production rate.
      mode:
        type: categorical
        values: [balanced, aggressive, conservative]
    constraints:
      - id: valid-rate-mode
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

Parameter schema fields:

| Field | Required | Allowed values / type | Meaning |
| --- | --- | --- | --- |
| `type` | yes | `float`, `int`, `categorical`, `bool`, `string` | Parameter type. |
| `min`, `max` | no | number | Bounds for `float` and `int`. Bounds are enforced by validation. |
| `values` | for `categorical` | list | Allowed categorical values. |
| `default` | no | any JSON value | Suggested default. Methods may use it but OptPilot does not require them to. |
| `description` | no | string | Human-readable explanation. |

Constraint expression nodes:

| Node | Shape | Meaning |
| --- | --- | --- |
| Compare | `{compare: {left: ..., op: "<=", right: ...}}` | Compare two scalar expressions. Operators: `<`, `<=`, `>`, `>=`, `==`, `!=`, `in`, `not_in`. |
| All | `{all: [expr, expr]}` | Logical AND. |
| Any | `{any: [expr, expr]}` | Logical OR. |
| Not | `{not: expr}` | Logical NOT. |
| Parameter scalar | `{param: name}` | Reads a candidate parameter. |
| Constant scalar | `{const: value}` | Literal value. |
| Numeric scalar | `{op: add|sub|mul|div, args: [...]}` | Numeric expression. |

#### File Candidates

Use `candidate.type: files` when methods edit or generate files.

```yaml
candidate:
  type: files
  artifactKind: code_bundle
  description: Strategic-airlift simulator control logic.
  files:
    root: simulator
    source:
      type: workspace_copy
      root: simulator
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
        language: python
        role: control_logic
    required:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    allow:
      - devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
    deny: []
```

File candidate fields:

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `files.root` | yes | relative path | Contract | Directory inside the trial workspace where the candidate source tree lives. |
| `files.source` | yes | object | Contract | Describes where the initial file tree comes from. Current examples use `{type: workspace_copy, root: ...}`. |
| `files.editable` | yes | list of objects | Contract | Files that methods are expected to edit. Each entry must include `path`. |
| `files.required` | no | list of paths | Contract | Candidate artifact must include these paths. Defaults to editable paths. |
| `files.allow` | no | list of glob patterns | Contract | Candidate paths must match at least one pattern when non-empty. Defaults to required paths. |
| `files.deny` | no | list of glob patterns | Contract | Candidate paths matching these patterns are rejected. |

File candidate methods must return file artifact manifests with `contentRef` and `sha256`. Use `CodeArtifactStore` unless you have a reason to construct the manifest yourself.

#### Opaque Candidates

Use `candidate.type: opaque` when OptPilot should validate only a broad artifact family and let the evaluator interpret the payload.

```yaml
candidate:
  type: opaque
  artifactKind: policy_weights
  description: Serialized policy artifact interpreted by the evaluator.
  opaque:
    family: rl_policy
```

Opaque candidates are useful for binary models, external artifacts, or domain-specific payloads. The evaluator must know how to interpret the method's artifact `spec`.

### `candidate.exposure`

Exposure fields are method-visible context. They do not run by themselves.

| Field | Type | Category | Meaning |
| --- | --- | --- | --- |
| `instructions` | list of paths | Path | Prompt or instruction files visible through `candidate_context.exposure.instructions`. |
| `contextFiles` | list of paths | Path | Static files visible to methods. |
| `contextRecords` | list of objects | Path/contract | Static record descriptors visible to methods. |
| `contextArtifacts` | list of `{id, path}` | Path/contract | Named artifacts visible to methods. |

### `workspace`

`workspace.copy` controls what exists in each trial workspace before a candidate is materialized.

```yaml
workspace:
  copy:
    - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
      to: simulator
      role: source
```

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `copy[].from` | yes | path | Path | Host file or directory copied into each trial workspace. Resolved from the environment config file. |
| `copy[].to` | yes | relative path | Path | Destination inside the trial workspace. |
| `copy[].role` | no | string | User value | Descriptive label such as `source`, `fixture`, `data`, or `support`. |

OptPilot copies only declared entries. For a simulator, copy the minimum runnable simulator tree, fixtures, datasets, and support files needed by the evaluator. Do not copy method code here unless the evaluator truly needs it.

### `metrics`

`metrics` tells OptPilot how to obtain metric values after the evaluator runs.

| Field | Required | Allowed values / type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `source` | yes | `return`, `file`, `stdout`, `sqlite`, `custom` | Enum | Where metric payload is read from. |
| `keys` | recommended | list of strings | Contract | Declared metric names. `StudyConfig.objective.metric` must be in this list when provided. |
| `path` | for `file` | path inside workspace | Path | JSON metrics file. |
| `database` | for `sqlite` | path inside workspace | Path | SQLite database produced by evaluator. |
| `query` | for `sqlite` | SQL query | Implementation | Query must return one row with metric columns. |
| `implementation` | for `custom` | `python:module:function_or_class` | Implementation | Custom extractor. |
| `config` | no | object | User value | Passed to custom extractor. |

Metric payloads can be either:

```json
{"metric_values": {"score": 0.9}, "status": "success"}
```

or a flat object:

```json
{"score": 0.9, "latency": 3.2}
```

### `recordsToExtract`

Use `recordsToExtract` when the evaluator produces CSV, JSONL, or SQLite data that should be queryable later as evidence.

```yaml
recordsToExtract:
  - name: delivered_orders
    source: csv
    path: outputs/delivered_orders.csv
  - name: events
    source: sqlite_query
    path: sim.sqlite
    query: select time, event_type, payload from events
```

| Field | Required | Allowed values / type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `name` | yes | string | User value | Evidence stream name. |
| `source` | yes | `jsonl`, `csv`, `sqlite_table`, `sqlite_query`, `custom` | Enum | Record source type. |
| `path` | yes except `custom` | path inside workspace | Path | Source file or database produced by evaluator. |
| `table` | for `sqlite_table` | table name | Implementation | Table to export. |
| `query` | for `sqlite_query` | SQL query | Implementation | Query to export rows. |
| `implementation` | for `custom` | `python:module:function_or_class` | Implementation | Custom extractor returning rows. |
| `config` | no | object | User value | Passed to custom extractor. |

Extracted records are written as JSONL under the trial workspace and referenced from observation artifacts.

### `interfaces`

Interfaces describe optional environment capabilities that methods may require. They are not generic protocol enums; the `capability` string is a contract between an environment and compatible methods.

```yaml
interfaces:
  - id: read_results_db
    capability: optpilot.sqlite.query.v1
    description: Read-only query access to historical result tables.
    adapter:
      implementation: builtin.sqlite_query
      config:
        database: results.sqlite
        maxRows: 1000
```

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `id` | yes | string | User value | Local interface id. |
| `capability` | yes | string | Contract | Capability string. Methods list this in `compatibility.requiredCapabilities`. |
| `description` | no | string | User value | Human-readable explanation. |
| `adapter.implementation` | required if adapter exists and no command | `builtin.*` or `python:*` | Implementation | Adapter implementation. |
| `adapter.command` | required if adapter exists and no implementation | list of strings | Implementation | Command adapter. |
| `adapter.config` | no | object | User value/path | Adapter-specific config. |
| `schema.input`, `schema.output` | no | objects | Contract | Optional documentation of interface payloads. |

The built-in UI uses interfaces for compatibility display. Methods can inspect compiled interfaces through `candidate_context.interfaces` and `runtime_context.environment_interfaces`.

## MethodConfig

A method config answers three questions:

1. What code proposes candidates?
2. Which candidate contracts can it work with?
3. Does the method need a special runtime, such as a container?

Minimal shape:

```yaml
apiVersion: optpilot.io/v1
kind: MethodConfig
id: baseline-file-copy

implementation:
  type: python
  callable: python:examples.methods.baseline_file_copy.method:BaselineFileCopyMethod
  protocol: optpilot.method.batch.v1

config:
  batchSize: 1

compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext: [files.source, files.editable]
```

### Top-Level Fields

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `apiVersion` | yes | string | Enum | Must be `optpilot.io/v1`. |
| `kind` | yes | string | Enum | Must be `MethodConfig`. |
| `id` | yes | string | User value | Stable method id recorded in evidence. |
| `description` | no | string | User value | Human-readable summary. |
| `tags` | no | list of strings | User value | UI/search metadata. |
| `implementation` | yes | object | Implementation | Python class/function or external command. |
| `config` | no | object | User value | Passed to the method. OptPilot also injects `searchSpace` for parameter candidates if missing. |
| `compatibility` | yes | object | Contract | Declares which environments this method can target. |
| `runtime` | no | object | Runtime | Host or container runtime for command methods. |
| `resourceProfile` | no | object | Runtime | Per-trial resource preferences merged into execution defaults. |
| `sandboxSpec` | no | object | Runtime | Per-trial sandbox preferences merged into execution defaults. |

### `implementation`

| Field | Required | Allowed values / type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `type` | yes | `python`, `command` | Enum | How the method is invoked. |
| `callable` | for `python` | `python:module:ClassOrFunction` or `builtin.*` | Implementation | Python method class or callable. |
| `command` | for `command` | list of strings | Implementation | External command. |
| `protocol` | no | `optpilot.method.batch.v1`, `optpilot.method.session.v1` | Enum | Invocation protocol. Defaults to batch. Command methods currently support batch only. |

Python batch methods can implement one of:

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng):
        ...

    def propose(self, n_candidates, study_state, evidence_view=None):
        return [...]

    def observe(self, observations):
        ...
```

or lifecycle methods:

```python
def start(self, method_input): ...
def poll(self, handle): ...
def finalize(self, handle): ...
```

Python session methods use:

```python
class MySessionMethod:
    def __init__(self, definition, study_spec, rng):
        ...

    def run(self, session):
        session.event({"event": "started"})
        session.submit({...})
```

Session exposes `study_state`, `evidence`, `candidate_context`, `config`, `n_candidates`, `submit(...)`, and `event(...)`.

Command batch methods receive this request JSON:

```json
{
  "protocol": "optpilot.method.batch.v1",
  "request_id": "method-call-...",
  "n_candidates": 1,
  "study_state": {},
  "objective": {},
  "candidate_context": {},
  "evidence": {},
  "runtime_context": {
    "method_workspace": "/path/to/run/method_calls/method-call-..."
  },
  "config": {}
}
```

If `implementation.command` contains `{input_file}` or `{output_file}`, OptPilot writes the request to the input file and expects the response file. Otherwise it sends JSON on stdin and expects JSON on stdout.

Command response shape:

```json
{
  "candidates": [
    {
      "artifact_id": "candidate-001",
      "artifact_kind": "parameter_spec",
      "spec": {"x": 0.5}
    }
  ],
  "method_events": [
    {"event": "debug", "message": "generated one candidate"}
  ]
}
```

`artifacts` can be used instead of `candidates`.

### `compatibility`

Compatibility is how OptPilot decides whether a method can be used with an environment.

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `candidateTypes` | yes | non-empty list of `parameters`, `files`, `opaque` | Environment `candidate.type` must be included. |
| `artifactKinds` | no | list of strings | If present, environment `candidate.artifactKind` must be included. |
| `requiredContext` | no | list of context paths | Each path must exist in compiled candidate context. Examples: `parameters.schema`, `files.source`, `files.editable`, `exposure.instructions`. |
| `optionalContext` | no | list of context paths | Documentation for context the method can use when available. |
| `requiredCapabilities` | no | list of strings | Each string must match an environment interface `capability`. |

Compatibility should describe method assumptions, not a specific environment id. A method that works for any file-editing environment should say it supports `candidateTypes: [files]` and the required context it needs, rather than hard-coding the SA simulator.

### `runtime`

`runtime` applies to command methods. Python methods run in the current Python process.

Host/local runtime:

```yaml
runtime:
  type: host
  workdir: .
  env:
    MODE: default
  envFromHost: [OPENAI_API_KEY]
```

Container runtime:

```yaml
runtime:
  type: container
  image: my-method-image:latest
  containerExecutable: docker
  networkPolicy: disabled
  workdir: .
  envFromHost: [OPENAI_API_KEY]
  env:
    MODE: default
  readOnlyMounts: []
  writableMounts: []
  build:
    context: .
    dockerfile: Dockerfile.method
    tag: my-method-image:latest
```

| Field | Required | Allowed values / type | Meaning |
| --- | --- | --- | --- |
| `type` | no | `host`, `local`, `process`, `container` | Method command runtime. Defaults to `host`. |
| `workdir` or `project` | no | path | Working directory for the method command. |
| `env` or `environmentVariables` | no | object | Literal environment variables. |
| `envFromHost` | no | list of strings | Pass through named host variables when set. |
| `commandPrefix` | no | list of strings | Prefix inserted before the command. |
| `image` or `runtimeImage` | for container unless `build.tag` exists | string | Container image. |
| `containerExecutable` | no | string | Defaults to `docker`; can be `podman`. |
| `networkPolicy` | no | string | Passed through container network helper. Default is `disabled`. |
| `readOnlyMounts` | no | list of paths | Extra read-only host mounts. |
| `writableMounts` | no | list of paths | Extra writable host mounts. |
| `extraArgs` | no | list of strings | Extra container CLI args. |
| `build` | no | object | Build image before first method call. |

The container method runtime mounts the current project directory, the study config directory, the method working directory, and the per-call method workspace. The method workspace is writable and appears in the request as `runtime_context.method_workspace`.

### `resourceProfile` and `sandboxSpec`

These fields are merged into the per-trial resource and sandbox settings used by the execution backend.

`resourceProfile` fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `cpu` | integer | Requested CPUs. |
| `memoryGiB` | integer | Requested memory. |
| `gpu` | integer | Requested GPU count. |
| `gpuClass` | string | Optional GPU class label. |
| `timeoutSeconds` | integer | Trial timeout. |
| `diskGiB` | integer | Optional disk request. |
| `networkRequired` | boolean | Informational network requirement. |
| `runtimeImage` | string | Fallback container image for container execution. |

`sandboxSpec` fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `runtimeType` | string | Runtime type recorded in evidence. |
| `writableWorkspace` | path | Extra writable mount for container backend. |
| `readOnlyMounts` | list of paths | Extra read-only mounts for container backend. |
| `networkPolicy` | string | Container network policy. |
| `environmentVariables` | object | Environment variables for container trial worker. |
| `cleanupPolicy` | string | Cleanup policy recorded in evidence. |

## StudyConfig

A study config binds one environment to one method and chooses the run policy.

```yaml
apiVersion: optpilot.io/v1
kind: StudyConfig
name: sa-baseline
description: Evaluate the unmodified strategic-airlift simulator files.

environment: ../environments/strategic_airlift_devs/environment.yaml
method: ../methods/baseline_file_copy/method.yaml

objective:
  metric: service_score
  direction: maximize
  secondaryMetrics: [delivered_count, expired_count, mean_latency]

instances:
  source: files
  paths:
    - ../environments/strategic_airlift_devs/instances/sa_default.yaml

budget:
  maxTrials: 1

execution:
  backend: local
  parallelism: 1
  timeoutSeconds: 180

evidence:
  level: full

reproducibility:
  seed: 0
```

### Top-Level Fields

| Field | Required | Type | Category | Meaning |
| --- | --- | --- | --- | --- |
| `apiVersion` | yes | string | Enum | Must be `optpilot.io/v1`. |
| `kind` | yes | string | Enum | Must be `StudyConfig`. |
| `name` | yes | string | User value | Run directory name prefix and study label. |
| `description` | no | string | User value | Human-readable summary. |
| `tags` | no | list of strings | User value | UI/search metadata. |
| `environment` | yes | path, `{ref: path}`, or inline config | Path/contract | Environment to use. Relative paths resolve from the study file. |
| `method` | yes | path, `{ref: path}`, or inline config | Path/contract | Method to use. Relative paths resolve from the study file. |
| `objective` | yes | object | Contract | Primary metric and optimization direction. |
| `instances` | no | object | Contract/path | Evaluation instances. Defaults to one empty instance. |
| `budget` | yes | object | User value | Stop limits. |
| `execution` | no | object | Runtime | Evaluation backend and parallelism. |
| `evidence` | no | object | Runtime | Evidence level and output directory. |
| `reproducibility` | no | object | Runtime | Seed settings. |

### `objective`

| Field | Required | Allowed values / type | Meaning |
| --- | --- | --- | --- |
| `metric` | yes | string | Primary metric to optimize. Must be in `EnvironmentConfig.metrics.keys` when keys are declared. |
| `direction` | yes | `maximize`, `minimize` | Whether larger or smaller is better. |
| `secondaryMetrics` | no | list of strings | Recorded for context; not used to choose best trial. |
| `aggregation` | no | `mean`, `median`, `min`, `max`, `sum`, `last`, `weighted_mean` | How to combine metrics across multiple instances. Defaults to `mean`. |
| `aggregation.weights` | for weighted mean | list, scalar, or dict | Weights by position, by metric, or `*` fallback. |

### `instances`

| Source | Required fields | Meaning |
| --- | --- | --- |
| `none` | none | Use one empty instance `{}`. |
| `inline` | `value` object | Use the inline object as the only instance. |
| `files` | `paths` list | Load one or more YAML/JSON instance files. |
| `sampler` | `implementation`, `config`, `count` | Sample instances. Default implementation is `builtin.parameter_sampler`. Custom sampler must be `python:module:object`. |

### `budget`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `maxTrials` | yes | positive integer | Maximum evaluated trials. |
| `maxWallClockSeconds` | no | integer | Stop after this much wall time. |
| `maxFailures` | no | integer | Stop after this many failed or timed-out trials. |

### `execution`

`execution` controls where environment evaluation runs. It does not control method command containers; those are configured in `MethodConfig.runtime`.

| Field | Required | Allowed values / type | Meaning |
| --- | --- | --- | --- |
| `backend` | no | `local`, `local_subprocess`, `container`, `custom` | Evaluation backend. Defaults to `local`. |
| `parallelism` | no | integer | Number of candidate trials evaluated concurrently. Defaults to 1. |
| `timeoutSeconds` | no | integer | Default per-trial timeout. Defaults to 600. |
| `retry.maxRetries` | no | integer | Retry policy recorded in the compiled spec. |
| `implementation` | for `custom` | `builtin.*` or `python:*` | Custom backend component. |
| `config` | no | object | Backend-specific config. |

Container backend example:

```yaml
execution:
  backend: container
  parallelism: 2
  timeoutSeconds: 300
  config:
    image: python:3.11
    containerExecutable: docker
    pythonExecutable: python
    build:
      context: .
      dockerfile: Dockerfile.environment
      tag: my-environment-image:latest
```

Container backend config fields:

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `image` | required unless `build.tag` exists | string | Worker image containing Python, OptPilot, and environment dependencies. |
| `containerExecutable` | no | string | Defaults to `docker`; can be `podman`. |
| `pythonExecutable` | no | string | Python executable inside the image. |
| `extraArgs` | no | list of strings | Extra container CLI args. |
| `build` | no | object | Build image before trial workers launch. |

Build fields used by method runtime and container execution:

| Field | Type | Meaning |
| --- | --- | --- |
| `context` | path | Build context. |
| `dockerfile` | path | Dockerfile path. |
| `tag` | string | Image tag to build and run. |
| `target` | string | Build target. |
| `platform` | string | Build platform. |
| `pull` | boolean | Add pull behavior when supported by helper. |
| `noCache` | boolean | Disable build cache when supported by helper. |
| `args` | object | Build args. |
| `extraArgs` | list | Extra build CLI args. |
| `timeoutSeconds` | integer | Build timeout. |

### `evidence`

| Field | Required | Allowed values / type | Meaning |
| --- | --- | --- | --- |
| `level` | no | `minimal`, `standard`, `full` | Controls evidence capture and retention policy. Defaults to `standard`. |
| `outputDir` | no | path | Root directory for run outputs. Defaults to a sibling `runs/` directory near the study config. |

### `reproducibility`

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `seed` | no | integer | Global seed. Per-trial seeds are derived deterministically. |

## Compatibility Examples

A parameter search method:

```yaml
compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext: [parameters.schema]
```

A general file-editing method:

```yaml
compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext: [files.source, files.editable]
  optionalContext: [exposure.instructions, exposure.contextFiles]
```

A method that requires a simulator API exposed by the environment:

```yaml
compatibility:
  candidateTypes: [opaque]
  requiredCapabilities: [my_lab.simulator.rollout.v1]
```

The capability string is not a built-in enum. It is an agreement between your environment interface and your method.

## What Gets Resolved At Runtime

| Config field | Runtime effect |
| --- | --- |
| `StudyConfig.environment` | Loads the environment YAML. |
| `StudyConfig.method` | Loads the method YAML. |
| `EnvironmentConfig.workspace.copy` | Copies files/directories into each trial workspace. |
| `EnvironmentConfig.candidate` | Builds method-visible `candidate_context`, validation rules, and materialization plan. |
| `EnvironmentConfig.evaluate` | Creates the environment adapter that evaluates candidates. |
| `EnvironmentConfig.metrics` | Extracts metric values from return value, file, stdout, SQLite, or custom extractor. |
| `MethodConfig.implementation` | Creates the method runtime and calls Python code or command code. |
| `MethodConfig.compatibility` | Validates that the method can target the environment before the study starts. |
| `MethodConfig.runtime` | Runs command methods on host or in a method container. |
| `StudyConfig.execution` | Runs environment evaluation locally, in subprocesses, in containers, or through a custom backend. |
| `StudyConfig.evidence` | Chooses where and how much run evidence is saved. |

See [How A Run Works](how-it-works.md) for the step-by-step runtime flow.
