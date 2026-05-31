# OptPilot StudySpec V1

Historical note: this document describes an earlier design where users wrote
`StudySpec` directly. The current public alpha config contract is
`StudyConfig`, `EnvironmentConfig`, and `MethodConfig`, documented in
[`config_files_v3alpha.md`](config_files_v3alpha.md). `StudySpec` is now the
internal expanded execution/audit representation.

## 1. Purpose

`StudySpec` is the declarative configuration format for one OptPilot study. It is the bridge between the platform design in [version 3.md](version%203.md) and an implementable system.

A `StudySpec` should be expressive enough to configure:

- direct search over one-shot simulators
- Bayesian optimization and meta-heuristic search
- reinforcement learning with rollout workers
- LLM-guided code evolution
- LLM-supervised RL or Bayesian optimization
- nested and multi-stage workflows

At the same time, it should avoid baking specific algorithm ecosystems into the schema. The spec should describe composition, policy, and evidence requirements, while concrete implementations remain pluggable.

## 2. Design Rules

A `StudySpec` should follow these rules:

1. Stable platform-owned structure, pluggable implementation details.
2. Explicit references to controllers, engines, adapters, backends, and sandbox providers.
3. Clear separation between target environment, optimizable artifacts, and runtime execution.
4. Resource and sandbox requirements must be first-class.
5. Trial-level execution should inherit defaults from study-level policy unless explicitly overridden.
6. Extension points should be data-driven so users can integrate external frameworks without changing the core schema.

## 3. Shape of a StudySpec

A `StudySpec` should have the following top-level sections:

- `apiVersion`
- `kind`
- `metadata`
- `target`
- `objective`
- `evaluationScope`
- `artifacts`
- `controllers`
- `engines`
- `execution`
- `evidence`
- `reproducibility`
- `stopping`
- `extensions`

The schema should allow YAML or JSON representation, but examples below use YAML.

## 4. Top-Level Structure

```yaml
apiVersion: optpilot/v1
kind: StudySpec
metadata:
  name: string
  description: string
  tags: [string]

target: {...}
objective: {...}
evaluationScope: {...}
artifacts: {...}
controllers: [...]
engines: [...]
execution: {...}
evidence: {...}
reproducibility: {...}
stopping: {...}
extensions: {...}
```

## 5. Field Definitions

### 5.1 `apiVersion`

Identifies the schema version of the study definition.

Example:

```yaml
apiVersion: optpilot/v1
```

### 5.2 `kind`

Identifies the document type.

Example:

```yaml
kind: StudySpec
```

### 5.3 `metadata`

Human- and system-facing metadata for the study.

Fields:

- `name`: unique study name within a workspace or project
- `description`: free-text description
- `tags`: optional labels for organization and search
- `owner`: optional user or team identifier
- `createdFrom`: optional template or parent study reference

Example:

```yaml
metadata:
  name: warehouse-routing-bo
  description: Optimize routing and buffering policy for a fixed warehouse simulation
  tags: [warehouse, bayesopt, fixed-instance]
  owner: ie-lab
```

### 5.4 `target`

Describes the target environment and how OptPilot should access it.

Fields:

- `targetId`: stable logical identifier
- `adapter`:
  - `type`: platform adapter type such as `python`, `cli`, `service`, `gym_like`, `compound`, `openenv`
  - `implementation`: pluggable adapter implementation identifier
  - `config`: adapter-specific configuration
- `instanceSchemaRef`: optional schema reference for instance definitions
- `observationSchemaRef`: optional schema reference for observations and artifacts
- `runtimeContract`: static environment constraints
- `accessPolicy`: target visibility policy
- `mutationPolicy`: target mutation policy

Example:

```yaml
target:
  targetId: warehouse-sim-v2
  adapter:
    type: python
    implementation: builtin.python_callable
    config:
      module: warehouse_sim.api
      callable: evaluate
  instanceSchemaRef: schemas/warehouse_instance.yaml
  observationSchemaRef: schemas/warehouse_observation.yaml
  runtimeContract:
    timeoutSeconds: 600
    dependenciesProfile: warehouse-sim-py310
  accessPolicy: SchemaAware
  mutationPolicy: NoMutation
```

### 5.5 `objective`

Defines how study performance is judged.

