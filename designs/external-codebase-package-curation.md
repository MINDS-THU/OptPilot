# External Codebase Package Curation

Status: draft implementation design

Audience: OptPilot maintainers

This document describes the gap between the current Studio registration flow
and the intended workflow:

> A user points OptPilot Studio at an existing project, the Assistant helps add
> thin OptPilot adapters and configs, and Studio turns the result into a clean
> OptPilot package that works in the core CLI and in Studio.

The goal is not to force external projects to reorganize themselves. The goal
is to preserve upstream code where possible, add explicit OptPilot boundaries,
and register a package whose environments, methods, resources, studies, setup,
and evidence behavior are runnable and reviewable.

## Why This Matters

OptPilot is useful when users can connect existing environments and methods
with little invasive rewriting. For example, a research codebase may contain:

- a simulator or evaluator
- baseline heuristics
- metaheuristics, RL trainers, or LLM search loops
- datasets, prompts, notebooks, and helper scripts

The OptPilot contribution should be small:

- adapter files that translate between OptPilot candidates and upstream APIs
- YAML configs that declare contracts, runtime setup, and metrics
- study files that bind compatible environment and method configs
- package docs that explain what to run first

If registration simply copies whichever files are near a config file, the
result can validate structurally but fail at runtime. That makes the Assistant
look helpful while producing a package users cannot trust.

## Current Implementation

### What Works

The core package model is already useful:

- `optpilot package validate` indexes package folders and validates
  `config: environment`, `config: method`, `config: resource`, and
  `config: study` files.
- Package ids are folder names, and Studio uses package-qualified ids such as
  `example_package/environment/job-shop-dispatch-rule`.
- Study launch copies environment and method sources into
  `runs/<run>/source/environment` and `runs/<run>/source/method`.
- `runtime.setup` runs in the copied source tree for process runtimes.
- `runtime.envFromHost` is explicit for core CLI runs, and Studio settings can
  provide declared variables for Studio-launched runs.
- Studio can discover environment and method configs inside an editable
  workspace and apply a registration into `catalog/local_package`.

### Current Registration Flow

Studio currently does this for a workspace:

1. scan YAML files under the workspace root
2. keep files with `apiVersion: optpilot.io/v1` and `config` equal to
   `environment` or `method`
3. validate each config
4. create one registration target per selected config
5. infer an include list from the config file, likely adapter file, command
   files, `methodContext.references`, and nearby `assets`, `prompts`, `data`,
   or `cases`
6. copy each included workspace-relative file into:
   - `catalog/local_package/environments/<id>/...`
   - `catalog/local_package/methods/<id>/...`

This is deliberately simple, but it is not a complete package curation system.

### Concrete Failure Case

The Factory AGV workspace contains OptPilot configs and adapters under:

```text
optpilot_configs/
  environments/factory_agv_scheduling/environment.yaml
  environments/factory_agv_scheduling/evaluator.py
  methods/factory_weighted_heuristic/method.yaml
  methods/factory_weighted_heuristic/method.py
```

It also contains upstream source under:

```text
src/factory_sim/
experiments/factory_sim/
optimization_app/
```

The current registration copied the environment config and adapter plus a few
focus files into:

```text
catalog/local_package/environments/factory-agv-scheduling/
  optpilot_configs/environments/factory_agv_scheduling/environment.yaml
  optpilot_configs/environments/factory_agv_scheduling/evaluator.py
  optimization_app/factory_optimization_app/agv_scheduling_optimization/heuristic.py
```

The method copied only:

```text
catalog/local_package/methods/factory-weighted-heuristic/
  optpilot_configs/methods/factory_weighted_heuristic/method.yaml
  optpilot_configs/methods/factory_weighted_heuristic/method.py
```

This is schema-valid but not a coherent package:

- The environment evaluator imports `src.factory_sim...`, but `src/` was not
  copied into the registered environment.
- The config remains nested under `optpilot_configs/...` instead of becoming
  the component-root config.
