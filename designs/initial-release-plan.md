# OptPilot Initial Release: Architecture Audit and Implementation Plan

Status: proposal for review · Prepared 2026-08-05 · Audited against local HEAD `82420ea` ("fix minor ui bugs")

This document is the contract for preparing OptPilot's first official release. It reflects a full audit of the core framework (`src/optpilot`), Studio (`studio/src/optpilot_studio`), the catalog packages, and the four research codebases under `resource/`, together with the four papers.

Release thesis: **OptPilot ships as a practitioner-oriented coordination platform for LLM-assisted optimization in IE/OR**, with four flagship capabilities from the group's research — the DEVS-Gen simulator generator, a generalized trace-aware LLM policy-search method with DES environments, COOPA for natural-language OR solving, and the Factorio Design Benchmark — registered through the same public contracts any future user integration would use. The four works are the proof that the contracts are general; nothing about them may be special-cased in core.

---

## 1. Where the project actually stands

The audit produced a much more positive picture than the working assumption that the UI transition is "in progress with much remaining":

**The conversation-first Studio shell is largely built.** Conversation is the default surface (`shellModeFromLocation()`, `app.js`); hash routes exist for `#/conversations`, `#/catalog`, `#/studies`, `#/runs`, `#/workspaces`, `#/interfaces/...`; the new-conversation onboarding state (capability explainer, five suggested intents, registered-Catalog summary) is implemented (`renderConversationOnboarding()`); the assistant card contract (`optpilot.studio-ui-card.v1`, kinds `catalog-use` / `run-setup` / `run`, allowlisted operations, sanitizer in `agent.py`) is enforced server- and client-side; full-stage interfaces with the **Ask from this page** overlay preserve live interface sessions; **Open work** exists as a pure projection (`buildOpenWorkItems()`). Both P0 security items from `resource/pre_release_fix_plan.md` (static-path traversal, assistant permission enforcement) are fixed in this tree. `TODO/FIXME` count across `src/optpilot` and `studio/src`: zero.

**The genuinely unbuilt UI piece is step 6 of the interaction model** — generating a simple input form from a registered YAML contract when a capability has no custom interface. Nothing in Studio consumes `candidate.parameters.schema` (or anything else) to render an input form, and — the root cause — **core has no typed declaration for most inputs**: `method.settings`, `evaluator.settings`, and `metrics.settings` are free untyped objects, and a Resource declares no inputs at all. Only the candidate parameter definition (`defs/candidate.schema.json#/definitions/parameter`: valueType, min/max, values, default, description, unit, recursive items/properties) is form-able today.

**Core is clean but deliberately narrow.** The executable slice (enforced centrally by `_preflight_first_slice` in `retained_study_compiler.py`, failing closed with stable codes) supports parameter and bounded file candidates, source-backed Python `batch` methods, Python evaluators, and process runtime with hash-locked *pure-Python* wheels. Command methods, command evaluators, `session` protocol, opaque candidates, containers, and native wheels validate but do not run. Two of the four integrations (COOPA, Factorio-with-game) press directly on these limits.

**Integration state of the four works:**

| Work | State | Key gap |
| --- | --- | --- |
| DEVS-Gen | Registered as Resource `devs-gen-interface` in `catalog/example_package`, with an e2e-tested pipeline: interface → published bundle (`devs.simulation.v1`) → Studio detects it → environment starter + hardened adapter | Metrics placeholder (`score`, `needs_editing: true`); trace-conformance checks unused; gallery simulators unregistered; no headless generation path |
| Trace-aware policy search | Fully integrated as `catalog/production_agv_scheduling` (7 env variants, 4 method families, 8 studies) | Method is hard-coupled to this environment at ~6 identified points; not reusable by practitioners on their own simulators |
| COOPA | Not integrated (`resource/reproduce-COOPA-BC8B`); research code, runnable, well-audited artifact | No OptPilot registration of any kind; native solver deps (OR-Tools, pymoo, GLPK/IPOPT) exceed the executable slice; **no license file on `code/coopa/`** |
| Factorio Bench | Not integrated (`resource/Factorio-Design-Benchmark-main`); verified in audit: static validation runs end-to-end with only `pydantic` | No registration; game-execution mode depends on the proprietary Factorio headless server (must stay user-provisioned) |

**Release logistics gaps:** the public GitHub remote is ~5 weeks behind local HEAD; `tests/` mixes 55 Studio test files among 177 total (sdist test-surface problem, fix-plan §8); "Run setup" wording is applied on server-side cards but the client still says "Study"/"Studies" (plus a third term, "Run preparation", in Open work); the legacy shell survives behind `?shell=legacy` with duplicated render paths.

---

## 2. Design position

Three judgments shape everything below.

**2.1 The release's unit of value is the contract, not the tool.** Each of the four works must land as an ordinary catalog package exercising only public schemas. Where a work cannot be expressed, that is a framework gap to fix (or a documented v1 exclusion), never a bypass. This is what makes the platform credible for future integrations by the group or by users.

