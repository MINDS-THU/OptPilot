---
title: Development
description: Local checks and release-readiness notes for OptPilot contributors.
---

# Development

## Install

```bash
uv sync
```

## Checks

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall src/optpilot
./scripts/smoke_test.sh
```

## Documentation

Serve the MkDocs site locally:

```bash
uv run --extra docs mkdocs serve
```

Build in strict mode:

```bash
uv run --extra docs mkdocs build --strict
```

## Release Hygiene

Before publishing:

- Confirm public docs point users to `config: study`, `config: environment`, and `config: method`.
- Confirm generated run directories are not committed.
- Confirm `.optpilot-ui/`, `.venv/`, `dist/`, `site/`, and `*.egg-info/` are absent from commits.
- Confirm local external projects are not committed under `resource/`.
- Confirm user-facing examples live under `examples/environments`, `examples/methods`, and `examples/studies`.
- Confirm test-only catalogs live under `tests/fixtures/catalog`.
