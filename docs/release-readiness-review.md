# Official release readiness review

**Review date:** 2026-08-30
**Reviewed revision:** `7b9388179cb3b656053f0ccb0c8ed970c59573dd` (latest `main` at the start of the review)
**Scope:** OptPilot Core, Studio, the Assistant harness, the four tracked Catalog packages, release artifacts, and public/maintainer documentation.

## Executive conclusion

The reviewed checkout passes the local code, package, documentation, smoke,
and release-artifact gates. The tracked packages can be discovered and
published through Studio; both credential-free example Runs complete through
the GUI; package runtime inheritance is consistent across validation, Catalog
display, preflight, and launch; the Assistant's executable tools match its
advertised tools; and the package-authoring workflow is documented from
creation through update and republishing.

The review did uncover release-blocking correctness and security issues. They
were fixed rather than documented away: cross-origin Studio mutation,
misleading host-network claims, incomplete or mutable approval targets,
ambiguous Resource-action retries, Workspace and control-state escapes,
credential-file exposure, authenticated redirects, package-tree symlinks,
silent package-validation omissions, package-container drift, prompt-injection
boundaries, and interface environment-default handling. Focused regressions
now cover each boundary.

Release is a **conditional go**. Provider-backed Assistant, DEVS-generation,
COOPA-solving, and image-specific Docker isolation journeys still require
authorized release-environment checks. This review deliberately did not use or
transmit a saved provider credential without explicit authorization.

## How the review was performed

The review combined:

- verification that the checkout and upstream `main` named the same commit;
- isolated Studio catalogs containing exactly the four tracked working-tree
  packages, excluding ignored or machine-local packages;
- GUI inspection of Catalog browsing, package publishing, Run setup, Run
  progress/results, interface prerequisites, Settings, approvals, and the
  no-provider Assistant state;
- source and test audits across Core compilation, Realm retention, Studio,
  Assistant/OpenHands integration, package lifecycle, documentation, and
  release artifacts;
- adversarial tests for symlinks, stale approvals, moved Workspace roots,
  control paths, secret files, mutable Resource actions, and unsupported
  runtime claims;
- strict documentation, package, source, smoke, distribution, and full test
  verification.

## GUI and package findings

Studio discovered exactly the four tracked packages, with 13 Environments, 10
Methods, and 2 Resources in total.

| Package | GUI result | Notes |
| --- | --- | --- |
| `optpilot_tutorial` | Passed | Published through Studio. A six-trial Run completed 6/6 with best score `44.2525`. |
| `production_agv_scheduling` | Passed | Published through Studio. A one-trial Run completed with `mean_total_score = 4.3548`. The larger package (about 21 MB) remained usable in the Catalog and Run flow. |
| `devs_gallery` | Passed with provider gate | Published, browsed, and launched through the real Studio UI. The interface's object-form host-environment defaults originally rendered as `[object Object]` and were treated as missing; Studio now renders names/defaults correctly. Actual generation still requires an authorized OpenRouter credential. |
| `or_solving` | Passed with provider gate | Published and browsed. COOPA no longer incorrectly requires `COOPA_HOME` when its bundled implementation is used. Actual solving still requires an authorized OpenRouter credential. |

The general Studio information architecture is clear: Catalog cards, detail
panels, launch setup, progress, results, and Settings are visually coherent.
The principal minor usability issue is that long identifiers can wrap densely
in the Catalog sidebar. It does not block use.

The initial Settings implementation was not release-ready at narrow widths:
the mobile navigation could cover the dialog, unrelated controls were packed
into one dense tab, inactive capability previews looked editable, refused
saves erased typed values, and there was no dirty-close or successful-save
feedback. Settings now uses three focused areas—**Assistant**,
**Permissions**, and **Local values**—with a full-height mobile sheet,
keyboard-complete tabs, staged and reversible value removal, inline
validation, explicit save progress and success, and unsaved-change
protection. Live checks covered 375×854 and 1440×900 viewports, keyboard tab
movement, rejection, discard, and success paths.

