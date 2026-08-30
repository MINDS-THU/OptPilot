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

Public pages under `docs/` are bundled into Studio under
`studio/src/optpilot_studio/docs_assets/`. Keep each mirrored file byte-for-byte
identical; the test suite checks that contract. When executable behavior
changes, update [Executable Capabilities](capabilities.md) and the compiler tests
in the same change rather than copying a capability list into another guide.

## Adding or updating a bundled package

The tracked `catalog/` is for intentional release examples, not ordinary user
work. Prototype in an external package root first. Once a package is accepted
for the release:

1. Give it one top-level `optpilot.package.yaml` with a stable 32-character
   lowercase hexadecimal identity, human title, category, and description.
   Generate a fresh identity for a new package; preserve it for every update or
   move of the same package.
2. Put Environment, Method, Resource, and Study configs under their documented
   package folders. Give components human-facing `name`/`title`, descriptions,
   and useful `tasks`; keep all referenced source and setup inputs inside the
   package.
3. Add a package README with prerequisites, trust/network implications, the
   first deep-validation command, the smallest smoke command, and a clear note
   for every Study that needs a key, native dependency, container engine, or
   licensed input. Retain license and third-party notices beside vendored code.
4. Run:

   ```bash
   uv run optpilot package validate catalog/<package> \
     --check-source --check-setup-files --check-imports
   uv run optpilot package setup-check catalog/<package>
   uv run optpilot package smoke catalog/<package> \
     --study studies/<smoke-study>.yaml
   ```

   Run `package setup-check --run-setup` only after reviewing declared setup.
5. Add targeted tests for the integration and keep a deterministic, bounded
   no-key smoke path whenever possible. Use `tests/fixtures/catalog/` for small
   schema/unit fixtures and `test_catalog/` for heavier test-only packages;
   neither is public release inventory.
6. Update the public catalog inventory and package-specific guide. Confirm the
   advertised combinations match [Executable Capabilities](capabilities.md),
   then run the full tests and strict docs build.

See [Build Your First Package](tutorial-package.md) for the corresponding
third-party author workflow.

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
python -m twine check \
  dist-check/*.whl dist-check/*.tar.gz \
  dist-check/studio/*.whl dist-check/studio/*.tar.gz
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

- Confirm the five version occurrences match: package versions in
  `pyproject.toml`, `studio/pyproject.toml`, `src/optpilot/__init__.py`, and
  `studio/src/optpilot_studio/__init__.py`, plus Studio's `optpilot==X.Y.Z`
  dependency pin.
- Confirm public docs point users to `config: study`, `config: environment`, and `config: method`.
- Confirm generated run directories are not committed.
- Confirm `.optpilot-ui/`, `.venv/`, `dist/`, `site/`, and `*.egg-info/` are absent from commits.
- Confirm Workspace **Publish checked version** publishes the exact artifact
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
- Confirm intentional bundled user-facing entries live under their package's
  `environments/`, `methods/`, `resources/`, or `studies/` folder in `catalog/`.
- Confirm small schema fixtures live under `tests/fixtures/catalog` and heavier
  end-to-end examples live under `test_catalog/`, never under the public
  `catalog/` tree.
