# OptPilot Interface and Config Design

This document defines a self-contained target interface for OptPilot
configuration. It focuses on clean boundaries between environments, methods,
studies, and runtime candidates.

The design goal is simple:

```text
EnvironmentConfig declares what can be evaluated.
MethodConfig declares how candidates are generated.
StudyConfig binds one environment to one method under objective, budget, data,
and execution policy.
```

Methods should be reusable across any compatible environment. Environments
should not need to know which optimization method will be used. Studies should
own experiment choices such as objective, budget, instances, and narrowing of a
declared search surface.

## 1. Design Principles

1. Keep environment-specific facts in `EnvironmentConfig`.
2. Keep method-specific strategy and model settings in `MethodConfig`.
3. Keep experiment-specific choices in `StudyConfig`.
4. Make method/environment compatibility checkable before launch.
5. Prefer explicit contracts over hidden conventions.
6. Use open namespaced strings for project-owned capabilities instead of closed
   enums when OptPilot cannot know all future protocols.
7. Use structured config for durable interfaces; use `config` only for
   implementation-owned details.
8. Do not put simulator-specific event semantics, visualization semantics, or
   domain-specific state machines into OptPilot core.

## 2. Field Ownership Model

Every field should be understood by who owns it and how it is validated.

| Field kind | User freedom | Validation |
| --- | --- | --- |
| Local labels | User chooses freely. | Must be unique in the local config where uniqueness matters. |
| Compatibility contracts | User or package author defines them, but methods and environments must agree exactly. | Checked by the compiler before launch. |
| Import targets | User chooses code, but it must exist. | Must resolve to importable Python code. |
| Commands | User chooses command argv, but it must be runnable. | Validated at launch or execution time. |
| Paths | User chooses files/directories. | Resolved relative to the owning config unless explicitly documented otherwise. |
| Implementation config | User provides structured values for an implementation. | OptPilot preserves it; the implementation validates it. |
| UI metadata | User writes human-facing text. | Used for catalog/search/display, not for execution. |

Examples:

- `interfaces[].id` is a local label.
- `candidate.artifactKind` is a compatibility contract.
- `evaluate.callable` is an import target.
- `workspace.copy[].from` is a path.
- `engine.config` is implementation config.
- `description` and `tags` are UI metadata.

## 3. Config Kinds

OptPilot has three user-authored config kinds.

```text
EnvironmentConfig
MethodConfig
StudyConfig
```

The compiler turns them into a normalized runtime `StudySpec`. Users should
normally not hand-write `StudySpec`.

## 4. EnvironmentConfig

`EnvironmentConfig` describes an evaluation target and the candidate contract it
accepts.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: string
description: string
tags: [string]

evaluate: EvaluateConfig
candidate: CandidateContract
workspace: WorkspaceConfig
interfaces: [EnvironmentInterfaceConfig]
metrics: MetricsConfig
filesToSave: [string]
recordsToExtract: [RecordExtractionConfig]
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | yes | OptPilot schema | Config schema version. |
| `kind` | yes | OptPilot schema | Must be `EnvironmentConfig`. |
| `id` | yes | Environment author | Stable catalog id. |
| `description` | yes | Environment author | Human-readable explanation of what is evaluated. |
| `tags` | no | Environment author | Catalog labels. |
| `evaluate` | yes | Environment author | How OptPilot runs final candidate evaluation. |
| `candidate` | yes | Environment author | Candidate contract accepted by the environment. |
| `workspace` | no | Environment author | Files/directories prepared for each trial. |
| `interfaces` | no | Environment or adapter author | Optional capabilities beyond black-box evaluation. |
| `metrics` | yes | Environment author | Official metric extraction. |
| `filesToSave` | no | Environment author | Extra files to preserve as evidence. |
| `recordsToExtract` | no | Environment author | Structured records extracted from outputs. |

### 4.1 EvaluateConfig

`evaluate` is the black-box evaluation interface. Every environment should
provide it, even if it also exposes richer interfaces for training or
interactive methods.

