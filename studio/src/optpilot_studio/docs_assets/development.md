---
title: Development
description: Local checks and release-readiness notes for OptPilot contributors.
---

# Development

## Install

```bash
uv sync --all-packages --group examples --group docs
```

## Checks

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python -m compileall src/optpilot
uv run python -m compileall studio/src/optpilot_studio
./scripts/smoke_test.sh
```

## Documentation

Serve the MkDocs site locally:

```bash
uv run --group docs mkdocs serve
```

Build in strict mode:

```bash
uv run --group docs mkdocs build --strict
```

## Release Hygiene

Before publishing:

- Confirm public docs point users to `config: study`, `config: environment`, and `config: method`.
- Confirm generated run directories are not committed.
- Confirm `.optpilot-ui/`, `.venv/`, `dist/`, `site/`, and `*.egg-info/` are absent from commits.
- Confirm the PyPI core package does not include Studio UI code or assistant assets.
- Confirm source-checkout installs still expose `optpilot ui`.
- Confirm only intentional internal Markdown notes are committed under
  `resource/`; local external projects should stay uncommitted.
- Confirm user-facing examples live under `catalog/example_package/environments`, `catalog/example_package/methods`, and `catalog/example_package/studies`.
- Confirm test-only catalogs live under `tests/fixtures/catalog`.
