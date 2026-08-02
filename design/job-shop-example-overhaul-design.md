# Job-Shop Example Package Redesign

Status: internal design draft. This page is not yet part of the public docs
navigation; decide whether to keep it as implementation planning material or
turn it into public documentation before adding it to `mkdocs.yml`.

This document redesigns the job-shop portion of `catalog/example_package` as a
clear tutorial rather than a broad inventory of possible OptPilot features. The
example should teach one coherent story:

1. What is the job-shop scheduling problem?
2. What does the OptPilot environment evaluate?
3. What candidate contracts can methods use to improve the metric?
4. Which methods are worth shipping because they either improve the result or
   teach a genuinely different integration pattern?

The goal is not to cover every configuration field. The goal is to make the
built-in package a trustworthy example users can learn from and adapt.

## Design Principle

Every public example method must earn its place.

A method should be included only if it satisfies at least one of these roles:

- `Optimizer`: it can improve the primary metric against a reference baseline
  under a fixed seed and reasonable budget.
- `Integration`: it teaches a real integration pattern that users commonly
  need, such as wrapping a command-line solver or passing a provider API key.
- `Reference baseline`: it exists only to define what "better" means. It should
  be clearly labeled as a baseline, not presented as an optimization method.

Do not include methods that only emit an unchanged candidate, duplicate another
method without teaching a new boundary, or exist only to tick a config-field
checkbox.

## The Problem To Teach

In job-shop scheduling, a case contains jobs. Each job is an ordered list of
operations. Each operation must run on a specific machine for a fixed duration.
A valid schedule chooses start times for all operations while respecting:

- job order constraints
- machine capacity constraints
- optional due-date or tardiness information

The evaluator should score each candidate schedule or scheduling policy on the
same validation cases. The primary tutorial objective should be easy to explain:
minimize makespan or a clearly named normalized/reference makespan metric.

Before publishing improvement claims, fix the current metric naming issue:
`normalized_makespan` is computed as `makespan / lower_bound`, but the bundled
case metadata currently uses denominators that are larger than feasible
schedules. That makes the score fall below `1.0`, which is misleading if the
denominator is called a lower bound. Either correct the lower bounds or rename
the denominator to something like `reference_makespan`.

## Upstream Decomposition

The job-shop package is not meant to invent a new scheduling stack. It should
show how OptPilot decomposes an existing open-source project, JobShopLib, into
environment-owned and method-owned pieces.

Environment-owned pieces:

- case loading and validation data
- job-shop dynamics or simulation interfaces
- candidate validation
- final metric computation
- evidence files such as schedules and per-case metrics

Method-owned pieces:

- dispatch-rule weight search
- CP-SAT solving
- local search or simulated annealing
- command-line wrappers around existing solvers
- RL policy training logic, using environment-owned cases and adapter code

The rule of thumb is simple: methods may use JobShopLib algorithms, but they
should not carry private copies of the environment dynamics when the point of
the example is to connect methods to an OptPilot environment.

## Tutorial Flow

The tutorial should move from simple to powerful:

1. Run a reference dispatch rule to establish the baseline score.
2. Tune the dispatch rule's numeric weights with OptPilot.
3. Replace the heuristic policy with a method that returns complete schedules.
4. Optionally show how to wrap an external command-line solver.
5. Optionally show provider-backed code editing as an advanced file-candidate
   example.

This flow teaches the same optimization problem through increasingly expressive
contracts. Users can see why the contract changes, not just that another YAML
file exists.

## Candidate Contracts

The package should focus on three contracts. Each contract answers the question:
"What does the method give back to the environment?"

### Contract 1: Dispatch Rule Weights

Use this when the environment already has a dispatch heuristic and the method
only chooses numeric weights.

Environment config:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      remaining_work_weight:
        valueType: float
      processing_time_weight:
        valueType: float
      machine_ready_weight:
        valueType: float
      job_ready_weight:
        valueType: float
```

The environment owns the heuristic implementation and evaluates different
weight settings. The method owns the search strategy.

This is the best first tutorial contract because it is dependency-free and
shows OptPilot doing actual optimization.

### Contract 2: Complete Schedules

Use this when the method is a solver, metaheuristic, learned policy, or wrapper
around another scheduling library.

Environment config:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      solutions:
        valueType: object
        properties: {}
```

Candidate payload:

```yaml
solutions:
  ft06_small:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 3
```

The method returns the full schedule for each validation case. The environment
validates feasibility and computes metrics. This keeps solver libraries out of
the environment and makes the environment-method boundary clean.

### Contract 3: Editable Scheduling Code

Use this only for methods that actually edit code, such as a provider-backed
LLM method or a real local code generator.

