# OptPilot

This public repository contains the Python Core, CLI, schemas, examples and technical documentation. The Electron Desktop client is developed in the private `MINDS-THU/OptPilot-Desktop` repository and consumes the public Core through its checkout and release workflow.

- `src/optpilot/` contains Python Core, the CLI, public schemas, execution and the versioned experiment ledger.
- `catalog/` contains example packages; `test_catalog/` and `tests/fixtures/` contain test inputs.

## Development

Use Python 3.10+ and `uv` for Core. From the repository root:

```sh
uv sync
uv run python -m unittest discover -s tests/core -p 'test_*.py'
./scripts/smoke_test.sh
uv run --group docs mkdocs build --strict
```

Desktop checks and packaging run in the private OptPilot-Desktop repository. Its workflow checks out this public repository as the Core dependency before building installers.

## Packaging

The Core wheel ships its own modules, schemas and database migrations.
The Desktop build bundles the Core sources for its managed Python runtime.
Core's version declarations in `pyproject.toml` and `src/optpilot/__init__.py`
must agree. Build and inspect Python distributions with:

```sh
uv build
python scripts/check_release_artifacts.py dist
```
