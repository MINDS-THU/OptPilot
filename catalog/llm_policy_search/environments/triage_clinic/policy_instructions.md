# Editing contract

The editable candidate file is `TriagePolicy.py` — the DEVS component
class `TriagePolicy` that owns the triage decision. Each candidate is a
complete replacement of this file, so everything except the selection
logic must be preserved exactly.

## Preserve verbatim

- The top-level class name `TriagePolicy` and its `__init__` signature
  `(self, name: str, parent, policy: str)`, including the call to
  `super().__init__(name)`.
- All port registrations exactly as in the template: input ports
  `patient_arrival` and `doctor_free`; output ports `selected_patient`,
  `remove_patient`, and `queue_empty` (all `dict`-typed).
- The message protocol: when the doctor is free and patients wait, emit
  `selected_patient` (the full patient dict) AND `remove_patient`
  (`{"patient_id": <id>}`) together in the same output; when the doctor
  is free and the queue is empty, emit `queue_empty`. Every selected
  patient must be removed from the local queue exactly once and never
  selected twice.
- The DEVS lifecycle structure: `initialize`, `deltext`, `lambdaf`,
  `deltint`, the `OUTPUT`/`IDLE` phase discipline
  (`hold_in("OUTPUT", 0.0)` to emit, `hold_in("IDLE", float("inf"))` to
  wait), and the `trace_state` method.
- The existing imports. Never add imports of random, os, sys,
  subprocess, socket, pathlib, importlib, or builtins — the decision
  must stay deterministic.

## What to change

Only the selection logic in `_select_patient()`: given the local
`self.queue` of waiting patients, return the patient dict to serve next
(and remove it from the queue, as the template does). Each patient dict
has:

- `id` (int): unique arrival number — use as the final tiebreaker.
- `urgency` (int, 1–3): higher is more urgent.
- `exam_duration` (float, hours): this patient's examination time.
- `arrival_time` (float, hours): when the patient joined the queue.

`get_current_time()` (already imported) returns the current simulation
time in hours, so `current_time - p["arrival_time"]` is the patient's
waiting time so far. You may ignore the `policy` string parameter and
implement one deterministic rule directly, but keep the parameter in
the signature.

## Objective

Each replication reports `avg_urgency_weighted_waiting_time` — the
average over served patients of `urgency * waiting_time` — which is
minimized (the evaluator negates it, so higher scores are better), plus
`patients_served`. The baseline rule is FIFO (earliest arrival first).
A candidate that breaks the class name, ports, or protocol will crash
or corrupt its own replications and score as a failed trial.