Environment config:

```yaml
candidate:
  format: files
  materialize:
    root: candidate
  files:
    editable:
      - path: dispatch_rule.py
```

The environment seeds a working source tree and imports the candidate's
`dispatch_rule.py`. The method returns a changed file bundle.

This contract should be advanced, not part of the first tutorial path. A method
that simply copies the baseline file should not be included as a public example
method.

### RL Training Context For Schedule-Solution Methods

Use this when a method must learn from environment-owned job-shop dynamics
before returning a final candidate.

This is not a fourth candidate contract. It is a schedule-solution method
pattern that needs extra environment-owned context before it can produce
`spec.solutions`.

Do not add a new top-level config field for this. The current configuration
model can express it with the existing schedule-solution candidate,
`methodContext.references`, and `capabilities`.

Target environment config shape:

```yaml
candidate:
  format: parameters
  parameters:
    schema:
      solutions:
        valueType: object
        properties: {}

methodContext:
  references:
    - name: train_tiny_a
      path: training_cases/train_tiny_a.yaml
      type: job_shop_training_case
      description: Training case for RL policy learning.
    - name: ft06_small
      path: cases/ft06_small.yaml
      type: job_shop_case
      description: Validation case also listed in evaluator.settings.cases.
    - name: rl_env_adapter
      path: rl_env_adapter.py
      type: python_module
      description: Environment-owned Gymnasium adapter factory loaded by path.

capabilities:
  - id: schedule-solution-candidate
  - id: job-shop-rl-training-context
```

The RL method then declares:

```yaml
accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
      - methodContext.references
    capabilities:
      - schedule-solution-candidate
      - job-shop-rl-training-context
```

Ownership is still clear:

- the environment owns the training cases, validation cases, adapter code,
  observations, actions, rewards, and final scoring semantics
- the method owns the RL algorithm, policy, hyperparameters, training loop, and
  checkpointing
- OptPilot checks the candidate format and top-level `solutions` parameter;
  the job-shop evaluator validates schedule structure and feasibility

That keeps RL comparable with CP-SAT and local search. The trained policy can
be saved by the method for debugging or reported in method events, but the
current public evidence config does not automatically retain arbitrary
method-created checkpoints. The environment evaluates the method-produced
rollout schedules through the same schedule-solution contract.

The `job-shop-rl-training-context` capability should be documented as a small
reference contract:

- at least one training reference named `train_*` with `type: job_shop_training_case`
- validation references with `type: job_shop_case` whose names mirror
  `evaluator.settings.cases[].id`
- one adapter reference named `rl_env_adapter` with `type: python_module`
- validation reference paths must mirror `evaluator.settings.cases[].path`

Compatibility can check the capability id, but the RL method should validate
these reference names and types at startup and fail with a clear error if the
context is incomplete.

## Proposed Method Set

Keep the public method set small and name methods by what they do. The first
tutorial path should contain only the reference run and two optimizers. The
gallery can then show additional method families without making the first read
feel like a catalog dump.

Reference run:

| Display name | Config id | Contract | Role | Why include it |
| --- | --- | --- | --- | --- |
| Reference Dispatch Rule | `reference_dispatch_rule` | Dispatch rule weights | Reference baseline | Defines the score that real methods try to beat. |

Core tutorial methods:

| Display name | Config id | Contract | Role | Public claim |
| --- | --- | --- | --- | --- |
| Tune Dispatch Weights | `tune_dispatch_weights` | Dispatch rule weights | Optimizer | First dependency-free optimizer; should improve over the reference baseline. |
| Solve With OR-Tools CP-SAT | `solve_with_ortools_cpsat` | Schedule solutions | Optimizer | Method-side solver returns `spec.solutions`; should match or improve over the reference baseline. |

Advanced method gallery:

| Display name | Config id | Contract | Role | Public claim |
| --- | --- | --- | --- | --- |
| Improve With Simulated Annealing | `improve_with_simulated_annealing` | Schedule solutions | Optimizer / metaheuristic | Include if it improves or behaves competitively on the tutorial cases. |
| Train Policy, Return Schedules | `train_policy_return_schedules` | Schedule solutions plus RL training context | Required learning pattern | Uses environment-owned training context from `methodContext.references`; improvement claim only if the default budget is competitive. |
| Wrap CLI Scheduler | `wrap_cli_scheduler` | Schedule solutions | Integration pattern | Shows how to connect an existing executable through command JSON I/O. |
| Edit Dispatch Rule With LLM | `edit_dispatch_rule_with_llm` | Editable scheduling code | Optional advanced pattern | Provider-backed file editing; no improvement claim unless a multi-trial study actually edits code. |

