# Third-party notices — factory-design environment

This environment vendors third-party material in two forms: source copied
into `fd_core/`, and pure-Python wheels under `vendor/` installed into the
environment's locked runtime.

## Factorio Design Benchmark (source, `../fd_core/`)

- License: MIT — `licenses/factorio-design-benchmark-LICENSE.txt`
- Copyright (c) 2026 Factorio Design Benchmark Contributors
- Vendored subset: `schemas/`, `validation/`, `tasks/` (including the 32 task
  configs and `recipes.py`, which `validation/validator.py` imports lazily).
- Excluded: the LLM harness, prompts, Factorio runtime/executor, storage,
  visualization, workflow and experiment runners.
- Modifications: two mechanical edits for pydantic-v1 compatibility, recorded
  in `../fd_core/__init__.py`, plus removal of a UTF-8 BOM from
  `validation/validator.py`. Validation semantics are unchanged and pinned by
  a differential test against the upstream tree.

## pydantic 1.10.22 (wheel, `vendor/pydantic-1.10.22-py3-none-any.whl`)

- License: MIT — `licenses/pydantic-LICENSE.txt`
- Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors
- Why 1.x: OptPilot's process runtime accepts only pure-Python `py3-none-any`
  wheels. pydantic 2.x is itself pure but requires `pydantic-core`, which
  publishes no pure wheel, so the 2.x closure cannot be locked. pydantic
  1.10.22 publishes a pure wheel and depends only on `typing-extensions`.
  Note that 1.10.22 also ships compiled per-platform wheels; the pure wheel is
  the one vendored here, and the lock file pins its exact sha256.

## typing-extensions 4.14.1 (wheel, `vendor/typing_extensions-4.14.1-py3-none-any.whl`)

- License: Python Software Foundation License — `licenses/typing_extensions-LICENSE.txt`
- Required by pydantic 1.10.22; no further dependencies.

Both wheel digests in `requirements.lock` were verified against the digests
published by PyPI at vendoring time.