```yaml
evaluate:
  type: python | command | custom
  callable: module:function
  command: [string]
  cwd: string
  env:
    NAME: value
  timeoutSeconds: integer
  implementation: python:module:Class
  config: object
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `type` | yes | OptPilot schema | Evaluation adapter family. |
| `callable` | for `type: python` | Environment author | Import target for `evaluate(candidate, instance, context)`. |
| `command` | for `type: command` | Environment author | Argv list, not a shell string. |
| `cwd` | no | Environment author | Working directory, relative to trial workspace. |
| `env` | no | Environment author | Environment variable overrides. |
| `timeoutSeconds` | no | Environment author | Per-evaluation timeout. |
| `implementation` | for `type: custom` | Environment author | Custom OptPilot target adapter. |
| `config` | no | Adapter author | Structured config passed to the evaluator adapter. |

Rules:

- `python` evaluation calls an importable Python function.
- `command` evaluation runs an argv list in a prepared workspace.
- `custom` evaluation delegates to an adapter that owns its `config`.
- `evaluate` produces official observations and metrics. Training-time or
  query-time interaction belongs in `interfaces`, not in `evaluate`.

### 4.2 WorkspaceConfig

`workspace` describes files OptPilot prepares for a trial before materializing a
candidate and running evaluation.

```yaml
workspace:
  copy:
    - from: path
      to: path
      role: source | fixture | data | support
  readonly:
    - path
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `copy` | no | Environment author | Files/directories copied into each trial workspace. |
| `copy[].from` | yes | Environment author | Source path, resolved relative to this EnvironmentConfig. |
| `copy[].to` | yes | Environment author | Destination path inside the trial workspace. |
| `copy[].role` | no | Environment author | Metadata hint for UI and method context. |
| `readonly` | no | Environment author | Workspace paths candidates should not modify. |

Workspace files are evaluation assets. They are not automatically method-visible
context. Method-visible context is declared under `candidate.exposure`.

### 4.3 EnvironmentInterfaceConfig

Most methods only need black-box `evaluate`. Some methods need additional
interaction capabilities, such as rollout APIs, simulator query APIs, or dataset
stream APIs. These are declared as open capabilities.

```yaml
interfaces:
  - id: training_api
    capability: optpilot.rollout.v1
    description: string
    adapter:
      implementation: python:module:Class
      command: [string]
      config: object
    schema:
      input: object
      output: object
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `id` | yes | Environment author | Local adapter label, unique within this environment. |
| `capability` | yes | Adapter/method ecosystem | Open namespaced compatibility string. |
| `description` | yes | Environment author | What this interface lets a method do. |
| `adapter.implementation` | one adapter entrypoint required | Environment author | Importable Python adapter. |
| `adapter.command` | one adapter entrypoint required | Environment author | Runnable command adapter. |
| `adapter.config` | no | Adapter author | Adapter-owned config. |
| `schema.input` | no | Capability/adapter author | Optional request schema. |
| `schema.output` | no | Capability/adapter author | Optional response schema. |

Rules:

- `id` is local. Methods should not require a specific `id`.
- `capability` is the compatibility contract. Methods require capabilities by
  this string.
- Capability strings are not a closed OptPilot enum. Project-owned values such
  as `my_lab.simulator_query.v1` are valid.
- The adapter must implement the API implied by the capability.

## 5. CandidateContract

`CandidateContract` is the most important boundary in the design. It describes
what a method may produce and what the environment can evaluate.

```yaml
candidate:
  type: parameters | files | opaque
  artifactKind: string
  description: string
  tags: [string]

  parameters: ParameterCandidateSpec
  files: FileCandidateSpec
  opaque: OpaqueCandidateSpec

  exposure: MethodExposureSpec
  validation: CandidateValidationSpec
  materialization: CandidateMaterializationSpec
