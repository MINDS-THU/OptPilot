---
title: Reinforcement Learning Methods
description: How JobShopLib reinforcement-learning policies fit the job-shop example.
---

# Reinforcement Learning Methods

Reinforcement learning is included because it exercises a different kind of
method: the method may train or load a policy before it can propose a candidate.

In this tutorial, the policy is not the environment-facing candidate. The method
uses environment-owned training context, rolls out a policy on validation cases,
and returns schedule solutions. The job-shop evaluator does not load the policy
and does not know that reinforcement learning was used.

## Contract

The RL study uses:

```text
catalog/example_package/environments/job_shop_scheduling/environment_schedule_solution.yaml
catalog/example_package/methods/job_shop_rl_stable_baselines/method.yaml
```

The environment exposes three kinds of method-readable references:

```yaml
methodContext:
  references:
    - name: ft06_small
      path: cases/ft06_small.yaml
      type: job_shop_case
    - name: ft06_standard
      path: cases/ft06_standard.yaml
      type: job_shop_case
    - name: train_tiny_a
      path: training_cases/train_tiny_a.yaml
      type: job_shop_training_case
    - name: rl_env_adapter
      path: rl_env_adapter.py
      type: python_module
```

The method declares that it needs:

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

This keeps training data and adapter code environment-owned while keeping the
training algorithm method-owned.

## Run It

Install optional example dependencies:

```bash
uv sync --extra examples
```

Run the study:

```bash
uv run optpilot validate catalog/example_package/studies/job_shop_rl_stable_baselines.yaml
uv run optpilot run catalog/example_package/studies/job_shop_rl_stable_baselines.yaml
```

The bundled run is intentionally small. It is meant to demonstrate the OptPilot
boundary, not to produce a strong benchmark policy.

## What Happens Inside The Method

The method:

1. reads training cases from `methodContext.references`
2. loads the environment-owned Gymnasium adapter from `rl_env_adapter.py`
3. trains a small Stable-Baselines3 `PPO` policy
4. rolls the policy out on the validation cases
5. returns schedule-solution parameters

Method settings control the training wrapper:

```yaml
settings:
  algorithm: PPO
  maxJobs: 6
  totalTimesteps: 128
  discountFactor: 0.95
  seed: 0
```

## What The Method Returns

The method returns the same schedule-solution candidate used by the solver
examples:

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
  ft06_standard:
    operations:
      - job: 0
        operation: 0
        machine: 0
        start: 0
        end: 1
```

The environment validates the final schedule. It does not inspect the policy,
reward function, neural network weights, or rollout trace unless the method
saves those as evidence.

## Why This Matters

RL demonstrates that an OptPilot method can do substantial internal work before
returning one candidate. The environment-facing boundary still stays simple:
the candidate is the schedule bundle, not the policy object.

For a serious experiment, use more training cases, train for many more steps,
save policy checkpoints as method evidence, and keep validation cases fixed.
