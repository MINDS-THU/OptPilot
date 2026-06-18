---
title: Evidence
description: Files recorded by OptPilot runs and how methods can inspect prior observations.
---

# Evidence

Evidence is the recorded history of one OptPilot run.

The public configs define what should happen:

```text
method proposes candidate
environment evaluates candidate
OptPilot records evidence
```

The run directory shows what actually happened: which candidates were proposed, how they were materialized, which trials succeeded or failed, which metrics were returned, and where evaluator output files were written.

## Run Directory

By default, runs are written under `runs/` next to the study config. You can also choose another output root through the CLI or study evidence settings.

Common files:

| File | Meaning |
| --- | --- |
| `summary.json` | Final run summary, best metric, failure count, and run status. |
| `study_spec.json` | Compiled run spec generated from the study, environment, and method configs. |
| `candidates.jsonl` | Candidate records, validation details, and materialization details. |
| `observations.jsonl` | Trial observations and metric values. |
| `trials.jsonl` | Trial lifecycle records and backend metadata. |
| `method_calls.jsonl` | Method requests, responses, and errors. |
| `method_events.jsonl` | Events emitted by methods. |
| `scheduler_events.jsonl` | Scheduling and worker events. |
| `environment_snapshot.json` | Environment contract used by the run. |
| `run_policy.json` | Budget, retry, parallelism, and timeout policy. |
| `run_lineage.json` | Resume and branch lineage metadata. |

The exact set can vary by evidence level and by which parts of the runtime are used.

## Storage Roles

OptPilot uses a few runtime folders with different jobs.

| Runtime storage | Purpose |
| --- | --- |
| Method workspace | Scratch space for one method invocation. Command wrappers often write request files and logs here. |
| Candidate store | Durable handoff area for candidates produced by methods, especially generated files. |
| Trial workspace | Fresh evaluation directory for one trial. `trialWorkspace` entries are copied here and file candidates are materialized here. |
| Evidence directory | Run-level records, summaries, and retained evaluator outputs. |

The evaluator normally reads the trial workspace, not the candidate store. For file candidates, the runner copies files from the candidate store into the trial workspace according to the environment candidate contract.

## Output Files

Evaluators may produce logs, JSON summaries, CSV files, SQLite databases, images, or other files inside the trial workspace.

There are two ways those files become visible in evidence:

- the evaluator returns `output_files` descriptors
- the environment config lists `outputFiles` patterns to collect after evaluation

`evidence.outputFileStorage` controls whether file bytes are copied into evidence storage:

| Value | Behavior |
| --- | --- |
| `reference` | Evidence records paths to files where they were produced, usually inside trial workspaces. |
| `copy` | Matching output files are copied into evidence storage so they remain easy to inspect even if trial workspaces are later cleaned up. |

Metric values should still be returned or extracted through `metrics`. Output files are for supporting evidence, debugging, traces, plots, logs, and databases.

## EvidenceView

Methods can inspect previous results through `EvidenceView` during iterative optimization.

Typical information available through this API includes:

- observations and metric values
- trial records
- candidate records
- method call records
- scheduler events
- method events
- extracted records

This gives methods a stable way to learn from previous trials without parsing raw run files by hand.

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
