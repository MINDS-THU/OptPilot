---
title: Methods
description: How OptPilot connects user-owned optimization methods to environments.
---

# Methods

OptPilot exposes one optimization abstraction: `method`.

A method proposes candidates. It can be a random search, Bayesian optimizer, RL trainer, metaheuristic, LLM workflow, or an existing agent process. OptPilot does not split methods into separate controller and engine concepts.

Methods remain user-owned. OptPilot provides the invocation protocol, candidate contract checking, trial orchestration, and evidence recording around them.

## Method Config

This is a minimal complete method config for a schema-general parameter method. It asks OptPilot to provide the selected environment's parameter schema at runtime.

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method

entrypoint:
  python: method:MyMethod
  protocol: batch

settings:
  batchSize: 4

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

`entrypoint` points to the method implementation. `settings` is a free object passed to that implementation. `accepts` declares the environment surface the method needs to run.

The environment owns the candidate contract. A method declares the candidate
formats and context it can use, then OptPilot validates every proposed candidate
against the selected environment before evaluation.

## Compatibility Contract

Method and environment compatibility is intentionally explicit.

`accepts` answers three questions:

- which candidate formats can this method submit?
- which environment context fields does it require?
- which environment capabilities does it depend on?

A general parameter-producing method can be compatible with any parameter-candidate environment:

Method compatibility fragment:

```yaml
accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

In that case, compatibility says the method can run because it supports `parameters` and receives the schema. The runner still validates every submitted candidate against the environment contract during evaluation.

File-candidate methods use the same pattern:

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

Method `entrypoint` fragment:

```yaml
entrypoint:
  command: [python, my_method.py, "{input_file}", "{output_file}"]
  protocol: batch
```

## Methods That Need Reference Inputs

Some methods need to read the same input files that the evaluator will use before proposing a candidate. External solvers, trained policies, and coarse-grained optimization scripts commonly work this way.

Expose those files through the environment config's top-level `methodContext.references`:

```yaml
methodContext:
  references:
    - name: ft06_small
      type: job_shop_case
      path: cases/ft06_small.yaml
```

OptPilot includes that context in `study_state["candidate_context"]`. A method can read the referenced files and emit candidate keys using the reference names, for example `spec.solutions.ft06_small`. The evaluator decides how those names map to its own settings.

## Session Protocol

A Python session method actively interacts with an OptPilot session object. It is useful for LLM agents or workflows that naturally operate through repeated tool-like calls.

Method `entrypoint` fragment:

```yaml
entrypoint:
  python: method:MyAgent
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

Python methods run through an OptPilot method worker process or container, not inside the main runner process. Use `runtime.setup` for process-runtime dependencies and `runtime.container` for container images.

Method runtime fragment:

```yaml
entrypoint:
  command: [python, my_agent.py, "{input_file}", "{output_file}"]
  protocol: batch

runtime:
  sandbox: container
  container:
    image: my-agent-image:latest
    executable: docker
    network: disabled
    build:
      context: .
      dockerfile: Dockerfile.agent
      tag: my-agent-image:latest
  envFromHost: [OPENAI_API_KEY]
```

Method runtime containers are independent from environment runtime containers. Use method runtime for optimizer or agent dependencies, and environment runtime for simulator or evaluator dependencies.