- The registered entry does not include a study file that proves the environment
  and method run together.
- The validator checks YAML shape, not import closure or smoke execution.
- Shared project source has no explicit owner, so it is easy for registration
  to put too much source into one component or too little source into all
  components.

## Desired User Experience

The ideal Studio flow should be:

1. The user creates or attaches an editable workspace for an external project.
2. The Assistant inspects the project and writes OptPilot adapters/configs into
   a draft area, without reorganizing upstream source unnecessarily.
3. Studio classifies the attached project as environment-only, method-only,
   environment-plus-method, resource-only, or not-yet-classifiable.
4. Studio discovers configs and proposes a package plan.
5. The user reviews:
   - package id
   - package classification
   - selected environments, methods, resources, and studies
   - source files included for each component
   - shared source policy
   - dependency/setup declarations
   - environment variables required from host or Studio settings
6. Studio validates schemas and import/source closure.
7. Studio runs at least one smoke study when a compatible pair is available,
   or explains why smoke execution is not possible yet.
8. Studio registers the curated package into `catalog/local_package` by
   default, or another user-selected package root in a future advanced flow.
9. The same package can run with:

```bash
optpilot package validate catalog/local_package
optpilot validate catalog/local_package/studies/<study>.yaml
optpilot run catalog/local_package/studies/<study>.yaml
```

The external project does not need to have `environments/` and `methods/`
folders before curation. The registered package should have that layout.

## Glossary

| Term | Meaning |
| --- | --- |
| External project | The user-attached codebase before OptPilot curation. It may be an upstream repository, local research folder, benchmark, simulator, method implementation, helper app, or a mix of these. |
| Adapter | Thin code added by the user or Assistant to translate between OptPilot's candidate/evaluator protocol and the external project's native API or command. |
| Component | A public OptPilot environment, method, or resource entry. Studies are not components; they bind components into a run. |
| Package | A reusable folder containing OptPilot configs, adapter code, source files, docs, resources, and optional studies. |
| Resource | Supporting material or a launchable helper interface that is useful in Studio but is not itself a method or evaluator in the core study loop. |
| Study | A concrete run plan that binds one environment config to one method config with an objective, budget, execution policy, and evidence policy. |
| Package plan | Studio state that records how an editable workspace should be normalized into a package. It is not a public YAML schema. |
| Source closure | The set of files needed for a component to import, set up, and run after registration. |
| Shared source | Upstream code used by more than one component. It must be handled explicitly; otherwise registration can copy too little or hide too much source in one component. |
| Draft workspace | The editable external project workspace before registration. |
| Registered package | The normalized package under `catalog/local_package` or another catalog root after registration. |

## Curation Modes

Attached workspaces do not all represent the same kind of package. Studio
should classify the workspace before it recommends a package plan.

| Mode | What the external project contains | Registered package contents | Smoke-run expectation |
| --- | --- | --- | --- |
| Environment-only | A simulator, benchmark, dataset evaluator, scoring harness, Gym-style environment, or service wrapper. | One or more environments, optional resources, optional study templates. | Validate configs and source closure. Run a smoke study only if the workspace or catalog also provides a compatible method. |
| Method-only | An optimizer, solver, heuristic search loop, RL trainer, LLM code editor, or candidate generator. | One or more methods, optional resources, optional study templates. | Validate configs and source closure. Run a smoke study only if the workspace or catalog also provides a compatible environment. |
| Environment-plus-method | A project that contains both evaluator/simulator code and candidate-producing algorithms. | Environments, methods, and at least one study binding a compatible pair. | Strongly prefer one runnable smoke study before applying. |
| Resource-only | Docs, datasets, helper apps, notebooks, dashboards, or launchable utilities that are useful but not a direct evaluator or method. | Resources and optional docs. | No study smoke run. Validate resource manifest and interface setup when declared. |
| Not-yet-classifiable | A project that may contain useful code, but no reliable OptPilot boundary has been identified. | Draft workspace only until an environment, method, or resource boundary is chosen. | Do not register as runnable components. Ask for user review or more upstream reconnaissance. |

