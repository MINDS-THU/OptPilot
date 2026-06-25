# Blueprint Text Guidelines

This guideline focuses on text quality for `scenario_blueprint.py` fields that are transformed into `description.yaml`.

## 1) Metadata and Objective Text

- `metadata.description` must be one clear business objective sentence.
- `metadata.products_mapping` must use stable string product IDs like `"1"`, `"2"`.
- Avoid vague terms like "custom logic" without physical meaning.

## 2) CLI and Stdin Text

- Every `cli_args_schema` entry must include: `type`, `default`, and a concrete `description`.
- Parameter descriptions should state business meaning, not implementation details.
- `stdin_schema.format_description` should explain exact token format if stdin is used.

## 3) Topology Text-Carrying Fields

- Provide explicit per-group values for `initial_inventory`, `policy`, and any lead-time/cost fields.
- Use consistent field names (`lead_time` or `lead_time_days`, `holding_cost` or `holding_cost_per_unit`) and keep semantics explicit.
- If behavior is hook-overridden, still provide a valid default policy as fallback semantics.

## 4) Hook Docstring Rules (Critical)

- Each hook function docstring should state:
  - scope (which product/group),
  - trigger condition,
  - exact arithmetic behavior,
  - output meaning.
- Keep docstrings deterministic and unit-aware (per day, per unit, etc.).
- Prefer "is/equals" style wording over ambiguous prose.

## 5) Event Schema Text Rules

- Event names should be stable, uppercase, and semantically unique.
- `event_schema[event].description` should define when the event is emitted.
- Every key needs exact type and physical meaning in `keys`.

## 6) Description Sufficiency Checklist

A blueprint text is considered sufficient if generated `description.yaml` clearly contains:

- objective and product vocabulary,
- temporal/accounting semantics,
- full topology defaults by group,
- integrated hook semantics and examples,
- connectivity and expansion rules,
- strict JSONL required-event contract,
- KPI measurement contract.

## 7) Common Text Gaps to Avoid

- Missing hook docstrings (hook behavior not surfaced in scenario text).
- Topology defaults incomplete (cost/lead-time/policy omitted).
- Event fields lacking units or physical meaning.
- CLI parameter descriptions that only restate type/default.
- Mixed naming that obscures intended semantics (for example role names not matching node IDs).

## 8) Stochastic Scenario Rules

If a scenario is stochastic (random demand/noise/sampling), require explicit seed control:

- Add `seed` to `cli_args_schema` with type `int` and a stable default.
- In hook docstrings, explicitly say which randomness is seed-controlled.
- In `test_cases`, set seed policy fields when needed:
  - `seed_mode`: `"incremental"` (recommended) or `"fixed"`
  - `seed_start`: starting integer seed
  - `seed_arg`: optional seed CLI name (default `seed`)
- Expect per-run evaluation seeds to differ when `seed_mode="incremental"`.

**How hooks access the seed:**

- The oracle runner automatically injects `seed` into `DYNAMIC_ARGS` regardless of whether the blueprint declares it as a CLI arg. Hooks read it via `globals().get("DYNAMIC_ARGS", {}).get("seed", 42)`.
- In stochastic scenarios, the demand/policy/sampling logic should use `PYTHON random.Random(seed * factor + period)` for deterministic-but-noisy per-period values.
- See `stochastic_seeded_noise/stochastic_seeded_noise_blueprint.py` for the canonical pattern.

**KPI stability for stochastic scenarios:**

- Single-KPI recommendation: for small `oracle_runs` (e.g., 12–24), prefer one broad aggregate metric (e.g., `total_holding_cost`) over narrow or zero-inflated metrics (e.g., `total_stockout_cost` which may be sparse and unstable).
- If multiple KPIs are needed, increase `oracle_runs` (e.g., 100+) to stabilize the distributions.

Note: deterministic scenarios do not need a `seed` CLI arg.
