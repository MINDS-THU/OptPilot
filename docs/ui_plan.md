# OptPilot Lightweight UI Plan

This document proposes a lightweight UI for OptPilot. The goal is to make the
existing study workflow easier to browse, launch, monitor, and inspect without
turning OptPilot into an optimizer, simulator, or domain-specific visualization
system.

Simulator visualization is intentionally out of scope for the first UI phase.
The UI should first make OptPilot's general workflow usable across many
environments.

## 1. Goals

The UI should support two modes:

1. Personal local use:
   - Launch with one command from the repository or an installed package.
   - Read local config files and run directories.
   - Start and monitor studies on the local machine.
2. Shared team use:
   - Run the same app as a small server.
   - Let multiple users browse a shared catalog and shared run roots.
   - Add authentication and permissions later without changing the core UI
     model.

The first version should help users answer:

- Which environments are available?
- Which controllers, engines, and methods are available?
- Which studies can I run?
- How can I customize a study before launching it?
- Which studies are currently running?
- What happened in a previous study?
- What was the best candidate and why?
- What failed, timed out, or was invalid?

## 2. Non-Goals

The UI should not:

- implement optimization algorithms
- interpret simulator-specific event semantics
- include built-in environment-specific animations
- replace the CLI
- require a database for local personal use
- require users to move their existing config files into a new project format
- hide the underlying YAML and evidence files from advanced users

The CLI and the UI should remain two frontends over the same OptPilot runtime.

## 3. Product Shape

The UI should feel like a compact lab console. It should prioritize dense,
scannable information over a landing-page style experience.

Primary sections:

```text
Studies      running, queued, completed, failed, branched, resumed
Catalog      environments, methods, controllers, engines, instances
Builder      compose and customize a StudyConfig
Run Detail   inspect one study run deeply
Settings     catalog roots, run roots, Python environment, server mode
```

## 4. Launch Model

Add a new CLI command:

```bash
uv run optpilot ui
```

Useful options:

```bash
uv run optpilot ui --host 127.0.0.1 --port 8765
uv run optpilot ui --catalog examples --catalog ~/optpilot-projects
uv run optpilot ui --runs .runs --runs examples/opt_devs_gen_sims/runs
uv run optpilot ui --open-browser
```

Default behavior for local use:

- bind to `127.0.0.1`
- choose port `8765` unless occupied
- use the current working directory as a catalog root
- discover common run roots from config files and `runs/` directories
- open the browser only when requested

Shared server behavior:

```bash
uv run optpilot ui --host 0.0.0.0 --port 8765 --runs /shared/optpilot/runs
```

Authentication can be added later. The local-first version should not depend on
it.

## 5. Data Model

The UI should use existing OptPilot artifacts as source of truth.

### Catalog Records

Catalog records come from scanning YAML files:

- `EnvironmentConfig`
- `MethodConfig`
- `StudyConfig`

The scanner should also inspect built-in components and Python-hook references
when possible.

The catalog index can be cached in SQLite for speed, but the YAML files remain
canonical.

### Run Records

Run records come from run directories containing files such as:

