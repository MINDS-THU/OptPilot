# OptPilot V3: Platform Interfaces for Environment-Driven Optimization

Historical note: this document defines the product goals that motivated the
current implementation. The release-facing config contract is documented in
[`config_files_v3alpha.md`](config_files_v3alpha.md).

## 1. Purpose

OptPilot is a platform for defining and running optimization studies over environments. Its purpose is not to hard-code one optimizer, one LLM stack, or one environment protocol. Its purpose is to provide the interfaces, execution model, tracking model, and reproducibility model that allow many optimization paradigms to run against a common target abstraction.

The platform should support workflows such as:

- direct parameter search over a simulator
- Bayesian optimization over structured inputs
- meta-heuristic search
- reinforcement learning with rollout workers
- LLM-guided code evolution
- LLM-supervised RL or Bayesian optimization
- nested optimization, where the artifact under study is itself a solver, trainer, or workflow

The key architectural goal is flexibility without loss of rigor. Users should be free to plug in their preferred LLM agent framework, RL framework, BO package, and runtime backend, while OptPilot remains the stable layer that defines how studies are described, executed, tracked, and reproduced.

## 2. What OptPilot Owns

OptPilot should standardize the following:

- the study abstraction
- the target environment abstraction
- the artifact and materialization abstraction
- the observation and evidence model
- the lineage and provenance model
- the controller and engine integration interfaces
- the trial specification model
- the resource declaration model
- the sandbox policy model
- the execution backend interface
- the reproducibility and audit model

OptPilot should not require users to adopt one specific:

- LLM vendor
- agent framework
- RL training framework
- Bayesian optimization library
- cluster scheduler
- container or sandbox implementation

That division of responsibility is central. OptPilot should standardize protocol and evidence, not lock users into one algorithm stack.

## 3. Design Principles

1. Separate orchestration from optimization logic.
2. Separate optimization representation from execution representation.
3. Treat all produced outputs as first-class evidence, not just final scores.
4. Make permissions and mutation boundaries explicit.
5. Make parallel execution resource-aware.
6. Preserve reproducibility across studies, trials, and artifacts.
7. Keep platform primitives stable and algorithm labels soft.

## 4. Core Architectural View

The right top-level abstraction is a `Study`.

A `Study` is a controlled search or training effort over a target environment under a defined objective, scope, budget, policy envelope, and execution policy. A study may use one or more controllers and one or more engines. This lets the platform represent all of the following without changing its core abstractions:

- simple single-layer search
- LLM controller over a Bayesian optimization engine
- LLM controller over an RL trainer
- code evolution where the evolving code is the optimizable artifact
- outer-loop evaluation of an inner solver or workflow

The stable conceptual loop is:

`Controller decides -> Artifact or Engine is updated -> Trial runs -> Observations are captured -> Evidence is stored -> Controller reacts`

## 5. Primary Abstractions

### 5.1 Study

A `Study` is the top-level specification and state of one optimization effort.

A study should define:

- target environment reference
- objective definition
- evaluation scope
- controller graph or controller stack
- engine definitions
- execution policy
- resource policy
- sandbox policy
- storage policy
- reproducibility policy
- stopping rules and budget rules

A study is broader than an optimizer. It captures the whole experimental protocol.

### 5.2 Target Environment

A `TargetEnvironment` is the system against which performance is measured. It may be a simulator, an interactive environment, a service-backed evaluation harness, or a compound evaluation workflow.

A target environment should define:

- `target_id`
- `target_version`
- `adapter_type`
- `instance_schema`
- `observation_schema`
- `runtime_contract`
- `access_policy`
- `mutation_policy`

The target environment is the protected reference system. It is not the same thing as the optimizable artifact.

### 5.3 Evaluation Scope

An `EvaluationScope` defines what population of instances a study optimizes against.

Supported modes should include:

- `FixedInstance`
- `InstanceSet`
- `Distribution`
- `Curriculum`

This makes the platform usable for both single-instance optimization and generalization-oriented optimization.

### 5.4 Objective

An `Objective` defines how study performance is judged.

It should support:

- primary metric
- optional constraints
- optional secondary metrics
- aggregation over seeds, instances, or episodes
- cost-aware scoring, such as reward adjusted by compute cost or latency

### 5.5 Optimizable Artifact

An `OptimizableArtifact` is the versioned object being improved by the study.

It should contain:

- `artifact_id`
- `artifact_kind`
- `spec`
- `lineage`
- `generator_record`
- `validation_rules`
- `materialization_plan`

Examples of artifact kinds include:

- parameter specification
- code module
- policy checkpoint
- training specification
- reward function
- search-space definition
- workflow graph
- hybrid bundle of code, parameters, and learned assets