**2.2 One-time solving becomes a first-class presentation, not a new entity.** The use case "apply one registered Method to a problem described in natural language, files, data, or code — once" (COOPA's natural shape, and use case 3 of the product statement) is today impossible without authoring env+method+study YAML by hand. The fix stays inside existing concepts: a Study with `budget.maxTrials: 1` **is** the one-time solve; what's missing is (a) per-launch inputs so the problem payload can vary without editing configs, and (b) a Studio presentation ("Apply a method" — one of the five onboarding intents already shipping) that assembles that Study behind a generated form. Run evidence, budget, and approval semantics come along for free. No new persisted entity is introduced.

**2.3 Typed inputs are the keystone adjustment.** One schema addition — reusing the existing `parameter` definition for `method.settingsSchema`, `evaluator.settingsSchema`, `resource.inputs`, and per-launch `study.inputs` — simultaneously unblocks UI step 6 (contract-generated forms), the one-time-solve presentation, headless/Assistant-driven Resource operation, and honest form rendering for all four integrations. It is the highest-leverage item in this plan.

---

## 3. Framework adjustments (core)

Ordered by leverage; "S/M/L" are rough effort classes (S ≤ 2 days, M ≤ 1 week, L > 1 week).

**F1 — Typed input declarations (M).** Add optional, schema-validated input declarations that reuse `defs/candidate.schema.json#/definitions/parameter`:
- `method.settingsSchema` and `evaluator.settingsSchema`: type the existing free `settings` objects (both stay optional; untyped settings remain valid and fall back to YAML inspection in Studio, as `ui.md` documents today).
- `resource.inputs`: named, typed inputs for Resources (see F4).
- Validation: when a schema is declared, validate the sibling `settings` against it at package-validate and retained-compile time.
This is authoring-surface only — no runtime behavior change — so it is low-risk and can land first.

**F2 — Per-launch Study inputs (M).** Add `study.inputs` (declaration, typed via F1) + a launch-time binding: values supplied at launch (CLI flag / Studio form / Assistant with approval), retained in the Run's compiled definition like any other config, and exposed to evaluator and method through the existing settings channel (e.g., merged under a reserved `inputs` key). This is what lets one saved "Solve an OR problem with COOPA" Run setup be launched repeatedly against different problem texts/files, with each Run retaining exactly what it was given. File-valued inputs should reuse the existing bounded file-candidate materialization machinery rather than a new path.

**F3 — Execute command-protocol batch methods (M).** The method schema already constrains `command → batch` and `docs_assets/methods.md` documents the stdin/stdout exchange contract; only `_preflight_first_slice` rejects it. Implementing it removes the "must be importable Python" constraint that research codebases (COOPA's smolagents stack, Factorio's LLM runners) fail. Scope: command batch methods only — command *evaluators* stay validate-only in v1 (nothing in the four integrations needs them; Factorio's evaluator is Python).

> **Status update 2026-08-06: done.** The retained worker
> (`retained_batch_worker.py`, `_RetainedCommandBatchMethod`) executes one
> bounded subprocess per proposal exchange — JSON on stdin/stdout or via the
> `{input_file}`/`{output_file}` placeholders, `exchangeTimeoutSeconds` as the
> subprocess bound, retained import roots on `PYTHONPATH`, `envFromHost`
> values in the environment. Because the worker environment is PATH-free,
> `command[0]` must be `python`/`python3` (mapped to the prepared
> interpreter); other heads fail preflight with `method_command_unsupported`,
> and the documented `python script.py` shape is checked for retention
> (`method_command_unretained`). Observations are acknowledged but not
> forwarded — proposal requests carry the evidence projection. Package
> validation reports command batch methods `method_command_unchecked`
> (smoke-eligible). Command evaluators stay validate-only. Worker-level tests:
> `tests/core/test_retained_batch_worker.py::RetainedCommandBatchWorkerTest`
> (incl. a supervised socket-worker case); compiler and capability coverage in
> `test_retained_study_compiler.py` / `test_package_validation_capabilities.py`.

**F4 — Operable Resource surface (M).** Resources today can only "launch a web UI" or run a pre-registered output action against a reported tree. Generalize `interfaceOutputAction` into declared **resource actions**: named commands with typed inputs (F1) and declared outputs, runnable headless from CLI/Studio/Assistant (approval-gated), without requiring the web presentation. This gives DEVS-Gen a batch "spec → simulator bundle" path and gives any generator/tool Resource a form-able, Assistant-drivable surface — "operate a specialized Resource" (use case 6) stops meaning "only its custom UI".

> **Status update 2026-08-06: core + CLI done.** New top-level
> `resource.actions` (schema `resourceAction` in `defs/common.schema.json`,
> ≤16 per resource): id/label/description, `command`, `cwd`, `env`, typed
> `inputs` (parameterMap), `grants` (envFromHost/secretsFromHost fail-closed;
> network declared), optional process `runtime` (container fails validation
> for actions), `timeoutSeconds` ≤ 86400. Typed compile + headless executor
> in `src/optpilot/resource_actions.py`; validation wired into
> `_validate_resource_semantics`. Execution contract: validated inputs JSON
> at `OPTPILOT_RESOURCE_ACTION_INPUTS_FILE`, results under
> `OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT`, command tokens may name
> `{inputs_file}`/`{output_root}`/`{input:<key>}` (scalar-only, checked
> against declarations at validation time); `runtime.setup` steps run in the
> resource root first (idempotent by convention — the local path does not use
> Studio's prepared-runtime cache). CLI: `optpilot resource list|run`
> (`--input`/`--inputs-file`/`--output-dir`/`--skip-setup`). Tests:
> `tests/core/test_resource_actions.py` (17). Docs: "Resource Actions" in
> `catalog.md`.
> Design note from the implementation survey: actions are deliberately **not**
> built on the interface *output-action* executor — that path is a
> network-disabled, grant-free container jail bound to a live Studio launch's
> prepared-runtime lease, which cannot serve generation-type actions needing
> LLM access. Interface output actions are unchanged. Studio/Assistant
> approval-gated surfaces consume the same compile (`compile_resource_actions`)
> and land with U1 forms. Also corrected: `.optpilot/resource_setup` never
> existed; the real setup mechanism is `interface.runtime.setup`.

**F5 — Formalized environment capabilities (M).** Two conventions used by the trace-aware method today are honored by convention and hacks:
- `exact_seed_replay`: `method.yaml` reaches across the package via `pythonPath: [., ../../environments/...]` to import the environment's `evaluator.replay_candidate`. Formalize: a capability declaration names the callable; the runner resolves and exposes the environment import root to methods that require the capability. (Cheaper complement: methods consume the evaluator's already-declared `worst_run.db` artifact, category `simulation_trace`, making replay optional.)
- Policy validation: hardcoded AST checks in `method.py` (entrypoint name/arity, forbidden imports, field lints) become an environment-declared `policyValidation` block (required entrypoint, forbidden import roots, forbidden identifiers, optional lints) that any code-editing method applies generically.

> **Status update 2026-08-06: done (framework side).** Environment capability
> declarations gain an optional `callable` (`module:object`, validated and
> required to be source-backed under the retained environment import roots).
> When a method's `accepts.requires.capabilities` names a capability with a
> callable, `retained_study_compiler` appends the environment's package
> import roots to the method runtime (deduped against the method's own
> roots) — the cross-package `pythonPath` hack is no longer needed; worker
> test `RetainedCapabilityImportRootTest` proves a hack-free method resolves
> the callable. New environment `policyValidation` block (entrypoint /
> forbiddenImports / forbiddenNames / string-constant lints) validated by
> `src/optpilot/policy_validation.py::validate_policy_declaration`, carried
> in the candidate context only when declared (existing contracts keep their
> exact bytes/digests), and applied generically by methods via the new
> core `validate_policy_sources(sources, policy)` checker (ports the AGV
> method's hardcoded AST semantics). `production_agv_scheduling`'s
> `environment_llm.yaml` now declares both (additive; the method's own
> checks keep working until W2 rebases it onto the block). Tests:
> `tests/core/test_policy_validation.py` (9), compiler capability cases in
> `test_retained_study_compiler.py`. Docs: `configuration.md` +
> `candidate-contracts.md`. W2 remains the consumer-side work: the general
> method reads `context.policyValidation` and the capability callable
> instead of its hardcoded copies.

> **Status 2026-08-11: the dependency half of `exact_seed_replay` is done.**
> `compile_retained_process_study` now shares the environment's prepared
> Python layer with the method runtime whenever the method declares
> `requires.capabilities` (`retained_study_compiler.py`,
> `_requires_environment_capability`): the layer is added to
> `prepared_method_runtime.prepared_layers` under
> `environment-prepared-python`, appended last in the method's import roots
> (so a method's own locked layer still wins), and retained under
> `run-prepared-method-runtime` so the run projects it for the method
> process. Before this, a capability-requiring method got the environment's
> *source* root (via authored `pythonPath`) but none of its locked
> dependencies, so those imports silently resolved from the host
> interpreter and only worked where the packages happened to be installed.
> Regression coverage: `tests/core/test_retained_study_compiler.py`
> (three cases) and `tests/core/test_capability_dependency_vertical_e2e.py`
> (a full local run whose dependency module exists in no host interpreter).
> Still open for F5: naming the capability callable in the declaration and
> resolving it in the runner, plus `policyValidation`. Also still open: a
> method that cross-imports an environment source root *without* declaring a
> capability gets no dependency layer and is not rejected.

**F6 — Dependency-locking escape hatch, documented (S now, L later).** `_validate_wheel_tags` accepts only pure-Python (`none-any`) wheels; OR-Tools/pymoo/simpy-class stacks therefore can't be locked. Do **not** attempt native-wheel or container execution for this release. Instead: (a) document the supported pattern — a package declares heavy runtimes as *user-provisioned* (host interpreter + documented extras, checked by `optpilot package setup-check`); (b) keep `runtime.sandbox: container` authoring valid so packages are forward-compatible; (c) schedule container evaluator execution as the first post-release framework slice (trust plumbing already exists in `provider_trust_policy.py` / digest-pinned preview approvals to generalize from). COOPA and Factorio-execution adopt pattern (a) in v1.

**F7 — Session protocol: cut from v1 surface (S).** `protocol: session` is a reserved-but-dead enum; shipping a validating-but-never-executable protocol invites broken packages. Mark it explicitly experimental in schema description + docs (or gate behind `apiVersion` bump later). Revisit when a concrete method needs mid-batch adaptation.

**F8 — Hygiene fixes (S each).** Split `tests/` into core vs studio suites so the core sdist test surface is honest (fix-plan §8); promote `pre_release_fix_plan.md` and the authoritative design notes out of the gitignored `resource/` scratch dir into `designs/` (fix-plan §12); ensure `resource/` research trees can never leak into distributions (MANIFEST already prunes — add a release-artifact check assertion). Already done in this tree, contrary to the fix plan's open list: the resource-setup `source_root` fix (§6, with regression test `test_cli_package_setup_check_runs_dot_optpilot_resource_setup_from_resource_root`) and the version-sync check (§11, `_check_versions` in `scripts/check_release_artifacts.py`).

---

## 4. Studio UI completion plan

The shell is done; this is a finishing list, not a rebuild. Ordered.

**U1 — Contract-generated forms (the real remaining feature, L).** One renderer, used in three places, all driven by F1 types:
1. **Run setup forms**: when configuring a Study over an environment with `candidate.format: parameters`, render the parameter schema as a form (today only the fixed Study fields are form-built; candidate contracts render read-only).
2. **Apply-a-method / one-time solve**: the `study.inputs` declaration (F2) renders as the launch form for the saved Run setup.
3. **Resource actions**: `resource.inputs` (F4) render as the action's form, for both user and Assistant filling.
Renderer scope for v1: flat + one-level nested parameters, all seven valueTypes, min/max/values/default/description/unit; deeper recursion falls back to the existing YAML inspection path (already the documented behavior).

> **Status update 2026-08-07: item 2 done (study.inputs launch form).**
> Server: the Studio launch request schema accepts optional typed `inputs`
> (all three request variants; bounded transport validation in
> `_canonical_study_launch_inputs`), threaded through
> `launch_study → prepare_selected_package / plan_local_package`
> (`prepare_selected_package` gained the `launch_inputs` kwarg in core);
> the durable intent replay path carries inputs automatically.
> `_validate_study` now compiles with `bind_launch_inputs=False` — fixing a
> latent bug where a required-input study reported `study_invalid` — and
> exposes the declared `inputs` map in the validation payload. Client
> (`app.js`): a "Launch inputs" card renders one typed field per declared
> input (number fields with bounds for int/float, dropdowns for
> bool/categorical, JSON textareas for array/object, defaults as
> placeholders), values are validated client-side (core stays
> authoritative), included as `request.inputs`, and persisted/replayed with
> the stored launch request. Tests: 5 in
> `test_studio_study_launch_capability.py` (validation exposure, mocked
> launch threading, malformed-inputs rejection, client-source assertions) +
> an unmocked HTTP end-to-end in `test_studio_realm_runs.py`
> (`test_http_study_launch_binds_declared_typed_inputs`). Items 1
> (candidate-schema Run setup form) and 3 (resource-action forms — greenfield
> on both server and client; consume `compile_resource_actions` /
> `run_resource_action`) remain.

**U2 — Finish the "Run setup" rename (S).** Server cards already say "Run setup"; the client still labels the same card kind "Study" (`kindLabels`), the nav says "Studies", and Open work says "Run preparation". Decide the final user-facing wording once — recommendation: **"Run setup"** everywhere user-facing, "Study" retained in docs as the underlying config kind (as the product statement specifies), and unify "Run preparation" → "Run setup · preparing". No schema/API/CLI renames.

> **Status 2026-08-10: DONE** (completed across the Studio sprint and
> U7): the conversation shell says "Run setup(s)" everywhere; the last
> legacy-shell "Studies" nav went away with the legacy shell itself.

**U3 — Open work completeness (S/M).** Add pending approvals (currently only visible inside the Conversation timeline — an approval raised while a full-stage tool is open has no shelf affordance) and completed-with-outputs interface sessions (path back to kept outputs). Keep Workspaces out per the "compact process monitor" doc contract, but re-confirm that choice against the product statement's "Runs, interfaces, Workspaces, approvals ... remain accessible through Open work" — if Workspaces stay out, the statement and `ui.md` should both say why (durable items live in their named destinations).

> **Status 2026-08-10: DONE.** `buildOpenWorkItems` now surfaces (a) one
> "Needs attention" approval card per Conversation with pending or queued
> approvals (from the session summaries the client already polls; click
> selects that Conversation), and (b) a stopped interface launch whose
> reported outputs remain reviewable — "Finished" section, or "Needs
> attention" when an output still needs saving (shared predicate
> `interfaceOutputsNeedAttention`, also used by the outputs drawer);
> dismissible once reviewed. Workspaces stay out; `ui.md`'s Open Work
> section now states the rationale (durable objects live in their named
> destinations). Tests in `test_studio_open_work_hardening_static.py` and
> the revised contract in `test_studio_conversation_continuity_static.py`.

**U4 — Assistant catalog search (S).** `optpilot_catalog_list` filters only by `config_kind`; intent-matching relies on the model reading full listings. Add free-text query + tag filtering (search over id/name/description/tags/purpose) so step 4 of the interaction model scales beyond small catalogs. With four flagship packages this matters already.

> **Status 2026-08-10: DONE.** `optpilot_catalog_list` accepts `query`
> (every whitespace term must match the entry's id, name, description,
> package, purpose, or tags, case-insensitive) and `tags` (all must be
> declared exactly); works alone or with `config_kind`, and the result
> summary reports "matched N of M". Filtering in
> `_search_catalog_entries` (server.py); schema + description in
> agent.py; a "Search, don't scan" rule added to the assistant system
> prompt. Tests: `tests/studio/test_studio_catalog_search.py` (9).
> Note: the OpenHands agent-server caches tool schemas per process —
> restart it after deploying.

**U5 — Interface identity in Ask-from-this-page context (S).** `assistantVisibleContext()` omits a `selected_interface` object, so asking from a full-stage tool carries only `current_page`. Add launch/source coordinates as bounded read-only context, per the `ui.md` promise.

> **Status 2026-08-10: DONE.** `assistantVisibleContext()` sends a
> bounded `selected_interface` (launch id, status, scope, profile,
> label, and the Catalog `kind/uid/key` or Workspace id/title source)
> when `isViewingActiveInterface()`; the server narrows it via
> `_assistant_selected_interface` (interface/editor pages only,
> 256-char caps, identity only — never preview URLs or presentation
> tokens) and `context_packet` carries it as a typed field. Tests:
> `tests/studio/test_studio_interface_context.py` (7).

**U6 — Dynamic onboarding (S).** The five suggested intents are static strings; derive visibility from the registry (suppress "Compare methods" with zero methods; surface flagship capabilities by name — "Generate a simulator (DEVS-Gen)", "Solve an OR problem (COOPA)" — from package metadata/tags rather than hardcoding).

> **Status 2026-08-10: DONE.** `onboardingIntents(groups)` in app.js:
> static intents stay while the Catalog loads or errors; once loaded,
> unbacked intents are suppressed (Explore needs an environment;
> Improve/Apply need a method; Compare needs two; Build/publish always
> shows) and up to three capability chips join by name from entry
> metadata — resources with `purpose: generator` ("Generate with …")
> and methods tagged `one-time-solve` ("Solve with …", which surfaces
> COOPA). Tests: `tests/studio/test_studio_onboarding_intents.py`.

**U7 — Retire the legacy shell (M).** `?shell=legacy`, the `legacy-navigation` markup, legacy assistant session cards, and `_shortlist_card_legacy_ui_projection` double the render paths. Remove after U1–U3 land; switch the no-hash route fallback from `#/workspaces` to `#/conversations` at the same time. *Status 2026-08-10: the shortlist renderer bridge is done — `_shortlist_legacy_ui_projection` / `_shortlist_card_legacy_ui_projection` are deleted along with the duplicated `review_collection` (run bundle) and `collection` (shortlist command response) payload fields, and the Run renderer now consumes the raw `shortlist` payload. The `?shell=legacy` shell, `legacy-navigation` markup, legacy assistant session cards, and the `#/workspaces` route fallback are still outstanding.*

> **Status 2026-08-10: shell retirement DONE; one bridge deferred.**
> `?shell=legacy` no longer exists (`shellModeFromLocation` returns
> "conversation" unconditionally), the `legacy-navigation` markup, the
> legacy assistant session cards, the legacy global interface bar
> (`activeInterfaceBar` — Open work is the running-interface
> affordance now), the legacy launcher, and their CSS are removed; the
> no-hash route fallback is `#/conversations`. This also finishes U2's
> client rename (the legacy "Studies" nav was the last user-facing
> string). Deferred: `_shortlist_legacy_ui_projection` /
> `_shortlist_card_legacy_ui_projection` are a Run-renderer payload
> bridge (`review_collection`/`collection` fields), not shell markup —
> removing them means migrating the client shortlist renderer to the
> raw `shortlist` payload shape; queued as its own follow-up task.

**U8 — Multi-entry recommendation cards (M, optional for v1).** Catalog recommendation cards are emitted only from `catalog_detail`, so a comparison shortlist costs N tool calls. A bounded multi-entry recommendation card would tighten the "small number of actionable cards" loop; defer if schedule pressure.

---

## 5. Integration designs

### 5.1 DEVS-Gen — from "registered" to flagship

Already the strongest integration: the pipeline *describe system → guided/automatic generation in the interface → immutable bundle (`devs.simulation.v1`) published via `interface_outputs.py` → Studio registration wizard detects it → environment starter + hardened `optpilot_adapter.py`* exists and is e2e-tested (`tests/test_generated_devs_student_handoff_vertical_e2e.py`). Remaining work:

1. **Close the metrics gap (M).** The environment starter hardcodes `metrics.keys: [score]` with `needs_editing: true` because the generator never emits a metric declaration — even though the generation pipeline knows the KPIs (logging specs, `kpi_counter`). Extend `simulation.json` (bump to `devs.simulation.v2`, keep v1 accepted) with declared metric keys + objective direction + optional per-metric descriptions, emitted by the generator; the starter then produces a launch-ready environment with no manual editing. This turns "generate a simulator, then optimize it" into a genuinely closed loop and is the single highest-value DEVS-Gen item.
   > **Status 2026-08-08: DONE.** Audit correction: no structured KPI
   > knowledge existed at generation time — metric names lived only inside
   > the generated runner's `write_simulation_summary(...)` call. The
   > contract is therefore static extraction from the runner source
   > (`declared_metrics` in `result_summary_contract.py`): an explicit
   > module-level `OPTPILOT_METRICS` literal (name → direction/description;
   > now requested by the generation prompts) wins, else the literal keys
   > at the writer call sites (names only); nothing is reported without
   > the full summary contract. `simulation.json` bumps to
   > `devs.simulation.v2` with an optional strict `metrics` block
   > ({keys, objective {metric, direction}, descriptions}); v1 stays
   > accepted everywhere, and `ensure_simulation_manifest` upgrades
   > repaired bundles whose runners newly declare metrics. Studio accepts
   > both versions; a v2 bundle with declared metrics now writes an
   > **enabled** `optpilot_configs/environment.yaml` with the real metric
   > keys (`needs_editing: false`, next action Check) and exposes
   > `metrics` in `detected_simulation`; v1 keeps the previous disabled
   > `score` template exactly. Tests: 10 backend
   > (`test_simulation_metrics_contract.py`), 7 Studio
   > (`test_devs_simulation_v2_metrics.py`), plus the untouched v1
   > vertical e2e passing throughout.
2. **Register gallery simulators as example Environments (S/M).** `resource/devs_gen_gallery` contains 7 generated simulators whose READMEs already document CLI args (candidate parameters) and JSONL trace schemas. Package 2–3 (e.g., barbershop, SEIRD, StratAirlift) as catalog Environments — instant, credible demo content for "open a simulator and understand system behavior" (use case 1) and cheap targets for Methods.
   > **Status 2026-08-08: DONE — `catalog/devs_gallery`** with SEIRD
   > (`seird-epidemic`) and ABP (`abp-protocol`). Candidate substitutions
   > from the audit of all 7: barbershop/IOBS/oft are stdin-driven and
   > StratAirlift hangs even at tiny horizons (generated-code defect), so
   > ABP replaced them — sub-second, deterministic (seeded channel noise),
   > and its sender `timeout` is a genuinely optimizable decision. Each
   > environment wraps the unmodified generated `devs_project/` with an
   > OptPilot-authored in-process evaluator returning final-state metrics,
   > plus a hash-locked vendored `xdevs 3.0.0` wheel (GPL; license and
   > third-party notice included, mirroring the DEVS interface resource's
   > precedent — owner may veto). A seeded `gallery-random-search`
   > baseline method and two 5-trial studies ship with it; both verified
   > end-to-end through the retained runner (`seird-minimize-deaths` best
   > deceased 41.0 vs 72.1 at defaults; `abp-tune-timeout` succeeded,
   > minimizing retransmissions). Gallery bundles predate the v2
   > manifest/summary contracts, so these wrap models directly rather
   > than via the registration wizard (noted in the package README).
3. **Trace-conformance checks as environment smoke tests (M).** The paper's operational-conformance idea (exit-0, schema-valid JSONL trace) becomes a reusable validation helper for generated environments; behavioral checkers stay research-side for v1.
   > **Status 2026-08-08: DONE.** Reusable validator
   > `event_trace_conformance.py` in the devs_construct_recon toolchain
   > (dependency-free AST-of-JSONL checks: header row, complete
   > event/state records per the writer's exact shapes, strictly
   > increasing record_sequence, truthful summary-footer counters;
   > bounded error list). Consumers: backend
   > `assess_trace_conformance(result_root)` — additive next to the
   > deliberately permissive `assess_behavior_smoke`, whose contract is
   > unchanged — and the generated environment adapter starter, which now
   > embeds a compact per-trial structural check so a truncated or
   > corrupt declared trace fails the trial. Tests:
   > `test_event_trace_conformance.py` (11, including a genuine trace
   > produced by simulating a minimal model through the real writer and
   > tamper rejections).
4. **Headless generation as a Resource action (S, after F4).** "spec file → simulator bundle" without the web UI — the paper's own batch mode, exposed to CLI and Assistant.
5. **Hygiene (S).** The curated `catalog/devs_gallery/resources/devs-gen-interface` copy is the release base; `resource/devs_display_new` (research monorepo, baselines, Chinese dev notes) and `devs_gen_gallery` remain untracked scratch. Loosen the hardcoded OpenRouter model registry to configured model IDs. Godot/MQTT visualization stays out of v1 (needs broker + Godot runtime; only one gallery example exists) — listed as post-release.

### 5.2 Trace-aware LLM policy search — the generalization

Target: a practitioner with *any* DES simulator can adopt the method. Split the existing `production_agv_scheduling/methods/process_aware_llm` into a general method plus an environment template, using the coupling-point inventory from the audit:

1. **General method package `llm_policy_search` (M/L).** Parameterize the six coupling points: editable file set from `candidate.files.editable` (drop hardcoded `TARGET_FILES`); policy interface contract (entrypoint name/signature, snapshot fields) moves into environment-owned `methodContext.instructions`; AST validation rules read from the environment's `policyValidation` block (F5); domain wording out of `method.py` strings and `prompts/manager.md`/`editor.md` into placeholders filled from methodContext; `worst_seed`/`worst_total_score` metric names behind a settings mapping with the current names as defaults; trace access via the evaluator's declared `simulation_trace` artifact, with `exact_seed_replay` (F5) as the optional stronger path. The trace summarizer (`_summarize_trace_database`) is already schema-agnostic; the search loop, staging, and LLM transport need no change.
2. **DES environment template + helper (M).** Extract the domain-independent scaffolding from `evaluator.py` (seeded replication loop, worst-run selection, trace dump, aggregation, fingerprinting) into a shared helper shipped with core or the package; publish an environment template package skeleton: `evaluator.py` (domain hooks only), `initial/` policy template, `prompts/` triple (policy contract, domain description, trace-schema description), `settings/`, YAML declaring files/references/metrics/capabilities. Document as "Bring your own simulator" — the practitioner-facing counterpart of DEVS-Gen's "generate a simulator".
3. **Keep `production_agv_scheduling` as the flagship instantiation** (rebased onto the general method; its studies keep working — same evidence, same objective).
4. **Second instantiation to prove generality (M).** Best candidate: the supply-chain app already present in `resource/autoie-lab` (the original repo's own second domain). A DEVS-Gen-generated simulator as a further instantiation is specified as its own joint workstream in §5.5.

> **Status 2026-08-08 (§5.2): items 1–3 DONE.** The method is fully
> contract-driven — all six coupling points read the environment's
> declarations (editable set, `policyValidation` via the shared core
> checker, entrypoint wording, description-based domain wording,
> settings-mapped replay metric names, capability-declared
> `module:function` replay; the cross-package pythonPath hack is gone,
> and core `_candidate_context_paths` now exposes
> `policyValidation`/`capabilities` as requirable tokens). New
> `catalog/llm_policy_search` package: byte-identical method mirror
> (`tests/core/test_llm_policy_search_mirror.py`; llm_policy_search is
> the source of truth), `des_replication.py` scaffolding helper, the
> executable `queue_demo` template instantiation, and a zero-LLM
> baseline smoke verified through the retained runner. Item 4's second
> instantiation is the §5.5 demo. Template gotcha: an environment
> declaring `runtime: {sandbox: process}` fails the first binding slice
> (runtime_requirements must be empty) — declare no runtime block.

> **Status 2026-08-08 (§5.5): items 2–4 largely DONE; demo pending.**
> Item 3: `trace_adapter.py` (queue_demo template) converts
> `devs.event-trace.v2` JSONL into the standard SQLite trace shape,
> verified on a genuine generated trace. Item 2: generated runners
> declare `OPTPILOT_POLICY` ({file, entrypoint, description}) mirroring
> the metrics contract — statically extracted
> (`declared_policy`), validated into the v2 manifest only when the
> declared file exists, requested by both generation prompts when the
> spec names an optimizable decision; Studio's wizard then emits a
> file-candidate variant starter alongside the parameters one
> (environment_policy template with prefilled `policyValidation`,
> `exact_seed_replay` bound to a generated adapter that overlays the
> candidate policy, runs seeded replications, scores from summary
> metrics, and converts the worst trace to SQLite; auto-generated
> policy_instructions.md covers item 4 in starter form; deliberately
> never launch-ready — seeds and scoring are a human decision).

> **Status 2026-08-09 (§5.5): DONE — definition-of-done demo verified.**
> `catalog/llm_policy_search/environments/dispatch_station/` is the
> checked-in reference composition (DEVS-Gen generated dispatch station
> with the declared policy hook + wizard-shaped adapter);
> `studies/dispatch_policy_search.yaml` ran 13/13 trials through the
> retained runner: FCFS baseline mean_total_score −7.81 → best LLM
> candidate −4.53 (42% lower average waiting; the model derived
> Shortest-Processing-Time-first in iteration 1, and all 12 LLM
> candidates beat the baseline). Walkthrough docs page:
> `generate-and-optimize.md`. Framework fix required en route: the CLI's
> `--method-request-timeout` default (10.0) silently overrode every
> method's declared `entrypoint.exchangeTimeoutSeconds`, so any retained
> exchange over 10s (an LLM proposal round) was abandoned as
> `method_failed`; the default is now None → defer to the declaration.

> **Status 2026-08-09 (§5.5 follow-up): declare-don't-extract policy
> hook.** Fresh-generation probes (elevator, triage clinic) showed the
> extract-to-policy.py contract failed systematically: only the
> runner-writing stage knew about it, so every generation declared
> `OPTPILOT_POLICY` without creating the module (manifest fail-safe →
> policy: null). Per owner decision the decision logic now stays inside
> the deciding DEVS component: prompt §6 declares the component file +
> top-level class the model actually built (no restructuring), the
> manifest builder statically verifies the declared entrypoint is
> defined in the declared file (declared-but-unwired dies at manifest
> time; new `declared_entrypoint_kind` helper), and the wizard's policy
> variant branches on entrypoint kind — class style emits the component
> file as the editable candidate with a generated DEVS editing contract
> (preserve class/ports/protocol/lifecycle; edit only selection logic),
> omits the function-only entrypoint pin, and forbids `random`; function
> style (dispatch_station) is unchanged. Method and core untouched —
> all coupling points were already declarations. Verified with a FRESH
> LLM generation under the new prompt (2026-08-10): the generator
> declared the component it actually built (class TriagePolicy in the
> deciding component file — no phantom policy.py, genuinely wired into
> the model), with one near-miss the pipeline now heals: the declared
> path carried a build-workspace prefix ("generated_simulator/..."), so
> `_derive_policy` re-anchors declared paths at the `devs_project/`
> segment (existence + entrypoint gates still apply) and the prompt now
> says the path must start with `devs_project/`. Full chain green on
> that unmodified bundle: manifest emits the component policy block →
> handoff kind=class → starter emits the editing-contract variant.
> Trade-off measured empirically (2026-08-10): the feared editor
> failure-rate increase on whole-component rewrites did not materialize.
> `catalog/llm_policy_search/environments/triage_clinic/` (the fresh
> class-style generation, finished by hand: --seed knob on the runner,
> seeds [7,11,23], score = negated avg_urgency_weighted_waiting_time,
> hand-written editing contract) ran `clinic-policy-search` 13/13
> through the retained runner: 12/12 whole-component TriagePolicy
> rewrites valid — zero crashes, zero protocol breaks, zero retries at
> the attempt level — every candidate preserved class/ports/lifecycle
> and edited only _select_patient. FIFO baseline −3.75 → best −2.26
> (WSPT/urgency with aging penalty, iteration 2; ~40% lower weighted
> waiting). Both hook styles now have green retained reference runs.

### 5.3 COOPA — natural-language OR solving

Register as package `or_solving` with two registrations sharing one pruned runtime:

1. **Runtime pruning (M).** Extract the ~10 packages the paper pipeline actually uses (smolagents, litellm, instructor, pydantic, pyomo, ortools, pymoo, numpy/scipy, tenacity, python-dotenv) from the 40+-pin research requirements; drop the retrieval/KB/web tail (crawl4ai, gradio, langchain, e2b, docker-py, serpapi). Solver backends are **user-provisioned extras** per F6: `package setup-check` verifies importability of ortools/pymoo and presence of GLPK/IPOPT, with graceful degradation (mathematical-agent-only mode covers 91% of benchmark dispatches).
2. **Solve shim (S/M).** A ~100-line CLI wrapper around `extract_formulation_with_refinement` + `create_manager_agent` (`process_single_problem` is the isolation point): problem text/file in → JSON out (formulation with element-level provenance, per-dimension confidence, routing decision, generated solver code, numerical solution). This artifact set is COOPA's most OptPilot-aligned asset — retain all of it as Run output files.
3. **One-time solve registration (after F2/F3).** Environment `or_problem`: `study.inputs: {problem: string|file}`; evaluator checks the returned solution artifact's well-formedness (and feasibility where a model file is returned); Method `coopa_solver` (command batch method via F3, or a thin Python entrypoint wrapping the shim) with `settingsSchema` for k iterations, backbone model, dispatch on/off; `envFromHost: [OPENROUTER_API_KEY]`; `exchangeTimeoutSeconds` sized for ~200 s per problem. Saved Run setup "Solve an OR problem" + generated launch form (U1) is the practitioner surface; Assistant fills the same form from conversation.
4. **Benchmark registration.** Environment `or_benchmark`: dataset (ComplexLP/IndustryOR/BWOR JSONL via `methodContext.references` or evaluator settings), file-candidate solution per problem, evaluator scores |pred − gold| ≤ 0.1 (reusing `checks/score_results.py` logic); Studies compare COOPA configs/backbones or future methods; `budget.maxTrials` = #problems, sequential.
5. **Blockers to clear before shipping (non-code).** `code/coopa/` has **no license file** — resolve with the authors before any redistribution (worst case: register as user-fetched external source via the configured-package ingress path, like other external codebases). Anti-blocker note: LLM-generated solver code executes locally; ship with the method's runtime documented as trusted-local-code and revisit under the container slice.

> **Status update 2026-08-07: items 1–3 done (`catalog/or_solving`).**
> Package ships OptPilot-original code only — COOPA is user-provisioned via
> `COOPA_HOME` (no redistribution; license blocker stands for bundling COOPA
> itself, not for this package). `or-problem` environment validates solution
> artifacts (schema `optpilot.or-solving-report.v1`; metrics solved /
> objective_value / artifact_bytes); `coopa-solver` is an F3 command batch
> method (`envFromHost: [OPENROUTER_API_KEY, COOPA_HOME]`,
> `exchangeTimeoutSeconds: 900`, settingsSchema for model / agentMode
> (manager | mathematical-only) / skipFormulation / refinement iterations);
> `methods/coopa_solver/coopa_shim.py` assembles the pruned manager (four
> optimizer agents + manager_curation prompt, no web/knowledge agents) and
> `requirements-pruned.txt` carries the ~10-package runtime (item 1).
> `solve-or-problem` declares `inputs.problem` (F2) — the Studio launch form
> renders it, and Studio gates the launch on the missing host values
> (`runtime_environment_missing`) until configured. An explicitly labeled
> mock twin (`coopa-solver-mock` / `solve-or-problem-mock`) smoke-tested the
> identical machinery with no LLM/network/COOPA; verified end-to-end through
> the retained runner: `optpilot run … --input problem="…"` → succeeded run,
> artifact retained with the launch input inside, evaluator metrics scored.
> **Removed 2026-08-12** — a method whose answers are canned placeholders does
> not belong in a practitioner-facing catalog. `optpilot package validate
> catalog/or_solving` covers the wiring check it used to serve.
> Real-pipeline execution verified 2026-08-07 through Studio's retained
> runner: LP test problem solved with predicted = 36.0 (exact optimum),
> solved = 1.0, full 8.9 KB artifact retained (manager routing,
> deepseek-v4-pro via OpenRouter). Two worker-environment fixes were
> required and are part of the package: `coopa_shim.solve_problem` sets
> fallback `PATH`/workspace-`HOME` via `setdefault` before COOPA imports
> (the retained worker env is deliberately PATH- and HOME-free; COOPA's
> import closure reads PATH at import time and Pyomo discovers solvers via
> PATH), and `coopa_solver.py` protects stdout at the fd level
> (`_protect_stdout`: dup real stdout, point fd 1 at stderr) because the
> worker parses the whole stdout as JSON while COOPA agents and generated
> solver subprocesses print to fd 1. The shim also wraps COOPA's
> `build_model` allowlist with a `LiteLLMModel` fallback so any
> litellm-routable model id works. An `or_solver` resource wrapping the
> same pipeline in an F4 solve action was briefly added for human use and
> removed the same day: it re-registered a method's capability under the
> resource kind for UI convenience, against §2.2's rule that one-time
> solving is a presentation over the `maxTrials: 1` study with no new
> entity. The human surface for coopa-solver is the `solve-or-problem`
> launch-inputs form today, and properly the "Apply a method"
> presentation (§2.2, U6) once built. Two framework improvements from
> that detour were kept: the F4 executor maps a `python`/`python3`
> command head to the optpilot interpreter (mirroring F3's contract), and
> Studio's resource-action runner resolves declared env/secret grants
> through Studio Settings variables before the process environment.
> Remaining: item 4 (`or_benchmark` dataset environment + scoring) and
> the license resolution (item 5).
> Update (2026-08-07, later): the human interface returned in the
> nature-correct shape — the coopa-solver method itself declares an
> `interface` (the interface grammar was already kind-agnostic across
> environment/method/resource; no framework change was needed). The
> COOPA Solve Console offers an interactive mode (formulation review
> with provenance and confidence, user-feedback re-extraction loops,
> approve-then-solve) plus automatic and mock modes, and was verified
> through Studio's containerized interface runtime: product-mix LP →
> integer-domain revision from user feedback → approved solve → 2160
> (exact optimum). The in-container COOPA checkout lives at
> `methods/coopa_solver/coopa_home/` (gitignored, user-provisioned).

### 5.4 Factorio Design Benchmark

Register as package `factorio_design_benchmark`, static-validation-first:

1. **Vendored evaluator core (S/M).** Depend only on the pydantic-only subset (`fd_benchmark/{schemas,validation,tasks}` — MIT, verified to run in isolation); do not inherit the litellm/openai-agents/matplotlib tail. Fix the UTF-8-BOM task configs on ingest (24 of the 32 carry a BOM); confirm canonical target rates (repo configs differ from paper Table 1 for 4 product families) with the authors before publishing numbers.
2. **Environment (M).** One environment template instantiated per task via `evaluator.settings.task_id` (32 generated configs from a template script). Candidate: `format: files`, `editable: [production_line.json]`. Metrics: `static_valid`, `failed_check_count`, per-family violation counts, `total_entity_cost`; task spec, `EXAMPLE_PRODUCTION_LINE`, knowledge/rule markdown → `methodContext.references`; the 5-dim specification-pressure profile → tags/description for difficulty-stratified selection.
3. **Direct-baseline Method (M).** The paper's Direct harness as a Python batch method: each `propose` renders task prompt + prior-trial validation feedback from evidence, calls the configured LLM, stages the JSON; `budget.maxTrials: 25`, `parallelism: 1` reproduces the 25-turn loop faithfully. The hierarchical Workflow solver becomes a second method over the identical environment — a live demonstration of the env/method separation argument.
4. **Execution mode (opt-in, documented, S).** A separate environment variant whose Python evaluator drives RCON against a **user-provisioned** Factorio headless server (proprietary; never vendored or defaulted), adding `actual_rate` / `rate_ratio` / `target_achieved`. Static-only is the shipped default: it captures nearly all instantiation failures (293 vs 292 of 384 runs in the paper) and full cost metrics; the doc page states plainly that rate achievement requires the runtime extra. Post-release option: analytic throughput estimator (recipe-graph propagation code already exists for the pressure profile).
5. **Studies.** Per-task Run setups (`minimize failed_check_count`, then `minimize total_entity_cost` among valid designs) plus a generated benchmark sweep script; this package is the researcher-facing "compare several Methods on an Environment through a repeatable pipeline" (use case 4) exemplar.

> **Status 2026-08-11: items 1-3 and 5 DONE; item 4 documented-not-shipped.**
> `catalog/factorio_design_benchmark` ships `factory-design` (file candidate
> `production_line.json`, 11 metrics, task chosen per launch), the
> `direct-designer` Direct baseline plus a deterministic `direct-designer-seed`
> twin, two studies and a 32-task sweep script. Verified end to end through
> the retained runner: the smoke study succeeds with
> `failed_check_count=5`/`total_entity_cost=9619.3125`, matching a standalone
> evaluator run exactly, and `--input task_id=` demonstrably changes the
> verdict (iron_plate_low_easy scores 4, military_science_pack_low_easy 6).
>
> Two audit corrections. (a) **Dependencies**: the plan assumed the pydantic
> subset could simply be depended on. pydantic 2's `pydantic-core` publishes
> no pure wheel, and OptPilot's process runtime accepts only
> `py3-none-any` (`locked_python_runtime._validate_wheel_tags`). The package
> therefore locks **pydantic 1.10.22** (pure, only `typing-extensions`) in the
> environment's own runtime, with three mechanical v1 edits to the vendored
> source; `tests/core/test_factorio_vendored_core.py` pins semantic
> equivalence with upstream across 288 cases. The BOM claim was correct
> (24/32) and the loader's `utf-8-sig` handling is load-bearing, because the
> upstream registry is fail-open: a config that fails to load is silently
> replaced by a programmatic task whose rate differs 3-10x.
> (b) **Canonical target rates**: this cannot be checked as written - the
> research tree contains **no paper artifact at all**. What is verifiable is
> that the 32 JSON configs disagree with the programmatic `rates` fallbacks in
> `tasks/task_config.py` for **all 8 families**, and one upstream doc quotes
> `inserter_high_hard` at 15/min against the config's 60. The JSON configs are
> treated as source of truth and the question is flagged in the package README;
> it still needs the authors before any comparative numbers are published.
>
> **Correction 2026-08-11 (post-inspection).** An adversarially verified
> review found the LLM path shipped broken in three compounding ways, each
> silent: the revision loop read a `study_state.observations` key the runner
> never supplies (the runner exposes aggregate counters only), the method was
> never told which of the 32 tasks it was designing for, and
> `factory_design_task` pointed at a method config whose `settings.mode` was
> `seed` — which no study, CLI flag or Studio form can override, because a
> Study cannot override method settings. A 25-trial "requires a model" run
> would have completed green with 25 identical template designs. Fixed:
> observations are now accumulated in `observe()`, the environment publishes a
> `task_specs` reference and the method renders the launched task (bounds, ore
> and water patches) plus the previous design into the prompt, and a
> `direct-designer-llm` twin carries `mode: llm`. Known remaining gaps, now
> documented on the page rather than implied away: feedback is per-family
> counts rather than the paper's check ids and details, so results are not
> directly comparable to published Direct numbers.
>
> Item 4 (execution mode) is deliberately not shipped: it needs a
> user-provisioned proprietary Factorio 1.1.110 server over RCON plus
> `factorio-rcon-py`/`lupa`/`slpp`/`pillow` (none lockable), and yields
> `actual_rate`/`target_achieved` which static validation cannot derive at all.
> The README and the docs page state that plainly.

### 5.5 Joint workstream W5: optimize DEVS-Gen simulators with `llm_policy_search`

The signature demonstration of the platform thesis: *describe a system in natural language → DEVS-Gen builds the simulator → register it as an Environment → `llm_policy_search` improves its decision logic from traces* — two flagship works composed purely through public contracts.

**Why it doesn't work today.** Two structural mismatches. (1) *Candidate format*: the method optimizes executable policy files, but DEVS-Gen's registration wizard emits `candidate.format: parameters` environments — launch-time interventions (fleet size, enumerated dispatch rules) with decision logic baked into generated atomic components; there is nothing for a policy-editing method to edit. (2) *Feedback signals*: the method consumes seeded replications, `worst_seed`/`worst_total_score` metrics, and a queryable `simulation_trace` artifact; generated simulators emit a JSONL event stream and (today) a placeholder `score` metric, with no replication or worst-run convention. Parameter-space methods (`rule_grid`, `evolutionary_rule_search`) already work against generated environments unchanged — that is what-if optimization, and remains the day-one story.

**What closes the gap.** Most of it is work this plan already schedules; one piece is new:

1. *Already planned:* the method generalization (§5.2 item 1 — editable set from the contract, interface contract from `methodContext.instructions`, `policyValidation` from the environment, trace via the declared `simulation_trace` artifact) and the DES template helper (§5.2 item 2 — seeded replication loop, KPI aggregation, worst-run selection), applied around the generated `run.py` inside the registration adapter rather than a hand-written simulator.
2. *New — policy hook in generated simulators (M):* extend DEVS-Gen's spec/templates so decision components delegate to a user-editable `policy.py` with a declared snapshot→action signature, and extend `simulation.json` v2 (§5.1 item 1) to declare the policy module, its interface contract, and validation rules alongside real metric keys. The registration wizard then emits a *file-candidate* environment variant (editable `policy.py`) in addition to the parameter-candidate one. Aligned with the paper's own direction ("LLMs as decision-making entities inside event-driven simulators"), not a bolt-on.
3. *New — trace adapter (S):* a JSONL→SQLite conversion in the template helper (or JSONL ingestion in the method) so the generated trace satisfies the `simulation_trace` artifact convention; the method's trace summarizer is already schema-agnostic.
4. *Bonus synergy (S):* DEVS-Gen already holds the NL spec and PlanTree, so the domain description and trace-schema documents the method needs in `methodContext.references` can be auto-generated into the bundle instead of hand-written — the generated environment arrives method-ready.

**Definition of done:** one gallery-class simulator regenerated with a policy hook, registered via the wizard as a file-candidate environment, and demonstrably improved by `llm_policy_search` over ≥3 iterations in a retained Run, with the walkthrough as a docs page. **Scheduling:** depends on §5.1 item 1, §5.2 items 1–2, and F5; it is the last Phase 2 item and may slip to immediately post-release without harming either work's individual story — but it is the announcement's best headline if it lands.

### 5.6 Coverage check — use cases × capabilities

| Product use case | Delivered by |
| --- | --- |
| 1. Open a simulator, adjust inputs, understand behavior | DEVS gallery Environments + interface launches; parameter forms (U1) |
| 2. Have OptPilot find methods to improve a simulator/evaluator | Onboarding intent → Assistant catalog search (U4) → `llm_policy_search` on the DES template |
| 3. Apply one Method to a described problem, one-time | F2 per-launch inputs + U1 forms + COOPA solve Run setup |
| 4. Compare Methods on an Environment repeatably | Factorio benchmark package; `production_agv_scheduling` studies; `or_benchmark` |
| 5. Build/register an Environment, Method, or Resource | DEVS-Gen generation → registration wizard; BYO-simulator template; docs |
| 6. Operate a specialized Resource | DEVS-Gen interface today; F4 resource actions + U1 forms for headless operation |

---

## 6. Package distribution: packages as repositories

Target model (post-v1 direction, with v1 groundwork): **a catalog package is a distributable unit whose natural home is its own Git repository.** A user adds a package by supplying a GitHub link or uploading a zip; OptPilot validates it; on success it is published into the catalog atomically; on failure the OptPilot Assistant is automatically engaged to help fix it. Community and future group integrations then follow exactly the same path as the four flagship packages.

**What already exists (more than expected).** The trunk of this pipeline is built: `configured-package ingress` (`src/optpilot/realm/configured_package_ingress*.py`, migrations 0024–0029) is a durable, replayable publication pipeline — typed request/receipt schemas (`optpilot.configured-package-ingress-request.v1`), digest-verified tree capture from an abstract `source_resolver` returning an `AllowedTreeSource`, bounded validation facts (≤256 facts / 64 KiB), atomic catalog publication with package revisions and governance, and a four-way outcome model (`PUBLISHED` / `UNCHANGED` / `REJECTED` / `CONFLICT`). Studio's **Link local folder** and workspace registration already feed it from local sources, and `designs/external-codebase-package-curation.md` drafts the curation workflow for external codebases. What's missing is exactly two things: *source acquisition adapters* and the *repair loop*.

**D1 — Zip upload as an ingress source (S/M, candidate for late v1).** A zip is just a `source_resolver` that extracts to a temp tree — no network, no auth, no trust question beyond what local folders already pose. Record the archive digest in the receipt. This is the cheapest way to make "add a package" real for users who received a package from a colleague or a paper artifact.

**D2 — GitHub URL import (M, post-release).** Clone at a pinned ref, resolve to a commit SHA, feed the tree to the same ingress; retain `{url, commit_sha, subdirectory?}` as provenance in the receipt so a published package is always traceable to its exact upstream state, and updates are re-ingests of a new SHA under the existing revision/governance machinery (visible diff between revisions before approval). Auth for private repos, ref-following policies, and marketplace-style discovery are explicitly out of scope until the basic flow is proven.

**D3 — Validation gate (S, mostly wiring).** The gate is the ingress outcome model; wire the full existing checks — `optpilot package validate --check-source` semantics plus `setup-check` — into the ingress validation facts, so a `REJECTED` receipt carries machine-readable, per-config failure codes rather than prose. Critical trust rule: validation of imported trees stays **static by default** — no setup steps, no smoke execution, no imports of package code without an explicit, separately-approved action. An imported package is untrusted code; publication makes it *visible and launchable*, and every launch still passes through the normal runtime/approval gates.

> **Status note 2026-08-11 (D3 input, done).** Package validation now carries a
> static run-time import closure scan (`src/optpilot/dependency_closure.py`,
> wired into `validate_package` and on by default). It parses the retained
> Python reachable from a component's declared entry points, subtracts the
> stdlib, in-package source, the top-level modules of the wheels named by the
> component's `runtime.setup` requirements lock, and OptPilot's own dependency
> closure, and reports the remainder under capability code
> `dependency_host_provisioned` plus a per-entry warning. It never imports
> authored code, so it satisfies D3's static-by-default trust rule, and it
> closes the hole that `--check-imports` cannot: a locked component is not
> exempt, and imports deferred inside `propose` are seen. Remaining D3 wiring:
> turn the capability into an ingress validation fact.

**D4 — Assistant repair loop (M, post-release; the differentiating feature).** On `REJECTED`: stage the imported tree into an editable Workspace, open (or reuse) a Conversation with the validation receipt's facts attached as bounded read-only context, and let the Assistant do what it already can — read files, propose approval-gated edits, re-run validation, and re-submit the ingress when clean. Every building block exists today (editable Workspaces, approval-gated `file_write`, `optpilot_registration_{prepare,validate,apply}` tools, bounded facts as context); D4 is orchestration plus one new trigger ("ingress rejected → offer to open repair Conversation"), not new machinery. The curation design's warning is the guardrail: the Assistant must fix packages so they are *runnable and reviewable*, not merely structurally valid — so the repair loop's definition of done includes a user-approved smoke run, not just a green validate.

**Implication for the four flagship packages (v1 requirement, not future work).** Each Phase 2 package must be structured to graduate into its own repository: fully self-contained, no cross-package or repo-relative reaches (the `pythonPath: ../../environments/...` hack that F5 removes is exactly the kind of coupling that would break repo extraction), documented entry points, and explicit runtime requirements. The flagship packages then double as the test corpus for D1–D4: "import `factorio_design_benchmark` from its repo URL" becomes both a CI case and a docs walkthrough. Package-id namespacing (e.g., owner-qualified ids) should be decided when D2 lands, before third-party packages can collide.

---

## 7. Phased execution plan

Phases are ordered by dependency; integration workstreams in Phase 2 are parallelizable. Effort classes as in §3; a "week" assumes one focused person.

**Phase 0 — Hygiene and truth (≈1 week).** Push local HEAD to the public remote (it is 5 weeks behind). F8 items: tests split, design-doc promotion into `designs/`, release-artifact assertions. U2 terminology finish. Resolve the COOPA license question and the Factorio canonical-rates question with authors (async — start now, they gate Phase 2 shipping, not Phase 2 start).

**Phase 1 — Framework primitives (≈2–3 weeks).** F1 typed inputs → F2 per-launch Study inputs → F3 command batch methods → F4 resource actions → F5 capabilities/policyValidation. F1/F2 first: everything else consumes them. Each lands with schema docs (`configuration.md`, `candidate-contracts.md`) and preflight/validation tests in the same PR.

**Phase 2 — Integrations (≈3–4 weeks, four parallel workstreams).**
- W1 DEVS-Gen: §5.1 items 1–4.
- W2 Policy search: §5.2 items 1–4 (item 4's second instantiation can slip to post-release without harming the story; the template + flagship must not).
- W3 COOPA: §5.3 items 1–4.
- W4 Factorio: §5.4 items 1–5.
- W5 (joint W1×W2, last): DEVS-Gen policy hook + `llm_policy_search` composition per §5.5 — scheduled after §5.1 item 1 and §5.2 items 1–2 land; explicitly allowed to slip to immediately post-release.
Each workstream's definition of done: package fully self-contained — no cross-package or repo-relative reaches — so it can later graduate to its own repository (§6); `optpilot package validate --check-source` clean; at least one smoke Study retained-launchable in CI (LLM-dependent methods get a stub/replay mode for CI); a docs page in `docs_assets/`; Assistant can find and card the package (tags + descriptions written for search).

**Phase 3 — UI completion (≈2 weeks, overlaps late Phase 2).** U1 forms renderer (against F1/F2/F4 data from real packages — build it against COOPA solve + a DEVS gallery environment, not synthetic fixtures). U3 Open work completeness, U5 interface context, U6 dynamic onboarding. U7 legacy-shell removal last, once the new surfaces cover everything. U8 only if time allows.

**Phase 4 — Release (≈1 week).** Version bump to 0.2.0 across the four synced strings; CI matrix green (core sdist/wheel isolation, studio artifact check, package smokes); docs pass (README, getting-started, new pages for the four capabilities, `concepts.md` alignment with final wording); `mkdocs build --strict`; tag, publish core to PyPI, publish docs; announcement draft pointing at the four flagship walkthroughs.

**Explicit v1 exclusions (documented, scheduled post-release):** container execution slice (evaluators first), native-wheel locking under trust, `session` protocol, Godot/MQTT visualization path, behavioral-conformance checkers, analytic Factorio throughput estimator, multi-user/remote execution, GitHub package import + Assistant repair loop (D2/D4 — D1 zip upload may land in v1 if Phase 3 finishes early; see §6).

---

## 8. Release criteria checklist

Status as of 2026-08-11 (verified on the owner's machine; see the notes).

- [x] All four packages install-validate-smoke in CI from a clean checkout — CI now runs `package validate --check-source` for all four flagship packages and a zero-LLM smoke Study for three of them (`factory_design_smoke`, `seird_minimize_deaths`, `queue_demo_baseline_smoke`). `or_solving` is validate-only in CI since the `solve_or_problem_mock` twin was removed 2026-08-12; its real study needs a user-provisioned COOPA checkout. The policy-search smoke needs a placeholder `OPENROUTER_API_KEY` because its method runtime is prepared before the first exchange; CI supplies one.
- [x] A new user can, in under 30 minutes with only the docs — `getting-started.md` now carries a "Where To Go Next" routing table mapping each of the four outcomes to its package root, command and prerequisites, and each flagship package has its own page. Three of the four outcomes run with no key at all; OR-from-text needs a key *and* a user-provisioned COOPA checkout, which the criterion's own "(given an API key)" wording does not cover — stated plainly in the docs. The 30-minute figure itself is not machine-verifiable and no page promises a time.
- [x] Every capability discoverable via Assistant search *and* via direct Catalog browsing — U4 gave `optpilot_catalog_list` free-text `query` + `tags`; U6 surfaces flagship capabilities by name on the welcome page from registry metadata.
- [x] No consequential action reachable from Assistant prose without a card + approval — enforced by the card contract and `_agent_permission_gate`; unchanged this cycle.
- [x] Core sdist/wheel contain no studio/catalog/tests/resource content; studio artifact contains no core prefix; versions synced — `scripts/check_release_artifacts.py` passes at 0.2.0. It was **failing before this cycle** (110 tracked non-script files carried the executable bit, and `catalog/or_solving/.../launch_console.sh` was a real launch script missing from `ALLOWED_EXECUTABLE_PATHS`); both fixed.
- [ ] `resource/` research trees absent from all distributions and from the public repo — **NOT MET, and it is the one hard blocker left.** The distributions are clean, but `_to_delete/_claude_resources.tar.gz` (86 MB, containing `reproduce-COOPA-BC8B/`) and `_to_delete/_claude_snapshot.tar.gz` (25 MB) are tracked in git, were added in `ab46b0a`, and that commit is already on `origin/phase-1-release-prep`. Because the blobs are in pushed history, `git rm` is insufficient — removal needs a history rewrite and a force-push, which is an owner decision. This also carries the unresolved COOPA licence question.
- [x] User-facing wording: "Run setup" consistent across Studio; schemas/APIs/CLI unchanged — completed by U2/U7.
- [x] Docs describe the executable slice honestly (what validates vs what runs), including user-provisioned runtimes for COOPA solvers and Factorio execution — every flagship page states its prerequisites and which paths are validate-only; the Factorio page states that no production rate can be derived statically, and the OR page leads with COOPA being user-provisioned and unlicensed.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| COOPA license unresolved | Ship package as external-source ingest (user fetches; OptPilot configures) rather than redistributing; keep the Run setup + docs in-tree |
| Generalized method regresses on DJSP-AGV | Keep the existing studies as regression fixtures; compare mean scores before/after rebase |
| F2 launch-inputs design grows into a session protocol | Scope strictly: inputs bound once at launch, immutable, retained; anything interactive stays in interfaces |
| Four parallel integration streams starve review | Phase-2 workstreams merge behind package-level CI smokes; core (Phase 1) is frozen during Phase 2 except bug fixes |
| Forms renderer scope creep | v1 depth limit + YAML fallback is the documented contract, not a temporary hack |
