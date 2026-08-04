# Release readiness

Status on 2026-08-04: **internal release candidate**. The package is runnable
and has an automated verification path, but it is not yet cleared for a public
official release.

## Automated gates

Run from the repository root:

```bash
optpilot package validate catalog/production_agv_scheduling \
  --check-imports --check-source --check-setup-files

optpilot package smoke catalog/production_agv_scheduling \
  --study studies/parallel_smoke.yaml

python catalog/production_agv_scheduling/scripts/run_package_tests.py

cd catalog/production_agv_scheduling
sha256sum --check UNITY_BUILD.sha256
```

The repository CI runs the same validation, all method/environment/interface
suites in isolated Python processes, and the Unity build checksum check on
Python 3.10, 3.11, and 3.12. Separate processes are intentional: several
method suites import their retained entrypoint as the generic module name
`method`, so combining them would test polluted module state rather than the
retained runtime contract.

The parallel smoke uses four deterministic candidates and the paper-default
ten-replication environment so its capacity-three setting produces three
overlapping running-attempt intervals. A release candidate must additionally
pass headless and 3D Candidate tries, MQTT telemetry, and clean interface
shutdown. Expensive or licensed studies are scheduled gates rather than
per-commit gates.

## Public-release blockers

1. **Redistribution rights.** The supplied research snapshot has no top-level
   license grant. The compiled Unity export also lacks a complete third-party
   asset inventory. `NOTICE.md` records both facts. Obtain written rights or
   remove the affected material before public distribution.
2. **Untrusted generated code.** LLM-produced Python is treated as trusted
   research code. Child-process limits and import/contract validation are
   guardrails, not an operating-system security boundary. Do not evaluate
   untrusted Candidates on a machine containing sensitive same-user data. A
   public multi-tenant service needs a separately sandboxed evaluator/replay
   worker with narrow IPC.
3. **Platform coverage.** The currently pinned preview image was verified on
   Apple Silicon. Publish and test a pinned multi-platform OCI index for
   `linux/arm64` and `linux/amd64` before claiming general desktop support.
4. **Licensed solver gate.** Run both rolling-MILP candidates on a supported
   `gurobipy` installation with a valid Gurobi license. Until then, that method
   remains an explicitly optional baseline.
5. **Durable LLM recovery.** Current retained workers do not project a durable
   method-state or prompt-object store. A completely replaced worker can lose
   iterative search state and repeat paid provider calls. The method also does
   not atomically persist each successful response before continuing. Add a
   Realm-owned, run-scoped state/blob projection and a per-call durable response
   commit before claiming crash-safe LLM execution.
6. **Bounded LLM provenance.** Every Candidate currently embeds cumulative
   prompt and response evidence under an 8 MiB cap, but the configured HTTP
   response and retry limits do not formally bound even one worst-case
   generation below that cap. Introduce generation-scoped, content-addressed
   provenance with aligned admission limits before a public release.

## Release-candidate evidence still required

- A live OpenRouter baseline-plus-four-candidate round with recorded model,
  provider, cost, and three-way evaluator overlap; keep it separate from CI
  because it transmits research inputs and incurs external cost.
- The full 21-trial LLM study, 135-point rule grid, and 704-candidate GA, DE,
  and PSO studies on appropriately sized runners.
- Both rolling-MILP variants on a licensed runner.
- Long-horizon and 100-replication baseline evaluations.
- Browser checks for Unity loading, Brotli response headers, local MQTT
  connection, visible AGV/product movement, and interface cleanup on the
  supported browser/platform matrix.
- An archived upstream source snapshot or release tag corresponding to the
  recorded local import digest, plus a complete third-party software/asset
  bill of materials.

## External-service disclosure

The LLM method sends bounded policy source, metrics, and selected trace evidence
to OpenRouter and the inference provider it selects. The default configuration
allows provider fallback for the requested model. Operators must review
provider retention terms, approve data egress, set spending limits, and retain
the model/provider/cost metadata captured with each Candidate. API credentials
must remain host-injected secrets and are not release artifacts.

## Release decision

An internal, access-controlled research release may proceed after the automated
gates and the relevant functional smoke tests pass. A public release must stay
blocked until every item in **Public-release blockers** is resolved or the
affected component is removed and the package is revalidated.
