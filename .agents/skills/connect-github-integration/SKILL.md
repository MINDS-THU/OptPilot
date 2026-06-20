---
name: connect-github-integration
description: Use this skill when connecting an environment, simulator, evaluator, optimizer, method, solver, heuristic-search repository, RL workflow, or LLM agent found on GitHub to OptPilot. It guides agents through inspecting the upstream project, choosing the right OptPilot boundary, creating environment/method/study configs, avoiding extra OptPilot abstractions such as instances, and validating runnable examples.
---

# Connect GitHub Integrations To OptPilot

Use this skill when the task is to connect an external GitHub project to OptPilot as either an environment, a method, or both.

OptPilot should stay minimal: it orchestrates candidates, evaluators, studies, and evidence. Do not introduce new first-class OptPilot concepts for domain-specific ideas such as instances, scenarios, datasets, tasks, benchmarks, engines, controllers, agents, or solvers. Put domain inputs in `evaluator.settings`, method inputs in `method.settings`, and method-visible read-only files in `methodContext.references`.

## First Read

Before editing code, read the current project docs that define the public contract:

- `docs/index.md`
- `docs/getting-started.md`
- `docs/candidate-contracts.md`
- `docs/concepts.md`
- `docs/configuration.md`
- The closest example page: job-shop, LLM code methods, LLM heuristic repositories, DEVS-Gen, or UI docs as relevant.

Prefer existing examples over inventing patterns:

- Environments: `examples/environments/job_shop_scheduling/`, `examples/environments/strategic_airlift_devs/`
- Methods: `examples/methods/*`
- Studies: `examples/studies/*.yaml`

## Upstream Recon

Inspect the GitHub project before designing the OptPilot side:

1. Identify what the upstream project owns: simulator/evaluator, solver/optimizer, LLM search loop, RL trainer, generated code, dataset benchmark, or service wrapper.
2. Find the smallest native command or Python API that already works outside OptPilot.
3. Run the upstream project once by its own README when feasible.
4. Record dependencies, required credentials, generated outputs, input files, and expected runtime.
5. Decide whether the integration can be fully runnable in this repo, or must be a template that requires local clone/dependencies/credentials.

If network access is needed to clone or install dependencies, request approval instead of silently working around it.

## Boundary Choice

Choose exactly one primary OptPilot boundary.

Use an **environment** when the upstream project evaluates a candidate and returns metrics:

- simulator, benchmark, dataset evaluator, scoring service, validation script
- write a thin evaluator wrapper
- declare the candidate contract and metrics in an environment YAML
- put scenario, dataset, benchmark, simulator, or run arguments in `evaluator.settings`

Use a **method** when the upstream project proposes candidates:

- optimizer, solver, metaheuristic, RL trainer/rollout, LLM code editor, heuristic-search repository
- wrap it as a Python or command method
- put optimizer/model/hyperparameter/credential settings in `method.settings`
- use `methodContext.references` only for environment-owned files the method must read

Use **both** only when the GitHub project contains both a reusable evaluator and reusable optimizer. Keep them decoupled through candidate contracts; do not let the environment import method libraries unless the evaluator genuinely needs them.

## Candidate Contract

Select the simplest candidate format that matches the upstream handoff:

- `parameters`: JSON-like decisions, solver outputs, schedules, route plans, policy rollout outputs, simulator knobs.
- `files`: generated or edited source files, config files, heuristic programs, policy scripts.
- `opaque`: only when both sides intentionally share a private payload and `parameters` or `files` would be misleading.

For fixed-shape methods, declare `produces`. For schema-general methods, omit `produces` and request the needed context under `accepts.requires.context`.

The environment owns what can be evaluated. The method owns how candidates are produced.

## Environment Pattern

Create or copy an environment directory under `user_catalog/environments/...` or `examples/environments/...` depending on whether this should be a public example.

Minimum environment config shape:

```yaml
apiVersion: optpilot.io/v1
config: environment
id: my-environment

evaluator:
  python: user_catalog.environments.my_environment.evaluator:evaluate
  settings: {}

candidate:
  format: parameters
  parameters:
    schema: {}

metrics:
  source: return
  keys: [score]
```

Python evaluator shape:

```python
def evaluate(candidate_runtime, context):
    settings = context["settings"]
    return {
        "status": "success",
        "metric_values": {"score": 0.0},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

Use `trialWorkspace` only for files that must be copied into each disposable trial workspace before evaluation. Use `outputFiles` and `records` to expose evaluator artifacts and per-case details as evidence.

## Method Pattern

Create or copy a method directory under `user_catalog/methods/...` or `examples/methods/...`.

For a small Python method:

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method

entrypoint:
  python: user_catalog.methods.my_method.method:MyMethod
  protocol: batch

settings: {}

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

For a large upstream GitHub repository with its own loop, prefer a command wrapper or the existing `llm_heuristic_search` adapter pattern. Do not rewrite the upstream algorithm into OptPilot unless the upstream interface is already small.

Command wrappers should:

- write/read JSON request and response files where possible
- capture stdout/stderr in evidence
- use explicit generated-file paths
- validate that the generated candidate matches the environment contract

## Study Pattern

A study binds one environment config to one method config:

```yaml
apiVersion: optpilot.io/v1
config: study
name: my-study

environmentConfig: ../environments/my_environment/environment.yaml
methodConfig: ../methods/my_method/method.yaml

objective:
  metric: score
  direction: maximize

budget:
  maxTrials: 1

execution:
  backend: local
  parallelism: 1

evidence:
  level: full
```

Do not put environment inputs directly in the study. Create environment config variants when the same evaluator needs different datasets, fidelity levels, simulator arguments, case suites, or metric settings.

## Dependency Policy

Keep core OptPilot dependencies small.

- Optional public examples should use extras in `pyproject.toml` when the dependency is reasonably installable.
- Large external repositories should usually live under local-only `resource/` and be documented as prerequisites.
- Provider credentials must be read from environment variables and documented as required only for real provider-backed runs.
- Do not commit generated run directories, local clones, private credentials, or bulky external resources.

## Verification

Run the smallest useful verification set for the integration:

1. `uv run optpilot validate path/to/study.yaml`
2. `uv run optpilot run path/to/study.yaml --output-root /tmp/optpilot-<name>-check`
3. Inspect the final summary for `failure_count: 0`, expected metric keys, and expected output files.
4. Run focused unit tests if code paths are shared.
5. Run `uv run --extra docs mkdocs build --strict` if public docs changed.
6. Run the repo smoke test when changing core behavior or public examples.

If an example cannot be run without external clone, API key, license, GPU, or long training, make that explicit in docs and ensure at least its config validates.

## Documentation Checklist

When adding a public example or template, update the appropriate docs:

- `docs/examples.md`
- the specific method/environment page
- `examples/README.md` if it should appear in quick runs
- `docs/configuration.md` only if a schema or public contract changed

For each integration, state:

- what the upstream project owns
- what OptPilot owns
- candidate format and required candidate shape
- install/setup commands
- which command is dependency-free, credential-free, or template-only
- how to inspect evidence after a run

## Common Mistakes

- Do not reintroduce `instances` as a study field or schema concept.
- Do not add domain labels as compatibility checks when candidate format and context requirements are enough.
- Do not make the environment depend on the optimizer just because a tutorial method uses that optimizer.
- Do not hide method-readable files in evaluator-only settings; expose them through `methodContext.references`.
- Do not turn one upstream repository into many OptPilot concepts. Usually it is one environment or one method.
