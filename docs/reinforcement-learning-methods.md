---
title: Reinforcement Learning Methods
description: How JobShopLib reinforcement-learning policies fit the job-shop example.
---

# Reinforcement Learning Methods

JobShopLib provides Gymnasium environments for job-shop scheduling, including graph-based single-instance and multi-instance environments. The upstream package exposes `SingleJobShopGraphEnv`, `MultiJobShopGraphEnv`, reward observers, observation helpers, and rollout utilities under `job_shop_lib.reinforcement_learning`.

In OptPilot, the trained policy belongs to the method. The job-shop evaluator does not load the policy and does not know that reinforcement learning was used.

The integration pattern is:

```text
OptPilot method receives study_state.instances
-> method creates a JobShopLib RL environment for each instance
-> method loads or constructs a policy
-> policy rolls out actions until the schedule is complete
-> method emits schedule-solution parameters
-> OptPilot evaluator validates and scores the schedules
```

## Runnable Example

The repository now includes a runnable RL method:

```text
examples/methods/job_shop_rl_stable_baselines/
```

Run it with the shared job-shop validation instances:

```bash
uv sync --extra examples
uv run optpilot validate examples/studies/job_shop_rl_stable_baselines.yaml
uv run optpilot run examples/studies/job_shop_rl_stable_baselines.yaml
```

The study uses `environment_schedule_solution.yaml`, so it is evaluated on the same `ft06_small` and `la01_tiny` instances and the same `normalized_makespan` objective as the JobShopLib solver methods.

The method trains on separate method-owned training samples:

```yaml
settings:
  algorithm: PPO
  trainInstances:
    - ../../environments/job_shop_scheduling/instances/train_tiny_a.yaml
    - ../../environments/job_shop_scheduling/instances/train_tiny_b.yaml
  maxJobs: 6
  totalTimesteps: 128
  seed: 0
```

`trainInstances`, `maxJobs`, `totalTimesteps`, and `algorithm` are method settings. OptPilot validates that `settings` is an object and passes it to the method; the RL method decides how to interpret those fields.

The example uses Stable-Baselines3 `PPO` with a small Gymnasium adapter around JobShopLib's single-instance RL environment. The adapter keeps the code focused on the OptPilot boundary: train on method-owned samples, roll out on `study_state.instances`, and emit schedule-solution parameters.

## What The Method Needs

An RL rollout method needs:

- the study instances from `study_state.instances`
- a policy implementation or policy checkpoint
- rollout settings such as deterministic sampling, maximum steps, and render mode
- JobShopLib and the policy framework dependencies in the method runtime

Checkpoint-rollout method settings:

```yaml
settings:
  policyPath: user_catalog/methods/job_shop_rl_policy/policy.zip
  deterministic: true
  maxSteps: 10000
  renderMode: null
```

## What The Method Produces

The method should produce the same schedule-solution candidate used by the other external solver methods:

```yaml
solutions:
  ft06_small:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 3
  la01_tiny:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 2
```

The environment validates the final schedule. It does not inspect the policy, reward function, neural network weights, or rollout trace unless the method explicitly saves those as evidence.

## Why This Uses Schedule Solutions

The trained policy is not the candidate being evaluated by the job-shop environment. The policy is part of the method that produces a schedule for each study instance. The schedule is the environment-facing candidate.

This keeps the boundary consistent with constraint programming and metaheuristics:

| Method family | Method-owned work | Environment-facing candidate |
| --- | --- | --- |
| Constraint programming | Build and solve a CP-SAT model. | Schedule solution. |
| Metaheuristics | Improve schedules through local search. | Schedule solution. |
| Reinforcement learning | Roll out a policy in a Gymnasium environment. | Schedule solution. |

## Example Scope

The bundled RL study is runnable as a small Stable-Baselines3 training-and-rollout example. It is not meant to be a strong benchmark policy. For a serious experiment, use more training instances, train for many more steps, save the policy checkpoint as method evidence, and keep the study instances fixed as validation.
