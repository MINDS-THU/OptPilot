# Dispatch policy contract

The editable candidate file is `policy.py`. It must define a top-level,
zero-argument `create_policy()` returning the policy object; the
simulator's dispatch component calls `policy.run(snapshot)` once per
decision, where `snapshot` is:

- `waiting_jobs` (non-empty list of dicts): `job_id` (int), `type`
  (`"quick"` ≈ 2 min or `"heavy"` ≈ 8 min), `processing_time` (float,
  minutes, known on arrival), `arrival_time` (float, minutes).
- `current_time` (float, minutes).

`run` must return the `job_id` of exactly one waiting job.

## Objective

Per replication, score = **negated average waiting time** of completed
jobs (higher is better). Roughly two quick jobs arrive for every heavy
one; the machine serves one job at a time. Shorter jobs held behind long
ones dominate the average wait, so sequencing (e.g. shortest-processing-
time ordering, tempered against starving heavy jobs) is where
improvements live. The baseline is first-come, first-served.

## Rules

Deterministic, bounded computation only; use only the snapshot fields
above; no simulator internals, os, sys, subprocess, socket, pathlib,
importlib, or random. The retained SQLite trace (`events`, `states`,
`kpi` tables) is a deterministic replay of the lowest-scoring
replication.
