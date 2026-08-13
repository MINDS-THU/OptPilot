# Container method runtimes — implementation plan

**Status: reviewed proposal, 2026-08-13. Not implemented.**

> **Superseded in part, 2026-08-14.** Three later decisions change the
> conclusions here: execution is **container-only with a default base
> image** (there is no local-process mode), a package is **a folder**
> with immutable snapshots for versions (there is no separate stored
> package kind), and images are hosted on **GitHub's container registry**
> alongside the package's source. See
> [`how-optpilot-runs-code.md`](how-optpilot-runs-code.md).

Two owner decisions on 2026-08-13 simplify this plan: there are **no existing
users or records to preserve**, and each package will live in **its own GitHub
repository with its image in that repository's container registry**. See B3 and
the open questions.

Implements the architecture in [`general-runtime-provisioning.md`](general-runtime-provisioning.md)
under the owner's three decisions: `image:` only (sha256-pinned), per-package
Docker acceptable, `or_solving` first.

This plan has been through four adversarial review lenses (backward
compatibility, evidence integrity, security, feasibility) which produced 23
findings, 8 of them blockers. Every blocker is folded in below. The
**Rejected approaches** section records what was tried and why it fails —
those are the traps, and they are not obvious.

## Scope: method runtimes only

Container runtimes are enabled for **command batch methods**. Environments,
evaluators and execution backends stay process-only, and
`container_runtime_unsupported` keeps firing for them.

This is not timidity, it is where the seam already is:

- The record layer is **already** container-ready.
  `PreparedMethodRuntimeManifest` accepts `runtime_kind="container"` and
  *requires* a `sha256:<64hex>` `oci_image_digest`;
  `study_realm_compiler._method_runtime_kind` already accepts `"container"`;
  `RunDefinitionManifest.__post_init__` already cross-checks the method
  contract kind against it. (All verified present.)
- The method-side execution touchpoint is **one bounded subprocess**:
  `_RetainedCommandBatchMethod.propose` in `retained_batch_worker.py`.
- The environment half is a different animal: a new container attempt
  provider, a new supervisor with a non-flock liveness model, and widening
  `runtime_binding.py` plus `ExecutionProviderFacts`.

`or_solving` needs none of the environment half — `or_problem`'s evaluator is
stdlib-only and must keep an empty `runtime_requirements`.

## Backward compatibility is structural, not argued

Three facts, each verified against the tree:

1. `runtime.schema.json#/definitions/runtime` is referenced from exactly two
   places — `environment.schema.json:34` and `method.schema.json:74`.
   Interfaces resolve `common.schema.json#/definitions/interfaceRuntime`, a
   different definition. Changing the study runtime grammar provably cannot
   affect interfaces.
2. **No existing package declares a top-level `runtime.sandbox: container`.**
   The seven `sandbox: container` hits in `production_agv_scheduling` are all
   *interface* runtimes.
3. Therefore every existing study keeps a byte-identical prepared-runtime
   `to_dict()` and an unchanged run-definition digest.

## Blockers found in review, and the resulting design

### B1. The trust gates sit on a path that is normally skipped

`prepare_local_package` and `prepare_selected_package` both call
`_reuse_committed_definition(...)` and **return early** when a definition can
be replayed. Since container definitions are portable, reuse is the *steady
state*, not the exception. Gates placed in `_prepare_retained_source` would
therefore never run on the common path — a complete bypass of the trust,
engine and image checks.

**Design:** the three pre-Run gates run on a path both branches traverse,
driven from the **retained** `method_revision.method_contract`, not from
freshly compiled config. A replayed definition must be gated exactly like a
fresh one.

### B2. Study approvals would silently become gateway approvals

`RealmProviderTrustPolicyService.list_active()` takes **no** contract
parameter and filters by nothing (verified). `local_runtime.py` feeds every
approved head into `ContainerGatewayImageTrust`. Adding a second contract
value without changing that call site means an image approved for *study
execution* becomes trusted as a *gateway host*.

