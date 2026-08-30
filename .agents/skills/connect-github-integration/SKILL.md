---
name: connect-github-integration
description: Connect an environment, simulator, evaluator, optimizer, method, solver, heuristic-search repository, RL workflow, or LLM agent from GitHub to OptPilot as a portable package with a runnable smoke study when possible.
---

# Connect GitHub Integrations To OptPilot

Adapt an external repository at the smallest useful OptPilot boundary. OptPilot
orchestrates Candidates, evaluations, Studies, and retained evidence; domain
concepts such as datasets, scenarios, benchmarks, engines, controllers, and
solvers remain package-owned settings or files rather than new OptPilot config
kinds.

## Read The Current Contract

Before authoring, read:

- `docs/tutorial-package.md` for the supported create/check/run/register loop
- `docs/capabilities.md` for the executable boundary
- `docs/candidate-contracts.md` for Candidate formats
- `docs/configuration.md` for config fields and callable shapes
- `docs/catalog.md` for package layout and Studio publication
- `docs/methods.md` when wrapping an optimizer or search loop

Use tracked packages as examples:

- `catalog/optpilot_tutorial/` for the smallest complete package
- `catalog/devs_gallery/` for generated simulators and reusable Resources
- `catalog/or_solving/` for a command Method and per-launch inputs
- `catalog/production_agv_scheduling/` for file Candidates and richer evidence

Do not use `test_catalog/` as user-facing guidance. It contains test fixtures
and broader authoring cases, not release examples.

## Inspect The Upstream Project

1. Identify whether the upstream owns an evaluator, a candidate generator, or
   both.
2. Find its smallest native Python API or command and run that path once when
   feasible.
3. Record dependencies, credentials, input files, generated outputs, runtime,
   license constraints, and the expected success signal.
4. Decide whether the integration can run from retained package source or must
   be explicitly documented as host-provisioned/template-only.

Ask for approval before cloning or installing over the network. Never commit
credentials, generated Runs, local clones, or licensed data that cannot be
redistributed.

## Choose One Primary Boundary

Use an **Environment** when the upstream evaluates a Candidate and returns
metrics. Put scenario, dataset, fidelity, simulator, and benchmark choices in
`evaluator.settings`; expose Environment-owned files needed by a Method through
`methodContext.references`.

Use a **Method** when the upstream proposes Candidates. Put optimizer, model,
and hyperparameter choices in `method.settings`. Declare only the host variables
the Method genuinely consumes in `runtime.envFromHost`.

Use both only when the upstream genuinely provides both reusable roles. Keep
them decoupled through the Candidate contract.

## Choose An Executable Candidate Contract

- `parameters`: JSON-like decisions, schedules, routes, solver answers, or
  simulator controls.
- `files`: generated or edited source, policy, or configuration files.
- `opaque`: a valid authoring contract for private integrations, but not
  executable by the current retained runner. Do not advertise an opaque Study
  as runnable.

The Environment owns what is valid. A fixed-shape Method may declare
`produces`; a schema-general Method should request the required context under
`accepts.requires.context`.

## Author A Portable Package

For a source-controlled package, work outside OptPilot's tracked `catalog/`
directory—for example in a project repository or the OS-local package root
described in `docs/tutorial-package.md`. A Studio Workspace may instead keep
draft configs under `optpilot_configs/`; the Workspace **Publish** flow materializes the final
portable package layout.

A package settings file preserves identity across moves and updates:

```yaml
apiVersion: optpilot.io/v1
config: package
identity: 0123456789abcdef0123456789abcdef
title: My Integration
category: local
description: Evaluate and optimize the upstream project.
```

Generate a fresh identity for a new package with:

```bash
uv run python -c "import secrets; print(secrets.token_hex(16))"
```

Keep the identity when moving or renaming the same package. Generate a different
identity when copying it to create a separate package lineage.

Every user-facing component should have a readable `name`, a useful
`description`, and one or more `tasks` slugs. Prefer the vocabulary already used
by shipped packages: `generate-simulator`, `optimize-policy`,
`solve-or-problem`, `tune-parameters`, `evaluate-design`, `benchmark-method`,
`learn-optpilot`, or `build-package`.

### Environment Pattern

```yaml
apiVersion: optpilot.io/v1
config: environment
id: my-environment
name: My evaluator
description: Scores one Candidate with the upstream project.
tasks: [evaluate-design]

evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
  settings: {}

candidate:
  format: parameters
  parameters:
    schema:
      x:
        valueType: float
        min: 0.0
        max: 1.0
        default: 0.5

metrics:
  source: return
  keys: [score]
```