```

Common fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `type` | yes | OptPilot schema | Structural candidate family. |
| `artifactKind` | yes | Environment/method ecosystem | Machine-readable artifact compatibility kind. |
| `description` | yes | Environment author | Human explanation of the candidate surface. |
| `tags` | no | Environment author | Catalog labels. |
| `parameters` | for `type: parameters` | Environment author | Parameter search-space contract. |
| `files` | for `type: files` | Environment author | File candidate contract. |
| `opaque` | for `type: opaque` | Environment author | Opaque artifact contract. |
| `exposure` | no | Environment author | Method-visible context. |
| `validation` | no | Environment author | Candidate validation policy. |
| `materialization` | no | Environment author | How candidates become evaluator inputs. |

Rules:

- Exactly one type-specific body must be present.
- `type` answers “what is the structural representation?”
- `artifactKind` answers “what semantic artifact is this?”
- `description` is required because catalog users and UI workflows need a clear
  human explanation.
- Type-specific details belong inside the type-specific body, not at the
  `candidate` top level.

Examples of `artifactKind`:

| `type` | Possible `artifactKind` values |
| --- | --- |
| `parameters` | `parameter_spec`, `simulator_knobs`, `policy_hyperparameters` |
| `files` | `files`, `code_bundle`, `prompt_bundle`, `config_bundle` |
| `opaque` | `opaque`, `policy_checkpoint`, `container_image`, `solver_binary` |

### 5.1 ParameterCandidateSpec

Use `parameters` when a candidate is structured parameter data.

```yaml
candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Parameters accepted by the evaluator.
  parameters:
    schema:
      x:
        type: float
        min: 0.0
        max: 8.0
        default: 4.0
        description: Main production-rate control.
      mode:
        type: categorical
        values: [balanced, aggressive, conservative]
    constraints:
      - id: x_within_range
        description: Keep x inside the valid simulator range.
        severity: hard
        expr:
          compare:
            op: <=
            left: {param: x}
            right: {const: 8.0}
    encoding:
      vectorizable: true
      order: [x, mode]
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `schema` | yes | Environment author | Parameter names and domains. |
| `constraints` | no | Environment/method ecosystem | Cross-parameter constraints for methods that support them. |
| `encoding` | no | Environment/method ecosystem | Vector/chromosome hints for methods that support them. |

Constraint fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `id` | yes | Environment author | Stable local constraint id. |
| `description` | yes | Environment author | Human explanation. |
| `severity` | no | Environment author | `hard` or `soft`; default `hard`. |
| `expr` | yes | Environment author | Structured boolean expression. |

Constraint expressions use a small AST, not free-form source code.

Boolean expression nodes:

| Node | Shape | Meaning |
| --- | --- | --- |
| Compare | `compare: {op, left, right}` | Compare two scalar expressions. |
| All | `all: [expr, ...]` | Logical AND. |
| Any | `any: [expr, ...]` | Logical OR. |
| Not | `not: expr` | Logical NOT. |

Compare operators:

```text
<  <=  >  >=  ==  !=  in  not_in
```

Scalar expression nodes:

| Node | Shape | Meaning |
| --- | --- | --- |
| Parameter reference | `{param: x}` | Candidate parameter value. |
| Constant | `{const: 8.0}` | Literal value. |
| Numeric operation | `{op: add | sub | mul | div, args: [scalar, ...]}` | Numeric expression. |

Examples:

```yaml
constraints:
  - id: x_le_twice_y
    description: x cannot exceed twice y.
    expr:
      compare:
        op: <=
        left: {param: x}
        right:
          op: mul
          args:
            - {const: 2}
            - {param: y}
```

```yaml
constraints:
  - id: aggressive_requires_high_x
    description: Aggressive mode requires x to be at least 4.
    expr:
      any:
        - compare:
            op: "!="
            left: {param: mode}
            right: {const: aggressive}
        - compare:
            op: ">="
            left: {param: x}
            right: {const: 4.0}
```

Validation rules:

- Every `{param: ...}` must reference a declared parameter.
- Compare operators must be from the allowed set.
- Numeric operations may only use numeric parameter types or numeric constants.
- `in` and `not_in` require the right side to be a list constant.
- A method may reject constraints it cannot use, but it should do so explicitly
  through compatibility or validation rather than silently ignoring hard
  constraints.

Parameter schema fields:

| Field | Applies to | Meaning |
| --- | --- | --- |
| `type` | all | `float`, `int`, `categorical`, `bool`, or `string`. |
| `min` | numeric | Inclusive lower bound. |
| `max` | numeric | Inclusive upper bound. |
| `values` | categorical | Allowed values. |
| `default` | all | Optional default. |
| `description` | all | Human explanation. |
| `scale` | numeric | `linear`, `log`, or `discrete`. |

Method fit:

- Random search needs `parameters.schema`.
- Bayesian optimization needs `parameters.schema`, and may use constraints.
- Genetic algorithms may use `parameters.encoding`.
- LLM-monitored parameter methods may use `candidate.exposure`, but the
  candidate itself remains a parameter candidate.

### 5.2 FileCandidateSpec