The no-provider Assistant state is honest and actionable: Studio explains that
the Assistant is enabled but no model/provider is configured, while manual
Catalog and Run workflows remain available.

## Findings and improvements by release question

### 1. Packages and their interfaces

Resolved:

- Interface `envFromHost` declarations now preserve public defaults and
  descriptions while never retaining resolved host or secret values.
- Studio understands both string and object declarations and shows which
  values are required versus defaulted.
- COOPA's bundled path no longer creates a false `COOPA_HOME` prerequisite.
- Package-level pinned container images are inherited consistently by
  Environments and Methods. Catalog list/detail views show the effective
  sandbox, image, platform, network policy, and whether the image came from
  the package or component.
- Component detail validation now uses package settings, matching package and
  Study preflight results.
- Resource action cards and confirmation dialogs disclose the main command,
  every setup operation, network mode, timeout, environment names, and secret
  names. Values that may be secret are not displayed.
- The GUI obtains a fresh server-generated Resource review immediately before
  confirmation. Execution requires its digest, and the digest covers the raw
  action declaration and complete executable package tree.
- Interface containers receive only their launch-owned runtime, output,
  control, action, log, cache, home, and workspace-data directories. The
  Workspace runtime authority parent remains unmounted. This fixes a live
  launch failure without restoring access to sibling authority records.
- A direct host executor now fails closed for `network: disabled`; it no longer
  claims isolation it cannot enforce.

### 2. Assistant prompt, tools, and harness

Resolved:

- Advertised tools and executable tools are checked for exact drift.
- Native OpenHands capabilities are limited to task tracking. File and shell
  access stays behind Studio's Workspace, credential, and approval policies.
- Every shell command and every smoke execution now requires explicit
  approval; the removed “safe command” heuristic was not a security boundary.
- Registration, launch, stop, interface, Resource-action, and smoke approvals
  remain explicit and independently configurable as approval-required or
  disabled where appropriate.
- Resource-action approval binds the complete executable Resource tree,
  declaration, setup, grants, Workspace root, and output target. Changes after
  approval fail and require a fresh review.
- Resource request ids bind the complete normalized execution request. A lost
  HTTP response is recovered by polling and retrying that same id, preventing
  an ambiguous timeout from duplicating an external effect. The concurrent
  Resource-run cap is enforced atomically.
- Approval-gated Workspace operations bind the canonical Workspace root, so
  reusing an id for a different folder cannot redirect an approved action.
- Resource actions can write only to an attached editable Workspace. Symlinked
  output paths and Workspace escapes are rejected before execution.
- `.optpilot-ui`, relocated authority paths, common cloud/CLI credential
  directories, keyrings, environment files, private keys, certificates, and
  recursive symlink aliases are denied to Assistant file tools. Workspace
  containers mask Studio control state and no longer mount runtime authority
  records.
- Runtime content directories are opened component-by-component without
  following symlinks. Existing installations with a legacy linked home,
  cache, or launch-output path now fail closed instead of crossing the
  Workspace boundary during an upgrade.
- API keys sent in request bodies require HTTPS or loopback transport, and
  authenticated requests reject redirects rather than forwarding credentials
  to a different destination.
- The system prompt treats repository/package text and all tool or command
  output as untrusted data. Embedded instructions cannot override approvals,
  request secrets, or broaden the Workspace boundary.
- Every HTTP mutation requires the process-local Studio token, a trusted Host,
  a same-origin (or absent non-browser) Origin, and JSON content type. This is a
  local anti-CSRF boundary, not authentication for remote deployment.
- Skill, MCP, and custom-tool records are described as inactive previews and
  intentionally omitted from the release form. A Settings save preserves any
  legacy records exactly instead of lossy browser round-tripping. Raw MCP
  authentication material is not returned to the browser or model.
