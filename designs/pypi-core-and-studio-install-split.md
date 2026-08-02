# PyPI Core And Source Studio Install Split

Status: draft implementation design

Audience: OptPilot maintainers

This document describes how to split OptPilot into two clear installation
stories without breaking the Studio functionality already tested in the GUI.

## Goals

OptPilot should have two official install modes:

1. **PyPI Core Install**
   - For users who want the Python SDK and CLI.
   - Supports validating and running OptPilot studies from user-owned packages.
   - Supports every executable field in the public config schema.
   - Does not ship or expose OptPilot Studio, OpenHands assistant code, Code
     Server workspace management, or Studio static assets.

2. **Source Checkout Studio Install**
   - For contributors and users who want to self-host the full Studio.
   - Includes the local web UI, catalog browser, configuration forms, run
     monitor, environment/secret settings, resource/interface launch, Code
     Server workspaces, workspace runtime containers, and OpenHands assistant.
   - Keeps the GUI behavior we have already tested.

The rule of thumb is:

> A user should be able to install `optpilot` from PyPI, create a package with
> environments, methods, resources, and studies, validate and run it from the
> CLI, and later point OptPilot Studio at the same package without rewriting it.

## Non-goals

- Do not publish the full Studio to PyPI in the initial split.
- Do not require OpenHands, Code Server, or Studio workspace containers for
  PyPI users.
- Do not make the PyPI CLI launch GUI interfaces. `interface` remains valid
  package metadata for Studio.
- Do not change the public YAML model as part of this split, except for small
  documentation clarifications if a field is Studio-only.
- Do not remove any Studio feature that currently works in source checkout.

## Current Problems

The current repo blurs the boundary between core and Studio:

- `src/optpilot/cli.py` imports `optpilot.ui.server` at module import time.
- `optpilot ui` is registered in the core CLI.
- `pyproject.toml` packages `optpilot.ui`, `optpilot.assistant_assets`, and
  `optpilot.docs_assets`.
- UI, assistant, docs-asset, and core tests currently live together in
  `tests/test_mvp.py`.
- Public docs use `uv run optpilot ui --open-browser` in first-run flows, which
  makes the UI look like part of the core install.

The core runtime itself is much closer to the desired model:

- Study runs copy catalog source into `runs/.../source`.
- process-runtime `runtime.setup` runs inside the editable source copy.
- Process and container runtimes are implemented for environment evaluation.
- Process and container runtimes are implemented for command and Python methods.
- `env` and explicit `envFromHost` are already supported.
- Resource configs are validated, while resource interface launch is handled by
  Studio.

## Design Decision

Use two Python distributions in one repository:

```text
OptPilot repo
  pyproject.toml                  # core PyPI distribution: optpilot
  src/optpilot/                   # core package
  studio/
    pyproject.toml                # source-checkout Studio distribution
    src/optpilot_studio/          # Studio package
```

The root `optpilot` distribution remains the public PyPI package. The Studio
distribution is source-checkout-only at first. It may be published later as
`optpilot-studio`, but that is not required for this release.

The source checkout must become an explicit `uv` workspace so local Studio
development installs both packages from the repository rather than accidentally
resolving `optpilot` from PyPI:

```toml
[tool.uv.workspace]
members = ["studio"]

[tool.uv.sources]
optpilot = { workspace = true }
```

The exact workspace metadata can be adjusted during implementation, but the
release check must prove `uv sync --all-packages --locked` installs both the
local root package and the local Studio package.

## Package Boundaries

### Core package: `optpilot`

Keep in `src/optpilot/`:

- config loading, validation, and compilation
- JSON schemas
- runner
- scheduler
- evidence store
- candidate materialization and validation
- environment adapters
- method runtime
- process and container execution backends
- container helper functions
- CLI commands for validation, runs, and package validation

Remove from the core wheel:

