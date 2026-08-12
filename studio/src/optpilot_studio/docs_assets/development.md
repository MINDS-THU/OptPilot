---
title: Contributor Guide
description: Local checks and release-readiness notes for OptPilot contributors.
---

# Contributor Guide

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

## Release artifact gate

Build both distributions from clean PEP 517 inputs and inspect their package
boundaries before tagging a release:

```bash
python -m pip install build twine
rm -rf dist-check
python -m build --wheel --sdist --outdir dist-check
python -m build --wheel --sdist --outdir dist-check/studio studio
python scripts/check_release_artifacts.py dist-check \
  --studio-dist-dir dist-check/studio
python -m twine check dist-check/* dist-check/studio/*
```

The gate verifies synchronized versions, required schemas and Realm migrations,
core/Studio isolation, Studio entry points, packaged UI/docs/assistant assets,
and common archive contamination. Install and exercise the artifacts themselves
afterward; an editable source checkout cannot reveal missing package data.

Only `dist-check/optpilot-*` is the public PyPI release for the current install
split. The Studio artifacts prove that the source-checkout package is complete;
do not upload `optpilot_studio-*` until Studio publication becomes an explicit
release decision.

## Maintainer Release Hygiene

Before publishing:

- Confirm public docs point users to `config: study`, `config: environment`, and `config: method`.
- Confirm generated run directories are not committed.
- Confirm `.optpilot-ui/`, `.venv/`, `dist/`, `site/`, and `*.egg-info/` are absent from commits.
- Confirm Workspace **Register checked version** publishes the exact artifact
  produced by Check/Test as a Realm Catalog revision; the internal package plan
  remains an implementation detail, and no test or documentation treats a
  generated filesystem package as publication authority.
- Confirm Realm catalog actions use exact entry refs/action-owned projections,
  and managed study launch uses workspace id, relative study path, and expected
  workspace revision rather than provider paths.
- Confirm Realm schema v28 workspace tests cover one-root adoption, strict
  same-store whole-tree union, request binding/recovery, leased-attempt cleanup,
  atomic finalization, and cross-store rejection without a copy fallback.
- Confirm Study Builder accepts non-conflicting exact environment/method package
  roots, records source/focus lineage, and rejects every file overlap,
  file/directory conflict, and case-fold collision deterministically.
- Confirm the PyPI core package does not include Studio UI code or assistant assets.
- Confirm source-checkout installs still expose `optpilot ui`.
- Confirm only intentional internal Markdown notes are committed under
  `resource/`; local external projects should stay uncommitted.
- Confirm user-facing entries live under a bundled package's `environments/`, `methods/`, and `studies/` folders in `catalog/`.
- Confirm test-only catalogs live under `tests/fixtures/catalog`.
