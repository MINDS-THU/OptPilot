# OptPilot Platform Design and Roadmap

Historical note: this roadmap predates the current v3alpha public config
contract. Use [`config_files_v3alpha.md`](config_files_v3alpha.md) for the
release-facing schema and treat this file as design background.

## 1. Purpose

OptPilot is a platform for defining, running, tracking, and reproducing optimization studies over target environments.

The project should not become a monolithic optimizer, an LLM agent framework, an RL framework, a Bayesian optimization package, a cluster scheduler, or a sandbox runtime. Its job is to provide the stable protocol and evidence layer that lets those tools be used safely and consistently.

The core platform loop is:

```text
Controller decides -> Engine or artifact action is produced -> Trial runs -> Observation is stored -> Evidence informs future decisions
```

The platform must make this loop reproducible, inspectable, and extensible across many optimization styles.

## 2. Architectural Boundary

OptPilot owns the following:

- study specification and validation
- target environment abstraction
- artifact and materialization abstraction
- controller and engine integration interfaces
- trial specification and execution lifecycle
- observation normalization
- evidence and artifact storage interfaces
- lineage and provenance records
- resource and sandbox declarations
- access and mutation policy declarations and enforcement hooks
- failure/status normalization
- resumability and branching metadata

Users or external packages own the following:

- Bayesian optimization algorithms
- meta-heuristic algorithms
- reinforcement learning trainers
- LLM agent loops
- model providers and prompt orchestration frameworks
- cluster schedulers
- container runtimes and strong sandboxing systems
- domain simulators and target environments

OptPilot may include small reference implementations for smoke tests and examples, but these should not be presented as production optimization stacks.

## 3. Core Concepts

### 3.1 Study

A `Study` is one controlled optimization effort. It defines:

- target environment
- objective
- evaluation scope
- artifact definitions
- controllers
- engines
- execution backend
- resource policy
- sandbox policy
- evidence policy
- reproducibility policy
- stopping rules

The `Study` is broader than an optimizer. It captures the whole experimental protocol.

### 3.2 Target Environment

A `TargetEnvironment` is the protected system being evaluated. It may be:

- a Python callable evaluator
- a CLI program
- a service endpoint
- a Gym/OpenEnv-style interactive environment
- a compound evaluation harness

The target environment is not the optimizable artifact. It should generally be read-only from the study’s point of view unless a policy explicitly allows otherwise.

### 3.3 Optimizable Artifact

An `OptimizableArtifact` is the versioned object being improved. Examples:

- parameter specification
- code module
- policy checkpoint
- training specification
- reward function
- workflow graph
- hybrid bundle

The artifact interface should remain stable:

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

The platform should treat artifact kinds as data, not as a rigid inheritance hierarchy.

### 3.4 Materialization

A `MaterializationPlan` turns an optimizable artifact into a runnable payload.

Examples:

- write parameter values into a config file
- inject candidate-owned code into a workspace
- restore a checkpoint
- assemble multiple files into a runtime directory
- produce CLI flags or service request payloads

Materialization is a core OptPilot responsibility because it separates optimization representation from execution representation.

### 3.5 Controller

A `Controller` decides what should happen next in a study.

Examples:

- launch an engine
- select an engine
- stop the study
- branch the study
- update allowed engine configuration
- retrieve evidence and use it to decide next actions

An LLM agent is one possible controller implementation, but OptPilot should not require or implement one specific LLM framework.

### 3.6 Engine

An `Engine` is a user-pluggable search, training, or transformation component.

Examples:

- an Optuna-based Bayesian optimization engine
- a BoTorch engine
- a DEAP evolutionary search engine
- an RLlib trainer
- a code mutation engine
- a validation engine

The platform should define the engine interface and record engine state/provenance. The actual algorithm belongs to users or external packages.

The general V3 engine lifecycle should support:

```python
class Engine:
    def start(self, engine_input) -> str: ...
    def poll(self, handle) -> dict: ...
    def intervene(self, handle, action) -> None: ...
    def finalize(self, handle) -> list[dict] | dict: ...
```

In the current implementation, lifecycle `finalize` returns candidate artifacts either directly as a list or under an `artifacts` key.

The current MVP also supports a simpler synchronous proposal interface:

```python
class CandidateProposalEngine:
    def propose(self, n_candidates, study_state) -> list[dict]: ...
    def observe(self, observations) -> None: ...
```

That simpler interface is useful for reference engines and lightweight user examples, but the full platform should grow toward the lifecycle form.

### 3.7 Trial

A `Trial` is one bounded execution unit. Trial shapes should eventually include:

