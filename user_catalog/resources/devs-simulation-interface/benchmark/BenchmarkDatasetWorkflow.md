# Benchmark Dataset Construction Workflow

This document defines a standard process for building scalable simulation benchmarks with three deliverables:
- clear `description` prompts,
- reliable `data/config` (single-run + multi-run),
- robust `checker` logic.

It is designed for expanding the dataset with minimal ambiguity and stable evaluation behavior.

## 1) Target Outcome Per Benchmark

Each benchmark folder should contain:
- `description.yaml` (or `<Name>.yaml`): prompt contract for model generation.
- `run.py`: reference simulator/oracle implementation.
- `checker.py`: score computation + rule diagnostics.
- `config.json`: L1/L2 entries.
- `golden_data/*.json`: multi-run distribution baselines.
- `tools/generate_golden.py`: reproducible golden data generator.

Recommended structure:
```text
benchmark/<CaseName>/
  description.yaml
  run.py
  checker.py
  config.json
  tools/generate_golden.py
  golden_data/<case>_<horizon>_distribution.json
```

## 2) Description Writing Standard

Follow 3 sections only.

### 2.1 `general`
- Fixed environment/interface requirements only.
- Include language/runtime, CLI requirement, seed determinism requirement, stdout/stderr conventions.
- Do not include scenario-specific business logic details here.

### 2.2 `scenario`
- Business behavior only: topology, policies, timing semantics, KPI semantics.
- Use explicit parameter tables where numeric alignment is required.
- Avoid code-level instructions (no implementation-specific APIs, no class/function names).
- Avoid "forbidden implementation" wording in this section.

### 2.3 `args_input_output`
- Define CLI args and stdin contract.
- Define required stdout JSONL event(s) and required fields.
- Explicitly state: extra events/extra fields are allowed; checker ignores unrelated logs.

## 3) Reference/Oracle Build

1. Build a reference `run.py` that reproduces scenario semantics.
2. Lock deterministic behavior for fixed seed (`random` + `numpy`).
3. Emit at least one required summary event (e.g., `sim_trace`).
4. Preserve KPI definitions consistently (units, time integration method, boundary timing).
5. Record a known-good L1 seed/horizon KPI tuple as calibration anchor.

## 4) Checker Design Rules

Checker must be strict on required business semantics, but permissive on logging noise.

### 4.1 Input tolerance
- Parse JSONL lines; ignore malformed/non-JSON lines.
- Filter only target business event(s) (e.g., `event == "sim_trace"`).
- Ignore additional event types and extra fields.

### 4.2 Rule layers
- **Schema rules**: required event/field presence and types.
- **Identity rules**: KPI equations (e.g., `profit = revenue - total_cost`).
- **Bounds rules**: non-negativity and logical inequalities.
- **Expected-value rules (L1)**: exact/near-exact anchors with tolerance.
- **Distribution rules (L2)**: KS/degenerate checks against golden data.

### 4.3 Multi-run robustness
- Support `golden_data_path`, `min_samples`, KS thresholds.
- Handle degenerate distributions explicitly.
- Provide fallback if SciPy unavailable.

## 5) Config/Data Standard

Use at least two entries:

1. **L1 (single-run exactness)**
- fixed seed + fixed horizon.
- include expected KPI values in `checker_args`.

2. **L2 (multi-run distribution)**
- multiple seeds (explicit list preferred over `${RANDOM}` for reproducibility).
- compare selected KPIs with golden distribution.

Seed policy recommendation:
- L2 use explicit seed set, e.g., 0..9.
- golden data use wider set, e.g., 0..29.

## 6) Golden Data Generation

1. Implement `tools/generate_golden.py`.
2. Run reference simulator for N seeds.
3. Extract only metrics used in distribution rules.
4. Save as JSON with metadata (horizon, seed range, run count).

## 7) Validation Gates (Before Scaling)

Pass all gates before adding new cases:

1. Reference run passes full config at score 1.0.
2. L1 metrics stable and reproducible.
3. L2 KS passes with explicit seed set.
4. Checker still passes when extra unrelated logs are added.
5. Checker output is valid JSON and includes actionable rule diagnostics.

## 8) Failure Triage Playbook

When generated model fails, classify root cause first.

### A. Description underspecified
Symptoms:
- Model uses different policy values/topology/cost scales but still structurally valid.

Action:
- Add missing business constraints to `scenario` (parameters, routing, policy cadence, KPI semantics).

### B. Generated model implementation bug
Symptoms:
- Violates explicit scenario constraints already written.

Action:
- Patch generated artifacts directly; do not regenerate blindly.

### C. Checker too strict or misaligned
Symptoms:
- Rejects valid extra fields/events; over-assumes log minimality.

Action:
- Filter to required business events; ignore unrelated logs/fields.

### D. Oracle mismatch
Symptoms:
- L1 anchor unstable or inconsistent across environments.

Action:
- Re-verify reference simulator and KPI extraction path; refresh golden data.

## 9) Practical Checklist (Copy/Paste)

- [ ] `description` has clean `general/scenario/args_input_output` separation.
- [ ] `scenario` includes all numerically critical business parameters.
- [ ] `args_input_output` explicitly allows extra logs/fields.
- [ ] reference `run.py` deterministic under fixed seed.
- [ ] checker filters event types and ignores unrelated logs.
- [ ] L1 fixed-seed expected KPIs set.
- [ ] L2 explicit multi-seed cases set.
- [ ] golden data generated from reference and versioned.
- [ ] `eval_pipeline` reference score is 1.0.