```python
def evaluate(candidate_runtime, context):
    return {
        "status": "success",
        "metric_values": {"score": 0.0},
        "constraint_results": {},
        "output_files": [],
        "event_summary": {},
    }
```

Use `trialWorkspace` only for package files copied into each disposable attempt.
Use `outputFiles` and `records` for evaluator artifacts and per-case evidence.

### Method Pattern

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-method
name: My optimizer
description: Proposes parameter Candidates through the upstream optimizer.
tasks: [optimize-policy]

entrypoint:
  python: method:MyMethod
  pythonPath: [.]
  protocol: batch

settings: {}

accepts:
  formats: [parameters]
  requires:
    context:
      - candidate.parameters.schema
```

For a large upstream loop, prefer a thin command batch wrapper rather than
rewriting the algorithm. The current retained command Method must use
`python`/`python3` as its logical command head and exchange JSON through stdin/
stdout or `{input_file}`/`{output_file}`. See `docs/methods.md`.

### Study Pattern

```yaml
apiVersion: optpilot.io/v1
config: study
name: my-study
title: My integration smoke run
description: Exercises one deterministic Candidate end to end.
tasks: [benchmark-method]

environmentConfig: ../environments/my_environment/environment.yaml
methodConfig: ../methods/my_method/method.yaml

objective:
  metric: score
  direction: maximize

budget:
  maxTrials: 1

execution:
  parallelism: 1
  timeoutSeconds: 120

evidence:
  level: full

reproducibility:
  seed: 0
```

Do not add an `execution.backend` field; it is not part of the public Study
schema. Put values that change on every launch in declared Study `inputs`, not
in an ad-hoc Study field.

## Dependencies And Credentials

Prefer package-retained source and the hash-locked pure-Python
`runtime.setup` form documented in `docs/configuration.md`. If native libraries,
licensed software, a GPU stack, or a large external repository must come from
the host, state that clearly in the package README and validation expectations.

Credentials are launch-time host values, never defaults or committed settings.
Declare only the names the executable path reads. Distinguish an LLM-backed
Method from a deterministic seed/baseline by both its source and declarations.

## Verify Before Registration

For a CLI-authored package:

```bash
uv run optpilot package validate path/to/package \
  --check-source \
  --check-setup-files
uv run optpilot validate path/to/package/studies/smoke.yaml
uv run optpilot package smoke path/to/package --study studies/smoke.yaml
```

Add `--check-imports` when importing authored callables in isolated subprocesses
is safe and dependencies are available. A direct Run uses:

```bash
uv run optpilot run path/to/package/studies/smoke.yaml \
  --package-root path/to/package
```

For a Studio Workspace:

1. Discover existing `optpilot_configs` before broad source scans.
2. Prepare the package plan and repair every schema, source, setup, import,
   callable-shape, and source-closure error.
3. For an Environment-plus-Method package, add the smallest deterministic smoke
   Study and require a completed Run with zero logical failures and the declared
   objective metric.
4. Register only the exact artifact that passed Check/Test. Registration and
   smoke may require user approval; never manufacture approval.

Static schema validation is not proof of run readiness. If credentials,
licenses, native dependencies, hardware, or long training block the real path,
validate what is possible and label the remaining path accurately.

## Documentation Checklist

For a public integration, update the package README and the appropriate existing
docs page or add a focused package page to `mkdocs.yml`. State:

- what the upstream project owns and what OptPilot owns
- Candidate format and required shape
- dependencies, credentials, licenses, and supported platforms
- the smallest dependency-free or credential-free check
- the exact smoke/Run command and expected objective metric
- where to inspect retained evidence
- whether the package is runnable, host-provisioned, or template-only

Run `uv run --group docs mkdocs build --strict` when public docs change. Run the
repository smoke test when changing shared behavior or shipped examples.

## Avoid These Mistakes

- Do not invent new OptPilot config kinds for domain concepts.
- Do not make the Environment depend on a tutorial optimizer.
- Do not hide Method-readable Environment files in evaluator-only settings.
- Do not use an unsupported opaque/session/command-evaluator contract for a
  Study advertised as runnable.
- Do not apply an Environment-plus-Method package before its smoke Study passes.
- Do not overwrite a bundled package; copy it outside the tracked catalog, give
  the copy a fresh identity, and register the copy.