- `AtomicTrial`: one artifact evaluated once
- `BatchTrial`: one artifact evaluated across multiple instances or seeds
- `CompoundTrial`: a longer process such as RL training, BO batches, or nested solver evaluation

The platform should not assume every trial is one simulator call.

### 3.8 Observation

An `Observation` is the normalized outcome of a trial.

It should include:

- trial id
- study id
- artifact id
- target id
- instance descriptor
- status
- metrics
- constraints
- resource usage
- produced artifacts
- event summary
- provenance

Failures are observations too. Invalid artifacts, target errors, timeouts, and partial runs should be stored as evidence rather than only raising exceptions.

### 3.9 Evidence Store

The evidence store is the persistent memory of the platform. It should store:

- study specs
- run policy snapshots
- artifacts
- materialization records
- validation records
- trials
- observations
- controller decisions
- engine state snapshots
- logs
- traces
- prompt/model records when relevant
- resource and sandbox records
- lineage edges

The evidence store should eventually support both write and query APIs so controllers can retrieve prior evidence.

## 4. Core Project Features

These should be part of OptPilot itself.

| Area | Feature |
|---|---|
| Specification | `StudySpec` schema, loading, validation, defaults |
| Orchestration | Study runner and control loop |
| Interfaces | Controller, engine, adapter, scheduler, backend, materializer, validator, evidence store |
| Artifacts | Artifact model, lineage, generator records, validation records |
| Execution | Trial model, scheduler handoff, backend execution, status normalization |
| Observations | Metrics, constraints, artifacts, event summaries, provenance |
| Evidence | Local evidence store, artifact store, scheduler events, query interface |
| Policies | Access policy, mutation policy, sandbox spec, resource profile |
| Reproducibility | Seeds, dependency/environment snapshots, backend identity |
| Failure Handling | Invalid, failed, timeout, partial, cancelled statuses |
| Evaluation Scope | FixedInstance, InstanceSet, Distribution, Curriculum |
| Objectives | Primary metrics, direction, constraints, aggregation, cost model |
| Reference Components | Local scheduler, local backend, Python adapter, CLI adapter, reference random search |

## 5. External Integrations and Examples

These should be demonstrated as user-owned integrations rather than core algorithm implementations.

| Capability | Existing Tools | OptPilot Role |
|---|---|---|
| Bayesian optimization | Optuna, Ax, BoTorch, scikit-optimize | Example user engine |
| Meta-heuristics | Nevergrad, DEAP, pymoo, scipy optimize | Example user engine |
| RL training | Ray RLlib, Stable-Baselines3, CleanRL | Example lifecycle engine |
| LLM controllers | LangGraph, OpenAI Agents SDK, custom loops | Example controller |
| Gym/OpenEnv targets | Gymnasium, OpenEnv | Optional adapter/example |
| Service targets | HTTP, FastAPI, gRPC | Generic service adapter |
| Docker execution | Docker SDK or CLI | Backend integration, not custom runtime |
| Kubernetes execution | Kubernetes client, Argo, Ray Jobs | Backend integration |
| SLURM execution | sbatch/sacct/scancel | Backend integration |
| Ray execution | Ray APIs | Backend integration |
| GPU scheduling | Kubernetes, Ray, SLURM, NVIDIA tools | Declare and record; delegate enforcement |
| Strong sandboxing | Docker, Podman, gVisor, Firecracker, Kata | Sandbox provider integration |
| Dependency snapshots | pip, uv, conda, poetry, Docker image metadata | Normalize and record |

## 6. Current MVP Status

The current implementation supports:

- YAML `StudySpec` loading and validation
- one controller selecting one engine
- synchronous candidate proposal through `propose/observe`
- lifecycle engines through `start/poll/intervene/finalize`
- user-owned engines loaded by `python:module:Class`
- Python callable target adapter
- CLI target adapter with JSON handoff and stdout/stderr evidence
- FixedInstance, InstanceSet, and simple Distribution evaluation scopes
- artifact normalization
- bounds validation
- passthrough parameter materialization
- controller evidence views
- engine snapshot evidence
- local scheduler with scheduler event evidence
- local threaded backend
- local JSONL/file evidence store
- run policy snapshot
- normalized success, invalid, failed, timeout, and partial observations

The current implementation does not yet support:

- controller graphs or multiple controllers
- real sandbox enforcement
- real Docker, Kubernetes, SLURM, or Ray backends
- GPU placement or reservation
- Gym/OpenEnv stepwise environments
- service-backed targets
- curriculum evaluation scope
- schema-based validation of target inputs and outputs
- dependency or environment snapshots
- study resume and branching
- prompt/model provenance for LLM controllers

## 7. Design Rules