This set demonstrates parameter optimization, solver integration, local search,
RL training from environment context, command wrapping, and optional file/code
editing without drowning users in redundant method families.

## How Reinforcement Learning Fits

RL is worth keeping because it teaches a method pattern that is different from
grid search, CP-SAT, and local search. The method trains a policy before it can
submit a useful candidate.

The important boundary is not "OptPilot calls one trial per RL step." That
would be too coarse. The important boundary is that the training dynamics,
training cases, validation cases, and adapter code are environment-owned and
method-visible through the existing config model.

The existing Stable-Baselines example trains inside the method runtime. That is
fine. What should change is where the training environment comes from: it should
come from environment-owned references, not from hidden method-owned copies of
the job-shop dynamics.

Implemented package shape:

- Training cases, validation cases, and the Gymnasium adapter live in the
  environment source and are exposed to the method through
  `methodContext.references`.
- The Stable-Baselines wrapper reads those references from
  `study_state["candidate_context"]`.
- The method settings contain PPO and rollout settings only; they do not list
  hidden method-owned training case paths.

### Correct RL Boundary

For a proper OptPilot RL example using the current config model:

- The environment owns the job-shop dynamics adapter, training cases,
  validation cases, and final scoring.
- The method owns the RL algorithm, policy architecture, hyperparameters,
  training loop, and checkpointing.
- OptPilot passes the environment-owned adapter and cases through
  `methodContext.references`.
- The final candidate is the `solutions` object produced by rolling out the
  trained policy.

This mirrors the upstream decomposition: JobShopLib contains environment-like
pieces such as Gymnasium-compatible job-shop environments and method-like pieces
such as solvers, dispatching rules, metaheuristics, and RL workflows. The
OptPilot example should split those pieces along the environment/method
boundary instead of letting a method carry its own hidden copy of the
environment.

### Candidate Choice For RL

For the first implementation, use the existing schedule-solution candidate.
The RL method returns rollout schedules:

```json
{
  "candidate_id": "rl-policy-rollout-001",
  "format": "parameters",
  "spec": {
    "solutions": {
      "ft06_small": {
        "operations": []
      }
    }
  },
  "generator": {"method_id": "train_policy_return_schedules"}
}
```

This keeps RL comparable with CP-SAT and local-search methods because all of
them submit finished schedules. The trained policy or checkpoint does not need
to be the candidate. The method may write a checkpoint for debugging and report
it in method events, but current public evidence settings do not automatically
retain arbitrary method artifacts. Returning a policy artifact as a file
candidate is a possible later example, but it is not needed to support RL in
the current config model.

### Why RL Is Usually Not `session`

RL has an action-observation loop, but that loop is internal to the method's
training procedure. The method can run that loop against the environment-owned
adapter it receives through `methodContext.references`. OptPilot's current
`session` protocol is for a method that actively submits OptPilot candidates and
observes completed trial results. Using one OptPilot trial per RL action would
be far too coarse.

RL could use `session` for a different purpose: train a policy, submit it or
its rollout schedules, observe validation metrics, then adjust hyperparameters
or continue training for another OptPilot candidate. That is meta-optimization
around RL, not the per-step RL loop itself.

### Required Implementation For This Package

RL should be included in the first redesign because it tests a method family
that does not fit simple one-shot candidate generation. The example should
prove that OptPilot can pass environment-owned training context to a learning
method, not avoid the problem.

Implementation requirement:

- move the JobShopLib/Gymnasium adapter code into the environment package, or
  otherwise make it an environment-owned reference
- expose training cases, validation cases, and the adapter through
  `methodContext.references`
- add a capability such as `job-shop-rl-training-context`
- update the Stable-Baselines method so training and rollout use those
  environment-owned references
- keep final scoring in the environment evaluator
- if a checkpoint is useful, save it from the method and report its path in a
  method event; do not imply current public evidence settings automatically
  retain method-created artifacts

This stays inside the current config design.

## What To Remove Or Hide

Remove from the public method gallery:

- `baseline_file_copy`: it does nothing. Keep a baseline source tree if needed,
  but do not present file copying as an example method.
- One-candidate "fixed" methods, except as reference baselines used by docs and
  tests.
- Random search, unless deterministic grid/coordinate search is already present
  and random search has a clear teaching purpose.
- JobShopLib dispatching if it only duplicates the reference dispatch heuristic
  without teaching a new contract.
- RL examples that only say "train for a few steps and hope." RL itself should
  stay in the required method set, but its docs must explain what is being
  trained, what the candidate is, how rollout uses the environment-owned
  training context, and whether the default budget is meant to optimize or only
  demonstrate the learning interface.