Fields:

- `primaryMetric`:
  - `name`
  - `direction`: `maximize` or `minimize`
- `constraints`: optional list of constraint clauses
- `secondaryMetrics`: optional list of reporting or tie-break metrics
- `aggregation`: how scores aggregate across instances, seeds, or episodes
- `costModel`: optional definition of cost-aware scoring

Example:

```yaml
objective:
  primaryMetric:
    name: throughput
    direction: maximize
  constraints:
    - metric: lateness
      operator: <=
      value: 0.05
  secondaryMetrics:
    - utilization
    - mean_cycle_time
  aggregation:
    mode: weighted_mean
    weightsRef: configs/instance_weights.yaml
```

### 5.6 `evaluationScope`

Defines what population of instances the study optimizes against.

Fields:

- `mode`: `FixedInstance`, `InstanceSet`, `Distribution`, or `Curriculum`
- `definition`: mode-specific configuration

Examples:

```yaml
evaluationScope:
  mode: FixedInstance
  definition:
    instanceRef: instances/factory_case_01.yaml
```

```yaml
evaluationScope:
  mode: Distribution
  definition:
    sampler:
      implementation: builtin.parameter_sampler
      config:
        demand_mean: [100, 140]
        demand_std: [10, 25]
        seedPolicy: deterministic_grid
```

### 5.7 `artifacts`

Defines the optimizable artifact space and any initial artifacts.

Fields:

- `primaryArtifact`:
  - `kind`
  - `schemaRef`: optional
  - `materializationPlan`
  - `validationRules`
- `initialArtifacts`: optional list of bootstrap artifacts
- `artifactTemplates`: optional reusable templates for controllers and engines

Example:

```yaml
artifacts:
  primaryArtifact:
    kind: parameter_spec
    schemaRef: schemas/warehouse_parameters.yaml
    materializationPlan:
      implementation: builtin.parameter_to_config
      config:
        outputPath: candidate/config.yaml
    validationRules:
      implementation: builtin.schema_validation
      config:
        enforceBounds: true
```

### 5.8 `controllers`

Defines the controllers participating in the study.

A study may have one controller or a controller graph.

Each controller definition should include:

- `id`
- `type`: semantic role such as `llm_agent`, `rule_based`, `workflow_controller`
- `implementation`: concrete user-selected implementation
- `config`: implementation-specific config
- `permissions`: optional narrowed access and mutation policies
- `inputs`: what evidence or state views it can consume
- `outputs`: what actions it is allowed to emit

Example:

```yaml
controllers:
  - id: orchestrator
    type: llm_agent
    implementation: python:my_lab.controllers:LangGraphSupervisor
    config:
      model: gpt-5.4
      temperature: 0.2
      maxContextArtifacts: 12
    permissions:
      accessPolicy: FullStudyContext
      mutationPolicy: EngineConfigOnly
    inputs:
      evidenceViews: [summary_metrics, trial_failures, learning_curves]
    outputs:
      allowedActions: [launch_engine, update_engine_config, stop_study, branch_study]
```

### 5.9 `engines`

Defines the search, training, or transformation engines available to the study.

Each engine definition should include:

- `id`
- `type`: semantic role such as `bayesopt`, `rl_trainer`, `code_mutation`, `validator`
- `implementation`: concrete engine package or adapter identifier
- `config`: implementation-specific config
- `resourceProfileRef` or inline `resourceProfile`
- `sandboxSpecRef` or inline `sandboxSpec`
- `produces`: artifact kinds or observation types expected from this engine

Example:

```yaml
engines:
  - id: bo_main
    type: bayesopt
    implementation: python:my_lab.engines:BoTorchEngine
    config:
      acquisition: qei
      batchSize: 8
      surrogate: single_task_gp
    resourceProfile:
      cpu: 4
      memoryGiB: 16
      gpu: 0
      timeoutSeconds: 1800
    sandboxSpec:
      runtimeType: process
      networkPolicy: disabled
      cleanupPolicy: on_success_or_failure
    produces:
      artifacts: [parameter_spec]
      observations: [candidate_scores]
```

### 5.10 `execution`

Defines study-level execution defaults and backend selection.

Fields:

- `backend`:
  - `type`: `local_process`, `docker`, `kubernetes`, `slurm`, `ray`, or custom
  - `implementation`
  - `config`