1. Prefer platform contracts over algorithm implementations.
2. Keep algorithm ecosystems pluggable.
3. Treat every produced file and event as evidence.
4. Make policy declarations explicit and eventually enforceable.
5. Do not advertise a backend or adapter as built-in unless it genuinely enforces its stated contract.
6. Keep reference components small and clearly labeled as examples or smoke-test fixtures.
7. Failures should be normalized into observations whenever possible.
8. User-owned code should be loaded through explicit component hooks.
9. Study specs should describe composition and policy, not framework-specific internals.
10. Provenance should be rich enough for audit and rerun.

## 8. Implementation Roadmap

### Phase 1: Stabilize MVP Contracts

Goal: make the current one-shot study loop reliable and honest.

Tasks:

- finish `StudySpec` validation for core fields
- add schema or dataclass normalization for top-level spec sections
- keep `builtin.reference_random_search` clearly marked as a fixture
- remove unsupported built-in aliases
- record run policy snapshots
- normalize failures into observations
- persist artifact validation and materialization evidence
- add evidence query primitives

Acceptance criteria:

- all runs produce `study_spec.json`, `run_policy.json`, `artifacts.jsonl`, `trials.jsonl`, `observations.jsonl`, and `summary.json`
- invalid and failed trials do not crash the study by default
- user-owned synchronous engines work through `python:module:Class`

### Phase 2: Evidence Retrieval and Controller Context

Goal: make evidence usable by controllers, not only stored after the fact.

Tasks:

- add `EvidenceView` abstractions
- add APIs to query trials, observations, artifacts, and summaries
- expose summary metrics and failure views to controllers
- persist controller decision context
- add tests for controller access to evidence views

Acceptance criteria:

- a controller can inspect prior observations through a platform API
- controller decisions record what evidence they used

### Phase 3: Full Engine Lifecycle

Goal: support long-running and compound user-owned engines.

Tasks:

- implement lifecycle engine protocol: `start`, `poll`, `intervene`, `finalize`
- add an adapter for simple `propose/observe` engines
- define `EngineHandle` and `EngineStateSnapshot`
- record engine lifecycle events
- support engine-level failures and partial results

Acceptance criteria:

- a fake long-running user engine can be started, polled, finalized, and recorded
- existing simple proposal engines still work through compatibility wrapper

### Phase 4: Execution Backends and Sandboxing

Goal: separate trial scheduling from local Python execution.

Tasks:

- split evaluator, scheduler, worker, and backend responsibilities
- create a local subprocess backend
- enforce trial timeouts at backend level
- record assigned resources
- add retry policy support
- add optional Docker backend integration only if it actually enforces the declared sandbox contract

Acceptance criteria:

- local subprocess trials capture stdout, stderr, return code, timeout, and workspace artifacts
- unsupported sandbox specs fail clearly
- retry policy is recorded and tested

### Phase 5: Target Adapter Expansion

Goal: support more target shapes while preserving the common observation model.

Tasks:

- harden CLI adapter
- add HTTP service adapter
- add optional Gymnasium/OpenEnv adapter example
- define batch and compound target adapter conventions
- add schema validation for adapter results

Acceptance criteria:

- Python, CLI, and service targets can run equivalent toy studies
- adapter failures normalize into observations

### Phase 6: Reproducibility and Resume

Goal: make studies auditable and resumable.

Tasks:

- record dependency snapshots
- record environment snapshots
- derive per-trial seeds deterministically
- support loading prior evidence stores
- support resume from existing run directory
- support study branch metadata

Acceptance criteria:

- a study can resume without losing lineage
- repeated runs with the same seed and spec are explainably reproducible

### Phase 7: Integration Examples

Goal: show how mature tools plug into OptPilot without moving them into core.

Examples:

- Optuna user engine
- Nevergrad or DEAP user engine
- LangGraph or OpenAI-controller example
- Stable-Baselines3 or RLlib lifecycle engine example
- Gymnasium/OpenEnv target adapter example
- Docker/Kubernetes/Ray backend examples

Acceptance criteria:

- each example lives outside core platform modules or in optional integration packages
- each example produces normal OptPilot evidence

## 9. Near-Term Next Steps

The next implementation pass should focus on Phase 1 and Phase 2:

1. Add retry policy and backend-level timeout handling around scheduler events.
2. Continue separating scheduler, backend, worker, and evaluator responsibilities.
3. Add backend worker metadata and assigned-resource records.
4. Define lifecycle engine checkpoint conventions.
5. Add examples for Optuna, Gymnasium/OpenEnv, and LLM-code engines as user-owned integrations.

This is the best next step because it strengthens OptPilot as a platform without prematurely implementing any specific optimization algorithm.
