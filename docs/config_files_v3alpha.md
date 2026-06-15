# OptPilot v3alpha Config Schema

OptPilot users author three config kinds:

- `EnvironmentConfig`: how candidates are evaluated.
- `MethodConfig`: the user-owned optimization method that proposes candidates.
- `StudyConfig`: the binding between one environment, one method, an objective, instances, and budget.

OptPilot compiles these authoring configs into an internal `StudySpec` and records that expanded spec in each run directory.

## EnvironmentConfig

```yaml
apiVersion: optpilot.io/v3alpha1
kind: EnvironmentConfig
id: toy-factory

evaluate:
  type: python
  callable: optpilot.examples.toy_factory_env:evaluate

candidate:
  type: parameters
  artifactKind: parameter_spec
  description: Parameters accepted by the evaluator.
  parameters:
    schema:
      x: {type: float, min: 0.0, max: 8.0}

metrics:
  source: return
  keys: [throughput]
```

Supported evaluator types are `python`, `command`, and `custom`.

Supported candidate types are `parameters`, `files`, and `opaque`. `artifactKind` and `description` are required for all candidates.

File candidates can declare `workspace.copy`, `candidate.files.editable`, `candidate.files.required`, `candidate.files.allow`, and `candidate.files.deny`. OptPilot copies only declared workspace inputs into trial workspaces before materializing the candidate.

## MethodConfig

```yaml
apiVersion: optpilot.io/v3alpha1
kind: MethodConfig
id: reference-random-search

implementation:
  type: python
  callable: builtin.reference_random_search
  protocol: optpilot.method.batch.v1

config:
  batchSize: 4

compatibility:
  candidateTypes: [parameters]
  artifactKinds: [parameter_spec]
  requiredContext: [parameters.schema]
```

`implementation.type` is one of:

- `python`: instantiate a `python:module:Class` or `builtin.*` method.
- `command`: run an external command using the batch protocol.

`implementation.protocol` is `optpilot.method.batch.v1` today. The method receives study state, candidate context, objective, evidence summary, and method config, then returns candidate artifact manifests.

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

## StudyConfig

```yaml
apiVersion: optpilot.io/v3alpha1
kind: StudyConfig
name: toy-random-search

environment: ../environments/toy_factory.yaml
method: ../methods/reference_random_search.yaml

objective:
  metric: throughput
  direction: maximize

instances:
  source: files
  paths:
    - ../instances/toy_factory_case.yaml

budget:
  maxTrials: 12

execution:
  backend: local
  parallelism: 4
  timeoutSeconds: 120

reproducibility:
  seed: 7
```

Supported execution backends are `local`, `local_subprocess`, `container`, and `custom`. `parallelism` controls candidate evaluation parallelism.

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
