# OptPilot Release Checklist

Use this checklist before publishing a release.

## Required Before Release

- Refresh the local environment and lockfile as needed: `uv sync`
- Run unit tests: `uv run python -m unittest discover -s tests -p 'test_*.py'`
- Run compile check: `uv run python -m compileall src/optpilot`
- Run smoke tests: `./scripts/smoke_test.sh`
- Confirm public docs point users to `StudyConfig`, `EnvironmentConfig`, and `MethodConfig`.
- Confirm public docs use the `uv` workflow for installation and examples.
- Confirm generated run directories are not committed under `examples/runs`.
- Confirm generated run directories are not committed under repository-level `runs/`.
- Confirm `.optpilot-ui/`, `.venv/`, `dist/`, and `*.egg-info/` are absent from commits.
- Confirm local external projects are not committed under `resource/`.
- Confirm public examples use only the `optpilot.io/v1` config design.
- Confirm user-facing examples live under `examples/environments`, `examples/methods`, and `examples/studies`.
- Confirm test-only catalogs live under `tests/fixtures/catalog`, not under public `examples/`.
- Confirm `README.md` and `docs/getting_started.md` describe public API boundaries and release blockers.
- Confirm the UI starts and discovers both `examples/` and `user_catalog/`.

## Required Before Public Package Publication

- Add real project URLs to `pyproject.toml`.
- Rebuild distribution artifacts from a clean tree instead of reusing local `dist/` output.
- Tag the release and publish from a clean working tree.

## Not In Scope For This Release

- Built-in Bayesian optimization, RL, or LLM agent algorithms.
- Remote execution backends.
- Strong sandbox isolation.
- Database-backed evidence store.