Use `files` when a candidate is one or more files or a file tree.

```yaml
candidate:
  type: files
  artifactKind: code_bundle
  description: Source files that can be edited to improve the evaluator score.
  files:
    root: simulator
    source:
      type: workspace_copy
      root: simulator
    editable:
      - path: devs_project/Controller.py
        language: python
        role: control_logic
        description: Main controller implementation.
    required:
      - devs_project/Controller.py
    allow:
      - devs_project/Controller.py
    deny:
      - "**/__pycache__/**"
      - "**/*.pyc"
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `root` | yes | Environment author | Candidate root inside the trial workspace. |
| `source` | no | Environment author | Baseline source provider. |
| `editable` | no | Environment author | Candidate-relative files methods may edit. |
| `required` | no | Environment author | Candidate-relative files every candidate must provide. |
| `allow` | no | Environment author | Candidate-relative paths allowed in artifacts. |
| `deny` | no | Environment author | Candidate-relative paths forbidden in artifacts. |

`source` fields:

```yaml
source:
  type: workspace_copy | path | artifact | none | custom
  root: path
  path: path
  artifactRef: string
  implementation: python:module:Class
  config: object
```

| Field | Owner | Meaning |
| --- | --- | --- |
| `type` | OptPilot/source adapter schema | Source provider family. |
| `root` | Environment author | Root inside workspace for `workspace_copy`. |
| `path` | Environment author | Local path for `path` source. |
| `artifactRef` | Environment/method ecosystem | Prior artifact reference. |
| `implementation` | Environment author | Custom source provider. |
| `config` | Source provider author | Provider-owned config. |

`editable` item fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `path` | yes | Environment author | Candidate-relative path. |
| `language` | no | Environment author | Syntax hint for editors. |
| `role` | no | Environment author | Semantic hint such as `solver`, `controller`, or `policy`. |
| `description` | no | Environment author | Human explanation of the file. |
| `maxBytes` | no | Environment author | Context-size guard. |
| `required` | no | Environment author | Whether this editable file must appear in every candidate. |

Method fit:

- Generic LLM file editors need `files.source` and `files.editable`.
- Patch search engines need `files.root`, and may use `allow`/`deny`.
- Human editing UI reads `files.editable`, file descriptions, and exposure.
- The method config should not contain environment-specific source directories
  or target file paths.

### 5.3 OpaqueCandidateSpec

Use `opaque` when OptPilot should store and pass an artifact by reference
without understanding its internal structure.

```yaml
candidate:
  type: opaque
  artifactKind: policy_checkpoint
  description: Trained policy checkpoint evaluated by the environment.
  opaque:
    family: policy_checkpoint
    formats: [pt, safetensors]
    requiredMetadata: [framework]
  validation:
    implementation: python:my_lab.validators:CheckpointValidator
  materialization:
    implementation: python:my_lab.materializers:CheckpointMaterializer
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `family` | yes | Environment/method ecosystem | Machine-readable opaque artifact family. |
| `formats` | no | Environment author | Accepted serialized formats or file suffixes. |
| `requiredMetadata` | no | Environment author | Metadata keys candidates must provide. |

Method fit:

- RL trainers can produce `policy_checkpoint`.
- External solvers can produce `solver_binary`.
- Container build methods can produce `container_image`.
- OptPilot records provenance and evidence, but does not inspect opaque
  internals unless a validator/materializer does so.

### 5.4 Composed Workflows

If an optimization needs coordinated code, parameters, data, and model
artifacts, model the official candidate as the representation the evaluator
actually receives: usually `files` for a runnable bundle, or `opaque` for a
checkpoint/container/handle. Put secondary inputs inside the bundle, expose
read-only context through `exposure`, or define separate environment configs for
separate optimization surfaces.

## 6. MethodExposureSpec

`exposure` declares environment-owned context that candidate-generation methods
may inspect.

