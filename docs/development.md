# Contribute to OptPilot Core

The public repository contains the experiment runtime, CLI, schemas, catalog and both documentation sites. Run the checks from its root with Python 3.10+ and uv. The Desktop client consumes Core from a separate repository.

## Find the implementation behind a public feature

| To change… | Start in… |
| --- | --- |
| The public experiment API or a CLI command | `src/optpilot/experiment.py` and `src/optpilot/experiment_cli.py` |
| Manifest validation, saved files or study selection | `src/optpilot/schemas/`, `_experiment_catalog.py` and `_experiment_study.py` in the package |
| Run execution or the recorded result | `_experiment_execution.py` and `_experiment_store.py` in the package |
| Resource admission or measurement records | `experiment_resource_plan.py` and `experiment_resources.py` in the package |
| A runnable example | The manifest, grader and declared files together in the corresponding `catalog/` directory |

When changing the supported workflow, follow the public entry point through validation, review, execution and result inspection. Some tests also exercise the project's earlier runtime. Use a versioned example to check user-visible behavior. [Experiment design and terminology](https://github.com/MINDS-THU/OptPilot/blob/main/designs/core-experiment-model.md)

## Run the checks

```sh
uv sync
uv run python -m unittest discover -s tests/core -p 'test_*.py'
./scripts/smoke_test.sh
uv run --group docs mkdocs build --strict
```

The smoke test validates document search and runs both candidates across all five instances in a temporary store. If a change affects a model provider, remote worker or external runner, also exercise that integration in its own environment. Record the actual outcome, including failures. [Repository instructions](https://github.com/MINDS-THU/OptPilot/blob/main/AGENTS.md)

## Preview documentation changes

```sh
uv run --group docs mkdocs serve
```

Open the URL printed by MkDocs. Edit the technical manual in `docs/` and its navigation in `mkdocs.yml`. For a tutorial, run the commands from the specified working directory with a fresh ledger and check the resulting measurements. Keep dependencies and credentials specific to the examples that need them.

The public website lives under `website/`. For changes to product copy, verify both English and Chinese builds from that directory:

```sh
npm ci
npm run typecheck
npm run build
```

GitHub Pages serves the website at the project root and this manual under `/reference/`. [Website development guide](https://github.com/MINDS-THU/OptPilot/blob/main/website/README.md)

## Check a Core distribution

Before publishing a Core release, check that the versions in `pyproject.toml` and `src/optpilot/__init__.py` agree. Inspect the packaged wheel and source distribution with:

```sh
uv build
uv run python scripts/check_release_artifacts.py dist
```

Desktop installer builds and their platform acceptance checks run separately. [Public releases](https://github.com/MINDS-THU/OptPilot/releases)
