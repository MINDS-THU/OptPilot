---
title: Methods
description: How OptPilot connects user-owned optimization methods to environments.
---

# Methods

OptPilot exposes one optimization abstraction: `method`.

A method proposes candidates. It can be a random search, Bayesian optimizer, RL trainer, metaheuristic, LLM workflow, or an existing agent process. OptPilot does not split methods into separate controller and engine concepts.

Methods remain user-owned. OptPilot provides the invocation protocol, candidate contract checking, trial orchestration, and evidence recording around them.

## Method Config

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method

entrypoint:
  python: user_catalog.methods.my_method.method:MyMethod
  protocol: batch

settings:
  batchSize: 4

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

`entrypoint` points to the method implementation. `settings` is a free object passed to that implementation. `accepts` declares which environment contracts the method can target.

## Compatibility Contract

Method and environment compatibility is intentionally explicit.

`accepts` answers three questions:

- which candidate formats can this method produce?
- which environment context fields does it require?
- which environment capabilities does it depend on?

Example:

```yaml
accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions
    capabilities: []
```

This avoids vague domain tags. Compatibility is defined by the actual candidate contract and method-visible environment surface.

## Batch Protocol

A batch method is passively asked to propose candidates. After evaluation, OptPilot calls `observe(...)` when the method implements it.

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng=None):
        self.definition = definition

    def propose(self, n_candidates, study_state):
        return [
            {
                "candidate_id": f"candidate-{index}",
                "format": "parameters",
                "spec": {"x": 1.0},
                "generator": {"method_id": self.definition["id"]},
            }
            for index in range(n_candidates)
        ]

    def observe(self, observations):
        return None
```

Command methods use the same batch protocol. They receive a JSON request on stdin unless the command includes `{input_file}`. They write JSON to stdout unless the command includes `{output_file}`.

```yaml
entrypoint:
  command: [python, my_method.py, "{input_file}", "{output_file}"]
  protocol: batch
```

## Instance-Aware Methods

Some methods need to solve the study instances directly before proposing a candidate. External solvers, trained policies, and coarse-grained optimization scripts commonly work this way.

OptPilot includes the configured study instances in `study_state.instances`:

```json
[
  {
    "id": "ft06_small",
    "path": ".../ft06_small.yaml",
    "payload": {"name": "ft06-small", "jobs": "..."}
  }
]
```

The `id` is the stable OptPilot instance id used by solution candidates. For file-based study instances, it comes from the instance file stem. A method that produces one solution per instance should use those ids in its candidate, for example `spec.solutions.ft06_small`.

## Session Protocol

A Python session method actively interacts with an OptPilot session object. It is useful for LLM agents or workflows that naturally operate through repeated tool-like calls.

```yaml
entrypoint:
  python: user_catalog.methods.my_agent.method:MyAgent
  protocol: session
```

```python
class MyAgent:
    def run(self, session):
        session.event({"event": "started"})
        session.submit({
            "candidate_id": "candidate-001",
            "format": "parameters",
            "spec": {"x": 1.0},
            "generator": {"method_id": session.method_id},
        })
```

Batch and session methods have the same candidate and evidence capability. The distinction is control flow: batch methods are asked to produce candidates; session methods actively submit candidates through the session.

## Parallel Candidates

Both protocols can submit multiple candidates. `settings.batchSize` controls how many candidates OptPilot asks a batch method to propose at once. `study.execution.parallelism` controls how many candidate trials can be evaluated at the same time.

## Runtime Isolation

Python methods run in the host process. Existing agents or optimizers that need isolated dependencies can be exposed as command methods and launched in a container.

```yaml
entrypoint:
  command: [python, my_agent.py, "{input_file}", "{output_file}"]
  protocol: batch

runtime:
  sandbox: container
  network: disabled
  container:
    image: my-agent-image:latest
    executable: docker
    build:
      context: .
      dockerfile: Dockerfile.agent
      tag: my-agent-image:latest
  envFromHost: [OPENAI_API_KEY]
```

Method runtime containers are independent from environment execution containers. Use method containers for optimizer or agent dependencies. Use study execution runtime for simulator or evaluator dependencies.
