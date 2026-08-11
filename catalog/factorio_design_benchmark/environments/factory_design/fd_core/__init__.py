"""Vendored evaluation core of the Factorio Design Benchmark (MIT).

Upstream: the Factorio Design Benchmark research codebase, `fd_benchmark/`.
Only the dependency-light evaluation subset is vendored here — `schemas/`
(the ProductionLine contract), `validation/` (the 22 static checks and the
entity cost model), and `tasks/` (the 32 task configs, recipes, registry).
The LLM harness, Factorio runtime, executor, visualization and experiment
runners are deliberately excluded.

Two mechanical edits were applied so the copy runs on a pure-Python locked
runtime (pydantic v2's `pydantic-core` is a compiled wheel and cannot be
locked; pydantic 1.10.22 is pure):

1. `from pydantic import AliasChoices, ...` -> `from pydantic import BaseModel, Field`
   (`field_validator` was imported but never used upstream).
2. `validation_alias=AliasChoices("target_rate_per_minute", "target_rate")`
   -> `alias="target_rate"` plus `allow_population_by_field_name = True`,
   which accepts exactly the same two input keys under pydantic v1.

Validation semantics are unchanged; `tests/core/test_factorio_vendored_core.py`
pins that with a differential test against the upstream tree.

See THIRD_PARTY_NOTICES.md for licensing.
"""
