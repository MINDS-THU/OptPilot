---
title: Methods
description: How OptPilot connects user-owned optimization methods to environments.
---

# Methods

OptPilot exposes one optimization abstraction: `method`.

The method owns the search loop: Bayesian optimization, RL training, LLM code editing, a hand-written heuristic, an existing agent workflow, or a command-line tool. OptPilot does not split that implementation into separate controller and engine concepts.

## Batch Protocol

A batch method is asked to propose one or more candidates. After evaluation, OptPilot calls `observe(...)` when the method implements it.

```python
class MyMethod:
    def __init__(self, definition, study_spec, rng):
        ...

    def propose(self, n_candidates, study_state):
        ...

    def observe(self, observations):
        ...
```

Command methods receive a JSON request on stdin unless their command declares `{input_file}` or `{output_file}` placeholders.

```yaml
implementation:
  type: command
  command: [python, my_method.py, "{input_file}", "{output_file}"]
  protocol: optpilot.method.batch.v1
```

## Session Protocol

Python session methods receive an active session object and submit candidates through that object.

```python
class MyAgent:
    def run(self, session):
        session.event({"event": "started"})
        session.submit({
            "artifact_id": "candidate-001",
            "artifact_kind": "parameter_spec",
            "spec": {"x": 1.0},
            "generator_record": {"method_id": session.method_id},
        })
```

The session exposes study state, evidence access, candidate context, method config, candidate submission, and method events.

## Runtime Isolation

Python methods run in the host process today. Existing agents that need isolated dependencies can be exposed as command methods and launched with a method runtime container.

```yaml
runtime:
  type: container
  image: my-agent-image:latest
  containerExecutable: docker
  networkPolicy: disabled
  build:
    context: .
    dockerfile: Dockerfile.agent
    tag: my-agent-image:latest
```

Method runtime containers are independent from environment execution containers. Use method containers for optimizer or agent dependencies. Use execution backend containers for simulator or evaluator dependencies.
