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

## State of the branch (2026-08-07)

Everything below is committed and pushed on the
**`phase-1-release-prep`** branch of `MINDS-THU/OptPilot`:

- `ab46b0a` — Phase 0 hygiene, F1 typed inputs, F2 per-launch Study inputs
- `5dce81d` — F3 command methods, F4 resource actions, F5 capabilities,
  U1 launch-input forms
- `98b8bb3` — remaining U1 forms, Studio reliability fixes, Run trial map
- `727ddbc` — Assistant quality pass
- `25ad874` — Assistant dispatch fixes (send timeout, stale-finished guard)

Phase 2 starts from this branch. Remaining owner chore: `_to_delete/` at the
repo root holds transfer artifacts and stale git index locks; delete it.

Full-suite state on the owner's machine (2026-08-07): ~2,460 tests with 4
known pre-existing, environment-dependent failures — all reproduced
identically on a pristine `ab46b0a` worktree (three
`tests/studio/test_mvp.py` UI-source assertions and one
`test_realm_study_definition_ledger` migration transaction count). Run the
full suite once before changing anything to establish YOUR baseline, and
diff failure sets rather than eyeballing counts.

## Completed so far (all verified and pushed)

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

**F3 — command-protocol batch methods (retained execution).**
- The retained worker now executes command batch methods:
  `_RetainedCommandBatchMethod` in `src/optpilot/retained_batch_worker.py`
  runs one bounded subprocess per proposal exchange (JSON stdin/stdout or
  `{input_file}`/`{output_file}`, per `docs_assets/methods.md`). The worker
  env is PATH-free, so `command[0]` must be `python`/`python3` and is mapped
  to the worker's prepared interpreter; retained import roots (incl. locked
  dependency layers) are passed via `PYTHONPATH`; `envFromHost` values reach
  the subprocess; `exchangeTimeoutSeconds` bounds each invocation. Observe
  exchanges are acknowledged, not forwarded (evidence rides each proposal).
- Gates lifted precisely: `_preflight_first_slice` and
  `_validate_retained_package` (retained_study_compiler),
  `_validate_batch_definition` (retained_batch_runtime),
  `_validated_method_contract` (worker), and
  `_retained_method_execution_capability` (package_validation → new
  smoke-eligible code `method_command_unchecked`). New typed failure codes:
  `method_command_unsupported` (non-interpreter head),
  `method_command_unretained` (missing `python script.py` script). Command
  *evaluators* stay validate-only. Shared constant:
  `RETAINED_COMMAND_METHOD_INTERPRETERS` in `method_protocol_limits.py`.
- Tests: `RetainedCommandBatchWorkerTest` (7, incl. supervised socket
  worker) in `tests/core/test_retained_batch_worker.py`; compiler positive +
  failure-code cases; package-validation capability cases. Docs updated in
  `docs_assets/methods.md` + `configuration.md` and mirrored to `docs/`.

**F4 — operable Resource actions (core + CLI).**
- New top-level `resource.actions` (schema `resourceAction` in
  `defs/common.schema.json`): named commands with typed `inputs`
  (parameterMap), `grants.envFromHost`/`secretsFromHost` (fail-closed),
  optional process `runtime` with `setup`, `timeoutSeconds` ≤ 86400. Typed
  `ResourceActionSpec` + `compile_resource_actions` + headless
  `run_resource_action` in `src/optpilot/resource_actions.py`; validation
  wired into `_validate_resource_semantics` (config.py imports the module —
  keep resource_actions free of config imports at module level; the executor
  lazily imports `validate_authoring_config`).
- Execution contract: inputs JSON at `OPTPILOT_RESOURCE_ACTION_INPUTS_FILE`,
  results under `OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT` (must be fresh),
  command-token placeholders `{inputs_file}`/`{output_root}`/`{input:<key>}`
  (scalar-only, validated against declarations). `runtime.setup` runs in the
  resource root before the command unless `--skip-setup`.
- CLI: `optpilot resource list <resource.yaml>` and `optpilot resource run
  <resource.yaml> <action> --input k=v --inputs-file f --output-dir d`.