- `defaults`:
  - `resourceProfile`
  - `sandboxSpec`
  - `retryPolicy`
- `parallelism`:
  - `candidateParallelism`
  - `rolloutParallelism`
  - `engineParallelism`

Example:

```yaml
execution:
  backend:
    type: docker
    implementation: python:my_lab.backends:DockerBackend
    config:
      imagePullPolicy: if_not_present
  defaults:
    resourceProfile:
      cpu: 2
      memoryGiB: 8
      gpu: 0
      timeoutSeconds: 900
    sandboxSpec:
      runtimeType: container
      networkPolicy: restricted
      writableWorkspace: /workspace
      cleanupPolicy: always
    retryPolicy:
      maxRetries: 1
      retryOn: [transient_backend_failure]
  parallelism:
    candidateParallelism: 4
    rolloutParallelism: 1
    engineParallelism: 1
```

### 5.11 `evidence`

Defines what evidence is stored and how much detail is retained.

Fields:

- `store`:
  - `metadataBackend`
  - `artifactBackend`
- `retention`:
  - prompts
  - logs
  - traces
  - checkpoints
  - intermediate tables
- `capture`:
  - controller decisions
  - engine snapshots
  - validation outputs
  - resource assignments

Example:

```yaml
evidence:
  store:
    metadataBackend: sqlite
    artifactBackend: local_fs
  retention:
    prompts: full
    logs: full
    traces: selected
    checkpoints: best_and_last
    intermediateTables: full
  capture:
    controllerDecisions: true
    engineSnapshots: true
    validationOutputs: true
    resourceAssignments: true
```

### 5.12 `reproducibility`

Defines what must be pinned and recorded for reruns and auditing.

Fields:

- `seedPolicy`
- `environmentSnapshot`
- `dependencySnapshot`
- `recordAssignedResources`
- `recordSandboxConfig`
- `recordModelInvocations`

Example:

```yaml
reproducibility:
  seedPolicy:
    globalSeed: 12345
    perTrialDerivation: deterministic_hash
  environmentSnapshot: required
  dependencySnapshot: required
  recordAssignedResources: true
  recordSandboxConfig: true
  recordModelInvocations: true
```

### 5.13 `stopping`

Defines the stopping rules and budget rules.

Fields:

- `maxTrials`
- `maxWallClockSeconds`
- `maxComputeCost`
- `convergenceRule`
- `earlyStopPolicy`

Example:

```yaml
stopping:
  maxTrials: 100
  maxWallClockSeconds: 21600
  convergenceRule:
    implementation: builtin.no_improvement
    config:
      patienceTrials: 20
      minDelta: 0.001
```

### 5.14 `extensions`

A free-form namespace for user or organization-specific fields that should not collide with the platform-owned schema.

Example:

```yaml
extensions:
  my_lab:
    reportingProfile: weekly_dashboard
    costCenter: ie-research
```

## 6. Profiles and Inheritance

The schema should support reuse and defaults.

A study often needs shared resource and sandbox policies across multiple engines and trials. `StudySpec` should therefore allow:

- study-level defaults in `execution.defaults`
- engine-level overrides
- trial-shape-specific overrides where necessary

Inheritance rule:

1. trial-specific override
2. engine-specific setting
3. execution default

This keeps the spec concise while still allowing GPU-heavy RL jobs to differ from lightweight validation runs.

## 7. Recommended Reference Pattern

Pluggable components should be referenced by `implementation` identifiers plus free-form `config`, instead of embedding framework-specific schema into the core spec.

Recommended pattern:

```yaml
implementation: python:my_lab.engines:BoTorchEngine
config:
  acquisition: qei
  batchSize: 8
```

This keeps the platform schema stable while allowing users to adopt any framework that can honor the interface.

## 8. Trial Semantics in the Spec

`StudySpec` should not require users to describe every trial manually. Instead, trials should be implied by controllers, engines, and evaluation scope.

Still, the spec should allow trial-shape hints where needed:

```yaml
execution:
  trialShapes:
    candidate_eval:
      type: AtomicTrial
    policy_benchmark:
      type: BatchTrial
      seeds: 10
    rl_training_round:
      type: CompoundTrial
```