- Settings validates and canonicalizes the OpenHands server URL before
  persistence, preserves explicit direct-model-chat mode across round trips,
  and treats malformed status probes as unreachable rather than making the
  Settings API unavailable.
- Assistant permissions have a dedicated, grouped screen and include the
  human-readable **Opening interfaces** label.
- Prompt, packaged prompt, tool descriptions, implementation notes, and
  permission UI now state the same approval behavior.

### 3. Code cleanliness and stale behavior

Resolved:

- Malformed expected configs can no longer disappear as ignored YAML, and an
  empty package is invalid.
- Package settings reject unknown fields. Runtime declarations reject shapes
  the launcher cannot consume.
- Package validation rejects a symlinked package root or any symlink anywhere
  in the executable package tree before discovery. Study references to
  Environment and Method sources are containment-checked before they are read;
  Studio also refuses to surface linked YAML as Catalog source.
- Method `runtime.envFromHost` is string-only, matching its launcher. Rich
  defaults remain supported only at the interface/Resource boundaries that
  implement them.
- Public interface defaults participate in equality and retained-digest
  behavior; names-only historical records normalize to the same canonical
  form.
- Resource execution uses a per-run writable snapshot for both configured and
  Realm-published sources, eliminating mutable-source execution after review.
- Dead branches/imports and leaked file descriptors identified during the
  audit were removed or closed.
- Browser JavaScript contains no literal NUL bytes and passes syntax checks.
- The Settings form no longer carries dead JSON editors for capabilities the
  release cannot activate, and it prevents closing while a save is in flight
  so **Discard changes** cannot misrepresent an already-committing request.
- Test subprocess pipe descriptors identified by strict warning checks are
  closed deterministically.
- Release-artifact checks now cover license placement and Core/Studio package
  separation in both wheels and source distributions.

No formatter, linter, or type-checker was added; the repository intentionally
does not define one, and introducing a new policy was outside this review.

### 4. Public documentation

Resolved:

- Added an executable-capabilities page that distinguishes implemented,
  intentionally unsupported, and preview-only behavior.
- Removed the unsupported promise of a stable public Python SDK.
- Reconciled CLI, Studio, runtime, retention, security, and package-layout
  descriptions with the current implementation.
- Corrected Catalog inventory, UI paths, COOPA workflow/counts, environment
  defaults, and Resource-action behavior.
- Documented the required OpenHands client-tool import module and the
  task-tracker-only native-tool boundary.
- Documented that import checks execute trusted package top-level code without
  a sandbox; default validation commands remain static unless that opt-in check
  is explicitly requested.
- Corrected compiler-selected container backend language, the exact pinned
  OpenHands `1.40.1` dependency claims for all four packages, and the current
  **Publish**, **Check files**, **Test**, and **Publish checked version** UI
  terminology.
- Documented the three Settings areas and disclosed the exact project-scoped,
  plaintext settings path, including the risk that a synchronized project
  folder can synchronize that file.
- Kept public docs and the Studio-packaged documentation mirror synchronized.

### 5. Package maintenance and extension

Resolved:

- Rewrote the first-package tutorial as a complete external-root workflow:
  create, validate, setup-check, smoke, run, register, update, and republish.
- Added a maintainer workflow for adding and updating bundled packages.
- Rebuilt the `connect-github-integration` skill around current package
  boundaries, schema rules, source ownership, runtime selection, and runnable
  validation.
- Package-level runtime inheritance reduces repeated component declarations
  while retaining component-local override precedence.
- Package validation now reports ignored domain YAML separately from malformed
  OptPilot configs, making future package changes easier to diagnose.
- The service launcher no longer depends on a contributor's personal editor
  configuration.

## Verification results

All automated results below were obtained from the reviewed working tree:

- complete supported suite: **3,269 tests passed** in **568.345 seconds**;
- focused Settings UI, API, key-transport, accessibility, and documentation
  regression set: **73 tests passed**;