- `optpilot.ui`
- `optpilot.agent`
- `optpilot.assistant_assets`
- `optpilot.docs_assets`
- Studio static files
- workspace runtime Dockerfile/assets

The core package can still depend on Docker or Podman being installed when a
config requests `runtime.sandbox: container`. It should not depend on Studio or
OpenHands to support container execution.

Container execution has one additional PyPI-specific requirement: the container
image must be able to import `optpilot` inside the container. In a source
checkout this can work by mounting the repository, but in a wheel install there
is no `cwd/src` tree to mount. The initial supported contract is therefore:

- process runtimes use the installed PyPI package directly
- container images or container builds must install `optpilot` plus the
  component's runtime dependencies
- release tests must include a clean-wheel container smoke where the image/build
  installs the wheel or installs `optpilot` from PyPI-compatible artifacts

An automatic "mount the installed core package into the container" mode can be
considered later, but it is not required for the first split.

### Studio package: `optpilot_studio`

Move into `studio/src/optpilot_studio/`:

- `src/optpilot/ui/`
- `src/optpilot/agent.py`
- `src/optpilot/assistant_assets/`
- `src/optpilot/docs_assets/`
- workspace runtime assets

Update imports in the moved Studio code:

```python
# before
from ..agent import OpenHandsAdapter
from ..config import compile_authoring_config

# after
from optpilot_studio.agent import OpenHandsAdapter
from optpilot.config import compile_authoring_config
```

Studio should depend on the core package and reuse the same core APIs. Studio
must not fork or copy core config/runtime logic.

## CLI Boundary

### Core CLI

Core PyPI CLI should expose:

```bash
optpilot validate path/to/config.yaml
optpilot run path/to/study.yaml
optpilot package validate path/to/package
```

Core CLI should not import Studio modules at import time.

### Studio CLI

The source checkout should keep an easy Studio command:

```bash
uv run optpilot ui --open-browser
```

and may also expose:

```bash
uv run optpilot-studio --open-browser
```

To preserve `optpilot ui` for source checkout without shipping Studio in PyPI,
add a lightweight command plugin mechanism:

```text
core optpilot.cli
  registers core commands
  loads optional command providers from entry points

studio package
  registers the ui subcommand through an entry point
```

Suggested entry point group:

```toml
[project.entry-points."optpilot.commands"]
ui = "optpilot_studio.cli:add_ui_subcommand"
```

The command-provider contract must be explicit. A provider receives the core
`subparsers` object, registers its parser, and sets a callable handler:

```python
def add_ui_subcommand(subparsers) -> None:
    parser = subparsers.add_parser("ui", help="Start OptPilot Studio")
    add_ui_arguments(parser)
    parser.set_defaults(handler=run_ui_from_args)
```

Core command handlers should use the same pattern:

```python
run_parser.set_defaults(handler=run_command)
validate_parser.set_defaults(handler=validate_command)
```

Then `main()` dispatches through `args.handler(args)`. This avoids hard-coded
Studio imports in core while still allowing `optpilot ui` in a source checkout
where the Studio entry point is installed.

Behavior:

- PyPI core install: no Studio entry point is installed, so `optpilot --help`
  shows no UI command.
- Source Studio install: Studio entry point is installed, so `optpilot ui`
  behaves as it does today.

This preserves the tested source-checkout command while keeping PyPI clean.

## Config Support Matrix

The PyPI core install must support every field that affects validation or study
execution. Fields used only by Studio must still validate so packages remain
portable.