This is useful for schedulers, evidence retention policies, and cost estimation.

## 9. Example: One-Shot Bayesian Optimization Study

```yaml
apiVersion: optpilot/v1
kind: StudySpec
metadata:
  name: warehouse-bo
  description: Bayesian optimization over warehouse simulator parameters
  tags: [warehouse, bo]

target:
  targetId: warehouse-sim-v2
  adapter:
    type: python
    implementation: builtin.python_callable
    config:
      module: warehouse_sim.api
      callable: evaluate
  accessPolicy: SchemaAware
  mutationPolicy: NoMutation

objective:
  primaryMetric:
    name: throughput
    direction: maximize
  constraints:
    - metric: lateness
      operator: <=
      value: 0.05
  aggregation:
    mode: mean

evaluationScope:
  mode: FixedInstance
  definition:
    instanceRef: instances/warehouse_case_01.yaml

artifacts:
  primaryArtifact:
    kind: parameter_spec
    schemaRef: schemas/warehouse_parameters.yaml
    materializationPlan:
      implementation: builtin.parameter_to_config
      config:
        outputPath: candidate/config.yaml
    validationRules:
      implementation: builtin.schema_validation
      config:
        enforceBounds: true

controllers:
  - id: controller_main
    type: workflow_controller
    implementation: builtin.single_engine_controller
    config: {}
    inputs:
      evidenceViews: [summary_metrics]
    outputs:
      allowedActions: [launch_engine, stop_study]

engines:
  - id: bo_main
    type: bayesopt
    implementation: python:my_lab.engines:BoTorchEngine
    config:
      acquisition: qei
      batchSize: 4
      surrogate: single_task_gp
    resourceProfile:
      cpu: 4
      memoryGiB: 16
      gpu: 0
      timeoutSeconds: 1800
    sandboxSpec:
      runtimeType: process
      networkPolicy: disabled
      cleanupPolicy: on_success_or_failure

execution:
  backend:
    type: docker
    implementation: python:my_lab.backends:DockerBackend
    config: {}
  defaults:
    retryPolicy:
      maxRetries: 1
      retryOn: [transient_backend_failure]
  parallelism:
    candidateParallelism: 4
    rolloutParallelism: 1
    engineParallelism: 1

evidence:
  store:
    metadataBackend: sqlite
    artifactBackend: local_fs
  retention:
    prompts: none
    logs: full
    traces: selected
    checkpoints: none
    intermediateTables: full
  capture:
    controllerDecisions: true
    engineSnapshots: true
    validationOutputs: true
    resourceAssignments: true

reproducibility:
  seedPolicy:
    globalSeed: 42
    perTrialDerivation: deterministic_hash
  environmentSnapshot: required
  dependencySnapshot: required
  recordAssignedResources: true
  recordSandboxConfig: true
  recordModelInvocations: false

stopping:
  maxTrials: 60
  convergenceRule:
    implementation: builtin.no_improvement
    config:
      patienceTrials: 12
      minDelta: 0.001

extensions: {}
```

## 10. Example: LLM-Supervised RL Study

