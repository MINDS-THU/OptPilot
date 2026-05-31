# OptPilot

OptPilot is an orchestrator for AI-assisted optimization studies over target
environments.

It is not a Bayesian optimization library, RL framework, LLM agent framework,
or simulator. Those pieces are user-owned. OptPilot provides the common study
protocol: configuration, candidate handoff, evaluation, evidence capture,
lineage, and reproducibility records.

## Current Status

This repository is preparing for an initial alpha release. The public
configuration interface is:

- `EnvironmentConfig`: describes how an environment accepts candidates and
  produces metrics, saved files, and extracted records
- `MethodConfig`: describes the user-owned controller/engine method
- `StudyConfig`: binds one method to one environment with an objective and
  budget

OptPilot compiles these files into an internal `StudySpec` that is recorded in
run evidence. Users should normally write the three public config kinds, not
hand-write `StudySpec`.

## Included

- v3alpha user-facing config loader and compiler
- configured-environment adapter for `evaluate.type: python` and
  `evaluate.type: command`
- parameter and file candidate materialization
- candidate validation for parameter bounds and referenced file artifacts
- `filesToSave` evidence capture
- `recordsToExtract` structured extraction from JSONL, CSV, and SQLite sources
- pluggable controller and engine interfaces through `python:module:Class`
- synchronous user engines and lifecycle engines
- reference random-search engine for examples and smoke tests
- evidence-aware controller example
- local threaded backend and local subprocess backend
- retry policy for failed or timed-out attempts
- run resume and branch lineage metadata
- local JSONL/file evidence store
- environment snapshots, run policy snapshots, observations, trials, artifacts,
  engine snapshots, controller decisions, and scheduler events
- prompt/model provenance helpers for user-owned LLM engines
- Frontier-Engineering metadata importer that generates a v3alpha
  `StudyConfig` draft

The complete config reference is
[docs/config_files_v3alpha.md](docs/config_files_v3alpha.md).
Release checks are tracked in
[docs/release_checklist.md](docs/release_checklist.md).

## Install For Development

```bash
python3 -m pip install -e .
```

If you do not install the package, run commands from the repository root with
`PYTHONPATH=src`.

## Run Example Studies

```bash
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_random_search.yaml
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_cli_random_search.yaml
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_user_engine.yaml
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_lifecycle_engine.yaml
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_evidence_aware_controller.yaml
```

Create a Frontier-Engineering draft config when a local copy of that external
project exists under `resource/`. The `resource/` directory is ignored and is
not part of the OptPilot release package.

```bash
PYTHONPATH=src python3 -m optpilot import-frontier \
  resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning \
  --output frontier_pid_study.yaml
```

Resume or branch a run:

```bash
PYTHONPATH=src python3 -m optpilot run examples/studies/toy_user_engine.yaml \
  --resume-run-dir path/to/existing-run

PYTHONPATH=src python3 -m optpilot run examples/studies/toy_user_engine.yaml \
  --branch-from-run-dir path/to/existing-run
```

## User-Owned Code Artifacts

Generated source code should be stored by reference, not inline in JSON/YAML.
During a study run, engines receive
`study_state["runtime_context"]["artifact_store_dir"]`:

```python
from optpilot.code_artifacts import CodeArtifactStore

store = CodeArtifactStore(
    study_state["runtime_context"]["artifact_store_dir"],
    content_ref_mode=study_state["runtime_context"]["artifact_content_ref_mode"],
)
artifact = store.store_directory(
    "/path/to/generated/solver",
    artifact_id="artifact-code-001",
    entrypoint="solver:solve",
    generator_record={"engine_id": "llm_engine", "strategy": "code_evolution"},
)
```

LLM-style engines can also store prompt/model provenance by reference:

```python
from optpilot.provenance import PromptStore, build_generator_record, build_model_record

prompt_store = PromptStore(study_state["runtime_context"]["prompt_store_dir"])
prompt_record = prompt_store.store_prompt(
    messages=[{"role": "user", "content": "Improve solver.py"}],
)
model_record = build_model_record(provider="example", model="code-model-v1")
generator_record = build_generator_record(
    engine_id="llm_engine",
    strategy="code_evolution",
    prompt_record=prompt_record,
    model_record=model_record,
)
```

## Test And Smoke Check

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPYCACHEPREFIX=/tmp/optpilot-pycache python3 -m compileall src/optpilot
./scripts/smoke_test.sh
```

## Public API Boundary

Stable-for-alpha user surface:

- CLI: `optpilot run`, `optpilot import-frontier`
- Python: `optpilot.runner.run_study`
- Configs: `StudyConfig`, `EnvironmentConfig`, `MethodConfig`

Internal implementation details may still change:

- expanded `StudySpec`
- low-level adapters and materializers
- worker internals
- local evidence store layout

## Release Blockers

Before publishing an official public package, choose and add a license. That is
a project/legal decision and is intentionally not guessed in this repository.
