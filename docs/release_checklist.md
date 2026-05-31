# OptPilot Release Checklist

Use this checklist before publishing an alpha or stable release.

## Required For Alpha

- Run unit tests: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
- Run compile check: `PYTHONPYCACHEPREFIX=/tmp/optpilot-pycache python3 -m compileall src/optpilot`
- Run smoke tests: `./scripts/smoke_test.sh`
- Confirm public docs point users to `StudyConfig`, `EnvironmentConfig`, and `MethodConfig`.
- Confirm generated run directories are not committed under `examples/runs`.
- Confirm local external projects are not committed under `resource/`.
- Confirm public examples use only the v3alpha config design.
- Confirm `README.md` describes public API boundaries and release blockers.

## Required Before Public Package Publication

- Choose and add a license.
- Add real project URLs to `pyproject.toml`.
- Tag the release and publish from a clean working tree.

## Not Required For Alpha

- Built-in Bayesian optimization, RL, or LLM agent algorithms.
- Remote execution backends.
- Strong sandbox isolation.
- UI.
- Database-backed evidence store.