**Design:** `list_active`/`read_active` take a **required, defaulted** contract
filter applied **in the ledger query**, and `local_runtime.py` plus all three
`cli.py` trust call sites pass `PROVIDER_TRUST_GATEWAY_CONTRACT` explicitly, in
the same commit as the migration. Regression test: a study approval for digest
X leaves `environment-preview trust list` and gateway trust unchanged.

### B3. ~~The migration would brick existing Realms~~ — dissolved by owner decision

**Owner decision 2026-08-13: there are no existing users and no records worth
preserving. The goal is a clean, robust, general codebase.**

That removes this blocker rather than mitigating it, and it changes the right
answer, not just the risk. The reviewed hazard was real — `_migrate` runs
inside `BEGIN IMMEDIATE`, where `PRAGMA foreign_keys` is a no-op, so an
in-place rebuild of the trust tables fires triggers and trips foreign keys.
But the careful drop/copy/rename/re-trigger dance existed *only* to preserve
rows nobody needs.

**Design:** do not write a data-preserving migration. Define the trust tables
with `contract` as a first-class column from the start — in the existing
trust migration if it can simply be edited, otherwise in a replacement that
creates the correct shape outright. The contract-qualified uniqueness
constraints and the contract-inclusive self-foreign-key become the definition
rather than a patch over an earlier one.

Cost, stated plainly: **anyone holding a local Realm must delete and recreate
it.** That is acceptable today and will not be later, so this is the moment to
get the shape right. Add a schema-version guard that fails with a clear
"recreate your Realm" message rather than a confusing integrity error.

### B4. Secrets on the command line

Passing resolved `envFromHost` as `--env NAME=VALUE` puts a live
`OPENROUTER_API_KEY` in the engine client's argv for the whole 900 s exchange,
readable by any local user via `/proc/<pid>/cmdline` or `ps`. The repo already
rejects this posture: `server.py::_docker_exec_env_file` exists precisely to
"pass exec environment without exposing values in argv".

**Design:** resolved secrets go through a per-exchange `0600` `--env-file`,
reusing the `_docker_exec_env_file` pattern including its NUL/newline
rejection. Non-secret `runtime.env` may stay on argv.

### B5. Evidence would stop being the executed code

COOPA is vendored as of `42a804b`, so the executed COOPA source is retained
package content under the method source role. Baking `coopa_home/` into the
image would mean the code that *runs* is no longer the code that is *retained*
— silently breaking the audit trail the package exists to provide.

**Design:** do **not** bake `coopa_home/` into the image and do **not** set
`COOPA_HOME`. Bind-mount the retained method projection and let the shim's
existing sibling-folder fallback resolve it. The image carries only the
third-party wheel closure plus `glpsol`.

### B6. The golden digest canary would invalidate itself

Step 1 pinned hard-coded digests using `or_solving`'s study; a later step
rewrites that study's method runtime. The tripwire would have to be edited by
the change it exists to detect.

**Design:** the canary uses only packages this slice never touches
(`llm_policy_search` smoke plus `devs_gallery`), and holds absolutely.
`or_solving` gets a separate before/after assertion whose single change is
deliberate and reviewed.

### B7. The digest check compares the wrong hash

The gate asserted `image inspect` → `Id` equals the pinned digest. `Id` is the
image **config** digest; a `repo@sha256:…` reference names the **manifest**
digest, which surfaces in `RepoDigests`. As written the gate fails closed for
every legitimately pinned image.

**Design:** branch on reference form — for `repo@sha256:x` assert `x` appears
in `RepoDigests`; for a bare `sha256:x` assert `Id == sha256:x`. Document
which digest kind `oci_image_digest` records and refuse to mix them.

### B8. Wrong-architecture images could run and be retained as portable

`platform` was optional, `--platform` was absent from the argv, and the
container branch drops the host-fingerprint equality that process runtimes
enforce.

**Design:** `platform` is **required** in `studyContainerRuntime`, `--platform
<declared>` is passed to the engine, and the pre-Run probe asserts the resolved
image's `Os`/`Architecture` match the declaration.

## Security posture

Every other container invocation in this repo sets resource limits
(`interface_output_execution.py` and
`local_container_web_provider._base_run_arguments` both set `--cpus`,
`--memory`, `--pids-limit`). A container running **LLM-generated code** must
not be the weakest one. Required in the argv:

- `--pids-limit`, `--memory`, `--cpus`, an explicit `--name`, and `rm -f` on
  the timeout/`finally` paths so a timed-out exchange cannot leak a container.
- `PYTHONPATH` emitted **last** — both engines are last-wins on repeated
  `--env`, so a declared `runtime.env` could otherwise override it.
  `PYTHONPATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH` and the existing
  `METHOD_WORKER_RESERVED_ENVIRONMENT_NAMES` are rejected at authoring time.
- The engine binary is resolved from operator configuration or `shutil.which`
  over a fixed `{docker, podman}` allowlist — **never** from the authored,
  retained `containerExecutable`, which today only rejects absolute paths and
  would otherwise let a package name the binary the host executes.
- `runtime_context['method_workspace']` is rewritten to the in-container path.
  It is currently a **host** path injected into the exchange request, and
  `coopa_solver.py` reads exactly that value.
- `network: enabled` uses a dedicated per-exchange user-defined bridge, not the
  default bridge, which reaches host-bound services, the LAN, and link-local
  `169.254.169.254`.

## Rejected approaches

| Tried | Why it fails |
| --- | --- |
| Optional `contract` filter defaulting to unfiltered | Silently promotes study approvals to gateway trust (B2) |
| Gates in `_prepare_retained_source` | Bypassed by definition reuse, which is the steady state (B1) |
| Bake COOPA into the image | Executed code stops being retained evidence (B5) |
| `Id` from `image inspect` as the pin check | Wrong digest kind; fails closed on every real pin (B7) |
| `--env NAME=VALUE` for secrets | Live API key in argv for 900 s (B4) |
| Authored `containerExecutable` | Package chooses the host binary to execute |
| `engine` **and** `executable` keys | Two retained keys for one concept; only one is guarded |

## Sequencing

1. Golden digest canary on untouched packages + record the baseline failure set.
2. Schema: `studyContainerRuntime` (image required + digest-pinned, platform
   required, **no** `build` property so it is unrepresentable rather than
   merely rejected).
3. Consolidate the image regex to one home; enforce pinning at authoring time.
4. Trust contract value + **migration 0036 with explicit sequencing**, and
   thread the gateway contract through every existing read site in the same
   commit.
5. Compiler: two-kind resolution; emit the container prepared method runtime.
6. Pre-Run gates on the **reuse-inclusive** path, driven from retained contract.
7. Execution: exchange inside the pinned image, with the full posture above.
8. Validation truth-telling: `dependency_container_provisioned`.
9. Migrate `or_solving` — image = wheels + `glpsol`, COOPA stays bind-mounted.
10. Docs, CI, follow-ups.

Steps 1–3 are independently useful and carry no execution risk. The first
irreversible step is 4 (the migration); it deserves its own review.

## Open questions for the owner

- ~~Who builds and hosts the image?~~ **Decided 2026-08-13:** each package
  gets its own GitHub repository, and its image is published to the container
  registry attached to that repository (`ghcr.io/<org>/<package>@sha256:...`).
  This aligns with the packages-as-repos direction already in
  `initial-release-plan.md` §6 (D1-D4), and it makes provenance obvious: the
  image and the package source share one repo, one owner, one history. The
  build should run in that repo's CI so the digest is produced by a recorded
  workflow rather than a laptop. Still open: who may *approve* a digest for
  execution, which is a policy question rather than a hosting one.
- **Image size.** ortools + pymoo + numpy + pandas + litellm + a Debian base
  with GLPK is realistically 1.5–2.5 GB, and there is no implicit pull. First
  use will be slow and must be an explicit, documented operator step.
- **Reproducibility of the build.** A sha256 pin makes replay exact, but the
  contents come from an unpinned pip resolve unless `requirements-pruned.txt`
  is hash-locked at build time (it currently pins 3 of ~25 packages).
- **Legacy backends.** `execution.py` and `method_runtime.py` run docker
  straight from study config with **no trust check at all**. This slice does
  not touch them. Either say so in `operations.md` or fix them separately.
