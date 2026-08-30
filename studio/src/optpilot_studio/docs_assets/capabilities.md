---
title: Executable Capabilities
description: The authoritative boundary between schema-valid OptPilot configs and studies the retained runner can execute.
---

# Executable Capabilities

OptPilot's public schemas describe an authoring surface that is intentionally
broader than the retained runner. A config can therefore be schema-valid but
not executable yet. The commands below answer different questions:

```bash
# Is this public YAML well formed?
optpilot validate path/to/config.yaml

# Are the package paths and setup inputs present?
optpilot package validate path/to/package \
  --check-source --check-setup-files

# Can the selected study compile and complete on this machine?
optpilot package smoke path/to/package \
  --study studies/smoke.yaml
```

After reviewing and trusting authored code, `--check-imports` can verify the
declared Python callables. It executes module top-level code in a normal host
child process and is not a sandbox; see [Packages and Catalogs](catalog.md).

`optpilot run` and `optpilot package smoke` compile through the same retained
execution boundary. Unsupported combinations fail closed during compilation;
OptPilot does not silently use an older or less reproducible runner.

## Retained runner matrix

This table is the release's authoritative executable-capability summary.

| Area | Executable now | Schema-valid but not executable now |
| --- | --- | --- |
| Candidate format | `parameters`; bounded `files` | `opaque` |
| Environment evaluator | Python callable using the configured Environment adapter | command evaluator; custom/legacy adapter |
| Method implementation | Python batch; command batch whose first token is the logical interpreter `python` or `python3` | session protocols; arbitrary command heads |
| Runtime | local `process`; digest-pinned local `container` when its declaration, platform, local engine, and approval agree | other runtime kinds; a container Environment used as a Method capability |
| Dependencies | retained, hash-locked pure-Python wheel setup prepared from declared inputs | arbitrary setup steps, builds, sdists/native build chains, container builds |
| Host values | a Method may explicitly list `runtime.envFromHost`; direct CLI uses exported values and Studio binds saved revisions transiently | Environment host inheritance; ambient inheritance |
| Package inputs | package-owned `methodContext.references`; `trialWorkspace` seed layers; sealed file-Candidate layers | paths outside the captured package; legacy path-backed attempt/output declarations |

For a container Environment, the compiler selects container execution from the
Environment runtime; `study.execution` has no `backend` field. Container images
must use an exact OCI digest and declare a platform. The local engine must have
or be able to obtain that exact image, and the image must be approved before
launch. A container declaration is an execution boundary, not permission to
mount the package tree or Realm broadly.

Locked Python setup is deliberately narrow: requirements must resolve to
declared, hash-verified pure-Python wheel artifacts that OptPilot can retain as
an exact layer. It is not a general install script. Run `package setup-check`
before a smoke test when a package declares setup:

```bash
optpilot package setup-check path/to/package
optpilot package setup-check path/to/package --run-setup
```

## Schema-only fields

Schema-only fields remain useful for forward-compatible authoring and tooling,
but validation alone is not an execution promise. In particular, an `opaque`
Candidate contract, session Method, command evaluator, custom adapter, or build
declaration must not be presented as runnable until a release adds a retained
compiler and tests for it.

When a package deliberately uses such a field, say so in its README and provide
a runnable smoke study that stays inside the current matrix if possible.

## Studio capabilities are separate

Studio builds on the same retained runner for Runs, then adds local surfaces
with their own policies:

- package browsing, validation, smoke tests, and registration
- editable Workspaces and Studio-guarded file/terminal tools
- Run inspection, comparisons, Shortlists, and re-evaluation
- package Resources and Environment interfaces
- an optional Assistant bridge

A Resource action or interface is not an Environment evaluator and does not
expand the retained runner matrix. Its own declaration, source boundary,
network policy, secret grants, approval, and launch checks still apply. See
[Packages and Catalogs](catalog.md), [OptPilot Studio](ui.md), and
[Local Operations and Security](operations.md) before exposing one.

## Python package stability

The PyPI distribution provides the `optpilot` command and the Python modules it
uses. This release does **not** promise a separately versioned, stable public
Python SDK. Integrations should prefer public YAML schemas and CLI commands;
code that imports internal modules may need updates between releases.

When this page and another guide disagree about executability, this page is the
source of truth. Maintainers should update the matrix and its compiler tests in
the same change that adds or removes an executable capability.