- Deliberately NOT built on Studio's interface output-action executor (that
  is a network-disabled container jail bound to a live launch's
  prepared-runtime lease — wrong shape for generation actions needing LLM
  access). Interface `outputs.actions` are unchanged. Studio/Assistant
  surfaces for actions arrive with U1 forms, consuming the same compile.
- Tests: `tests/core/test_resource_actions.py` (17). Docs: "Resource
  Actions" in `catalog.md`, mirrored to `docs/`.
- Correction: `.optpilot/resource_setup` (mentioned in older notes) never
  existed; the real mechanism is `interface.runtime.setup` /
  `action.runtime.setup`.

**F5 — formalized environment capabilities (framework side).**
- Capability declarations (`environment.capabilities[]`) gain optional
  `callable` (`module:object`; semantic validation + retained source-backed
  check under environment import roots). When a method requires such a
  capability, `_capability_environment_roots` in
  `retained_study_compiler.py` appends the environment package import roots
  to the method runtime (deduped) — replaces the cross-package `pythonPath`
  hack. Worker test `RetainedCapabilityImportRootTest` proves the hack-free
  path.
- New environment `policyValidation` block (entrypoint, forbiddenImports,
  forbiddenNames, string-constant lints) — declaration validation and the
  generic AST checker live in `src/optpilot/policy_validation.py`
  (`validate_policy_declaration` / `validate_policy_sources`). Carried in
  candidate context ONLY when declared, so existing candidate contracts and
  run-definition digests are unchanged. Methods apply it generically; the
  checker ports the AGV method's `_validate_policy_sources` semantics.
- `production_agv_scheduling/environments/.../environment_llm.yaml` now
  declares `callable: evaluator:replay_candidate` and a `policyValidation`
  block (additive — the method's own hardcoded checks keep working; W2
  rebases the method onto the declarations).
- Tests: `tests/core/test_policy_validation.py` (9); capability compile
  cases + policy retention in `test_retained_study_compiler.py`. Docs:
  `configuration.md` (environment reference), `candidate-contracts.md`
  (context table), mirrored to `docs/`.


**Studio completion sprint (2026-08-07, commits 98b8bb3..25ad874).**
**U1 — contract-generated forms (all three consumers done).**

   (a) `study.inputs` launch form (see below). (b) Candidate-parameter
   "Search space" card in the Run setup detail (`studySearchSpacePanel` in
   app.js, typed rows from `environment.raw_config.candidate.parameters.
   schema`, flat + one nesting level). (c) Resource actions in Studio:
   `POST /api/resource-actions/run` + `GET /api/resource-actions/<id>`
   (in-process run registry `state._resource_action_runs`, executes
   `run_resource_action` in a thread against the configured-catalog resource
   folder; published-projection resources not yet runnable), client Actions
   panel on the resource detail (`resourceActionsPanel` + generalized
   `typedDeclarationField`), tests in
   `tests/studio/test_studio_resource_actions.py` (5). Follow-up: an
   Assistant tool for filling/running resource actions (approval-gated, like
   `optpilot_study_launch`) is not wired yet.
   Run detail (2026-08-07): new trial-centric "Trials" map on the Run page
   (`runTrialMapHtml`/`runTrialNodes`/`bindRunTrialMap` in app.js) — one chip
   per accepted trial in order (status-colored, live-updating, ★ best so
   far, dashed ghosts for planned budget), click opens an inline inspector
   (status, result, candidate, attempts) with jump buttons into the
   Candidate/Attempts/Observations views. Chip values come from the loaded
   candidate page only; deeper pages still need Load more.
   Assistant quality pass (2026-08-07): root causes of the "confusing
   responses" on welcome-page intents were (1) a stale OpenHands
   agent-server holding an outdated cached OptPilot tool schema — every
   message errored with "restart the OpenHands agent server" until the
   server process was restarted (operational note: restart `agent-server`
   after tool-schema changes), (2) sessions orphaned by service restarts
   sitting in `waiting_for_agent` forever until the next sync marks them
   "Assistant restarted" (sync heals them; the client only syncs busy
   sessions while polling), and (3) response behavior: for a broad intent
   like "open and explore a simulator", the agent (driven by OpenHands'
   native software-agent prompt; OptPilot guidance is only a
   system_message_suffix from `assistant_assets/prompts/system.md`) dives
   into Workspace creation and multi-minute tool loops before any reply.
   Added an "Opening moves for broad goals" section to system.md: answer
   after at most a Catalog inspection, 1–3 fitting entries, exactly one
   proposed next action, no Workspaces/package plans on a broad opening, no
   internals jargon. Re-verify adherence per configured backbone; further
   candidates: collapse assistant-initiated `workspace_attached` banners in
   the timeline (noise before substance), and an Assistant-side resource
   action tool.
   Bug fixes shipped alongside: tolerant Study-launch listing
   (`_tolerant_views` in `study_launch_service.py` — one v2-era record no
   longer kills `/api/jobs` and, critically, startup reconciliation, which
   had left a live run orphaned after a Studio restart; regression test in
   `test_realm_study_launch_service.py`), archived-Conversations browser
   (`GET /api/agent-sessions?archived=1` + restore UI at the bottom of the
   conversation list), and the onboarding-flash fix (transcript merge +
   sticky conversation-started in app.js,
   `tests/studio/test_studio_assistant_transcript.py`).