| Config area | Core PyPI behavior | Studio behavior |
| --- | --- | --- |
| `environment.evaluator.python` | validate and execute | same |
| `environment.evaluator.command` | validate and execute | same |
| `environment.evaluator.adapter` | validate and execute | same |
| `environment.candidate` | validate and compile into study contract | display and edit |
| `environment.methodContext` | validate, resolve, pass to method context | display and edit |
| `environment.capabilities` | validate, check method requirements | display and edit |
| `environment.metrics` | validate and extract metrics | display and edit |
| `environment.records` | validate and extract records | display and edit |
| `environment.trialWorkspace` | validate and copy into trial workspace | display and edit |
| `environment.outputFiles` | validate and capture evidence | display and edit |
| `environment.runtime.sandbox: process` | run locally in subprocess worker | same |
| `environment.runtime.sandbox: container` | run through Docker/Podman backend | same |
| `environment.runtime.setup` | process runtime only; run in editable source copy | same |
| `environment.runtime.env` | inject into runtime process/container | same |
| `environment.runtime.envFromHost` | explicit host env passthrough | same, with Studio-managed values |
| `environment.runtime.workdir` | must be implemented for environment workers before release, or removed/marked unsupported for environments | same |
| `environment.interface` | validate only | create editable workspace, install, launch |
| `method.entrypoint.python` | validate and execute | same |
| `method.entrypoint.command` | validate and execute batch protocol only | same |
| `method.entrypoint.protocol: batch` | execute for Python or command methods | same |
| `method.entrypoint.protocol: session` | execute for Python methods only | same |
| `method.settings` | pass to method | display and edit |
| `method.accepts` | validate compatibility | display and edit |
| `method.runtime` | execute process/container method runtime; `runtime.setup` is process-only | same |
| `method.interface` | validate only | create editable workspace, install, launch |
| `resource` config | validate as package/catalog metadata; not compiled into study runs | catalog display |
| `resource.interface` | validate only | create editable workspace, install, launch |
| `study.environmentConfig` | resolve and compile | display and edit |
| `study.methodConfig` | resolve and compile | display and edit |
| `study.objective` | choose metric, direction, aggregation | display and edit |
| `study.budget` | control run stopping | display and edit |
| `study.execution` | control parallelism, timeout, retry | display and edit |
| `study.evidence` | control run evidence retention/output | display and edit |
| `study.reproducibility` | control seed | display and edit |

Important clarification:

`interface` is a Studio launch contract. The PyPI core package validates it so
the same package can later be opened in Studio, but the PyPI CLI does not launch
interfaces because that would be GUI functionality.

`runtime.setup` and `interface.setup` are intentionally different:

- `runtime.setup` prepares code that participates in `optpilot run`; it is
  supported only with `runtime.sandbox: process`
- container runtime dependencies belong in `runtime.container.image` or
  `runtime.container.build`
- `interface.setup` prepares a GUI/helper process for Studio launch; the PyPI
  CLI validates it but does not run it

## Package Validation Command

Add:

```bash
optpilot package validate path/to/package
```

Expected package layout:

```text
my_package/
  environments/
  methods/
  resources/
  studies/
```

Validation behavior:

1. Recursively scan YAML files under the package, but validate only recognized
   OptPilot config YAML files.
2. A YAML file is recognized only when it is a mapping with
   `apiVersion: optpilot.io/v1` and `config` equal to `environment`, `method`,
   `resource`, or `study`.
3. Non-OptPilot YAML files, such as benchmark cases, training data, prompts, or
   simulator metadata, are ignored and counted.
4. Validate environment, method, resource, and study semantics.
5. Compile every study to catch broken relative references and incompatible
   environment/method contracts.
6. Report package-index diagnostics that match Studio catalog behavior:
   duplicate ids, package-qualified ids, missing expected package folders, and
   resource manifest discovery.
7. Optionally run importability/runnable checks when requested.
8. Report a structured summary.

The package validator and Studio catalog browser should share the same discovery
module. This avoids a bad split where `optpilot package validate` accepts a
package but Studio indexes it differently. Suggested core module:

```text
src/optpilot/package_index.py
```

This module should expose:

- package root discovery
- recognized OptPilot config discovery
- resource manifest discovery
- package-qualified id generation
- duplicate id diagnostics
- ignored YAML counts

Studio can add presentation fields, launch state, and workspace actions on top
of the shared package index instead of owning a separate scanner.

Validation levels:

```bash
optpilot package validate path/to/package
optpilot package validate path/to/package --check-imports
optpilot package validate path/to/package --run-smoke
```

- default: schema, semantic, path, compatibility, and package-index checks
- `--check-imports`: also imports referenced Python callables after declared
  process setup when possible
- `--run-smoke`: runs studies explicitly marked as smoke studies, or studies
  selected by path

Suggested JSON output:

```json
{
  "valid": true,
  "package": "/abs/path/to/my_package",
  "counts": {
    "environment": 2,
    "method": 3,
    "resource": 1,
    "study": 4,
    "ignored_yaml": 12
  },
  "entries": [
    {
      "path": "/abs/path/to/my_package/studies/demo.yaml",
      "config": "study",
      "valid": true,
      "errors": []
    }
  ]
}
```

This command is important because PyPI users need a way to know whether their
package will be Studio-compatible before opening Studio.

## Package Author Workflow

The PyPI workflow should teach users how to create a package from scratch, not
only how to validate an existing package.

Recommended authoring path:

1. Create a package folder.

   ```text
   my_package/
     environments/
     methods/
     resources/
     studies/
   ```

2. Add one environment config and its implementation files. Python imports
   should be relative to the config's source folder or use `pythonPath` fields
   that remain valid after OptPilot copies the source into `runs/.../source`.

3. Add one method config and its implementation files.

4. Declare dependencies in `runtime.setup` for process runtimes, or in
   `runtime.container.image/build` for container runtimes.

5. Declare required host values with `runtime.envFromHost`; do not rely on
   accidental inheritance of shell secrets.

6. Add one study config that binds the environment and method.

7. Validate the package.

   ```bash
   optpilot package validate my_package
   ```

8. Run one study from the CLI.

   ```bash
   optpilot run my_package/studies/smoke.yaml
   ```

9. Later, open the same package in Studio from a source checkout.

   ```bash
   uv run optpilot ui --catalog /abs/path/to/my_package --open-browser
   ```

No config should need to change between the PyPI CLI workflow and the Studio
workflow.

## Source Checkout Studio Install

The source checkout install should install both packages:

```bash
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync --all-packages --group examples --group docs
uv run optpilot ui --open-browser
```

The exact `uv` flags may be adjusted during implementation depending on the
final dependency-group names, but the public behavior should remain:

- one repo clone
- one dependency sync
- one command to open Studio

The full source install may require:

- Docker or Podman for workspace runtimes and containerized study runtimes
- Code Server image or build support through the workspace runtime image
- OpenHands agent server only when the assistant tab is enabled

Implementation requirements:

- add `studio/` as a `uv` workspace member
- make Studio depend on the local workspace `optpilot`, not the published PyPI
  package
- update `uv.lock`
- verify `uv sync --all-packages --locked`

## PyPI Core Install

The public PyPI flow should be:

```bash
python -m venv .venv
source .venv/bin/activate
pip install optpilot
optpilot --help
optpilot package validate path/to/my_package
optpilot run path/to/my_package/studies/my_study.yaml
```

PyPI users who use container runtimes still need Docker or Podman installed:

```yaml
runtime:
  sandbox: container
  container:
    image: python:3.12-slim
    executable: docker
    network: disabled
```

The configured image must also be able to import OptPilot inside the container.
For a PyPI user, that usually means the Dockerfile installs OptPilot:

```Dockerfile
FROM python:3.12-slim
RUN pip install optpilot
```

or, for a local package under development:

```Dockerfile
FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
RUN pip install optpilot
```

Container dependencies should be installed through the image/build path, not
through `runtime.setup`, because setup is process-only.

PyPI users who use secret passthrough provide values through their shell:

```bash
export OPENAI_API_KEY=...
optpilot run path/to/study.yaml
```

and declare the requirement in method/environment config:

```yaml
runtime:
  sandbox: process
  envFromHost: [OPENAI_API_KEY]
```

