# General runtime provisioning

**Status: proposal, 2026-08-12. Not implemented.**

> **Superseded in part, 2026-08-13.** Two later decisions change the
> conclusions here: execution is **container-only with a default base
> image** (there is no local-process mode), and a package is **a folder**
> with immutable snapshots for versions (there is no separate stored
> package kind). See [`how-optpilot-runs-code.md`](how-optpilot-runs-code.md).

## The problem, stated generally

A package must be able to bring its own runtime, whatever that runtime is,
without asking the user to install anything by hand.

Today it cannot. `or_solving` instructs the user to run `uv pip install -r
requirements-pruned.txt` and `brew install glpk ipopt`, because a retained
study runtime accepts only vendored pure-Python wheels
(`locked_python_runtime.py::_validate_wheel_tags`). Every dependency outside
that keyhole becomes a manual step and a `dependency_host_provisioned`
finding.

The narrow framing of this problem is "support native Python wheels." That is
the wrong frame, because **the catalog is already polyglot** and will get more
so.

## Evidence: the catalog is already not a Python catalog

| Ecosystem | Where | Status |
| --- | --- | --- |
| Native Python wheels | `ortools`, `pymoo`, `numpy` in `or_solving` | manual install |
| Node / npm | `devs-gen-interface/_start_frontend.sh`; `uses: npm` is one of four `SETUP_STEP_TYPES` | supported for interfaces only |
| Unity WebGL | `production_agv_scheduling/.../unity_webgl/Build/*.unityweb`, with `UNITY_BUILD.sha256` | vendored binary blobs |
| System solver binaries | GLPK, IPOPT | manual install |
| Proprietary game server | Factorio headless | documented, not shipped |

Future entries will add more: Java or Julia solvers, R, CUDA builds, licensed
solvers (Gurobi, CPLEX), simulators shipped as binaries. Assuming Python is
the boundary case is not safe.

## Why per-ecosystem mechanisms do not scale

The narrow fix — allow platform-tagged wheels in the existing lock — solves
exactly one row of that table. Each subsequent row then needs its own lock
format, its own validation, its own retention shape, and its own cache
semantics. That is N subsystems, each with its own bugs, and none of them help
the next one.

It also does nothing for the two hardest rows (system binaries, proprietary
runtimes), which are precisely the ones a user cannot reasonably be asked to
install correctly.

## The general mechanism already exists here

An image digest describes an entire runtime — any language, any system
library, any toolchain — in one lock value. OptPilot already uses containers,
and already has most of the machinery:

- **Declaration shape.** `interfaceContainer` in `defs/common.schema.json`:
  `{engine, image, platform, build}`, with `image` XOR `build`.
- **Exactness.** `local_container_web_provider.py` enforces
  `^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$` — *"image_ref must
  be pinned by sha256."* An image digest is a **stronger** exactness claim
  than a wheel set, not a weaker one.
- **Trust.** `ContainerGatewayImageTrust` already models administrator
  approval of a specific image digest, on the explicit reasoning that *"image
  pinning alone is not sufficient: an arbitrary image controls the gateway."*
  `RealmProviderTrustPolicyService` exists alongside it. (The earlier draft of
  this doc called the class `TrustedGatewayApproval`; that name is the
  provider-side concept, not the class.)
- **Execution.** Container interfaces and Studio workspaces already run this
  way (`workspace_runtime/Dockerfile`).
- **Platform awareness.** `prepared_runtime_cache.py::key_payload` already
  keys on `{os, architecture, variant, libc}` and interpreter ABI.

So containers are not a new concept to introduce. They are an existing
capability that study execution is currently *excluded* from.

## What actually blocks it

One check, `retained_study_compiler.py` ~1005–1014:

```python
runtime_kinds = (
    environment.runtime.type, method.runtime.type,
    "container" if backend.type == "container" else "process",
    sandboxSpec.runtimeType,
)
if any(value != "process" for value in runtime_kinds):
    _fail("container_runtime_unsupported",
          "The retained process-study slice does not prepare container runtimes.")
```

