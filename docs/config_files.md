# OptPilot Config Schema

OptPilot users author three config kinds:

- `EnvironmentConfig`: how candidates are evaluated.
- `MethodConfig`: the user-owned optimization method that proposes candidates.
- `StudyConfig`: the binding between one environment, one method, an objective, instances, and budget.

OptPilot compiles these authoring configs into an internal `StudySpec` and records that expanded spec in each run directory.

The repository has two default catalog roots:

- `examples/`: curated built-in integrations.
- `user_catalog/`: the recommended place for user-owned configs, assets, and implementation code.

Inside `examples/` and `user_catalog/`, environment and method directories should own both reusable configs and implementation code. Studies remain project-centric bindings:

```text
user_catalog/
  environments/my_environment/
    environment.yaml
    evaluator.py
  methods/my_method/
    method.yaml
    method.py
  studies/my_study.yaml
```

Reference code with module paths such as `user_catalog.environments.my_environment.evaluator:evaluate` or `python:user_catalog.methods.my_method.method:MyMethod`.

## EnvironmentConfig

```yaml
apiVersion: optpilot.io/v1
kind: EnvironmentConfig
id: sa-simulator-code-edit

evaluate:
  type: python
  callable: examples.environments.strategic_airlift_devs.evaluator:evaluate

candidate:
  type: files
  artifactKind: code_bundle
  description: SA simulator control logic files.
  files:
    root: simulator
    source: {type: workspace_copy, root: simulator}
    editable:
      - path: devs_project/StrategicAirlift_D0_libs/Aircraft_libs/MissionController.py
        language: python
        role: control_logic

metrics:
  source: return
  keys: [service_score]
```

Supported evaluator types are `python`, `command`, and `custom`. `custom` evaluators use a component reference such as `python:my_lab.envs:Adapter`; the adapter receives the target definition and study spec, and implements `evaluate(artifact_spec, instance, context)`.

Supported metric sources are `return`, `file`, `stdout`, `sqlite`, and `custom`. Custom metric extractors use `python:module:function_or_class` and receive workspace, evaluator result, process result, and extractor config.

Record extraction supports `jsonl`, `csv`, `sqlite_table`, `sqlite_query`, and `custom`. Custom record extractors return rows directly or a dict containing `rows`/`records`.

Supported candidate types are `parameters`, `files`, and `opaque`. `artifactKind` and `description` are required for all candidates.

File candidates can declare `workspace.copy`, `candidate.files.editable`, `candidate.files.required`, `candidate.files.allow`, and `candidate.files.deny`. OptPilot copies only declared workspace inputs into trial workspaces before materializing the candidate.

## MethodConfig

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

`implementation.type` is one of:

- `python`: instantiate a `python:module:Class` or `builtin.*` method.
- `command`: run an external command using the batch protocol.

`implementation.protocol` can be:

- `optpilot.method.batch.v1`: the method is passively called by OptPilot to propose a batch.
- `optpilot.method.session.v1`: a Python method receives a small active session object and submits candidates through `session.submit(...)`.

Both protocols flow into the same evaluator and scheduler path. Batch command methods can either read/write JSON through stdin/stdout or explicit request/response files. Session methods are Python-only in the current implementation.

For parameter candidates, OptPilot injects `candidate.parameters.schema` into `method.config.searchSpace` when the method did not provide a search space explicitly.

`runtime` is optional. When omitted, the method command runs in the host process environment. Command methods can also run through a Docker/Podman-compatible container:

```yaml
runtime:
  type: container
  image: my-method-image:latest
  containerExecutable: docker
  networkPolicy: disabled
  build:
    context: .
    dockerfile: Dockerfile.method
    tag: my-method-image:latest
    args:
      PACKAGE_SET: default
  workdir: .
  envFromHost: [OPENAI_API_KEY]
  env:
    OPTIMIZER_MODE: production
  readOnlyMounts: []
  writableMounts: []
```

For `runtime.type: container`, the image must contain the executable named by `implementation.command`. OptPilot mounts the project directory, the study config directory, and the per-call method workspace at the same absolute paths inside the container. The method workspace is writable and is exposed in the request as `runtime_context.method_workspace`. `env` values are literal config values; `envFromHost` passes through named host environment variables when they are set.

`runtime.build` is optional. When present, OptPilot runs `containerExecutable build` once per run before the method command is launched. Paths in `build.context` are resolved relative to the method or study config base directory after compilation. The build block supports `context`, `dockerfile`, `tag`, `target`, `platform`, `pull`, `noCache`, `args`, `extraArgs`, and `timeoutSeconds`.

Command methods can either read the request JSON from stdin and write response JSON to stdout:

```yaml
implementation:
  type: command
  command: [python, my_method.py]
  protocol: optpilot.method.batch.v1
```

or use explicit request/response files:

```yaml
implementation:
  type: command
  command: [python, my_method.py, "{input_file}", "{output_file}"]
  protocol: optpilot.method.batch.v1
```

The response must contain `candidates` or `artifacts`, each entry being a candidate artifact manifest. Optional `method_events` entries are recorded in `method_events.jsonl`.

Python session methods implement `run(session)` or are callable. The session exposes `study_state`, `evidence`, `candidate_context`, `config`, `n_candidates`, `submit(...)`, and `event(...)`.

## StudyConfig

```yaml
apiVersion: optpilot.io/v1
kind: StudyConfig
name: sa-baseline

environment: ../environments/strategic_airlift_devs/environment.yaml
method: ../methods/baseline_file_copy/method.yaml

objective:
  metric: service_score
  direction: maximize
  aggregation: mean

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

reproducibility:
  seed: 0
```

Supported objective aggregation modes are `mean`, `median`, `min`, `max`, `sum`, `last`, and `weighted_mean`.

`weighted_mean` accepts `objective.aggregation.weights` as a list, a scalar per metric, or a dict keyed by metric name with `*` as a fallback.

Supported execution backends are `local`, `local_subprocess`, `container`, and `custom`. `parallelism` controls candidate evaluation parallelism. `custom` requires an explicit `implementation` value that resolves through the component registry, such as `python:my_lab.backends:Backend`.

Container execution uses a Docker/Podman-compatible CLI and runs the same OptPilot worker contract as `local_subprocess`:

```yaml
execution:
  backend: container
  parallelism: 2
  timeoutSeconds: 120
  config:
    image: python:3.11
    containerExecutable: docker
    pythonExecutable: python
    build:
      context: .
      dockerfile: Dockerfile.environment
      tag: python:3.11
```

The container backend mounts the current project directory, the run directory, and the study config directory at the same absolute paths inside the container. The image must contain Python and any dependencies needed by the environment evaluator. `containerExecutable` can be `docker`, `podman`, or another compatible wrapper. `execution.config.build` uses the same build schema as method runtimes and is executed once per backend instance before trial workers launch.

Method runtime containers and execution backend containers are independent. Use method runtime containers when the optimization method or agent has its own dependencies. Use execution backend containers when the environment evaluator or simulator has its own dependencies.

## Run Evidence

Each run directory records:

- `study_spec.json`
- `summary.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `method_calls.jsonl`
- `method_events.jsonl` when methods emit events
- `scheduler_events.jsonl`
- `environment_snapshot.json`
- `run_policy.json`
- `run_lineage.json`