Studio users can provide the same value through Studio settings. The package
does not change between these two modes.

## Implementation Plan

### Phase 1: Add a safe CLI plugin boundary

Files:

- `src/optpilot/cli.py`
- new core tests for CLI command registration

Changes:

1. Remove top-level import of `optpilot.ui.server`.
2. Keep core commands registered directly.
3. Convert core commands to `set_defaults(handler=...)`.
4. Add optional command provider loading from `optpilot.commands` entry points.
5. Require providers to register subcommands and set a handler.
6. Dispatch with `args.handler(args)`.
7. In this phase, keep the current UI code where it is and add a temporary
   local provider if needed so source checkout still has `optpilot ui`.

Acceptance checks:

```bash
uv run optpilot --help
uv run optpilot validate catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml --output-root /tmp/optpilot-core-smoke
uv run optpilot ui --open-browser
```

Tests:

- importing `optpilot.cli` does not import `optpilot.ui`, `optpilot_studio`, or
  `optpilot.agent`
- a fake `optpilot.commands` entry point can register and execute a command
- source checkout exposes `optpilot ui`
- PyPI-like core install does not expose `optpilot ui`

### Phase 2: Add package validation

Files:

- `src/optpilot/package_validation.py`
- `src/optpilot/package_index.py`
- `src/optpilot/cli.py`
- tests for valid and invalid package directories

Changes:

1. Extract shared package discovery into `package_index.py`.
2. Implement `validate_package(path)`.
3. Add `optpilot package validate`.
4. Reuse `validate_authoring_config` and `compile_authoring_config`.
5. Ensure resource configs are included in package validation.
6. Ignore non-OptPilot YAML and report ignored counts.
7. Report Studio-index diagnostics such as duplicate ids and resource manifest
   discovery.

Acceptance checks:

```bash
uv run optpilot package validate catalog/example_package
uv run optpilot package validate tests/fixtures/catalog
```

### Phase 3: Extract Studio package

Files moved:

```text
src/optpilot/ui/                 -> studio/src/optpilot_studio/ui/
src/optpilot/agent.py            -> studio/src/optpilot_studio/agent.py
src/optpilot/assistant_assets/   -> studio/src/optpilot_studio/assistant_assets/
src/optpilot/docs_assets/        -> studio/src/optpilot_studio/docs_assets/
```

New files:

```text
studio/pyproject.toml
studio/src/optpilot_studio/__init__.py
studio/src/optpilot_studio/cli.py
```

Root package changes:

- Exclude Studio packages from the root `optpilot` wheel.
- Keep only `optpilot.schemas` as core package data.
- Move source-only docs/examples dependencies out of public core extras unless
  the matching assets are intentionally shipped in the core distribution.
- Declare `referencing` explicitly, or pin `jsonschema` to a version that
  guarantees the `referencing` dependency used by schema validation.

Studio package changes:

- Depend on the local core package in source checkout.
- Register `ui` through the `optpilot.commands` entry point.
- Expose `optpilot-studio` console script.
- Package Studio static files, workspace runtime Dockerfile, assistant assets,
  and docs assets.
- Replace fragile `Path(__file__)` package-root assumptions with
  `importlib.resources` for Studio assets and core schemas.
- Rework subprocess `PYTHONPATH` construction so study subprocesses can import
  the installed `optpilot` package after the UI code moves to
  `optpilot_studio`.
- Update assistant prompt loading to search Studio assets through package
  resources and source-checkout fallbacks deliberately.

Acceptance checks:

```bash
uv sync --all-packages --group examples --group docs
uv run optpilot ui --open-browser
uv run optpilot-studio --open-browser
```

Additional Studio install smoke:

```bash
uv run optpilot-studio --host 127.0.0.1 --port 8866
```

Then request:

- `/`
- `/static/app.js`
- `/api/catalog`
- `/api/agent/settings`
- `/api/platform/status`

and verify workspace runtime status points to an existing
`workspace_runtime/Dockerfile`.