```yaml
apiVersion: optpilot/v1
kind: StudySpec
metadata:
  name: dispatch-rl-supervised
  description: LLM controller supervises RL training for dispatch optimization
  tags: [rl, llm, dispatch]

target:
  targetId: dispatch-env-v1
  adapter:
    type: openenv
    implementation: python:my_lab.adapters:OpenEnvAdapter
    config:
      baseUrl: http://dispatch-env.internal
  accessPolicy: TraceAware
  mutationPolicy: NoMutation

objective:
  primaryMetric:
    name: episode_return
    direction: maximize
  secondaryMetrics:
    - safety_violation_rate
    - training_cost
  aggregation:
    mode: weighted_mean
    weights:
      episode_return: 1.0
      safety_violation_rate: -2.0
      training_cost: -0.1

evaluationScope:
  mode: Distribution
  definition:
    sampler:
      implementation: builtin.parameter_sampler
      config:
        demand_profile: [low, medium, high]
        disruption_rate: [0.0, 0.1, 0.2]

artifacts:
  primaryArtifact:
    kind: training_spec
    materializationPlan:
      implementation: builtin.training_package_builder
      config:
        workspacePath: candidate/
    validationRules:
      implementation: builtin.training_spec_validation
      config: {}
  artifactTemplates:
    - id: reward_template
      kind: reward_function
    - id: policy_init_template
      kind: policy_checkpoint

controllers:
  - id: llm_supervisor
    type: llm_agent
    implementation: python:my_lab.controllers:LangGraphSupervisor
    config:
      model: gpt-5.4
      temperature: 0.1
      maxContextArtifacts: 20
    permissions:
      accessPolicy: FullStudyContext
      mutationPolicy: EngineConfigOnly
    inputs:
      evidenceViews: [learning_curves, rollout_failures, evaluation_summaries]
    outputs:
      allowedActions: [launch_engine, update_engine_config, branch_study, stop_study]

engines:
  - id: rl_train
    type: rl_trainer
    implementation: python:my_lab.engines:RllibTrainer
    config:
      algorithm: PPO
      trainBatchSize: 32768
      rolloutFragmentLength: 256
    resourceProfile:
      cpu: 8
      memoryGiB: 32
      gpu: 1
      gpuClass: any_cuda
      timeoutSeconds: 14400
    sandboxSpec:
      runtimeType: container
      networkPolicy: restricted
      writableWorkspace: /workspace
      readOnlyMounts:
        - /targets/dispatch-env
      cleanupPolicy: always
    produces:
      artifacts: [policy_checkpoint, learning_curve, rollout_trace]
      observations: [training_summary, benchmark_scores]

  - id: policy_eval
    type: evaluator
    implementation: builtin.batch_policy_evaluator
    config:
      seeds: 10
      instanceSampling: fixed_set
    resourceProfile:
      cpu: 16
      memoryGiB: 32
      gpu: 0
      timeoutSeconds: 3600
    sandboxSpec:
      runtimeType: container
      networkPolicy: disabled
      cleanupPolicy: always

execution:
  backend:
    type: kubernetes
    implementation: python:my_lab.backends:KubernetesBackend
    config:
      namespace: optpilot
      gpuNodeSelector: nvidia
  defaults:
    retryPolicy:
      maxRetries: 1
      retryOn: [evicted, transient_backend_failure]
  parallelism:
    candidateParallelism: 1
    rolloutParallelism: 16
    engineParallelism: 2

evidence:
  store:
    metadataBackend: postgres
    artifactBackend: s3
  retention:
    prompts: full
    logs: full
    traces: selected
    checkpoints: best_and_last
    intermediateTables: selected
  capture:
    controllerDecisions: true
    engineSnapshots: true
    validationOutputs: true
    resourceAssignments: true

reproducibility:
  seedPolicy:
    globalSeed: 12345
    perTrialDerivation: deterministic_hash
  environmentSnapshot: required
  dependencySnapshot: required
  recordAssignedResources: true
  recordSandboxConfig: true
  recordModelInvocations: true

stopping:
  maxWallClockSeconds: 86400
  earlyStopPolicy:
    implementation: builtin.metric_plateau
    config:
      metric: episode_return
      patienceEvaluations: 5
      minDelta: 0.01

extensions: {}
```

## 11. Minimal Validation Rules

A compliant `StudySpec` validator should at minimum enforce:

1. `apiVersion` and `kind` are present.
2. `target`, `objective`, `evaluationScope`, `controllers`, `engines`, `execution`, `evidence`, `reproducibility`, and `stopping` are present.
3. all referenced controller and engine IDs are unique.
4. all referenced implementations are registered or resolvable.
5. access and mutation policies are valid enum values.
6. resource profiles are structurally valid.
7. sandbox specs are structurally valid.
8. objective metrics referenced by constraints exist.
9. declared artifact kinds are compatible with their materialization plans.
10. backend-specific config is only validated by the selected backend implementation, not by the core schema.

That last point is important. The core schema should validate stable structure, while pluggable components validate their own `config` payloads.

## 12. Summary

`StudySpec` should be the stable, declarative contract for OptPilot studies.

It should let the platform define:

- what target is being optimized
- what artifact is being improved
- how controllers and engines are composed
- what resources and sandboxing are required
- how observations and evidence are captured
- how reproducibility is enforced

And it should do so without hard-wiring one LLM framework, one RL system, one BO library, or one execution backend into the platform itself.
