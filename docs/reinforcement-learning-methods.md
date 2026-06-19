---
title: Reinforcement Learning Methods
description: How JobShopLib reinforcement-learning policies fit the job-shop example.
---

# Reinforcement Learning Methods

JobShopLib provides Gymnasium environments for job-shop scheduling, including graph-based single-instance and multi-instance environments. In OptPilot, the trained policy belongs to the method. The job-shop evaluator does not load the policy and does not know that reinforcement learning was used.

The integration pattern is:

```text
OptPilot method receives study_state.instances
-> method creates a JobShopLib RL environment for each instance
-> method loads or constructs a policy
-> policy rolls out actions until the schedule is complete
-> method emits schedule-solution parameters
-> OptPilot evaluator validates and scores the schedules
```

## What The Method Needs

An RL rollout method needs:

- the study instances from `study_state.instances`
- a policy implementation or policy checkpoint
- rollout settings such as deterministic sampling, maximum steps, and render mode
- JobShopLib and the policy framework dependencies in the method runtime

Example method settings:

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

## Turnkey Status

The repository does not currently bundle a trained RL policy checkpoint. To add a runnable RL example, place the policy and wrapper in `examples/methods/` or `user_catalog/methods/`, then bind it to `environment_schedule_solution.yaml`.