The same package may declare a container *interface* and be refused a
container *study*. That inconsistency is the thing to remove.

## Design

**Opt-in per package.** The process sandbox stays the default and stays
Docker-free. A package declares a container runtime only when it needs one:

```yaml
runtime:
  sandbox: container
  container:
    image: ghcr.io/minds-thu/optpilot-orsolving@sha256:<64 hex>
    platform: linux/amd64
```

Pure-Python packages are untouched — no Docker, no migration, no edit. This is
the property that makes the change safe: it adds a capability instead of
imposing a requirement.

**Reuse, don't invent.**

1. Generalize `interfaceContainer` to a shared `containerRuntime` definition;
   the interface path keeps using it unchanged.
2. Require digest pinning for study images, reusing `_IMMUTABLE_IMAGE_RE`.
   Tag-only refs (`:latest`) must be refused — they break retention.
3. Extend the provider trust policy so an image digest is approved for study
   execution the way gateway images are approved today.
4. Replace the blanket `container_runtime_unsupported` failure with: process
   runtimes prepare as now; container runtimes resolve the pinned digest,
   verify approval, and record the digest in the run definition.

**Retention.** The image digest becomes part of the run-definition digest, the
way the prepared-layer identity is today. Same study + same image = same
digest. This *improves* the evidence model: today a study's runtime is
described by a wheel set that only means something on one platform; a digest
means the same thing everywhere.

**Failure modes, all pre-Run:**

- `container_engine_unavailable` — the package needs a container runtime and
  none is installed. Message names the package and the install step.
- `container_image_unpinned` — a tag was used instead of a digest.
- `container_image_untrusted` — digest not approved by policy.

## Invariants

| Invariant | Under containers |
| --- | --- |
| Exactness | **Stronger.** One digest covers the entire runtime, not just Python packages. |
| No index access at run time | Preserved after first pull; the image is cached and digest-verified. First pull needs a registry, exactly as the first wheel vendor needs the wheels present. |
| No host inheritance | Preserved; grants stay explicit, as they are for interfaces today. |
| Replayability | **Stronger.** A digest replays anywhere the platform matches, versus a wheel layer that silently means different things per platform. |

## Interim: vendoring native wheels

If `or_solving` needs to be self-contained before the above lands, vendoring
its native wheels is about 36 MB per platform (`ortools` 22, `numpy` 12,
`pymoo` 2) — comfortably under the existing 512 MB cap. It requires only
relaxing `_validate_wheel_tags` against a declared platform set.

Treat this as **tactical relief with a deletion date**, not architecture. It
solves one row of the table, and the container work supersedes it. Do it only
if the timing demands it.

Note that under the container design the GLPK-vs-HiGHS question disappears
entirely: the image can carry GLPK, IPOPT, or anything else. Choose HiGHS on
solver merits if at all, not to work around packaging.

## Implementation

See [`container-method-runtime-plan.md`](container-method-runtime-plan.md) for the
reviewed implementation plan. Note that it narrows the first slice to **method**
runtimes: environments, evaluators and execution backends stay process-only,
because the record layer is already container-ready on the method side while the
environment side needs a new attempt provider and supervisor.

## Open questions for the owner

- **Who builds and hosts the images?** This is the real cost, and it is
  operational rather than technical: a registry, a build pipeline, and a
  policy for who may approve a digest. Vendoring has no equivalent cost.
- **Is a container runtime acceptable as a per-package requirement?** Studio
  users already have one. CLI-only users of pure-Python packages would still
  never need one under this design.
- **Should `build:` be allowed for study runtimes, or `image:` only?**
  Building from a Dockerfile at prepare time is reproducible only if the
  build is; `image:` with a digest is unambiguous. Recommend starting with
  `image:` only.
- **What is the migration for `or_solving`?** It is the natural first
  consumer: one image with COOPA's pruned deps, ortools, pymoo and a solver
  backend, and its four `dependency_host_provisioned` findings disappear.
