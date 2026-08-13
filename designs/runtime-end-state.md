# Runtime provisioning — the end state

**Status: design, 2026-08-13. Not implemented.**

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
| Resource action | Artifacts that may become catalog entries | **Unresolved — see below** |
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

## The unresolved case: resource actions

Resource actions are the one surface the principle does not cleanly place, and
that is a real question rather than an oversight.

They look non-evidence: one-shot local operations, container explicitly refused
(`resource_actions.py:195`). But the DEVS generator's `generate` action
produces a bundle that is then **registered as an environment**, and that
environment produces Runs. So the provenance of the generator arguably belongs
in the evidence chain of everything it generated.

Two defensible answers:

- **Actions are tooling.** Their outputs are inputs a human reviews and
  registers deliberately. Provenance stops at registration. Keep them as they
  are.
- **Actions are evidence-producing.** A generated environment should record
  what generated it. Then actions need content-sealed runtimes like any other
  evidence producer.

The second is more defensible if OptPilot's claim is end-to-end reproducibility.
It is also more work. This needs an owner decision, not a default.

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
6. **Decide the resource-action question** — and act on it.

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
