---
title: Methods
description: How OptPilot connects user-owned optimization methods to environments.
---

# Methods

OptPilot exposes one optimization abstraction: `method`.

A method proposes candidates. It can be a random search, Bayesian optimizer, RL
trainer, metaheuristic, LLM workflow, or an existing agent process.

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
  # Optional. Maximum duration of one propose/observe exchange.
  exchangeTimeoutSeconds: 60

settings:
  batchSize: 4

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

`entrypoint` points to the method implementation. `settings` is a free object passed to that implementation. `accepts` declares the environment surface the method needs to run.

`entrypoint.exchangeTimeoutSeconds` declares how long OptPilot should wait for
one complete Method request/response exchange, such as one `propose(...)` or
`observe(...)` call. Studio automatically uses the value from the selected
Method revision for every Study; it is not a Study setting. The default is 10
seconds when omitted. This limit does **not** bound the whole Run, and it is
distinct from any timeout the Method applies to its own HTTP or model-provider
calls inside that exchange.

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

Command methods use the same batch protocol. The command is executed once per
proposal exchange. It receives a JSON request on stdin unless the command
includes `{input_file}`. It writes JSON to stdout unless the command includes
`{output_file}`.

Method `entrypoint` fragment:

```yaml
entrypoint:
  command: [python, my_method.py, "{input_file}", "{output_file}"]
  protocol: batch
  exchangeTimeoutSeconds: 60
```

The retained process slice executes command batch methods under the same
prepared runtime as Python batch methods:

- `command[0]` must be the logical interpreter name `python` or `python3`. The
  worker maps it to its exact prepared interpreter; arbitrary host executables
  and absolute host paths are rejected because the worker runs with an
  explicit, PATH-free environment.
- The working directory is the projected method config directory, so a
  relative script such as `my_method.py` resolves next to the method config.
  Retained package validation checks that a plain `python script.py` script is
  present in the package.
- The retained import roots (including any prepared locked dependency layers
  from `runtime.setup`) are supplied through `PYTHONPATH`.
- Values named in `runtime.envFromHost` are present in the command's
  environment.
- One command invocation is bounded by the method's
  `exchangeTimeoutSeconds`; a hung command fails that exchange without
  wedging the worker.

The JSON request carries `protocol`, `request_id`, `n_candidates`,
`candidate`, `methodContext`, `study_state`, `objective`, `candidate_context`,
`evidence`, `runtime_context` (including a private scratch
`method_workspace`), `settings`, `config`, and the study's global `seed`. The
response must be a JSON object with a `candidates` list; an optional
`method_events` list is echoed into the worker's private log. Observations are
not forwarded to the command — each proposal request already contains the
method-visible evidence projection, so a command method reads `evidence`
instead of implementing `observe`.

## Methods That Need Reference Inputs

Some methods need to read the same input files that the evaluator will use before proposing a candidate. External solvers, trained policies, and coarse-grained optimization scripts commonly work this way.

Expose those files through the environment config's top-level `methodContext.references`:

```yaml
methodContext:
  references:
    - name: validation_small
      type: validation_case
      path: cases/validation_small.yaml
```

OptPilot includes that context in `study_state["candidate_context"]`. A method
can read the referenced files and emit candidate keys using the reference
names, for example `spec.solutions.validation_small`. The evaluator decides how
those names map to its own settings.

## Session protocol

`protocol: session` is reserved in the public schema for methods that keep
their own search loop alive and adapt after individual completions. The current
public Realm runner does not execute session configs.

Live session will provide runner-mediated `submit`, `wait`, `poll`, events,
stop signals, filtered evidence, and method-owned state. Existing
session-shaped helper code does not provide those semantics and is not a
compatibility mode; unsupported session studies fail during retained
compilation.

Use `batch` unless and until live observations must influence another
submission before the method returns.

## Proposal width and execution capacity

`settings.batchSize` controls how many candidates OptPilot asks a batch method
to propose in one exchange. It is not evaluator capacity. The retained
controller rejects an oversized proposal atomically.

`study.execution.parallelism` is the semantic evaluator-capacity ceiling. The
retained local driver overlaps evaluator waits up to that ceiling, with an
additional process-local cap of 32 evaluator threads; excess ready attempts are
queued. Canonical launch/adoption remains serialized and observations retain
proposal order after the batch barrier.

## Runtime isolation

The retained method worker is supervised and bound to exact retained source and
durable method-exchange checkpoints. Python batch Methods run through the
prepared Python runtime; Python-headed command batch Methods run as one bounded
subprocess per exchange in the same projection and declared `envFromHost`
surface. A supported Method may select a local process or an approved,
digest-pinned container runtime. Builds and arbitrary setup commands are not
supported, and a container never receives a broad package or Realm mount. See
[Executable Capabilities](capabilities.md) for the exact release boundary and
unsupported combinations.
