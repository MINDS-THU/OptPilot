---
title: Evidence
description: Files recorded by OptPilot runs and how methods can inspect prior observations.
---

# Evidence

Every run creates an evidence directory. By default it is written under `runs/` next to the study config, unless `--output-root` or config evidence settings choose another location.

## Run Files

Common files include:

- `study_spec.json`
- `summary.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `method_calls.jsonl`
- `method_events.jsonl`
- `scheduler_events.jsonl`
- `environment_snapshot.json`
- `run_policy.json`
- `run_lineage.json`

Trial workspaces and saved artifacts live under the run directory as well.

## EvidenceView

Methods can inspect prior evidence through `EvidenceView` during iterative optimization:

- observations
- trials
- artifacts
- method calls
- scheduler events
- method events
- extracted records

This gives methods a stable API for learning from previous trials without parsing raw run files by hand.

## Resume And Branch

Resume appends more trials to an existing run:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml \
  --resume-run-dir path/to/existing-run
```

Branch starts a new run that records a previous run as its parent:

```bash
uv run optpilot run examples/studies/sa_baseline.yaml \
  --branch-from-run-dir path/to/existing-run
```