- integrated security, Resource, prompt, and package regression set: **63
  tests passed**;
- Studio server security/Resource/Assistant set: **44 tests passed**, plus
  **93 related routing, storage, and interface-output tests passed**;
- post-fix Studio runtime/interface regression set: **308 tests passed**;
- retained-worker tests under `ResourceWarning`-as-error: **36 tests passed**;
- strict MkDocs build: passed;
- repository smoke test: passed;
- source, setup-file, and trusted import validation for all four tracked
  packages: passed;
- Core and Studio wheel/sdist builds, release-artifact hygiene, and Twine
  metadata checks: passed;
- JavaScript syntax, service-launcher shell syntax, schema parsing, mirror
  equality, NUL scan, and `git diff --check`: passed.

GUI evidence was also collected through the real Studio interface: exactly 13
Environments, 10 Methods, and 2 Resources were visible across the four tracked
packages. Tutorial Run `run-1451092314e0409d27ec9ed3289ba5e1` completed 6/6
with best score `44.2525`; production AGV Run
`run-cc463e231246d111173c8f27a0848f20` completed 1/1 with mean total score
`4.3548`. DEVS Generator launch `launch-60dc3cc52f72` reached `ready` and its
live interface opened through Studio. Provider-gated package forms, action
approvals, launch inputs, the redesigned Settings workflows at mobile and
desktop widths, and the no-provider Assistant state were visually inspected.
No provider-backed generation request was submitted.

## Remaining release gates and known limitations

These are explicit limitations, not silent failures:

1. Run one provider-backed Assistant journey in the release environment:
   list a package, inspect details, create/attach an editable Workspace, edit a
   file, approve a shell command, validate/smoke a package, and approve an
   update or registration.
2. The DEVS Generator interface itself now launches successfully. Run one real
   DEVS generation and one real COOPA solve with a test credential whose use
   has been authorized. Confirm success, cancellation, failure, and log
   redaction in the GUI.
3. Confirm the Workspace container's `.optpilot-ui` tmpfs mask with the exact
   Docker image/platform intended for release. Construction is covered by
   tests, but an image-specific live check remains valuable.
4. `network: disabled` Resource actions remain unavailable through the local
   host-process executor until an isolated executor can enforce that promise.
5. Skill, MCP, and custom-tool records remain inactive previews and are not
   editable in Settings. Do not market them as connected capabilities.
6. Settings are stored as plaintext in the project's
   `.optpilot-ui/settings.json`. The UI now shows the exact path and warns
   about synchronized folders, but storage has not yet moved to an OS-local
   secret store. Corrupt-file recovery, atomic replacement, and concurrent-edit
   revision checks also remain follow-up hardening work.
7. `optpilot.package.yaml` has strict hand-written validation but no published
   JSON Schema. The capability matrix is compiler-backed by tests/review but
   does not yet have an automated table-drift generator.

## Release checklist

Before creating the tag:

- [x] Run the complete supported test suite.
- [x] Run `./scripts/smoke_test.sh`.
- [x] Run `mkdocs build --strict`.
- [x] Validate all four tracked packages with source, setup-file, and trusted
  import checks.
- [x] Build both distributions and run
  `scripts/check_release_artifacts.py`.
- [x] Launch the DEVS Generator interface through Studio and confirm it reaches
  `ready`.
- [ ] Run the provider-backed Assistant GUI journey with an authorized test
  credential.
- [ ] Run real DEVS generation and COOPA solving, including success,
  cancellation, failure, and redaction checks, with an authorized credential.
- [ ] Verify the `.optpilot-ui` tmpfs mask using the exact release Docker image
  and platform.
- [ ] Record the release environment, image digests, model id, and gate results
  without recording secrets; review the limitations above against the release
  notes.

If any gate changes package or runtime behavior, repeat package validation,
Studio MVP tests, strict docs, artifact checks, and the complete suite before
tagging.
