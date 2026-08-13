# Runtime provisioning — the end state

**Status: design, 2026-08-13. Not implemented.**

> **Superseded in part, 2026-08-13.** Two later decisions change the
> conclusions here: execution is **container-only with a default base
> image** (there is no local-process mode), and a package is **a folder**
> with immutable snapshots for versions (there is no separate stored
> package kind). See [`how-optpilot-runs-code.md`](how-optpilot-runs-code.md).

Supersedes the framing in [`general-runtime-provisioning.md`](general-runtime-provisioning.md),
which argued from "containers already exist here." That was an argument from
where code happens to live. This one argues from what OptPilot is for.

## The principle

A Run is evidence. `run_definition_digest` claims: *same digest, same
execution*. For that claim to be true, everything that can change the result
must be inside the digest — environment source, method source, candidate,
settings, and **the runtime**.

From which:

> **Any execution whose output becomes evidence must name its runtime by a
> content-addressed identifier that a third party can resolve and verify, and
> that identifier must be inside the run-definition digest.**

Everything below is a consequence.

### What this reframes

`dependency_host_provisioned` is not a developer-convenience lint. It is the
system correctly reporting that **the evidence is incomplete**. A Run whose
result depended on whichever `ortools` happened to be installed is not
reproducible, and its digest overstates what it knows.

So the problem was never "users must install things by hand." That is the
symptom. The problem is that manual installation puts dependencies *outside the
evidence*, and better documentation cannot fix that.

This is also why the answer must be a *content-addressed* identifier rather
than, say, a `requirements.txt` that OptPilot installs for the user.
Convenience-wise those are similar. Evidentially they are not comparable.

## The line the principle draws

Not interactive-versus-headless. **Evidence-producing or not.**

| Surface | Output | Runtime obligation |
| --- | --- | --- |
| Environment evaluator | Observations → retained | Content-addressed, in the digest |
| Method | Candidates → retained | Content-addressed, in the digest |
| Resource action | Files a human may choose to register | None; the record starts at registration |
| Interface | An interactive session | None; isolation only |
| Studio workspace | A place a human edits | None; isolation only |

The last two are **principled as they are**. Their containers exist for
isolation and convenience, not evidence. Unifying them with the study path
would be tidiness at the cost of meaning.

## Two provisioned runtime kinds, distinguished by how identity is established

This is the load-bearing distinction, and review found that collapsing it is
fatal:

- **Content-sealed.** Identity *is* the set of verified content digests. The
  locked wheel layer hashes each wheel and verifies it against the sealed
  package; preparation touches no network. Admissible as a study runtime
  because the identifier cannot be wrong.
- **Recipe-built.** Identity is the recipe plus whatever the network returned
  when it ran (`uv sync` against a live index). Reproducible only if the index
  is. Legitimate for a workspace; **not** admissible as a study runtime.

An OCI image digest is content-sealed in the sense that matters: the digest
names the exact bytes, and a third party can resolve and verify it. That is why
it can carry native wheels, GLPK, CUDA, Java or a licensed solver and still
satisfy the principle, while `pip install ortools` cannot.

These must remain **two kinds with different admissibility**, not one kind with
a flag. A recipe-built layer must never be able to present itself as sealed.

## What the end state looks like

**Evidence-producing surfaces** accept exactly two runtime declarations:

1. *No declaration* — the component uses only what OptPilot itself provides,
   or pure-Python source vendored into the package (as `production_agv_scheduling`
   does with `simpy`). No engine required. This stays the default and stays
   Docker-free.
2. *A content-sealed runtime* — either the vendored pure-wheel layer as today,
   or a digest-pinned OCI image. Both are content-addressed; both go into the
   run-definition digest; both are trust-approved before execution.

There is no third option. In particular there is no "install these packages for
me" mode, because it cannot satisfy the principle.

**Non-evidence surfaces** keep recipe-built runtimes and network-enabled setup,
because nothing they produce is being claimed as reproducible.

**Declarations that cannot be enforced are refused rather than recorded.** A
host process cannot enforce `network: disabled`; today that declaration is
accepted and silently ignored. Either the surface enforces it or the author is
told it cannot — with one caveat review raised: shipped packages already
declare `grants: {network: disabled}` on process runtimes as *intent*. So
record an enforcement status per grant (`enforced` / `unenforceable-here`)
rather than deleting the vocabulary and breaking them.

**The honest framing that follows:** the process sandbox is a *reproducibility*
boundary, not a *security* boundary. Containers are the only real isolation
OptPilot has. The end state stops implying otherwise.

## Mechanics: where code lives, where dependencies live, what runs where

The principle is not only about *what is recorded*; it dictates *where code
physically goes*. Three artifacts are combined at execution, and they stay
separate on purpose:

| Artifact | Identity | Retained as | Lands where |
| --- | --- | --- | --- |
| Authored code | Realm content digest | `run-method-source`, `run-environment-source` | Projected onto the host filesystem |
| Dependencies | Sealed-layer digest, or image digest | `locked-python-runtime-payload`, or the image | A `site-packages` tree on the host, or inside the image |
| Execution | — | — | Subprocess, or container with the projection bind-mounted |

Today the worker resolves `import_roots` from
`prepared_method_runtime.runtime_settings` into host paths and points the
interpreter at them. Code and dependencies are **separate artifacts joined at
run time**, never merged.