```yaml
exposure:
  instructions:
    - path/to/task.md
  contextFiles:
    - README.md
    - src/main.py
  contextArtifacts:
    - id: historical_database
      path: database.db
      role: historical_data
      mediaType: application/vnd.sqlite3
      readonly: true
  contextRecords:
    - name: architecture_summary
      path: docs/architecture.md
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `instructions` | no | Environment author | Task/context documents for methods. |
| `contextFiles` | no | Environment author | Files safe for methods to read. |
| `contextArtifacts` | no | Environment author | Non-candidate artifacts safe for methods to inspect, such as datasets, databases, traces, or reference outputs. |
| `contextRecords` | no | Environment author | Named structured context records. |

Rules:

- `workspace.copy` controls evaluation workspace contents.
- `exposure` controls method-visible context.
- A file can be copied for evaluation without being exposed to a method.
- `contextArtifacts` are method context, not candidate payload. They should be
  treated as read-only unless the environment explicitly says otherwise.
- If a method needs query semantics rather than raw artifact bytes, the
  environment should expose an `interfaces` capability, such as a read-only
  SQLite query adapter, or provide extracted `contextRecords`.
- LLM methods often use exposure; BO and many meta-heuristics often do not.

## 7. Validation and Materialization

Validation checks whether a proposed candidate is acceptable. Materialization
turns an accepted candidate into evaluator input.

```yaml
validation:
  implementation: builtin.workspace_policy | python:module:Class
  config: object

materialization:
  implementation: builtin.workspace_bundle | python:module:Class
  config: object
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `implementation` | yes when present | Environment author | Built-in or importable implementation. |
| `config` | no | Implementation author | Implementation-owned config. |

Rules:

- Parameter candidates can use schema validation by default.
- File candidates can use workspace/path policy validation by default.
- Opaque candidates often need explicit validators and materializers.
- Custom implementations must produce evaluator inputs compatible with
  `evaluate`.

## 8. MethodConfig

`MethodConfig` describes candidate-generation strategy.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: string
description: string
tags: [string]

controller: ControllerConfig
engine: EngineConfig
monitor: MonitorConfig
compatibility: MethodCompatibility
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | yes | OptPilot schema | Config schema version. |
| `kind` | yes | OptPilot schema | Must be `MethodConfig`. |
| `id` | yes | Method author | Stable catalog id. |
| `description` | yes | Method author | Human explanation of the method. |
| `tags` | no | Method author | Catalog labels. |
| `controller` | no | Method author | Study-control logic. |
| `engine` | yes | Method author | Candidate-generation engine. |
| `monitor` | no | Method author | Optional observing or guidance component. |
| `compatibility` | yes | Method author | Declares compatible candidate contracts. |

### 8.1 ControllerConfig

```yaml
controller:
  id: string
  implementation: builtin.single_engine_controller | python:module:Class
  config: object
```

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `id` | no | Method author | Local controller id. |
| `implementation` | yes | Method author | Built-in or importable controller. |
| `config` | no | Controller author | Controller-owned config. |

The controller decides when to call engines, when to stop, and how to react to
evidence. It should not contain environment-specific source paths or target
files.

### 8.2 EngineConfig

```yaml
engine:
  id: string
  implementation: builtin.reference_random_search | python:module:Class
  config: object
  resourceProfile: object
  sandboxSpec: object
```

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `id` | no | Method author | Local engine id. |
| `implementation` | yes | Method author | Built-in or importable engine. |
| `config` | no | Engine author | Engine-owned strategy config. |
| `resourceProfile` | no | Method author | Resource hints. |
| `sandboxSpec` | no | Method author | Sandbox hints. |

Examples of valid `engine.config`:

- random-search batch size
- Bayesian optimization acquisition function
- LLM provider, model, temperature, and token limits
- RL learning rate and rollout count
- genetic algorithm population size and mutation rate

Examples of invalid `engine.config`:

- environment source directory
- list of editable environment files
- metric extraction rules
- workspace copy rules

### 8.3 MonitorConfig

`monitor` observes or guides a method without changing the candidate contract.
It is suitable for LLM-monitored versions of existing methods.