**U1 slice — study.inputs launch form (done).**
- Server: Studio launch request schema accepts optional `inputs` (all three
  variants; `_canonical_study_launch_inputs` bounds transport shape);
  threaded `launch_study → prepare_selected_package/plan_local_package`
  (core `prepare_selected_package` gained `launch_inputs`); durable intent
  replay carries inputs. `_validate_study` compiles with
  `bind_launch_inputs=False` (bug fix: required-input studies no longer
  report `study_invalid`) and exposes `inputs` in the validation payload.
- Client: "Launch inputs" card on the Run setup detail; typed fields per
  valueType; client-side parse/validation (core authoritative); values sent
  as `request.inputs` and persisted with the stored launch request
  (`state.studyLaunchInputDrafts` / `studyLaunchInputErrors`).
- Tests: 5 in `test_studio_study_launch_capability.py`, plus unmocked HTTP
  end-to-end `test_http_study_launch_binds_declared_typed_inputs` in
  `test_studio_realm_runs.py`. Docs: Studio paragraph in configuration.md's
  "Per-Launch Study Inputs".

**Assistant dispatch fixes (25ad874).** Client message timeout 15s→60s
(healthy dispatches execute Catalog tools inline and measured 23s; the
old timeout surfaced them as "Studio did not respond in time").
Completion detection settles turns that end with a plain final
MessageEvent (newest execution_status=finished, required to be newer
than the latest user message; silent-finish fallback only in the sync
path).

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

## What to do next: Phase 2 (in priority order)