The platform should treat artifact kinds as typed data, not as a rigid inheritance hierarchy.

### 5.6 Materialization Plan

A `MaterializationPlan` defines how an optimizable artifact becomes runnable.

Examples:

- convert parameter specs into config files or CLI flags
- inject study-owned code into a controlled workspace
- restore a checkpoint into an evaluator or trainer
- construct a training job package
- assemble multiple assets into one runtime payload

This abstraction separates the optimization representation from the execution representation.

### 5.7 Controller

A `Controller` is the decision-making component in a study. A controller decides what should happen next based on the current study state and accumulated evidence.

A controller may:

- propose a new artifact
- select or configure an engine
- inspect intermediate observations
- branch a study
- reallocate budget
- stop a run
- retrieve historical evidence
- revise configuration within allowed mutation policies

An LLM agent fits naturally here. OptPilot should not implement one required LLM agent framework. Instead, it should define a controller interface so users can integrate their preferred framework.

### 5.8 Engine

An `Engine` is an execution-capable search, training, or transformation mechanism that can operate under controller direction.

Examples include:

- Bayesian optimization engine
- evolutionary search engine
- RL training engine
- rollout engine
- code mutation engine
- prompt-construction engine
- validation engine

An engine is not always the top-level decision maker. In many important cases it is subordinate to a controller.

This distinction is what supports cases where an LLM configures, monitors, and adapts RL or BO rather than replacing them.

### 5.9 Trial

A `Trial` is one bounded execution unit inside a study.

Trial shapes should include:

- `AtomicTrial`: one materialization and one evaluation
- `BatchTrial`: one artifact evaluated across many instances or seeds
- `CompoundTrial`: one internally multi-step process such as RL training, Bayesian optimization batches, or solver evaluation

The platform should not assume that every trial is one simulator call.

### 5.10 Observation

An `Observation` is the normalized result of running a trial.

It should include:

- `trial_id`
- `study_id`
- `artifact_id`
- `target_id`
- `instance_descriptor`
- `status`
- `metric_values`
- `constraint_results`
- `resource_usage`
- `artifacts`
- `event_summary`
- `provenance`

Observations are the shared language between execution and control.

### 5.11 Evidence Store

An `EvidenceStore` is the persistent memory of the platform.

It should store:

- study definitions
- trial records
- observations
- artifact lineage
- trial lineage
- engine state lineage
- prompts and retrieved evidence
- logs and traces
- CSV outputs, SQL outputs, and tables
- checkpoints
- generated code
- controller decisions and decision context

This is first-class because evidence retrieval is part of the optimization loop, especially in LLM-guided workflows.

## 6. Platform Boundary: What Users Plug In

OptPilot should define interfaces, not one mandated implementation, for:

- controllers
- engines
- target adapters
- execution backends
- sandbox providers

This means a user should be able to bring:

- a LangGraph-based controller
- a custom OpenAI or Anthropic agent loop
- a Ray RLlib or Stable-Baselines trainer
- an Ax, BoTorch, Optuna, or custom BO engine
- a local Docker backend or a Kubernetes backend

As long as those components satisfy the OptPilot interfaces, the rest of the study model should remain unchanged.

## 7. Environment Flexibility and Adapter Model

The platform should not assume that every environment is Gym-like.

At minimum, the environment layer should support the following target shapes:

- stepwise interactive environment
- one-shot evaluator
- batch simulator
- service-backed environment
- compound evaluation harness

Accordingly, the platform should define a general `TargetAdapter` abstraction and then allow multiple concrete adapters such as:

- `PythonTargetAdapter`
- `CLITargetAdapter`
- `ServiceTargetAdapter`
- `GymLikeTargetAdapter`
- `CompoundTargetAdapter`

### 7.1 How OpenEnv Fits

OpenEnv appears useful as an optional adapter backend, especially for stepwise agentic or RL-style environments. Its Gym-like `reset`, `step`, and `state` model, typed actions and observations, and containerized deployment model align well with one important subset of environments.

However, OpenEnv should not define the core OptPilot environment abstraction because OptPilot also needs to support:

- one-shot simulations with no episode or action loop
- batch simulators that emit metrics, CSVs, or SQL outputs
- compound evaluation harnesses
- study-level tracking, lineage, and artifact retrieval beyond environment interaction

The right design is therefore:

- OptPilot owns the general environment interface
- OpenEnv is supported through an `OpenEnvTargetAdapter`
- other environment forms are supported through sibling adapters

## 8. Access and Mutation Policies

Permissions should be represented explicitly.

### 8.1 Access Policy

An `AccessPolicy` defines what a controller or engine may inspect.

Levels should include:

- `InvocationOnly`: the component knows how to call the target and receive outputs, but does not receive structured semantic schemas for inputs or outputs
- `SchemaAware`: the component additionally receives explicit structured definitions of inputs, outputs, constraints, and artifact schemas
- `TraceAware`: the component can inspect logs, intermediate files, traces, tables, and databases produced during execution
- `CodeAwareReadOnly`: the component can inspect environment source code but cannot modify it
- `FullStudyContext`: the component can access all study-owned evidence and artifacts

The key distinction between `InvocationOnly` and `SchemaAware` is that `InvocationOnly` still includes a minimal operational contract. Without that, the system would not be usable.

### 8.2 Mutation Policy

A `MutationPolicy` defines what a component may change.

Mutation scopes may include:

- `NoMutation`
- `StudyArtifactOnly`
- `StudyWorkspaceOnly`
- `EngineConfigOnly`
- `ControllerConfigOnly`

Visibility and mutability must remain separate concepts.

## 9. Resource-Aware Execution Model

Parallelism is not just a worker pool problem. The platform must explicitly model resources, placement, isolation, and runtime supervision.

### 9.1 Resource Profile

Every trial should declare a `ResourceProfile` that includes, as applicable:

- CPU cores
- memory
- GPU count
- GPU type or capability constraints
- local disk or scratch requirements
- wall-clock timeout
- network requirements
- container image or runtime environment requirements

This resource declaration is necessary for scheduling and reproducibility.

### 9.2 Sandbox Spec

Every trial should declare a `SandboxSpec` that defines its execution boundary.

It should include:

- runtime type, such as process, container, or stronger isolation backend
- mounted inputs
- writable workspace
- read-only mounts for protected target assets
- network policy
- environment variables
- cleanup policy

This is especially important for studies involving generated code or long-running training jobs.

### 9.3 Execution Backend

OptPilot should not implement low-level scheduling and sandboxing from scratch. It should define an `ExecutionBackend` interface and delegate actual execution to existing infrastructure.

Candidate backends may include:

- local subprocess backend
- Docker or Podman backend
- Kubernetes backend
- SLURM backend
- Ray-backed execution backend

OptPilot should decide what a trial needs and record what happened. The backend should decide how to launch, isolate, place, and supervise the actual runtime.

## 10. GPU and Parallel RL Management

GPU-heavy RL workloads require more structure than generic parallel execution.

A compound RL trial may include:

- one trainer process or container with GPU resources
- multiple rollout workers using CPU or mixed CPU and GPU resources
- separate evaluation workers for unbiased benchmarking

This means the scheduler must distinguish at least three kinds of parallelism:

- candidate parallelism: many artifacts evaluated simultaneously
- rollout parallelism: many environment executions for one artifact or policy
- engine parallelism: many long-running engines active concurrently

The resource model should support whole-GPU reservation as the default. More complex policies such as fractional GPU scheduling or MIG should be optional later extensions, not part of the minimal core.

Assigned resources, including actual GPU model and count, should be recorded in observation provenance.

## 11. Sandboxing Strategy

OptPilot should not implement its own low-level sandbox technology unless there is a very strong reason.

The platform should instead define a `SandboxProvider` abstraction and rely on existing mechanisms such as:

- subprocess isolation for lightweight local runs
- Docker or Podman for container isolation
- NVIDIA Container Toolkit for GPU-enabled containers
- Kubernetes for clustered container orchestration
- stronger isolation technologies such as gVisor, Firecracker, or Kata Containers if needed later

The platform responsibility is to declare sandbox policy and track its use. The runtime infrastructure responsibility is to enforce isolation.

For most practical early-stage research use, container-based isolation is the correct starting point.

## 12. Supporting LLM-Supervised RL and BO

The architecture must support the following cases naturally.

### 12.1 LLM supervises RL

- the optimizable artifact may be a training specification, reward definition, or policy initialization
- the RL trainer is an engine
- the LLM is a controller
- learning curves, checkpoints, rollout traces, and evaluation summaries become evidence
- the controller may revise engine configuration or artifacts between trials

### 12.2 LLM supervises Bayesian optimization

- the search space, surrogate settings, acquisition rules, and batching settings may be artifacts or engine config
- the BO engine runs under the controller
- the controller inspects progress and modifies priors, bounds, or stopping rules

### 12.3 LLM generates a new solver implementation

- the study artifact is code implementing a solver, trainer, or workflow
- the materialization plan packages that code into a study-owned runtime
- the generated solver is used in a subsequent trial
- the final observation measures target performance, robustness, and efficiency

### 12.4 Multi-stage orchestration

A controller may mix engines inside one study, for example:

1. run BO to identify a promising region
2. launch RL training in that region
3. evolve reward-shaping or control code
4. compare branches and reallocate budget

This should be represented as normal study behavior, not as a special case.

## 13. Data and Provenance Requirements

The lineage model should support:

- artifact lineage
- trial lineage
- study branching lineage
- engine state lineage
- controller decision lineage

For LLM-related flows, the platform should store:

- prompt inputs
- retrieved evidence identifiers
- model identity and configuration
- produced outputs
- acceptance or rejection decisions

For execution-related reproducibility, the platform should also store:

- assigned resources
- sandbox configuration
- backend identity
- environment version and dependency snapshot
- seeds and sampling descriptors

Without this information, optimization results are not auditable or scientifically defensible.

## 14. Configuration Model

Users should define studies declaratively through a `StudySpec`.

A `StudySpec` should include:

- target environment reference
- objective and constraints
- evaluation scope
- controller graph or controller stack
- engine definitions
- artifact definitions or templates
- access and mutation policies
- resource policies
- sandbox policies
- execution backend preferences
- stopping rules and budget rules
- reproducibility settings
- artifact retention rules

The key requirement is compositionality. The configuration model should describe how a study is assembled, not force the user to select from a few hard-coded optimization modes.

## 15. Minimal Stable Interfaces

The platform should keep only a small set of interfaces rigid.

### 15.1 Artifact Interface

```python
class OptimizableArtifact:
    artifact_id: str
    artifact_kind: str
    spec: dict
    lineage: dict
    generator_record: dict
    validation_rules: dict
    materialization_plan: dict
```

### 15.2 Controller Interface

```python
class Controller:
    def decide(self, study_state, evidence_view) -> list[Action]:
        ...
```

Actions may include proposing artifacts, launching engines, updating config, branching studies, or stopping runs.

### 15.3 Engine Interface

```python
class Engine:
    def start(self, engine_input) -> str:
        ...

    def poll(self, handle) -> dict:
        ...

    def intervene(self, handle, action) -> None:
        ...

    def finalize(self, handle) -> list[Observation]:
        ...
```

### 15.4 Execution Backend Interface

```python
class ExecutionBackend:
    def submit(self, trial_spec) -> str:
        ...

    def status(self, handle) -> dict:
        ...

    def cancel(self, handle) -> None:
        ...

    def collect(self, handle) -> list[Observation]:
        ...
```

### 15.5 Evaluator Interface

```python
class Evaluator:
    def run_trial(self, trial_spec) -> list[Observation]:
        ...
```

These interfaces are intentionally generic. Concrete implementations can differ without changing the platform model.

## 16. What Should Be Rigid and What Should Stay Soft

The following should be rigid:

- study
- target environment
- optimizable artifact
- materialization plan
- controller interface
- engine interface
- trial model
- observation model
- evidence store
- resource profile
- sandbox spec
- execution backend interface
- access and mutation policies

The following should stay soft and pluggable:

- artifact kinds
- controller implementations
- engine implementations
- LLM frameworks
- RL libraries
- BO packages
- environment adapters
- scheduler backends
- sandbox technologies
- algorithm labels such as RL, BO, meta-heuristic, or code evolution

This is the main extensibility rule. Keep platform primitives stable, and keep concrete algorithm ecosystems pluggable.

## 17. Recommended Internal Modules

A corresponding code structure could be:

- `optpilot.studies`
- `optpilot.targets`
- `optpilot.adapters`
- `optpilot.artifacts`
- `optpilot.controllers`
- `optpilot.engines`
- `optpilot.execution`
- `optpilot.sandbox`
- `optpilot.observations`
- `optpilot.storage`
- `optpilot.policies`
- `optpilot.specs`
- `optpilot.ui`

This module split follows ownership boundaries rather than algorithm categories.

## 18. Summary

OptPilot should be a platform for environment-driven optimization studies, not a monolithic optimizer and not a thin wrapper over one specific agent or RL framework.

Its core responsibility is to define:

- how studies are described
- how targets are adapted
- how artifacts are materialized
- how controllers and engines integrate
- how trials declare resources and sandbox needs
- how execution backends run work
- how observations, lineage, and evidence are stored

Within that model, users can plug in their own LLM agent frameworks, RL libraries, BO engines, and runtime infrastructure.

That design directly supports:

- one-shot simulators
- Gym-like interactive environments
- OpenEnv-backed environments through an adapter
- LLM-guided code evolution
- LLM-supervised RL or BO
- GPU-aware RL training
- rich artifact logging and retrieval
- reproducible and auditable optimization studies

This is the right level of abstraction for a platform that needs to remain open to future workflows without becoming vague about execution, evidence, or infrastructure responsibilities.