```yaml
monitor:
  id: string
  implementation: builtin.llm_run_monitor | python:module:Class
  observes:
    - trials
    - metrics
    - artifacts
    - logs
  canIntervene: boolean
  config: object
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `id` | no | Method author | Local monitor id. |
| `implementation` | yes | Method author | Built-in or importable monitor. |
| `observes` | no | Method author | Runtime streams the monitor consumes. |
| `canIntervene` | no | Method author | Whether it can ask the controller to adjust strategy. |
| `config` | no | Monitor author | Monitor-owned config. |

Rules:

- A monitor does not change `candidate.type` or `artifactKind`.
- A monitor may read trial history, metrics, logs, artifacts, and exposure.
- A monitor should not introduce environment-specific target files into method
  config.

### 8.4 MethodCompatibility

```yaml
compatibility:
  candidateTypes: [parameters | files | opaque]
  artifactKinds: [string]
  requiredContext: [string]
  optionalContext: [string]
  requiredCapabilities: [string]
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `candidateTypes` | yes | Method author | Candidate families the method can handle. |
| `artifactKinds` | no | Method author | Artifact kinds the method can produce. |
| `requiredContext` | no | Method author | CandidateContext paths required by the method. |
| `optionalContext` | no | Method author | CandidateContext paths the method can use. |
| `requiredCapabilities` | no | Method author | Environment interface capabilities required by the method. |

Examples:

```yaml
compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext:
    - parameters.schema
```

```yaml
compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext:
    - files.source
    - files.editable
  optionalContext:
    - exposure
```

```yaml
compatibility:
  candidateTypes: [opaque]
  artifactKinds: [policy_checkpoint]
  requiredCapabilities:
    - optpilot.rollout.v1
```

Compatibility checking:

1. Environment `candidate.type` must be in `candidateTypes`.
2. Environment `candidate.artifactKind` must match `artifactKinds` when
   `artifactKinds` is provided.
3. Every `requiredContext` path must exist in compiled `CandidateContext`.
4. Every `requiredCapabilities` value must be provided by an environment
   interface.

## 9. StudyConfig

`StudyConfig` binds environment, method, objective, budget, data, and execution
policy.

```yaml
apiVersion: optpilot.io/v3alpha1
kind: StudyConfig
name: string
description: string
tags: [string]

environment: path | inline EnvironmentConfig
method: path | inline MethodConfig

objective: ObjectiveConfig
instances: InstancesConfig
budget: BudgetConfig
execution: ExecutionConfig
evidence: EvidenceConfig
reproducibility: ReproducibilityConfig
overrides: StudyOverrides
```

Fields:

| Field | Required | Owner | Meaning |
| --- | --- | --- | --- |
| `name` | yes | Study author | Run/study name. |
| `description` | no | Study author | Human explanation. |
| `tags` | no | Study author | Catalog labels. |
| `environment` | yes | Study author | Environment reference or inline environment. |
| `method` | yes | Study author | Method reference or inline method. |
| `objective` | yes | Study author | Metric and direction. |
| `instances` | no | Study author | Evaluation cases or sampled task distribution. |
| `budget` | yes | Study author | Trial/time/failure limits. |
| `execution` | no | Study author | Backend/parallelism/retry policy. |
| `evidence` | no | Study author | Retention and output policy. |
| `reproducibility` | no | Study author | Seeds and environment capture. |
| `overrides` | no | Study author | Study-specific narrowing of environment contract. |

### 9.1 ObjectiveConfig

```yaml
objective:
  metric: service_score
  direction: maximize | minimize
  aggregation: mean | median | min | max | sum | last
  secondaryMetrics:
    - delivered_count
```

Rules:

- `metric` should be declared by the environment metrics config when known.
- `direction` is required.
- `aggregation` applies across instances.
- Secondary metrics are recorded and displayed but do not drive the primary
  objective unless a method chooses to use them.

### 9.2 InstancesConfig

```yaml
instances:
  source: none | inline | files | sampler
  value: object
  paths: [path]
  implementation: builtin.parameter_sampler | python:module:Class
  config: object
  count: integer
```

Use cases:

| `source` | Meaning |
| --- | --- |
| `none` | Empty/default instance. |
| `inline` | One inline instance object. |
| `files` | Fixed benchmark instance files. |
| `sampler` | Generated task distribution. |

### 9.3 BudgetConfig

```yaml
budget:
  maxTrials: integer
  maxWallClockSeconds: integer
  maxFailures: integer
```

`maxTrials` is required. Other limits are optional.

### 9.4 StudyOverrides

Overrides narrow the environment contract for one study. They should not expand
the environment contract unless the environment explicitly allows that.

```yaml
overrides:
  candidate:
    files:
      editable:
        include:
          - controller.py
        exclude:
          - experimental.py
```

Rules:

- Overrides are study-owned.
- Overrides are useful for comparing narrow and broad search surfaces.
- Methods should not use overrides to smuggle environment paths into method
  config.