This classification is Studio planning state. It is not a new public config
field. Public package contents remain ordinary `environment`, `method`,
`resource`, and `study` YAML files.

Environment-only and method-only packages are still valid packages. They should
not be forced to invent dummy counterpart components. A smoke study becomes
required only when the package claims to provide a runnable environment-method
pair.

## Curation Workflows

The package plan should follow the attached project's mode.

| Workflow | Plan behavior | Readiness claim |
| --- | --- | --- |
| Environment-only | Register evaluator configs, evaluator adapters, environment-owned data, trial workspace files, setup declarations, metrics, and method-visible references. Do not synthesize a method unless a trivial baseline is semantically meaningful. | Component-ready when schema, source closure, imports, and setup-file checks pass. Run-ready only after pairing with a compatible method and smoke study. |
| Method-only | Register method configs, method adapters, dependencies, settings, and `accepts.formats` plus required context/capabilities. | Component-ready when schema, source closure, imports, and setup-file checks pass. Run-ready only after pairing with a compatible environment and smoke study. |
| Environment-plus-method | Register each side independently, then add studies only for pairs that pass candidate-format, required-context, and capability checks. | Run-ready when at least one paired study passes schema, source closure, imports, setup requirements, and smoke execution, or when a concrete external blocker is reported. |
| Resource-only or support resource | Register resource manifests, docs, helper apps, datasets, notebooks, launch setup, and interface declarations. Resources may appear by themselves or alongside environment/method components. | Resource-ready when manifests and declared interface/setup paths validate. It is not run-ready because it does not provide a study loop. |

Readiness labels in Studio should reflect these distinctions. In particular,
`schema valid`, `component-ready`, `resource-ready`, and `run-ready` are
different states.

## Design Principles

### Preserve The OptPilot Core Model

Do not add domain-specific first-class concepts such as instances, factories,
jobs, AGVs, benchmarks, engines, or datasets. Domain inputs remain in:

- `environment.evaluator.settings`
- `environment.methodContext.references`
- `method.settings`
- normal package files

### Register Packages, Not Just Files

The unit of curation should be a package plan. A package plan may contain
multiple components and studies, plus shared docs/resources. A component target
is still important, but it should be reviewed in the context of the package.

### Keep Catalog Source Clean

Registration should normalize draft adapter/config files into a clean package
layout. It should not preserve temporary draft paths such as
`optpilot_configs/environments/...` unless the user explicitly wants that.

### Validate What Users Care About

Schema validation is necessary but not sufficient. A release-quality registered
package should also pass:

- source path resolution
- visible Python import checks under the registered package layout
- setup-file checks without executing setup commands
- approval-gated setup execution when requested
- at least one smoke study when the package claims to provide a runnable
  environment-method pair

Known path fields can be checked automatically. Arbitrary strings inside
component-owned `settings` objects cannot be interpreted as paths unless the
package plan records additional source hints. For example,
`evaluator.settings.layoutConfig` might be a path in one project and a plain
identifier in another.

### Keep Shared Source Explicit

External projects often have shared source code used by both environment and
method adapters. The registration plan should make this visible instead of
accidentally assigning shared code to one component.

For the first implementation, shared upstream source should be duplicated into
the component roots that need it. Package-level shared source under `shared/` or
top-level `src/` requires an explicit run-source change; `pythonPath` alone is
not a source-copy contract in the current runner.

## Proposed Package Plan Model

Studio should create a package plan before registration. This is a Studio
planning artifact, not a new public OptPilot YAML schema.

Example shape:

```json
{
  "id": "pkg_plan_123",
  "workspace_id": "ws_abc",
  "package_id": "local_package",
  "classification": "environment-plus-method",
  "readiness": "schema-valid",
  "destination": "catalog/local_package",
  "source_root": "/path/to/workspace",
  "components": [
    {
      "kind": "environment",
      "id": "factory-agv-scheduling",
      "draft_config_path": "optpilot_configs/environments/factory_agv_scheduling/environment.yaml",
      "registered_config_path": "environments/factory-agv-scheduling/environment.yaml",
      "component_root": "environments/factory-agv-scheduling",
      "include": [
        "optpilot_configs/environments/factory_agv_scheduling/evaluator.py",
        "src/factory_sim/**",
        "experiments/factory_sim/baseline/heuristics/enhanced_snapshot.py",
        "optimization_app/factory_optimization_app/agv_scheduling_optimization/heuristic.py"
      ],
      "source_hints": [
        {
          "path": "src/factory_sim/config/factory_layout_multi.yml",
          "reason": "Referenced by evaluator.settings.layoutConfig, which is opaque to core schema validation."
        }
      ],
      "path_rewrites": [
        {
          "from": "optpilot_configs/environments/factory_agv_scheduling/evaluator.py",
          "to": "evaluator.py"
        },
        {
          "from": "optpilot_configs/environments/factory_agv_scheduling/environment.yaml",
          "to": "environment.yaml"
        }
      ],
      "validation": {}
    },
    {
      "kind": "method",
      "id": "factory-weighted-heuristic",
      "draft_config_path": "optpilot_configs/methods/factory_weighted_heuristic/method.yaml",
      "registered_config_path": "methods/factory-weighted-heuristic/method.yaml",
      "component_root": "methods/factory-weighted-heuristic",
      "include": [
        "optpilot_configs/methods/factory_weighted_heuristic/method.py"
      ],
      "path_rewrites": [
        {
          "from": "optpilot_configs/methods/factory_weighted_heuristic/method.py",
          "to": "method.py"
        },
        {
          "from": "optpilot_configs/methods/factory_weighted_heuristic/method.yaml",
          "to": "method.yaml"
        }
      ],
      "validation": {}
    }
  ],
  "studies": [
    {
      "name": "factory-agv-fixed-weights",
      "path": "studies/factory_agv_fixed_weights.yaml",
      "environment": "factory-agv-scheduling",
      "method": "factory-weighted-heuristic",
      "smoke": true
    }
  ],
  "resources": [],
  "validation": {
    "schema": {},
    "source_closure": {},
    "imports": {},
    "setup_files": {},
    "smoke": {}
  }
}
```

The plan is allowed to contain implementation details because it is Studio
state. The public package that results from the plan still uses ordinary
OptPilot config files.

## Registered Package Layout

For a curated external project, the registered package should look like this:

```text
catalog/local_package/
  README.md
  environments/
    factory-agv-scheduling/
      environment.yaml
      evaluator.py
      src/factory_sim/
      experiments/
      optimization_app/
  methods/
    factory-weighted-heuristic/
      method.yaml
      method.py
  studies/
    factory_agv_fixed_weights.yaml
```

This layout is intentionally boring. Each component root should contain the
config at the top level and enough source for that component to run.

If a large shared source tree is required by multiple components, use the
component-owned copy policy first:

| Policy | When to use | Layout |
| --- | --- | --- |
| Duplicate into component roots | Source is small or one component is the clear owner. | Copy needed files into each component root. |
| Package shared source | Future feature for large source trees shared by multiple components. | Copy source once under `shared/` or `src/` only after the run-source copier supports package-level shared source roots. |

The first implementation should support only the component-owned copy policy.
Package shared source should remain a later feature because current study launch
copies environment and method source independently into `runs/<run>/source/...`.
A package-level `src/` sibling will not reliably appear in those run-source
copies unless the core run-source copier is extended.

## Source Ownership Heuristics

Studio should propose source ownership, then let the user edit the plan.

Environment-owned files usually include:

- simulator or evaluator source
- benchmark cases and small datasets used by evaluation
- evaluator adapter files
- environment-owned prompts or instructions
- files referenced by `methodContext.references`

