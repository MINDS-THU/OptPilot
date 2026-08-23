# DEVS-Gen

The companion package for *Specification-Driven Generation and Evaluation of
Discrete-Event World Models via the DEVS Formalism*. It contains the DEVS-Gen
interface and generated simulators packaged as ordinary OptPilot Environments.
They can be inspected directly or paired with compatible Methods from another
package.

| Environment | What it simulates | Metrics | Interesting decision |
|---|---|---|---|
| `seird-epidemic` | SEIRD compartmental epidemic (quantized-integrator xDEVS model) | final `deceased`, `recovered`, `infective`, `exposed`, `susceptible` | epidemiological parameters (`transmission_rate`, periods, …) |
| `abp-protocol` | Alternating Bit Protocol over deterministic lossy channels | `packets_delivered`, `retransmissions`, `forward_dropped`, `ack_dropped` | sender retransmission `timeout` |
| `dispatch-station` | One machine serving quick and heavy jobs | mean and worst waiting-time score | editable dispatch `policy.py` |
| `triage-clinic` | Urgency-aware clinic triage | mean and worst weighted-wait score | editable `TriagePolicy` component |

Each environment folder contains:

- `devs_project/` — **unmodified** generator output from the DEVS-Gen
  gallery (the group's own generated code).
- `evaluator.py` — the only OptPilot-authored code: builds the model from
  candidate parameters, runs the xDEVS coordinator in-process, and returns
  final-state metrics.
- `runtime_dependencies/` — a hash-locked vendored copy of the pure-Python
  `xdevs 3.0.0` wheel (GPL; license text and third-party notice included,
  mirroring the DEVS Generator interface resource), installed into an
  isolated per-run virtual environment by the retained runtime.

`gallery-random-search` is a deliberately simple seeded baseline over the
declared parameter schema; the two studies (`seird-minimize-deaths`,
`abp-tune-timeout`) run five trials each and complete in seconds.

The parameter examples are deterministic for fixed parameters (SEIRD is an ODE
integrator; ABP uses seeded deterministic channel noise), so retained runs
replay exactly.

The dispatch and triage Environments demonstrate function-style and
component-class policy hooks. Pair either in Studio with **Trace-guided policy
design (language model)** from the LLM-Guided Heuristic Design package; the
pairing uses declared Candidate, validation, and replay contracts rather than
cross-package imports.

Provenance note: the gallery bundles predate the `devs.simulation.v2`
manifest and the summary/trace contracts; these environments wrap the raw
generated models directly rather than going through the Studio
registration wizard. Newly generated simulators should instead be saved
from the interface and registered via **Set up for Catalog**, which is
launch-ready since v2.