- Fake retry, opaque candidate, placeholder interface, or SQLite examples whose
  main purpose is config coverage.

The package can still contain internal smoke tests or reference studies, but
the public docs should focus on methods that move the user's understanding
forward.

## Study Sequence

### Study 0: Reference Baseline

Purpose: define what the methods are trying to beat.

This can use a tiny baseline method or a built-in reference candidate, but docs
should label it as a reference baseline.

Important outputs:

- per-case makespan
- aggregate objective score
- secondary metrics such as tardiness and utilization

### Study 1: Tune Dispatch Rule Weights

Purpose: the first real optimization loop.

Method behavior:

- reads `candidate.parameters.schema`
- proposes a deterministic grid or coordinate-search batch
- uses `budget.maxTrials > 1`
- uses `execution.parallelism` only if it proposes multiple candidates

Why it works as teaching:

- no API key
- no solver dependency
- no file editing
- clear before/after metric comparison

### Study 2: Solve Schedules With CP-SAT

Purpose: show that methods can be complete solvers.

Method behavior:

- reads validation cases from `methodContext.references`
- builds a CP-SAT model
- returns complete schedules through the schedule contract

Why it works as teaching:

- solver code stays method-side
- the job-shop evaluator validates schedule structure, feasibility, and metrics
- users see how external algorithms connect without changing the environment

### Study 3: Improve Schedules With Local Search

Purpose: show a different optimization style without changing the environment.

Method behavior:

- starts from a simple feasible schedule or dispatch-rule schedule
- performs local search, simulated annealing, or another bounded heuristic
- returns complete schedules

Include this only if it gives a meaningful comparison with CP-SAT and the
reference baseline. If it cannot improve on the small cases, add a slightly
harder but still fast validation case.

### Study 4: Wrap Command-Line Scheduler

Purpose: teach users how to connect an existing executable.

Method behavior:

- `entrypoint.command: [python, scheduler.py, "{input_file}", "{output_file}"]`
- `entrypoint.protocol: batch`
- reads OptPilot's method request JSON
- writes a candidate response JSON
- returns candidates with `spec.solutions`

This should be included because many real methods start as command-line tools.
It should not be a toy script that prints a hard-coded answer.

### Study 5: Train RL Policy From Environment Context

Purpose: teach a learning method that trains from environment-owned cases and
adapter code exposed through `methodContext.references`.

Method behavior:

- `entrypoint.protocol: batch`
- reads training cases, validation cases, and RL adapter code from
  `methodContext.references`
- trains a policy inside the method runtime using the environment-owned adapter
- rolls out the trained policy with that adapter and returns method-produced
  schedule solutions as the candidate
- may write a checkpoint for debugging and report it through a method event
- lets the OptPilot environment perform final validation and scoring of the
  submitted schedules

Why it is useful:

- RL has a real internal action-observation loop.
- The candidate boundary becomes clear: RL training is not the same
  thing as final candidate evaluation.
- Users can later replace the training cases, policy class, checkpoint, or
  rollout budget while keeping the same environment boundary.

This is a required example for the redesign. It may be labeled advanced because
of its dependencies and runtime cost, but it should not be deferred out of the
package. Its purpose is to prove that OptPilot can support learning methods
that need environment-owned training context before submitting a final
candidate.

### Optional Advanced Study: Edit Dispatch Rule With LLM

Purpose: teach file candidates and provider-backed code editing.

Method behavior:

- receives `trialWorkspace` with editable `dispatch_rule.py`
- receives `methodContext.instructions`
- uses `envFromHost` for the provider API key
- runs with enough trials to actually edit code, not just emit the baseline

This should be optional. It is useful, but it should not be required for a
smooth first-run experience.

## Config Features This Example Should Teach

Main tutorial:

- package layout under `catalog/`
- environment, method, and study configs
- evaluator settings and validation cases
- returned metrics and secondary metrics
- parameter candidate schema
- candidate constraints, if there is a domain-motivated weight constraint
- study objective, aggregation, trial budget, timeout, seed, and evidence level
- method-visible references for schedule solvers
- output files for schedules and metrics
- the existence of the RL training-context contract, even if the first tutorial does
  not run the RL study

Method gallery:

- component-local `runtime.setup` for solver dependencies
- command method entrypoint
- `execution.parallelism` for multi-candidate parameter search
- `evidence.outputFileStorage: copy` for small useful artifacts
- optional file candidates, `trialWorkspace`, `methodContext.instructions`, and
  `envFromHost`
- environment-owned RL training context through `methodContext.references`

