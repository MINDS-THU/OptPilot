# OptPilot Release Checklist

Use this checklist before publishing an alpha or stable release.

## Required For Alpha

- Refresh the local environment and lockfile as needed: `uv sync`
- Run unit tests: `uv run python -m unittest discover -s tests -p 'test_*.py'`
- Run compile check: `uv run python -m compileall src/optpilot`
- Run smoke tests: `./scripts/smoke_test.sh`
- Confirm public docs point users to `StudyConfig`, `EnvironmentConfig`, and `MethodConfig`.
- Confirm public docs use the `uv` workflow for installation and examples.
- Confirm generated run directories are not committed under `examples/runs`.
- Confirm local external projects are not committed under `resource/`.
- Confirm public examples use only the v3alpha config design.
- Confirm `README.md` and `docs/getting_started.md` describe public API boundaries and release blockers.

## Required Before Public Package Publication

- Add real project URLs to `pyproject.toml`.
- Tag the release and publish from a clean working tree.

## Not Required For Alpha

- Built-in Bayesian optimization, RL, or LLM agent algorithms.
- Remote execution backends.
- Strong sandbox isolation.
- UI.
- Database-backed evidence store.
