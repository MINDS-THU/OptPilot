# AGENTS.md

## One-time setup

```bash
uv sync --all-packages --group examples --group docs
```

## Monorepo boundaries

- `src/optpilot/` — core `optpilot` package (CLI, runner, realm, schemas)
- `studio/src/optpilot_studio/` — `optpilot-studio` package (Studio UI, depends on `optpilot==0.2.0`)
- `catalog/` — example authoring packages (not in distributions, pruned by MANIFEST.in)
- `tests/` — test suite for both packages

Source lives under `src/`, not repo root (`tool.setuptools.package-dir = {"" = "src"}`).

## Commands

```bash
# Run with uv (preferred for dev work)
uv run optpilot --help
uv run optpilot run <study.yaml> --package-root <path> [--realm-root <path>]
uv run optpilot validate <config.yaml>
uv run optpilot package validate <pkg> --check-source
uv run optpilot ui --open-browser          # Studio

# Tests
uv run pytest                                # all tests
uv run pytest tests/test_mvp.py              # single file
uv run pytest tests/test_mvp.py::TestClass::test_name  # single test

# Docs
uv run mkdocs serve
mkdocs build --strict

# Smoke test (compile, validate, run, assert output)
./scripts/smoke_test.sh

# Release artifact hygiene check
python scripts/check_release_artifacts.py dist --studio-dist-dir dist/studio
```

## Version synchronization

All five version occurrences must match: the package versions in
`pyproject.toml`, `studio/pyproject.toml`, `src/optpilot/__init__.py`, and
`studio/src/optpilot_studio/__init__.py`, plus the Studio dependency pin
(`optpilot==X.Y.Z`) in `studio/pyproject.toml`. These are checked by
`scripts/check_release_artifacts.py`.

## Architecture

The core flow: **Method** proposes candidates → **Realm** validates, admits, launches → **Environment** evaluates → Realm commits evidence.

- Config kinds: `environment`, `method`, `study`, and `resource` (all validated against JSON schemas in `src/optpilot/schemas/`)
- Realm: SQLite-backed ledger with migrations in `src/optpilot/realm/migrations/`
- Default realm root is the OS user-data location; `--realm-root` overrides it
- Retained Realm evidence is sealed read-only (must `chmod -R u+w` before deleting)

Tests use `unittest.TestCase`. The CI runs `python -m unittest discover -s tests -p 'test_*.py'` but `uv run pytest` works too (pytest discovers unittest tests).

No lint/formatter/typecheck config exists in the repo. Do not add one without being asked.

## Distribution constraints

- Core `optpilot` wheel/sdist must **not** include `catalog/`, `studio/`, `tests/`, `docs/`, `designs/`, `scripts/`
- Studio `optpilot-studio` wheel/sdist must **not** include core `optpilot/` prefix
- `MANIFEST.in` enforces pruning for sdist; `scripts/check_release_artifacts.py` validates both distributions
- `.env` files are gitignored (`*.env`); never commit secrets

## CLI entrypoints

- `optpilot` → `src/optpilot/cli.py:main` (core)
- `optpilot-studio` → `studio/src/optpilot_studio/ui/server.py:main` (Studio standalone server)
- `optpilot ui` subcommand is registered via `entry-points."optpilot.commands"` in studio's pyproject.toml

## Studio assistant

Agent skill definitions live in `.agents/skills/`. Assistant prompts and implementation docs are in `studio/src/optpilot_studio/assistant_assets/`.