## 10. Runtime Interfaces

### 10.1 CandidateContext

The compiler produces a normalized `CandidateContext` from
`EnvironmentConfig.candidate`, resolved paths, workspace declarations, and study
overrides. Controllers, engines, monitors, and UI components read
`CandidateContext`.

Example:

```json
{
  "type": "files",
  "artifactKind": "code_bundle",
  "description": "Strategic airlift simulator control logic files.",
  "files": {
    "root": "simulator",
    "source": {
      "type": "workspace_copy",
      "root": "simulator",
      "resolvedRoot": "/abs/path/to/simulator"
    },
    "editable": [
      {
        "path": "devs_project/Controller.py",
        "language": "python",
        "role": "control_logic"
      }
    ],
    "required": ["devs_project/Controller.py"],
    "allow": ["devs_project/Controller.py"],
    "deny": ["**/__pycache__/**"]
  },
  "exposure": {
    "instructions": ["/abs/path/to/task.md"],
    "contextFiles": ["/abs/path/to/README.md"],
    "contextArtifacts": [
      {
        "id": "historical_database",
        "path": "/abs/path/to/database.db",
        "role": "historical_data",
        "mediaType": "application/vnd.sqlite3",
        "readonly": true
      }
    ]
  }
}
```

Rules:

- Engines read environment candidate details from `candidate_context`, not from
  method config.
- Compatibility checks operate on `CandidateContext`.
- UI can use `CandidateContext` to explain why a method/environment pair is
  compatible or incompatible.

### 10.2 CandidateProposal

Methods emit candidate proposals. Proposal metadata is method-owned; evaluator
inputs are created through materialization.

```yaml
proposal:
  type: parameters | files | opaque
  artifactKind: string
  payload: CandidatePayload
  parentIds: [string]
  metadata:
    generatedBy: string
    rationale: string
    confidence: number
```

Payload examples:

```yaml
payload:
  parameters:
    values:
      x: 4.2
      mode: aggressive
```

```yaml
payload:
  files:
    patch: path/to/candidate.patch
    tree: path/to/materialized/tree
```

```yaml
payload:
  opaque:
    artifactRef: path/to/checkpoint.pt
    metadata:
      framework: pytorch
```

Rules:

- Proposal `type` must match the environment candidate type.
- Proposal `artifactKind` must match the environment artifact kind.
- Proposal payload follows the selected candidate type.
- Metadata may include LLM rationale, acquisition score, mutation operator,
  training summary, or confidence.
- Evaluators consume materialized candidates, not proposal metadata.

### 10.3 EvidenceView

Controllers, engines, monitors, and analysis tools read prior run evidence
through `EvidenceView`.

Common calls:

```python
summary = evidence_view.summary()
observations = evidence_view.observations(limit=10)
artifacts = evidence_view.artifacts(limit=20)
streams = evidence_view.record_streams("machine_events")
rows = evidence_view.records("machine_events", limit=100)
```

Extracted records are returned with provenance:

```python
{
    "name": "machine_events",
    "source": "csv",
    "trial_id": "trial-abc",
    "artifact_id": "artifact-def",
    "row_index": 0,
    "record": {"event": "queued", "machine": "m1"},
}
```

This keeps simulator-generated CSV, JSONL, and SQLite records accessible without
requiring methods to know where trial workspaces or extracted files live.

## 11. Concrete Use Cases

### 11.1 Parameter Search

Environment:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: toy-factory
description: Toy factory simulator.

evaluate:
  type: python
  callable: optpilot.examples.toy_factory_env:evaluate

candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Toy factory tuning parameters.
  parameters:
    schema:
      x:
        type: float
        min: 0.0
        max: 8.0
      y:
        type: int
        min: 1
        max: 10
      mode:
        type: categorical
        values: [balanced, aggressive, conservative]

metrics:
  source: return
  keys: [throughput, cycle_time]
```

Random search method:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: reference-random-search
description: Random search over declared parameter schemas.

engine:
  implementation: builtin.reference_random_search
  config:
    batchSize: 4

compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext:
    - parameters.schema
```

Bayesian optimization can use the same environment:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: bayesian-optimization
description: Sequential model-based optimization over parameters.

engine:
  implementation: builtin.bayesian_optimization
  config:
    acquisition: expected_improvement

compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext:
    - parameters.schema
  optionalContext:
    - parameters.constraints
    - parameters.encoding
```

### 11.2 File Editing

Environment:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: sa-simulator-code-edit
description: Strategic airlift simulator source optimization.

evaluate:
  type: python
  callable: examples.opt_devs_gen_sims.sa_eval:evaluate
  timeoutSeconds: 180

workspace:
  copy:
    - from: ../../../resource/devs_gen_gallery/simulators/SA/simulator
      to: simulator
      role: source

candidate:
  type: files
  artifactKind: code_bundle
  description: Strategic airlift simulator control logic files.
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
  exposure:
    instructions:
      - ../prompts/sa_file_edit_system_prompt.md

metrics:
  source: return
  keys: [service_score, delivered_count, expired_count, mean_latency]
```

Method:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: generic-llm-file-editor
description: LLM method that edits files declared by a file candidate contract.

engine:
  id: llm_file_editor
  implementation: python:examples.user_engines.llm_file_edit_engine:LLMFileEditEngine
  config:
    provider: openrouter
    model: openai/gpt-4.1-mini
    temperature: 0.2
    maxTokens: 4000
    includeBaselineCandidate: true

compatibility:
  candidateTypes: [files]
  artifactKinds: [code_bundle]
  requiredContext:
    - files.source
    - files.editable
  optionalContext:
    - exposure
```

The method has no simulator source directory and no hard-coded target file list.
Those belong to the environment candidate contract.

### 11.3 Opaque RL Checkpoint

Environment:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: policy-eval
description: Evaluates trained policy checkpoints.

evaluate:
  type: python
  callable: my_lab.eval:evaluate_policy_checkpoint

interfaces:
  - id: training_api
    capability: optpilot.rollout.v1
    description: Episode rollout API used by online RL trainers.
    adapter:
      implementation: python:my_lab.rollout:RolloutInterface

candidate:
  type: opaque
  artifactKind: policy_checkpoint
  description: Policy checkpoint artifact evaluated by the environment.
  opaque:
    family: policy_checkpoint
    formats: [pt]
    requiredMetadata: [framework]
```

Method:

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: ppo-training
description: PPO trainer that produces policy checkpoints.

engine:
  implementation: python:my_lab.engines:PPOTrainingEngine
  config:
    rolloutSteps: 10000
    learningRate: 0.0003

compatibility:
  candidateTypes: [opaque]
  artifactKinds: [policy_checkpoint]
  requiredCapabilities:
    - optpilot.rollout.v1
```

The candidate is the policy checkpoint. The rollout API is an environment
capability used by the method while producing that checkpoint.

### 11.4 LLM-Monitored Bayesian Optimization

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: bayesian-optimization-with-llm-monitor
description: Bayesian optimization with an LLM monitor that explains progress.

engine:
  implementation: builtin.bayesian_optimization
  config:
    acquisition: expected_improvement

monitor:
  implementation: builtin.llm_run_monitor
  observes: [trials, metrics, artifacts, logs]
  canIntervene: false
  config:
    provider: openrouter
    model: openai/gpt-4.1-mini

compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext:
    - parameters.schema
```

The monitor changes how the run is observed. It does not change the candidate
contract.

## 12. UI Implications

The UI should be able to derive these views from config alone:

- environment catalog
- method catalog
- candidate type and artifact kind
- candidate surface details
- editable files for file candidates
- parameter schemas for parameter candidates
- required method context
- missing compatibility requirements
- study builder with invalid pairings blocked before launch
- active and historical studies grouped by environment, method, objective, and
  status

Example compatibility explanation:

```text
Environment: SA Simulator
Candidate: files / code_bundle
Provides: files.source, files.editable, exposure

Method: Generic LLM File Editor
Requires: files.source, files.editable
Compatible: yes
```

Example incompatibility explanation:

```text
Environment: Toy Factory
Candidate: parameters / parameter_spec

Method: Generic LLM File Editor
Requires candidateTypes: files
Compatible: no
```

## 13. Summary

The interface is healthy when:

- environments declare evaluation and candidate surfaces
- methods declare strategy and compatibility
- studies bind environment and method without smuggling environment details into
  method config
- engines consume normalized `candidate_context`
- compatibility is checkable before launch
- rich future methods use capabilities and monitors without changing the
  candidate boundary