### Phase 4: Split tests by boundary

Suggested layout:

```text
tests/core/
  test_cli.py
  test_config_schema.py
  test_runtime_process.py
  test_runtime_container.py
  test_package_validation.py
  test_examples_cli.py

tests/studio/
  test_ui_catalog.py
  test_ui_config_forms.py
  test_ui_runs.py
  test_ui_settings.py
  test_ui_workspace_runtime.py
  test_openhands_assistant.py
```

Core tests should pass with only the root `optpilot` package installed.

Studio tests should pass only in the full source checkout.

Preserve current Studio regression coverage:

- catalog scans environments, methods, studies, and resources
- complete config forms render optional fields
- Studio settings manage environment variables and secrets
- launching studies through UI streams run updates
- source inspection opens read-only catalog source
- editable copy creation works
- launch interface creates editable copy, runs setup, and opens preview
- Code Server workspace management still works
- OpenHands assistant settings and dispatch still work

After the package move, these tests should run against installed
`optpilot_studio` imports, not against the old `optpilot.ui` package path.

### Phase 5: Add release checks

Add automated checks that prove the split:

1. Build the core wheel.
2. Build the core sdist.
3. Inspect both artifact contents.
4. Assert both artifacts contain:
   - `optpilot/config.py`
   - `optpilot/runner.py`
   - `optpilot/schemas/...`
5. Assert both artifacts do not contain:
   - `optpilot/ui`
   - `optpilot/agent.py`
   - `assistant_assets`
   - `docs_assets`
   - `workspace_runtime`
6. Install the wheel in a clean venv.
7. Install the sdist in a clean venv.
8. Run in both environments:

```bash
optpilot --help
optpilot package validate path/to/package
optpilot run path/to/package/studies/smoke.yaml
```

9. Verify `optpilot --help` does not expose Studio commands in the PyPI core
   environment.
10. Run a container smoke from the installed wheel using an image/build that can
    import `optpilot`.
11. Run Studio package-content checks from the source workspace.

### Phase 6: Update docs

Update public docs:

- `README.md`
- `docs/index.md`
- `docs/getting-started.md`
- `docs/catalog.md`
- `docs/ui.md`
- `docs/configuration.md`
- `docs/development.md`
- moved Studio `docs_assets` copies, if Studio continues to package docs for
  assistant/search surfaces

Docs should clearly separate:

1. PyPI Core Install
2. Source Checkout Studio Install
3. Package authoring workflow

Remove `uv run optpilot ui --open-browser` from the PyPI getting-started path.
Keep it only in Studio/source-checkout pages.

Clarify:

- `interface` is Studio metadata.
- `resource` configs are validated by core but launched only by Studio.
- `runtime` is executable in the core CLI.
- `runtime.setup` is process-only.
- container runtimes require images/builds that can import `optpilot`.
- `envFromHost` means explicit passthrough from shell env in CLI mode and from
  Studio-managed settings in Studio mode.

## Backward Compatibility Strategy

We do not need long-term backward compatibility with old packaging, but we do
need to avoid breaking the tested Studio workflow during the migration.

Preserve these source-checkout behaviors:

- `uv run optpilot ui --open-browser` works after a full source install.
- Existing Studio URLs and API routes remain stable.
- Existing `.optpilot-ui/settings.json` remains readable.
- Existing catalog package layout remains unchanged.
- Existing run directories remain inspectable.
- Existing environment/method/resource/study configs remain valid.

Acceptable changes:

- PyPI core install no longer exposes `optpilot ui`.
- Studio internals move from `optpilot.ui` to `optpilot_studio.ui`.
- Tests import Studio helpers from `optpilot_studio`, not `optpilot.ui`.
- Public docs describe Studio as source-checkout install.

## GUI Regression Plan

After the split, run the following source-checkout Studio smoke test before
release:

1. Launch Studio.
2. Confirm catalog shows example environments, methods, studies, and resources.
3. Open a study and edit all exposed config sections.
4. Save a copy and validate it.
5. Launch a dependency-free study from the UI.
6. Confirm run status, trials, candidates, events, runtime, and files update
   without changing tabs.
7. Add a Studio environment variable in Settings.
8. Launch an OpenAI-backed method study from the UI when a real key is present.
9. Open read-only source code from the catalog.
10. Create editable copy and install for one environment/method.
11. Launch a resource or component interface and confirm preview opens.
12. Open a Code Server workspace.
13. Send a basic assistant message with OpenHands configured.

These checks protect the GUI functionality already tested during development.

Automated Studio smoke should also cover:

- config-form edit/save in the browser
- UI study launch and live run refresh
- settings save without echoing secret values
- launch-interface job progress
- static asset serving after package move
- docs/assistant asset lookup after package move

## Core Runtime Regression Plan

In a clean PyPI-like environment, run:

1. Validate a package.
2. Run a process-runtime environment with `runtime.setup`.
3. Run a process-runtime method with `envFromHost`.
4. Run a command method.
5. Run a Python method.
6. Run a container-runtime environment using a fake Docker/Podman executable in
   tests.
7. Run a container-runtime method using a fake Docker/Podman executable in
   tests.
8. Run a clean-wheel container smoke whose image/build can import `optpilot`.
9. Validate a resource config with `interface`.
10. Validate and compile every built-in example study from source checkout.

The PyPI core release should not depend on a real Docker daemon in unit tests,
but it should support real Docker/Podman when users configure it.

## Risks And Mitigations

### Risk: Studio imports break during the move

Mitigation:

- Move Studio as one package.
- Keep all route names and UI server APIs unchanged.
- Run Studio tests immediately after import rewrites.

### Risk: `optpilot ui` disappears for source users

Mitigation:

- Add Studio command registration through entry points.
- Keep `optpilot-studio` as a direct fallback command.
- Add a test that full source checkout registers the UI command.

### Risk: Core wheel accidentally ships Studio files

Mitigation:

- Add wheel and sdist content release tests.
- Keep package-data declarations minimal in root `pyproject.toml`.

### Risk: PyPI users cannot know whether their package will work in Studio

Mitigation:

- Add `optpilot package validate`.
- Make Studio package scanning use the same validation/compiler functions.
- Share package discovery between CLI validation and Studio catalog indexing.

### Risk: Container runtime works from source checkout but not from PyPI

Mitigation:

- Document that container images/builds must install `optpilot`.
- Add clean-wheel container smoke tests.
- Avoid relying on `cwd/src` for wheel-installed container execution.

### Risk: Moving Studio breaks file and asset lookup

Mitigation:

- Use `importlib.resources` for packaged static files, workspace runtime assets,
  assistant prompts, and docs assets.
- Add installed-Studio endpoint smoke checks.
- Audit every `Path(__file__)` usage during the move.

### Risk: Config fields become fake schema

Mitigation:

- Keep the config support matrix above as the release checklist.
- Any executable public schema field must have a core test.
- Any Studio-only public schema field must be explicitly labeled Studio-only in
  docs.

## Open Questions

1. Should `optpilot-studio` remain source-only forever, or should it eventually
   be published as a separate PyPI package?
2. Should automatic container support eventually mount the installed core
   package into containers, or should OptPilot always require images/builds to
   install `optpilot` explicitly?
3. Should `optpilot package validate --run-smoke` infer a default smoke study,
   or require packages to mark one explicitly?

## Recommended First Implementation Slice

Start with the least risky boundary work:

1. Add `optpilot package validate`.
2. Remove the top-level UI import from `optpilot.cli`.
3. Add optional command-provider loading.
4. Add tests proving PyPI-style CLI can run without importing Studio.
5. Only then move Studio into `optpilot_studio`.

This gives us a working checkpoint before the larger file move and makes it
easy to confirm that core behavior stays stable.
