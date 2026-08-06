# CLAUDE.md — Release-preparation handover

This file briefs an AI coding agent (or a new contributor) continuing the
**initial-release implementation** of OptPilot. Read `AGENTS.md` first for
build/test/monorepo basics; this file covers the release effort: what it is,
what is already done, and exactly what to do next.

## The governing plan

**`designs/initial-release-plan.md` is the contract for this effort.** It was
produced from a full audit (2026-08-05/06) of the core, Studio, the catalog
packages, and the four research codebases under `resource/` (untracked
scratch), and it defines:

- Framework adjustments **F1–F8** (§3)
- Studio UI completion items **U1–U8** (§4)
- Integration designs for the four flagship works (§5): DEVS-Gen (W1),
  trace-aware LLM policy search generalization (W2), COOPA (W3), Factorio
  Design Benchmark (W4), plus the joint DEVS-Gen × policy-search workstream
  (W5, §5.5)
- Package-distribution direction **D1–D4** (§6): packages as repos, zip/GitHub
  import, validation gate, Assistant repair loop
- Phased execution (§7), release criteria (§8), risks (§9)

Do not re-derive the design; follow the plan and update it when reality
diverges. `designs/pre-release-fix-plan.md` carries per-item status notes.

## State of the working tree (2026-08-06)

The working tree contains a large body of **uncommitted, fully verified**
changes (Phase 0 + F1 + F2, described below). Nothing has been git-committed;
the human owner reviews and commits. Two manual follow-ups are pending for the
owner:

1. `ci.yml.updated` at the repo root must replace `.github/workflows/ci.yml`
   (workflow files were write-protected for the remote agent). It adds a
   core-only test step (`discover -s tests/core`) to the clean-venv core-wheel
   check. Delete `ci.yml.updated` after moving it.
2. `_to_delete/` at the repo root holds transfer artifacts and can be deleted.

Also: the public GitHub remote (`MINDS-THU/OptPilot`) is ~5 weeks behind local
HEAD `82420ea`. Pushing is Phase 0 business and needs the owner.

## Completed so far (verified, uncommitted)

**Phase 0 — hygiene and terminology.**
- User-facing "Run setup" wording finished across the client
  (`studio/.../ui/static/app.js` ~60 strings, `index.html` nav, Open work's
  "Run preparation" unified) and docs. The `study` schema, API routes, CLI,
  card-kind slug `run-setup`, internal view id `experiments`, and the legacy
  shell (`?shell=legacy`) are deliberately unchanged.
- Design docs promoted from `resource/` into `designs/` (5 files, with status
  notes). `MANIFEST.in` and `scripts/check_release_artifacts.py` now forbid
  `resource/`, `design/`, `designs/`, `catalog/` content in all distributions.
- **Tests split** (fix-plan §8): `tests/core/` (no `optpilot_studio` imports;
  runnable against the bare core wheel) and `tests/studio/`. Discovery from
  `tests/` still finds everything (2,392+ tests). `tests/studio/test_mvp.py`
  still mixes some core-only cases — extracting those into `tests/core` is
  noted follow-up work (see its module docstring).

**F1 — typed input declarations (the keystone).**
- `method.settingsSchema`, `environment.evaluator.settingsSchema`, and
  `resource.inputs` added to the JSON schemas, all referencing the new
  `defs/candidate.schema.json#/definitions/parameterMap` (reusing the existing
  `parameter` definition).
- New `src/optpilot/parameter_values.py`: `validate_parameter_values` +
  `apply_parameter_defaults` (types, bounds, categorical membership, pattern,
  nested arrays/objects, undeclared-key rejection; a declared entry without
  `default` is required).
- Enforcement wired into `_validate_method_semantics`,
  `_validate_environment_semantics`, `_validate_resource_semantics` in
  `src/optpilot/config.py` — so `optpilot validate`, `optpilot package
  validate`, and study compile all enforce it. Untyped `settings` without a
  schema behave exactly as before.
- Tests: `tests/core/test_typed_settings.py`. Docs: `configuration.md`
  ("Typed Settings With settingsSchema"), `catalog.md` (resource `inputs`).

**F2 — per-launch Study inputs.**
- `study.inputs` declaration (same `parameterMap`); launch-time values via
  `optpilot run ... --input key=value` (repeatable, YAML-scalar parsed) and
  `--inputs-file file.yaml` (`--input` wins).
- Binding happens in `compile_authoring_config(path, launch_inputs=...)`
  (`src/optpilot/config.py`): defaults applied, values validated, then merged
  into the compiled evaluator config and method config/settings under the
  reserved key `"inputs"` (collision with a pre-existing top-level `inputs`
  settings key fails validation). Threaded keyword-only (default `None`)
  through `spec.load_study_spec` → `realm/local_study_package.py` →
  `retained_study_service.prepare_local_package` →
  `study_launch_service.plan_local_package` → `realm_study_runner.
  run_local_realm_study` → `runner.StudyRunner` → `cli.py`.