### Consequence: authored code is never baked into an image

The image carries third-party dependencies only. The component's own source —
including anything vendored into the package, such as `or_solving`'s
`coopa_home/` — stays retained Realm content, is projected on the host, and is
**bind-mounted** into the container.

This is forced by the principle, not a convenience:

- The two identities change at different rates. Code changes per edit; the
  image changes per rebuild. Merging them means every code edit invalidates
  the dependency artifact, and every dependency bump rewrites code identity.
- More seriously, baking code in breaks *the code that ran is the code that was
  retained*. Execution would use a copy inside the image while the evidence
  points at Realm content. The digest would be claiming something false.

**The container is a dependency environment, not a code container.**

### Consequence: installation moves from run time to build time

- **Today:** dependencies are installed at prepare time, on the user's machine,
  into a content-addressed layer cached by `prepared_runtime_cache`.
- **End state:** dependencies are installed at image build time, in the
  package's own repository CI. The user's machine pulls a digest and verifies
  it; it installs nothing.

That is why "stop making users install things by hand" is answered by moving
the build, not by writing a better installer. An installer that runs on the
user's machine reintroduces exactly the uncertainty the principle forbids.

### Worked example: `or_solving`

```
ghcr.io/<org>/or-solving@sha256:...        <- ortools, pymoo, smolagents, glpsol
        built once, in that package's CI       (third-party only)
                    |
                    |  pulled by digest, verified, trust-approved
                    v
        +--------------------------------+
        |  container                     |
        |   /optpilot/method   <---------+---- bind mount of the projected,
        |   PYTHONPATH -> that path      |     retained method source
        |   runs coopa_solver.py         |     (including vendored coopa_home)
        +--------------------------------+
```

The environment (`or_problem`) is unaffected: stdlib-only, so it keeps running
as a host subprocess with its code projected exactly as today.

### What does not change

A component that declares no runtime is untouched — code projected,
dependencies already present (OptPilot's own, or pure-Python source vendored
into the package as `production_agv_scheduling` does with `simpy`), subprocess
on the host, no engine involved. The mechanism above engages only when a
component declares an image.

## Resolved: the record starts at registration

Resource actions look ambiguous — the DEVS generator's `generate` produces a
bundle that is then registered as an environment, and that environment produces
Runs. So does the generator belong in the evidence chain?

**Owner decision 2026-08-13: no.** A Run records the code that produced it, not
the code that produced that code. The boundary is **registration into the
catalog**.

This is the stronger rule, for two reasons:

1. **It terminates.** If a Run's record covered the code that wrote its code, it
   should equally cover whatever wrote that, and the editor, and the author.
   "Is it registered in the catalog?" is a bright line that can be checked;
   "is it in the provenance chain?" is unbounded. It is the boundary git draws:
   a commit records the code, not the editor that typed it.
2. **Nothing is actually lost.** A generated environment's source is captured
   and fingerprinted at registration like any other package, and every Run using
   it records that content digest. The generated code is fully inspectable. What
   is not captured automatically is its *origin story* — that an LLM wrote it,
   which model, from which prompt.

That origin story, if wanted, is **package metadata**, not a runtime property.
It can be recorded as a note attached to the package without the generator
needing a content-addressed runtime. The two concerns were never coupled;
conflating "we should be able to trace this" with "this must execute in a
sealed runtime" is what made the case look unresolved.

**Consequence:** resource actions keep running as ordinary host processes, and
the explicit refusal at `resource_actions.py:195` is correct rather than a
limitation. The evidence-producing set is exactly two surfaces: environment
evaluators and methods.

## Sequencing, each stage useful alone

1. **Container method runtimes** — the smallest evidence-producing surface that
   needs a non-pure runtime, and the one whose record layer is already ready.
   Delivers `or_solving` with no manual installs.
2. **Make the two sealed kinds explicit** — put the seal policy in the artifact
   identity so content-sealed and recipe-built cannot be confused. Pure
   refactor; no new capability; removes the fatal ambiguity before anything
   depends on it.
3. **Contract-scoped trust** — one approval ledger, with the contract as a real
   filtered dimension, so an image approved for one purpose is not trusted for
   another.
4. **Publish the per-surface capability table** — today an author reads
   `sandbox: process | container` in the schema and cannot tell which surfaces
   accept it; the answer lives in four unrelated modules. Make the accepted
   dialect declared rather than emergent.
5. **Container environment runtimes** — only when a package actually needs one.
   No shipped package does today.
Resource actions, interfaces and workspaces need no stage: the first is
settled above, and the last two do not produce evidence.

## What this design does not solve

- **Image provenance.** A digest pins bytes, not trustworthiness. Whether the
  image was built from the sources it claims is a supply-chain question this
  design does not answer.
- **Reproducible image builds.** A pinned digest makes replay exact; it does not
  make the *build* reproducible. `requirements-pruned.txt` pins 3 of ~25
  packages, so rebuilding "the same" image can produce a different digest.
- **First-use cost.** A ~2 GB image with no implicit pull is an explicit,
  slow operator step. Better than five manual installs; not instant.
- **Non-Python evidence producers.** The principle covers them, but nothing
  ships one yet, so the design is untested there.