- `summary.json`
- `study_spec.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `controller_decisions.jsonl`
- `engine_snapshots.jsonl`
- `scheduler_events.jsonl`
- `run_policy.json`
- `run_lineage.json`
- `environment_snapshot.json`

The run directory remains the canonical evidence store. The UI may keep a small
index for search and sorting, but should be able to rebuild that index from the
files.

### Job Records

For studies launched from the UI, keep a lightweight process/job record:

```text
job_id
study_config_path
run_dir
process_id
status
started_at
finished_at
exit_code
stdout_log
stderr_log
```

This job record is UI metadata. It should not replace OptPilot's evidence
records.

## 6. Catalog UI

### Environments

Each environment entry should show:

- id
- description and tags
- candidate type: `parameters`, `files`, or `opaque`
- evaluate type: `python`, `command`, or `custom`
- metric keys
- workspace requirements
- recent runs using this environment
- compatible methods when known

Environment detail should show:

- normalized config summary
- raw YAML
- candidate schema or file policy
- metrics configuration
- evaluation command/callable
- related studies

### Methods

Each method entry should show:

- id
- controller implementation
- engine implementation
- default batch size when configured
- resource profile overrides
- sandbox spec overrides
- tags and description
- recent runs using this method

Method detail should show:

- controller config
- engine config
- raw YAML
- known compatibility with candidate types

### Controllers And Engines

The UI should expose both:

- method-level configs, which most users will choose
- underlying controller and engine implementations, which advanced users may
  inspect

For built-ins, metadata can come from a small registry manifest. For user-owned
Python hooks, the UI can show the import string and whether it resolves.

## 7. Study Builder

The builder should create or edit `StudyConfig` files.

Recommended layout:

- left panel: structured form
- right panel: YAML preview
- bottom panel: validation messages

Steps:

1. Choose environment.
2. Choose method.
3. Select objective metric and direction.
4. Select instance source.
5. Configure budget.
6. Configure execution settings.
7. Configure evidence level and output root.
8. Review YAML.
9. Launch or save.

The builder should validate:

- referenced files exist
- config kinds and API versions are correct
- objective metric is declared by the environment when metric keys are known
- method and environment candidate types are compatible when compatibility is
  known
- budget values are positive
- Python hooks are syntactically valid import strings

The builder should not hide YAML from users. The generated YAML is part of the
product.

## 8. Running Study Monitor

The running-study monitor should update as evidence files change.

Top summary:

- run status
- completed trials
- best metric
- best trial
- best artifact
- failure count
- elapsed time
- objective
- environment
- method

Main views:

- metric chart over trial order
- trial status timeline
- trial table
- recent scheduler events
- recent controller decisions
- recent engine snapshots
- recent failures and errors

Trial table columns:

```text
trial_id
status
primary_metric
artifact_id
engine_id
instance_count
wall_clock_seconds
retry_count
created_at
```

For local running studies, the UI can watch JSONL files and send updates to the
browser with Server-Sent Events. SSE is enough for append-only study events and
is simpler than WebSockets.

## 9. Previous Study Browser

The previous-study browser should make old run directories useful without
manual file inspection.

Run list columns:

```text
study name
status
completed trials
best metric
failure count
environment
method
started at
finished at
run directory
```

Run detail tabs:

- Overview
- Trials
- Metrics
- Artifacts
- Controller Decisions
- Engine Snapshots
- Scheduler Events
- Logs And Errors
- Reproducibility
- Raw Files

The browser should support:

- filtering by status, environment, method, tag, and date
- sorting by best metric, start time, failure count, and completed trials
- opening the run directory path
- copying the CLI command to resume or branch
- comparing two or more runs of the same study

## 10. Artifact And Evidence Inspection

The UI should provide general evidence inspection that works for all
environments.

Artifacts:

- list artifact records from `artifacts.jsonl`
- show validation status
- show materialization metadata
- show generator record
- preview text, JSON, YAML, logs, and small images when available
- diff text/code artifacts when two related artifacts are selected

Observations:

- show metric values
- show status and errors
- show resource usage
- show provenance
- link to trial workspace files

Prompts and model records:

- show prompt metadata and prompt file references
- show model, provider, invocation id, and parameters
- avoid inline exposure of large prompt bodies unless the user opens the prompt
  file explicitly

## 11. Customization Model

Customization should happen at three levels.

1. Study customization:
   - objective
   - instances
   - budget
   - execution settings
   - evidence level
2. Method customization:
   - engine config
   - controller config
   - resource overrides
3. Environment customization:
   - instance files
   - workspace copy settings
   - metric extraction settings
   - evaluation timeout

For the first UI version, prefer editing StudyConfig and MethodConfig values
that already exist. Avoid inventing a new UI-only configuration language.

## 12. Backend Architecture

Recommended backend stack:

- Python
- FastAPI or Starlette
- SQLite for optional local index and UI job records
- Server-Sent Events for live updates
- existing `optpilot.runner.run_study` for execution

Suggested package layout:

```text
src/optpilot/ui/
  __init__.py
  server.py
  catalog.py
  runs.py
  jobs.py
  schemas.py
  events.py
  static/
```

The backend should expose APIs like:

```text
GET  /api/catalog/environments
GET  /api/catalog/methods
GET  /api/catalog/studies
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/observations
GET  /api/runs/{run_id}/trials
GET  /api/runs/{run_id}/artifacts
GET  /api/runs/{run_id}/events
POST /api/studies/validate
POST /api/studies/launch
POST /api/jobs/{job_id}/stop
```

For local mode, `run_id` can be a stable hash of the absolute run directory.

## 13. Frontend Architecture

Recommended frontend stack:

- React with Vite, or Svelte with Vite
- static build served by the Python backend
- no heavy state framework at first
- charts through a lightweight charting library
- code/text preview with a small editor component

The UI should be restrained:

- left navigation
- dense tables
- compact summary strips
- tabs for run details
- icon buttons for common actions
- forms for configuration
- YAML preview for transparency

Avoid marketing-style pages. The first screen should be the studies dashboard.

## 14. Multi-User Path

The local UI should not block a future shared deployment.

Future shared-mode additions:

- user authentication
- role-based permissions
- shared catalog roots
- shared run roots
- optional Postgres instead of SQLite
- job queue abstraction
- remote workers

The same run-directory evidence model should remain valid in both local and
shared modes.

## 15. Implementation Phases

### Phase 1: Read-Only Run Browser

- scan run directories
- list previous studies
- show summary, observations, trials, artifacts, and raw files
- no launching yet

This phase gives immediate value with low risk.

### Phase 2: Catalog Browser

- scan config roots
- list environments, methods, and studies
- show YAML previews
- validate references

### Phase 3: Launch And Monitor

- launch studies from existing StudyConfig files
- capture UI job records
- stream evidence updates
- show live metric charts and trial tables

### Phase 4: Study Builder

- structured StudyConfig editor
- environment/method picker
- objective and budget forms
- YAML preview
- save and launch

### Phase 5: Artifact Diff And Compare

- compare runs
- compare artifacts
- show code/text diffs
- summarize failure and metric differences

### Phase 6: Shared Server Mode

- authentication
- shared run roots
- permission model
- optional remote worker integration

## 16. First MVP Definition

The smallest useful MVP is:

- `optpilot ui` starts a local web server
- dashboard lists discovered run directories
- run detail shows summary, observations, trials, artifacts, and raw files
- catalog page lists discovered EnvironmentConfig, MethodConfig, and StudyConfig
- user can launch an existing StudyConfig
- running run detail updates while observations are appended

This MVP avoids domain-specific visualization and still solves the main daily
pain: users no longer need to manually inspect scattered YAML, JSON, and JSONL
files to understand what OptPilot did.