Files in `methodContext.references` remain owned by the environment contract.
They are copied with the environment and exposed to compatible methods as
read-only context; they are not method implementation files.

Method-owned files usually include:

- optimizer/search/trainer source
- method adapter files
- method prompts
- default candidate generators or solver wrappers

Shared files usually include:

- import packages used by both environment and method
- common data schemas
- project-level dependency files
- project README or docs

The Assistant can recommend ownership, but Studio should show the include plan
before applying it.

If the same upstream library is imported by both evaluator and method code,
duplicate the required source into both component roots in the first
implementation. Later, package-level shared source can reduce duplication after
the runner knows how to copy or mount a package-level shared source root.

## Config Rewriting

When registering a package, Studio should normalize config files:

- copy environment configs to
  `environments/<id>/environment.yaml`
- copy method configs to `methods/<id>/method.yaml`
- copy resource manifests to `resources/<id>/optpilot.resource.yaml`
- write study files under `studies/`
- preserve public config semantics
- update relative paths so they resolve in the registered package

Config rewriting needs a field-by-field rule table:

| Field | Rewrite behavior |
| --- | --- |
| `environment.evaluator.pythonPath` | Rewrite entries relative to the new environment config location. For component-owned copies, prefer `"."` plus any copied subdirectories. |
| `method.entrypoint.pythonPath` | Rewrite entries relative to the new method config location. |
| `environment.methodContext.instructions` | Rewrite to the copied environment-owned instruction files. |
| `environment.methodContext.references[].path` | Rewrite to copied environment-owned reference files. |
| `environment.trialWorkspace[].from` | Rewrite to copied environment-owned seed files or directories. |
| `environment.evaluator.command` and `method.entrypoint.command` | Rewrite only arguments that are clearly file paths relative to the old config directory. Leave placeholders such as `{input_file}` unchanged. |
| `runtime.setup.steps[].cwd` and setup requirement files | Rewrite when the path is declared in a setup field with known semantics. |
| `runtime.container.build.context` and `runtime.container.build.dockerfile` | Rewrite relative to the new component root when the build context is included. |
| `resource.interface.command`, `resource.interface.cwd`, and `resource.interface.setup` paths | Rewrite for registered resources and support resources. |
| study `environmentConfig` and `methodConfig` | Rewrite to the registered package paths. |
| `outputFiles`, `records`, and evaluator metric paths | Keep relative to trial workspace/evaluator runtime semantics unless the public config gives clear source-file semantics. |
| `evaluator.settings`, `method.settings`, and arbitrary command arguments | Do not infer path meaning automatically. Use package-plan `source_hints` when the Assistant or user identifies a path inside opaque settings. |

The rewriting rule should be conservative. If Studio cannot safely rewrite a
path, it should keep the draft value, record a warning, and require either a
source hint or manual review before the package can be called run-ready.

## Validation Levels

Add explicit validation levels for package plans:

| Level | Purpose | Blocking for apply? | Checks |
| --- | --- | --- | --- |
| Schema | Config shape is valid. | Yes. | Existing `validate_authoring_config`. |
| Source closure | Known public path fields and package-plan source hints resolve in the registered layout. | Yes for component-ready and run-ready claims. | Resolve `pythonPath`, `methodContext`, `trialWorkspace`, command paths with clear semantics, setup-file paths, resource interface paths, and `source_hints`. |
| Import | Python callables import under registered layout. | Yes when the user asks for import validation or run-ready claims. | Run each entry in isolated import state, respecting declared `pythonPath`. |
| Setup files | Declared setup files exist without executing setup. | Yes when setup is declared. | Check setup `cwd`, requirement files, Dockerfile/build context, and package manager files. |
| Setup execution | Declared process setup can run. | Approval-gated; required only when claiming fully installed readiness. | Execute process `runtime.setup` or `interface.setup` in a temporary editable copy or workspace runtime. |
| Smoke | The package actually runs. | Required for run-ready paired packages. Not required for one-sided packages. | Run selected smoke studies with small budgets or explain dependency, credential, runtime, or missing-counterpart blockers. |

