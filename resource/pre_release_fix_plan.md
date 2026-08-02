# OptPilot Pre-Release Fix Plan

This document records the blockers found during the July 2026 pre-release
review. Treat it as the implementation checklist before tagging or publishing a
public release.

## Release Goal

Prepare a release that is safe to self-host, has a clear Core CLI/SDK vs Studio
boundary, and ships a coherent example package.

## P0 Blockers

### 1. Constrain Studio static file serving

Problem: Studio serves `/static/*` by joining the request suffix to the static
directory and serving the file if it exists. The path must be resolved and
checked against the static root so `..` or symlink escapes cannot expose local
files such as `.optpilot-ui/settings.json`.

Implementation:

- Resolve the requested static path.
- Require it to remain under `static_dir.resolve()`.
- Return `404` or `403` for escapes.
- Add regression tests for traversal attempts such as `/static/../server.py`
  and attempts to reach `.optpilot-ui/settings.json`.

### 2. Enforce Assistant permission settings

Problem: Assistant permissions are displayed and persisted, but tool execution
does not consistently consult them. Users can set actions to `disabled` or
`approval_required` and still have some tool families run through hardcoded
behavior.

Implementation:

- Add one central permission check before every mutating assistant tool family:
  file writes, shell commands, package registration/apply, package smoke runs,
  study launch, and job stop.
- Reject actions configured as `disabled`.
- Request approval for actions configured as `approval_required`.
- Keep read-only tools approval-free.
- Add tests for disabled and approval-required paths.

## P1 Blockers

### 3. Strip private headers from workspace preview proxy requests

Problem: The preview proxy authenticates with an OptPilot preview cookie and
then forwards request headers to workspace services. Workspace apps should not
receive Studio-private cookies or authorization headers.

Implementation:

- Strip `Cookie`, `Authorization`, and OptPilot-private preview headers before
  forwarding to upstream workspace apps.
- Keep normal browser headers needed by development servers.
- Add a proxy regression test proving the upstream app does not receive the
  preview token.

### 4. Restrict OpenHands native tools

Problem: OpenHands conversations use `NeverConfirm`. If mutable native tools are
enabled through settings, they can bypass Studio workspace and approval gates.

Implementation:

- Maintain a server-side allowlist for native OpenHands tools.
- Allow only non-mutating tools unless a tool is bridged through Studio's
  workspace runtime and approval system.
- Reject or sanitize stale/manual settings that request mutable native tools.
- Add a test that tools such as `terminal` and `file_editor` cannot be enabled
  as raw OpenHands native tools.

### 5. Make package smoke setup-aware

Problem: `optpilot package smoke` validates imports before component setup runs.
Packages whose setup creates or installs import dependencies can fail smoke
before the real run path has a chance to prepare them.

Implementation:

- Decide whether smoke should run setup before import validation or defer import
  checks until after setup materialization.
- Keep `package validate --check-imports` as a static check when requested, but
  make `package smoke` reflect the actual execution path.
- Add a temp package regression test where `runtime.setup` provides an import
  needed by the evaluator or method.

### 6. Run resource interface setup from the resource root

Problem: Resources can use `.optpilot/resource.yaml`, but setup currently runs
from the manifest parent. For `.optpilot/resource.yaml`, that is `.optpilot/`
instead of the resource root.

Implementation:

- Use `entry.source_root` when available for resource setup execution.
- Fall back to `entry.path.parent` only when no source root is known.
- Add a regression test for `.optpilot/resource.yaml` with setup writing a
  marker into the resource root.

### 7. Prove the Core CLI/SDK artifact boundary in CI

Problem: CI installs editable source packages and tests the full checkout, but
does not prove the PyPI artifact boundary.

Implementation:

- Build core wheel and sdist in CI.
- Install the built core package into a clean environment without Studio.
- Verify core commands such as `optpilot --help`, `optpilot validate`, and
  `optpilot package validate`.
- Inspect artifacts to ensure Studio, catalog examples, local resources, and
  generated state are excluded from the core PyPI package.
- Keep a separate source-checkout/Studio CI job that installs `-e . -e ./studio`
  and runs Studio tests.

### 8. Fix the sdist test surface

Problem: The core sdist excludes Studio but includes tests that import
`optpilot_studio` at module import time. That creates a partial, broken test
surface for people inspecting or testing the source distribution.

Implementation:

- Either prune `tests/` from the core sdist, or split tests into core-only and
  Studio test modules with optional dependencies.
- Prefer adding CI coverage for the chosen behavior.

## P2 Cleanup

### 9. Clean up command setup file-token validation

Problem: setup validation treats any command token containing `/` or `\` as a
file path. Inline code such as `python -c "..."` can be misclassified as a
missing file.

Implementation:

- Make command scanning command-aware.
- Skip the argument following `python -c`, `python3 -c`, or equivalent inline
  execution forms.
- Continue validating explicit script/path arguments.

### 10. Clarify Studio Catalog wording

Problem: public UI docs say Catalog browses study files, but Studio presents
studies through the Studies page. Packages can contain studies, but studies are
not reusable Catalog component entries.

Implementation:

- Update `docs/ui.md` and mirrored docs assets.
- Say Catalog browses environments, methods, and resources.
- Say Studies presents saved study plans and package study YAMLs.

### 11. Add a version consistency release check

Problem: version `0.1.0` is duplicated in core metadata, Studio metadata, the
Studio dependency pin, and both package `__init__.py` files.

Implementation:

- Add a release check that verifies all version declarations are synchronized.
- Run it in CI or as part of a documented release checklist.

### 12. Keep the release worktree clean

Problem: the review checkout contains many modified files and one untracked
design doc. That is expected during active development but not acceptable for a
tagged release.

Implementation:

- Review all modified files.
- Commit intentional changes.
- Remove or intentionally track design docs.
- Build release artifacts from git-tracked files, not from a raw workspace
  archive.

## Example Package Requirement

Keep `catalog/example_package/resources/devs-gen-interface/` in the
source-checkout example package as the launchable resource example. It should
stay focused on launching the DEVS GUI and generating discrete-event simulator
projects. Benchmark suites, experiment logs, papers, and unrelated local copies
belong under ignored `resource/` scratch space, not inside the shipped example
package.

Before release, validate that this resource:

- appears in Studio Catalog under Resources;
- declares required host environment variables through `interface.envFromHost`;
- creates an editable copy before launch;
- launches through Studio Preview without modifying catalog source;
- does not include benchmark data, generated run output, local `.env` files, or
  dependency folders.