- Retention: inputs land inside the retained environment/method contracts and
  therefore inside `run_definition_digest` — same study + same inputs → same
  digest; different inputs → different digest. These are problem payloads and
  are **retained in evidence** (deliberate contrast with `envFromHost`, whose
  values stay out).
- Fail-closed: inputs supplied to a study that declares none; missing
  required (no-default) input; undeclared keys; reserved-key collision — all
  error before any Realm mutation. Authored code sees
  `context["settings"]["inputs"]` (evaluator) / `settings["inputs"]` (method).
- Tests: `tests/core/test_study_inputs.py` (22). Docs: "Per-Launch Study
  Inputs" in `configuration.md`; flag mention in `getting-started.md`.
- Studio launch paths are untouched and keep working (they pass no inputs; a
  study requiring inputs fails a Studio launch with the core error). Studio
  UI for inputs is **U1** work, not done yet.

## Verification protocol used (keep following it)

- Full suite on the final tree: 2,392 tests, **zero regressions**. 55 failures
  are pre-existing and environment-dependent — they fail identically on the
  untouched baseline in the cloud container (realm timing/subprocess-worker
  tests, Studio tests needing code-server/OpenHands, dependency verticals).
  On a proper dev machine (`uv sync --all-packages`), most should pass; run
  the full suite once before changing anything to establish YOUR baseline,
  and diff failure sets rather than eyeballing counts.
- Docs under `docs/` must stay **byte-identical** to
  `studio/src/optpilot_studio/docs_assets/` (a studio test asserts this).
  Edit in `docs_assets/`, then copy.
- Bundled packages must stay green:
  `optpilot package validate catalog/example_package` and
  `catalog/production_agv_scheduling`.
- After JS edits: `node --check studio/src/optpilot_studio/ui/static/app.js`.

## What to do next (in order)

1. **F3 — execute command-protocol batch methods** (plan §3). The schema
   already constrains `command → batch` and `docs_assets/methods.md`
   documents the stdin/stdout JSON exchange; only
   `_preflight_first_slice` in `src/optpilot/retained_study_compiler.py`
   (~L815–1000, code `method_mode_unsupported`) rejects it. Implement the
   command exchange in the method worker path
   (`retained_batch_worker.py` / `method_runtime.py` /
   `realm/_local_attempt_worker.py` — read how Python batch methods are
   spawned first), lift the preflight for command+batch methods only
   (command *evaluators* stay validate-only), and add worker-level tests.
   COOPA (W3) and Factorio's Direct baseline (W4) depend on this.
2. **F4 — operable Resource actions** (generalize `interfaceOutputAction`
   into named commands with typed `inputs`, runnable headless). Unblocks
   DEVS-Gen headless generation (W1 item 4).
3. **F5 — formalized environment capabilities** (`exact_seed_replay`
   resolution, `policyValidation` block). Required by W2.
4. **U1 — contract-generated forms** in Studio, consuming
   `settingsSchema` / `resource.inputs` / `study.inputs` (all three now
   exist in core — build the renderer against real declarations).
5. Then the Phase 2 integration workstreams W1–W5 per plan §5, and the
   remaining U-items (U3–U8) per plan §4.

Non-code blockers for the owner (start early, they gate shipping W3/W4):
COOPA license (`resource/reproduce-COOPA-BC8B/code/coopa/` has no LICENSE) and
Factorio canonical target rates (repo configs vs paper Table 1 differ for 4
product families).

## Gotchas

- `tests/` is now a regular package (`__init__.py` in `tests/`, `tests/core/`,
  `tests/studio/`); cross-test imports use `tests.core.<module>`; several
  moved files use `Path(__file__).resolve().parents[2]` for repo root and
  `parent.parent / "fixtures"` — preserve that if you move tests again.
- `compile_authoring_config` / `plan_local_package` /
  `prepare_local_package` grew keyword-only params with defaults; never make
  them positional or required — Studio and tests call the old shapes.
- `validate_authoring_config` compiles studies with
  `bind_launch_inputs=False` so required-input studies still validate
  statically. Don't "fix" that.
- The terminology rule: user-facing = "Run setup"; schema/API/CLI/docs
  config-kind = `study`. Legacy-shell strings stay as-is until U7 removes the
  legacy shell entirely.
- The four research codebases under `resource/` are untracked scratch and
  must never ship or be imported by shipped code. Integration happens by
  creating proper catalog packages (plan §5), not by referencing `resource/`.