The UI should distinguish these levels. “Schema valid” must not be displayed as
“ready to run”.

Current `--check-imports` is not strong enough to be used as a readiness gate.
Before package-plan validation depends on it, import validation must:

- respect declared `pythonPath`
- avoid `sys.modules` collisions between config-local modules such as
  `method.py`
- run in a subprocess or otherwise isolate import state per entry
- surface warnings in normal CLI and UI output
- be able to fail the requested validation level instead of returning a valid
  package with hidden warnings

`--check-source` should be authoritative only for fields with public path
semantics and for package-plan `source_hints`. It should not pretend to
understand arbitrary component settings.

## Assistant Workflow

The Assistant should become better at external-codebase curation through
workflow guidance and tools, not by inventing package rules in chat.

### Prompt Changes

The system prompt should tell the Assistant:

- first find the smallest upstream command/API that already works
- identify whether the project is environment-only, method-only,
  environment-plus-method, resource-only, or not-yet-classifiable
- write adapters under a draft OptPilot area
- include at least one smoke study when a compatible environment-method pair is
  available
- do not assume schema validation proves runtime readiness
- ask Studio to prepare a package plan before applying registration

### Tool Changes

Add tools or extend existing tools:

| Tool | Purpose |
| --- | --- |
| `optpilot_package_plan_prepare` | Discover configs and propose a package-level plan. |
| `optpilot_package_plan_update` | Let the Assistant adjust includes, shared source, package id, and study paths. |
| `optpilot_package_plan_validate` | Run schema/source/import validation on a temp package. |
| `optpilot_package_plan_smoke` | Run selected smoke studies after approval. |
| `optpilot_package_plan_apply` | Register the normalized package after approval. |

The existing `optpilot_registration_prepare`, `validate`, and `apply` tools can
remain as lower-level compatibility paths, but the UI should steer external
project curation toward package plans.

The Assistant also needs package-plan context in the visible Studio context
packet. That context should include classification, selected components,
include/exclude lists, path rewrites, source hints, required environment
variables, setup declarations, validation levels, smoke status, and apply
blockers. Without that context, the Assistant can call tools but cannot reliably
notice that a package is method-only, that a paired package lacks a compatible
study, or that shared source was omitted.

## UI Changes

Replace the current registration panel’s component-only flow with a package
curation flow:

1. **Discover**
   - show found configs
   - show likely package id
   - show likely package classification
   - show whether studies are present
2. **Plan**
   - show each component
   - show support resources, not only resource-only fallbacks
   - show source includes and path rewrites
   - show plan-only source hints for opaque settings paths
   - show shared source policy
   - show required env vars and setup steps
3. **Validate**
   - schema/source/import results separately
   - setup-file checks separately from setup execution
   - warnings or blockers for missing smoke studies, unresolved imports, large
     source copies, undeclared secrets, and opaque settings paths without source
     hints
4. **Smoke Run**
   - optional for one-sided packages
   - strongly recommended for paired packages
   - use a selected or temporary smoke study with a small budget
5. **Apply**
   - write normalized package files
   - refresh catalog
   - show exact registered paths

Resource registration should not be only a fallback when no configs are found.
Real external projects often contain runnable components plus helper UIs,
dataset browsers, upstream docs, notebooks, or reference material. The plan
should support:

- resource-only packages
- support resources registered alongside environments and methods
- resource fallback when no runnable boundary has been found

## Core CLI Changes

The core CLI should stay Studio-free, but it should gain deeper package checks
that Studio can also call.

Add:

```bash
optpilot package validate path/to/package --check-source --check-imports
```

`--check-source` should verify:

- config paths resolve under the package
- `evaluator.pythonPath` and `entrypoint.pythonPath` entries exist
- Python module files are findable relative to declared `pythonPath`
- `methodContext.references` paths exist
- `trialWorkspace.from` paths exist
- setup step `cwd` and requirement files exist
- resource interface `cwd`, command scripts, and setup paths exist
- command entrypoint script paths are present when they look file-like
- package-plan source hints exist when validating a plan-materialized temp
  package

`--check-imports` should be rewritten before it becomes a readiness gate:

- run each import check in isolated state, preferably a subprocess
- honor declared `pythonPath`
- surface warnings in normal CLI output
- optionally fail the command when import validation was explicitly requested

Setup needs two modes:

```bash
optpilot package validate path/to/package --check-setup-files
optpilot package setup-check path/to/package --run-setup
```

The first command only verifies files needed by setup declarations. The second
executes setup and should remain explicit because setup can install
dependencies, write files, and require credentials.

Add later:

```bash
optpilot package smoke path/to/package --study studies/foo.yaml
```

This can be a wrapper over `optpilot run` with a temporary output root, but it
also needs a budget story. Studio can either materialize a temporary smoke
study with a small `budget.maxTrials`, or the CLI can grow explicit smoke
overrides. Until then, “small budget” should mean “the selected smoke study
itself declares a small budget.”

## Implementation Plan

### Phase 1: Make The Gap Explicit And Prevent False Readiness

1. Rename UI validation wording from “ready” to “schema valid” where only
   schema validation has run.
2. Fix `optpilot package validate --check-imports` so it respects declared
   `pythonPath`, isolates imports per entry, surfaces warnings in normal output,
   and can fail when explicitly requested.
3. Add `--check-source` and `--check-setup-files` to the core validator.
4. Update docs to distinguish schema-valid, component-ready, resource-ready,
   and run-ready states.

Expected tests:

- package with missing `methodContext.references` fails `--check-source`
- package with missing Python import target warns or fails under
  `--check-imports --check-source`
- package with missing resource interface command path fails source checks
- package with `method.py` in multiple method directories does not produce
  false import collisions
- existing `catalog/example_package` passes

### Phase 2: Normalize Registered Component Layout

1. Change registration apply so selected config files become:
   - `environments/<id>/environment.yaml`
   - `methods/<id>/method.yaml`
2. Copy sibling adapter files to component root by default:
   - `evaluator.py`
   - `method.py`
   - command scripts
3. Rewrite known public path fields using the field-by-field rewrite table.
4. Record warnings for opaque settings paths unless the package plan supplies
   source hints.
5. Preserve the old component-registration endpoint, but mark it as a
   component registration, not package curation.

Expected tests:

- a draft config in `optpilot_configs/...` registers to a top-level component
  config
- registered `config_path` points to the actual copied file
- rewritten `methodContext.references`, `trialWorkspace.from`, setup paths, and
  study config paths resolve in the registered layout
- existing neat component directories still register correctly

### Phase 3: Add Package Plans

1. Add package plan persistence under:

```text
.optpilot-ui/workspaces/<workspace_id>/package_plans/<plan_id>.json
```

2. Implement package plan preparation from discovered configs.
3. Add classification for environment-only, method-only,
   environment-plus-method, resource-only, and not-yet-classifiable workspaces.
4. Add source-root inference:
   - config-local adapter files
   - declared `pythonPath`
   - importable module package roots
   - referenced files
   - project dependency files
   - resource interface files
   - user/Assistant-provided source hints for opaque settings paths
5. Add plan editing endpoints for include/exclude/path rewrite/source-hint
   policy.
6. Add UI review for component includes, support resources, source hints, and
   readiness states.

Expected tests:

- Factory AGV-like fixture with `optpilot_configs/` and `src/` produces a plan
  that includes the required `src/` tree
- environment-only and method-only fixtures produce valid one-sided package
  plans without dummy counterpart components
- resource plus environment fixture keeps the helper app as a resource instead
  of copying it into the environment component
- user can remove an include and validation reports the missing source
- plan survives Studio restart

