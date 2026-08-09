# Dispatch policy contract

You are improving the executable dispatch policy of a single-server job
queue. The only editable file is `scheduler.py`.

## Interface

- `create_scheduler()` — top-level, synchronous, takes no arguments,
  returns the policy object. Do not define `create_controller`.
- `policy.run(snapshot)` — called every time the server becomes free.
  It must return the `id` of exactly one job from `snapshot["queue"]`.

## Snapshot fields

`snapshot` is a dict:

- `time` (float) — current simulation time in minutes.
- `queue` (list of dicts), one entry per waiting job:
  - `id` (str) — job identifier; return one of these.
  - `class` (str) — `standard`, `express`, or `bulky`.
  - `arrival` (float) — arrival time.
  - `service_time` (float) — known service duration in minutes.
  - `due` (float) — due time; lateness beyond it is penalized.
  - `late_penalty` (float) — per-minute lateness weight for this job.
  - `waited` (float) — minutes waited so far.

## Objective

`total_score = 100 − mean_wait − 0.5 × weighted_lateness / served`,
maximized. Lower waiting times and less (weighted) lateness are better;
`express` jobs carry the highest lateness penalty.

## Rules

- Deterministic, bounded computation only; no randomness, network,
  subprocess, filesystem, or clock access.
- Use only the snapshot fields listed above.
- The retained SQLite trace is a deterministic replay of the
  lowest-scoring replication (`events` and `kpi` tables).