Do not force into job-shop:

- resource interfaces
- container runtime
- retry behavior
- SQLite metrics or records
- opaque candidates
- every possible parameter value type
- placeholder environment or method interfaces

Those can be covered by a separate technical package when they have real
examples.

## Implementation Plan

### Phase 0: Fix Metric Credibility

- Decide whether the denominator is a true lower bound or a reference makespan.
- Correct the case metadata or rename the metric.
- Add a small validation case if the current cases leave too little room for
  methods to separate.
- Record expected baseline and optimizer smoke results under fixed seeds.

### Phase 1: Simplify The Environment Contracts

- Keep the dispatch-weight contract.
- Keep the complete-schedule contract.
- Keep one editable-code contract only if the LLM/code-editing tutorial remains
  useful.
- Remove public emphasis on duplicate dispatch-rule-file and solver-code-file
  baselines unless they teach a distinct workflow.

### Phase 2: Rename And Build The Core Methods

Target public method ids:

- `tune_dispatch_weights`
- `solve_with_ortools_cpsat`
- `improve_with_simulated_annealing`
- `train_policy_return_schedules`
- `wrap_cli_scheduler`
- optional `edit_dispatch_rule_with_llm`

Each method should have:

- a clear README section
- one study
- a validation command
- a smoke-run result expectation
- a short explanation of what the method can see and what it returns

### Phase 3: Move Dependencies Into Component Runtime

- Current state: JobShopLib, OR-Tools, Stable-Baselines, and related example
  dependencies are still installed through project-level optional extras.
- Put solver requirements in the method folder.
- Add method-local `runtime.setup`.
- Keep global `uv sync --extra examples` only as a developer convenience.
- Do not advertise a method as smooth until its local setup path is tested with
  an actual run, not only config validation.

### Phase 3b: Refactor RL To Use Environment-Owned Context

- Status: implemented in the example package.
- Move the JobShopLib/Gymnasium training adapter into the job-shop environment
  package.
- Expose training cases, validation cases, and the adapter through
  `methodContext.references`.
- Add an environment capability such as `job-shop-rl-training-context`.
- Update the RL method to discover those references from
  `study_state["candidate_context"]`.
- Keep `entrypoint.protocol: batch`.
- Return `solutions` as the final candidate.
- If a checkpoint is useful, save it from the method and report its path in a
  method event; do not rely on `evidence.outputFileStorage` for method-created
  checkpoints.
- Add smoke tests that prove the RL method is not using hidden method-owned
  copies of the environment dynamics.

### Phase 4: Rewrite Public Docs

Suggested docs:

- `docs/getting-started.md`: one no-dependency run.
- `docs/job-shop-tutorial.md`: problem, baseline, weight tuning, solver
  contract.
- `docs/job-shop-methods.md`: compact method table and commands.
- `docs/job-shop-environment.md`: evaluator and contract reference.
- method-family pages only for methods that remain in the package.

## Verification Standard

Every public study should pass:

```bash
uv run optpilot validate catalog/example_package/studies/<study>.yaml
uv run optpilot run catalog/example_package/studies/<study>.yaml --output-root /tmp/optpilot-example-check
```

Also run:

```bash
uv run python -m unittest tests.test_mvp
uv run python -m compileall src/optpilot catalog/example_package
git diff --check
```

For every method presented as an optimizer, keep a checked result table:

| Method | Baseline compared against | Cases | Seed | Budget | Expected outcome |
| --- | --- | --- | --- | --- | --- |
| Tune Dispatch Weights | reference dispatch rule | validation cases | fixed | small | improves aggregate or at least one case |
| Solve With OR-Tools CP-SAT | reference dispatch rule | validation cases | fixed | bounded time | matches or improves |
| Improve With Simulated Annealing | reference dispatch rule | validation cases | fixed | bounded time | include only if competitive |
| Train Policy, Return Schedules | reference dispatch rule | validation cases | fixed | documented timesteps | must use environment-owned references; improvement claim optional |

Do not promise exact numbers in public docs unless they are deterministic across
platforms and dependency versions. Do promise the qualitative role of each
method and verify that the claim is true in smoke tests.

## Recommended Decision

Rewrite the example package around a small, coherent job-shop story:

1. establish a reference dispatch baseline
2. improve it by tuning dispatch-rule weights
3. solve the same cases by returning complete schedules
4. show command-line wrapping as a real integration pattern
5. keep provider-backed file editing optional and advanced

This gives users a cleaner mental model: OptPilot does not care whether a
method is a parameter tuner, solver, local search, command-line tool, or LLM
editor. OptPilot only needs the method to satisfy the environment's candidate
contract.