### Phase 4: Validate And Smoke Planned Packages

1. Materialize a package plan into a temporary directory.
2. Run schema validation on the temp package.
3. Run source closure validation.
4. Run import checks under registered layout.
5. Run setup-file checks.
6. If a compatible study is present and user approves, run selected smoke
   studies with a temporary output root.
7. If setup execution is requested, run it as an explicit approval-gated step.

Expected tests:

- temp package validation catches missing `src/factory_sim`
- temp package validation catches an opaque settings path only when it is
  represented as a package-plan source hint
- smoke run creates a run dir and records zero unexpected failures for a paired
  toy package
- method-only package reports component-ready but not run-ready until paired
- failure output is shown in the plan instead of applying a broken package

### Phase 5: Assistant Integration

1. Update Assistant prompt around external project curation.
2. Add package-plan tools.
3. Add package-plan context to the Assistant context packet.
4. In registration mode, make the Assistant use package-plan tools by default.
5. Keep approval required for apply, setup execution, and smoke runs.

Expected tests:

- Assistant tool schema exposes package-plan operations
- Assistant context includes classification, include lists, path rewrites,
  source hints, validation levels, and smoke status
- package-plan apply requires approval
- stale component-registration flows still work for simple cases

## Open Decisions

### Shared Source Layout

For large projects, should registered packages copy shared source once under
`shared/` or `src/`, or duplicate needed source into each component?

Recommendation:

- start with duplicated component-owned copies because this matches current
  run-source semantics
- add package-level shared source only after the runner can copy or mount a
  package-level shared source root into runs
- make any package-level shared source behavior explicit in the package-plan
  validator rather than relying on `pythonPath` side effects

### Destination Package

Should Studio always write into `catalog/local_package`, or should users choose
a package folder?

Recommendation:

- keep `catalog/local_package` as the default
- add an advanced field for package id/destination
- never overwrite `catalog/example_package`

### Smoke Study Generation

Should the Assistant be allowed to synthesize a study when none exists?

Recommendation:

- yes, when exactly one compatible environment/method pair is present
- the generated study should be visibly marked as a draft smoke study
- apply should warn when a runnable package has no study

## Success Criteria

The implementation is good enough when:

1. A package made by the Assistant from an external project has a clean package
   layout.
2. The package passes schema validation, source-closure validation, setup-file
   checks, and visible import checks for all applicable components.
3. Source closure checks catch missing known-path files and missing
   package-plan source hints before registration.
4. Environment-only and method-only packages can be registered as
   component-ready without inventing dummy counterpart components.
5. Paired packages include at least one smoke study, or clearly report the
   dependency, credential, runtime, or missing-counterpart blocker.
6. Studio catalog browsing, editable copies, installs, launches, and studies
   still work for `catalog/example_package`.
7. The core PyPI package can validate and run the curated package without
   Studio-only code.

## Immediate Recommendation For The Factory AGV Package

Do not treat the current `catalog/local_package` copy as release-quality. It
should be regenerated through the package-plan workflow.

Until that exists, the practical manual fix is:

1. Create a clean package root under `catalog/local_package`.
2. Move the environment config to
   `environments/factory-agv-scheduling/environment.yaml`.
3. Move `evaluator.py` beside it.
4. Include the upstream simulator source needed by the evaluator, especially
   `src/factory_sim/**`.
5. Move the method config to
   `methods/factory-weighted-heuristic/method.yaml`.
6. Move `method.py` beside it.
7. Add a small study under `studies/`.
8. Run:

```bash
uv run optpilot package validate catalog/local_package --check-source --check-imports
uv run optpilot validate catalog/local_package/studies/<study>.yaml
uv run optpilot run catalog/local_package/studies/<study>.yaml --output-root /tmp/optpilot-factory-agv-check
```

If the evaluator needs too much upstream source, prefer an explicit
component-owned copy for now. Package-level shared source should wait until the
runner and package-plan validator support it directly.