Framework primitives F1–F5 and the U1 forms are all done (see "Completed so
far"). Phase 2 is the integration workstreams of plan §5 plus the remaining
Studio items of plan §4. Read `designs/initial-release-plan.md` §5–§9 before
starting any item; per-item scope, risks, and acceptance criteria live there,
and inline status notes mark what is already built.

1. **W1 — DEVS-Gen flagship** (plan §5.1, 5 items). Highest value first:
   close the metrics gap (`devs.simulation.v2` with declared metric keys →
   launch-ready generated environments); register 2–3 gallery simulators as
   catalog Environments; trace-conformance smoke checks; headless
   generation as an F4 resource action (author a `generate` action on
   `catalog/example_package/resources/devs-gen-interface` — core executor,
   CLI, and the Studio Actions panel already exist); hygiene.
2. **W2 — trace-aware LLM policy search generalization** (plan §5.2).
   Split `production_agv_scheduling/methods/process_aware_llm` into a
   general `llm_policy_search` method + DES environment template. The F5
   contracts to consume are already declared by the AGV environment:
   read AST rules from `context.policyValidation` (apply them with core
   `optpilot.policy_validation.validate_policy_sources`) and resolve replay
   through the `exact_seed_replay` capability callable (the runner already
   supplies the environment import roots — drop the cross-package
   `pythonPath` hack from method.yaml).
3. **W3 — COOPA or_solving package** (plan §5.3). Items 1–3 DONE
   (2026-08-07): `catalog/or_solving` ships OptPilot-original code only —
   COOPA stays user-provisioned via `COOPA_HOME` (license blocker applies to
   bundling COOPA, not to this package). `coopa-solver` F3 command method +
   pruned shim (no web/knowledge agents), `or-problem` artifact-validating
   environment, `solve-or-problem` with `inputs.problem`, and an explicitly
   labeled mock twin verified end-to-end through the retained runner.
   Real pipeline ALSO verified end-to-end (2026-08-07): LP test problem
   through Studio's retained runner → predicted 36.0 (exact optimum),
   solved=1.0, full artifact retained (manager routing, deepseek via
   OpenRouter). Worker-env lessons baked into the package (keep them if
   you touch it): the shim sets fallback PATH + workspace HOME before
   COOPA imports (the retained worker env is PATH/HOME-free), and
   `coopa_solver.py::_protect_stdout` dups the real stdout and points
   fd 1 at stderr — the worker parses the ENTIRE stdout as JSON while
   COOPA agents and generated solver subprocesses print to fd 1.
   Remaining: item 4 (`or_benchmark` dataset environment reusing
   `checks/score_results.py` logic), item 5 license resolution.
4. **W4 — Factorio design benchmark** (plan §5.4). Static-validation-first;
   Direct baseline is a Python batch method; **confirm canonical target
   rates** (below) before publishing numbers.
5. **W5 — DEVS-Gen × policy-search joint workstream** (plan §5.5), after
   W1/W2.
6. **Remaining U-items** (plan §4): U2 finish the "Run setup" client-side
   rename; U3 Open work completeness; U5 interface context; U6 dynamic
   onboarding; U7 legacy-shell removal (last); U8 optional.
7. **Studio follow-ups queued from the 2026-08-07 review** (small, good
   first tasks): an approval-gated Assistant tool for filling/running F4
   resource actions (mirror `optpilot_study_launch` in
   `_execute_agent_tool`); collapse assistant-initiated
   `workspace_attached` banners in the conversation timeline (noise before
   substance); resource actions for published-projection (non-configured)
   catalog resources; re-verify Assistant prompt adherence ("Opening moves"
   section of `assistant_assets/prompts/system.md`) whenever the configured
   backbone model changes; trial-map chips only show values from the loaded
   candidate page (deeper pages need Load more).

Operational notes for Phase 2 development:
- Restart the OpenHands `agent-server` process after any OptPilot
  tool-schema change; it caches the schema per process and every message
  errors until restarted.
- `.claude/launch.json` boots Studio on port 8866 (`optpilot-studio`);
  one Studio process supervises a project's workspace runtimes at a time.
- **Never keep `.venv` inside this repo when it sits on Synology Drive**
  (or any sync-managed folder). Sync repeatedly corrupted the venv during
  development (stale NFS file handles reading `*.dist-info`, conflict
  duplicates like `rich-15.0.0 2.dist-info`, stale directory listings) and
  one such corruption failed a live retained run. The working setup: the
  venv lives at `~/.optpilot-venvs/OptPilot-venv` and everything uses
  `UV_PROJECT_ENVIRONMENT` to point uv at it — `.claude/launch.json`
  already sets it; export it in your shell too before `uv run`/`uv sync`.
  Note `uv pip` does NOT honor it — pass
  `--python ~/.optpilot-venvs/OptPilot-venv/bin/python` explicitly.
- OpenHands stack pins: `openhands-agent-server/-sdk/-tools/-workspace
  ==1.40.1` (+ `libtmux`); these are not in the lockfile's default set, so
  reinstall them after any venv rebuild.

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
