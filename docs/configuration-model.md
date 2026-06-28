# Configuration Model

This document defines the next public configuration model for OptPilot. It is
intentionally smaller than everything OptPilot may support in the future. A
field belongs in this model only when we are willing to implement schema
validation, runner behavior, Studio behavior when relevant, and at least one
runnable example.

Status: the initial implementation is in place. The public JSON schemas,
compiler, Studio-generated study drafts, bundled example-package configs,
per-run source copies, setup reuse, process/container runtime execution, retry
attempt folders, and package-qualified catalog ids have been implemented. The
implementation checklist at the end of this document is the current source of
truth for remaining follow-up work.

The goal is clarity:

- Catalog source is immutable.
- Code runs from writable copies.
- Environments and methods each have their own runtime.
- Studies describe experiment policy, not component installation.
- Resources are lightweight catalog entries, not part of the core
  environment-method-study contract.

## Out Of Scope For Now

These are intentionally not part of the public model yet:

| Field or concept | Why it is excluded now |
| --- | --- |
| `execution.backend` | There is no meaningful user choice yet. OptPilot currently orchestrates studies on the user's machine. Component code runs in declared component runtimes, such as local subprocesses or containers. Cluster or remote orchestration backends can be added later. |
| `execution.resources` | CPU, memory, GPU, disk, and network resource allocation are not uniformly enforced. |
| `execution.runtime` | Runtime belongs to components. Study execution should not duplicate component runtime. |
| `method.resourceProfile` | This mixes method metadata with execution resource requests. Remove it from the public model. |
| `method.produces` | This duplicates the environment-owned candidate contract. Keep the public method contract to `accepts` until a concrete use case needs a separate produced-shape declaration. |
| `local_subprocess` as a public backend | The public concept should be `runtime.sandbox: process`. Whether the implementation uses subprocess workers is internal. |
| GPU and disk requests | They require real allocation/enforcement before they can be public config fields. |
| Top-level `runtime` on resources | Resources are not core executable experiment components. A launchable resource uses `interface.setup` and `interface.command`. |
| General host mounts | Mounts are useful but easy to overpromise. Keep them out of the simple public model until container behavior is uniform. |

## Mental Model

OptPilot connects one method to one environment through a study.

- The environment defines what can be evaluated.
- The method defines how candidates are proposed.
- The study binds the environment and method, chooses the objective, and sets
  experiment policy such as budgets, timeouts, retries, and parallelism.
- A resource is supporting catalog material, such as an article, dataset,
  helper tool, documentation page, or optional GUI demo.

There are two different concepts:

| Concept | Where it appears | Meaning |
| --- | --- | --- |
| `runtime` | Environment and method configs | How this component is installed and executed. |
| `execution` | Study config | How OptPilot runs the experiment loop. |

`runtime` answers: what does this component need in order to run?

`execution` answers: how many trials should the study run, how long may they
take, and how should failures be retried?

## Package Layout

Adding a package means adding a new sibling under `catalog/`. A package should
not overwrite another package. The package folder name is the package id.

```text
catalog/
  example_package/
    README.md
    environments/
      dispatch_rule_env/
        environment.yaml
        evaluator.py
        cases/
        prompts/
    methods/
      file_editor_method/
        method.yaml
        method.py
        prompts/
    resources/
      devs_simulation_interface/
        optpilot.resource.yaml
        README.md
        app/
        scripts/
    studies/
      dispatch_rule_study.yaml
```

Package folders may contain ordinary source code and small sample data. Large
external data, generated files, private keys, local model checkpoints, and
experiment outputs should not be committed into catalog source. Large datasets
or support resources should usually be linked or documented. They may be fetched
only by an applicable environment, method, or interface setup into a writable
copy.

Environment, method, and resource ids must be unique inside one package. Studio
should identify entries with package-qualified keys such as
`example_package/environment/dispatch-rule-environment` so that two different
packages may use the same local component id without overwriting each other.

## Folder Semantics

| Folder | Owner | Writable while executing | Purpose |
| --- | --- | --- | --- |
| Catalog source | Package author or user | No | Clean reusable source code, configs, docs, and small sample data. |
| Studio editable copy | Studio/user | Yes | Copied workspace for editing, setup, interface launch, and registration. |
| Run source copy | OptPilot run | Yes | Copied environment and method source prepared for one study run. |
| Trial workspace | OptPilot trial | Yes | Per-candidate evaluation folder. |
| Run directory | OptPilot run | Yes | Evidence, candidates, observations, logs, and summaries. |

Typical generated paths:

```text
.optpilot-ui/
  workspaces/
    ws_abc123/
      workspace/                 # editable copy opened by Studio
  runtime/
    ws_abc123/
      logs/
      cache/

runs/
  my-study-2026-06-27/
    source/
      environment/               # copied environment source for this run
        .optpilot/               # environment setup status and logs
      method/                    # copied method source for this run
        .optpilot/               # method setup status and logs
    trials/
      trial-001/                 # one candidate evaluation
        attempt-1/               # candidate materialization and evaluator work
    candidates/                  # retained candidate file bundles, when used
    method_calls/
      method-call-001/           # request, response, stdout, stderr
    evidence_files/              # copied retained evaluator outputs, when enabled
    candidates.jsonl             # candidate specs proposed by the method
    observations.jsonl           # metric observations returned to the method
    trials.jsonl                 # trial lifecycle and status records
    method_calls.jsonl           # method proposal and observe call records
    scheduler_events.jsonl       # scheduler decisions and lifecycle events
    study_spec.json              # compiled study spec used for the run
    run_policy.json              # resolved budget, timeout, retry, and parallelism policy
    environment_snapshot.json    # environment contract snapshot
    run_lineage.json             # source/config/copy lineage
    summary.json                 # run summary
```

`trials/` is where evaluation happens. `candidates.jsonl` records what the
method proposed. `candidates/` is optional retained candidate material, usually
for file-based candidates. A candidate may be tried in one or more trials later,
so candidate records and trial folders are intentionally separate.

Retries do not overwrite prior evaluator work. Each retry uses a new
`attempt-*` folder under the same trial folder and appends its own lifecycle
record to `trials.jsonl`.

`observations.jsonl` contains the results returned to the method: status,
metric values, constraint results, output-file metadata, and provenance.

`trials.jsonl` contains trial lifecycle records: queued, running, succeeded,
failed, timed out, retried, and similar scheduler-visible state.

`summary.json` contains the final run summary. It is JSON, not JSONL, because it
is rewritten once at the end rather than appended during the run.

## Execution Lifecycle

This section describes the target execution behavior. Studio and command-line
execution should use the same source-copy rule.

Catalog actions:

1. **Inspect Read-Only Source Code** opens catalog source in a read-only Studio
   viewer or editor mode. No setup runs. Filesystem-level read-only mounts are
   optional deployment hardening, not the core user-facing behavior.
2. **Create Editable Copy and Install** copies the catalog entry into
   `.optpilot-ui/workspaces/...`, then runs the relevant setup hook in the copy.
3. **Launch Interface** creates or reuses an editable copy, runs the relevant
   setup hooks, starts `interface.command`, and opens the Preview panel.

Studio action requirements:

| Action | Needs writable copy | Needs setup tools | Needs workspace runtime | Launches code |
| --- | --- | --- | --- | --- |
| Inspect Read-Only Source Code | No | No | No | No |
| Create Editable Copy and Install | Yes | Yes, when setup is declared | Yes, when setup is declared | Setup commands only |
| Launch Interface | Yes | Yes, when setup is declared | Yes | `interface.command` |

For self-hosted Studio, setup and interface launch require the configured
workspace runtime to be available, such as local subprocess execution or a
Docker/Podman-compatible runtime that can start the interface process and expose
the requested ports.

The workspace runtime is a Studio operational setting. It is not a package YAML
field. It controls how Studio runs setup and preview commands for editable
copies.

Setup order:

- for a process-runtime environment or method install, run `runtime.setup` when
  declared
- for a process-runtime environment or method interface launch, run
  `runtime.setup` first, then `interface.setup` when declared
- for a container-runtime environment or method, do not run `runtime.setup`;
  prepare the declared image or build instead
- for a resource install or launch, run `interface.setup` when declared

Studio may reuse a completed setup when the setup fingerprint is unchanged.
Setup must rerun when `.optpilot/setup-status.json` is missing, failed, or
outdated, or when the user explicitly requests reinstall.

After editing a copy, users can run a study against that copy's config path
directly, or copy/register it into a local package such as
`catalog/local_package/`. This should not overwrite curated package source.

Study launch:

1. Read the study config.
2. Resolve the environment and method configs.
3. Create the run directory.
4. Copy the environment source folder into `runs/<run>/source/environment/`.
5. Copy the method source folder into `runs/<run>/source/method/`.
6. Resolve component-relative paths against the copied configs.
7. Run environment setup in the copied environment source, when declared.
8. Run method setup in the copied method source, when declared.
9. Start the method in its own runtime and request candidates.
10. For each trial, create a fresh trial workspace, copy `trialWorkspace`
    entries, materialize the candidate, run the evaluator in the environment
    runtime, and record evidence.

Python methods should not be imported directly into the main OptPilot process
in this model. A Python method runs behind a method worker process or container
so that its runtime can be prepared and isolated like any other component.

## Schema Conventions

All config files use YAML. JSON Schema validates allowed fields, value types,
and simple structural rules. Semantic validation checks path safety,
copied-source rules, component compatibility, and runtime-specific
requirements. Unless a schema says otherwise, additional properties are not
allowed.

Common scalar types:

| Type name | Allowed value |
| --- | --- |
| `apiVersion` | Exactly `optpilot.io/v1`. |
| `identifier` | Non-empty string. |
| `description` | String. |
| `tags` | Array of strings. |
| `path` | Non-empty string. |
| `pythonImport` | String shaped like `module.path:symbol.path`. |
| `command` | Non-empty array of strings, for example `[bash, -lc, ./scripts/setup.sh]`. |
| `env` | Object whose keys are environment variable names and whose values are strings. |
| `settings` | Object. Contents are component-specific and intentionally open. |

## Component Runtime

`runtime` belongs to environments and methods. It describes how that component
is prepared and where that component executes.

Allowed `runtime` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `sandbox` | No | `process` or `container` | `process` runs in a separate local process. `container` runs in Docker/Podman-compatible execution. Default is `process`. |
| `setup` | No; valid only with `sandbox: process` | Setup object | Idempotent setup steps run in the copied source before execution. |
| `env` | No | Object of string values | Component environment variables. Use this for values the component needs when run alone. |
| `envFromHost` | No | Array of strings | Host environment variable names allowed through, usually secrets such as `OPENAI_API_KEY`. |
| `workdir` | No | String | Default working directory, relative to the copied component source. Default is `.`. |
| `container` | When `sandbox: container` | Container object | Container image or build settings. |

`runtime` does not contain CPU, memory, GPU, disk, retry, trial budget, or
parallelism. Those are either study execution policy or future resource
allocation features.

For `sandbox: process`, OptPilot runs component code in a separate local
subprocess. Process runtimes use the host's normal network behavior; OptPilot
does not claim network isolation for them.

For `sandbox: container`, dependency installation belongs in the image or
`runtime.container.build`, not in `runtime.setup`.

Allowed `runtime.container` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `image` | One of `image` or `build` | String | Container image to run. |
| `executable` | No | String | Container executable, usually `docker` or `podman`. Default is platform-defined. |
| `build` | One of `image` or `build` | Build object | Build settings for an image produced from the copied source. |
| `network` | No | `enabled` or `disabled` | Container execution network policy. Default is `disabled`. |

Allowed `runtime.container.build` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `context` | No | String | Build context path, resolved against the copied component source. Default is `.`. |
| `dockerfile` | No | String | Dockerfile path relative to `context`. |
| `tag` | Yes | String | Image tag to build and run. |
| `target` | No | String | Multi-stage build target. |
| `platform` | No | String | Target platform, for example `linux/amd64`. |
| `args` | No | Object of string values | Build arguments. |
| `extraArgs` | No | Array of strings | Extra build arguments passed to the container tool. |
| `timeoutSeconds` | No | Integer, minimum `1` | Build timeout. |

## Setup

`setup` is explicit. OptPilot does not guess an installer from files alone. If
`setup` is omitted, OptPilot does not install dependencies.

Setup always runs in a writable copy, never in catalog source. Setup may create
`.venv/`, `node_modules/`, build outputs, logs, or `.optpilot/` metadata inside
that copied source.

`runtime.setup` is valid for `sandbox: process`. For `sandbox: container`, put
dependencies in the declared container image or build.

Setup steps run with the host's normal network behavior for process runtimes.
Container image builds use the container tool's build behavior. The
`runtime.container.network` field controls component execution after setup, not
dependency installation.

Allowed setup object fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `steps` | Yes | Non-empty array of setup step objects | Ordered setup operations. |
| `env` | No | Object of string values | Extra environment variables shared by all setup steps. |
| `envFromHost` | No | Array of strings | Host environment variables allowed through for setup. |
| `timeoutSeconds` | No | Integer, minimum `1` | Total setup timeout for all steps. |

Allowed common setup step fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `uses` | Yes | `uv`, `python-venv`, `npm`, or `command` | Built-in setup runner or explicit command. |
| `cwd` | No | String | Working directory for this step, relative to the copied source. Default is `.`. |
| `env` | No | Object of string values | Extra environment variables for this step. |

Allowed `uses: uv` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `extras` | No | Array of strings | Extras passed as repeated `--extra` flags to `uv sync`. |
| `groups` | No | Array of strings | Dependency groups passed as repeated `--group` flags to `uv sync`. |
| `frozen` | No | Boolean | When true, pass `--frozen`. |

OptPilot runs `uv sync` in the step `cwd`. This creates a `.venv/bin/python`
that can become the component Python, and `.venv/bin` is prepended to `PATH`.

Allowed `uses: python-venv` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `python` | No | String | Python executable used to create the venv. Default is `python3`. |
| `venv` | No | String | Virtual environment directory, relative to `cwd`. Default is `.venv`. |
| `requirements` | No | Array of strings | Requirement files installed with `pip install -r`. Default is `requirements.txt` when present. |
| `installProject` | No | Boolean | When true, install the project itself into the venv with `pip install -e .`. |

OptPilot creates or reuses the venv, installs the requested requirements, and
prepends the venv's `bin` directory to `PATH`.

Allowed `uses: npm` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `install` | No | `ci` or `install` | npm install command. Default is `ci`. Use `install` only when no lockfile exists. |

OptPilot runs `npm ci` or `npm install` in the step `cwd`. Later component or
interface commands run with `node_modules/.bin` from that directory on `PATH`,
depending on whether the setup belongs to `runtime` or `interface`.

Allowed `uses: command` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `command` | Yes | Non-empty array of strings | Explicit setup command. |

Use `command` when the typed setup runners are not expressive enough. A command
step is still run automatically, but it does not choose the component Python.
Use `uv` or `python-venv` when OptPilot should select a Python interpreter.

For process runtimes, the last successful `uv` or `python-venv` setup step sets
the component Python. If no typed Python setup step is declared, Python
entrypoints use the first `python` found on the prepared `PATH`. PATH entries
created by setup steps are applied in declaration order, with later Python setup
steps taking precedence for Python entrypoints.

## Interface

`interface` describes an optional GUI or long-running service for an
environment, method, or resource. The command should start services only. Large
dependency installation for study execution belongs in `runtime.setup`.
Dependency installation needed only for launching the GUI belongs in
`interface.setup`.

Allowed `interface` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `label` | No | String | Display label. |
| `description` | No | String | Display description. |
| `setup` | No | Setup object | Install hook for this launchable interface. It runs in the same editable workspace/runtime used by `interface.command`. |
| `command` | Yes | Non-empty array of strings | Long-running service command. |
| `port` | Yes | Integer, `1` to `65535` | Primary preview port. |
| `cwd` | No | String | Working directory relative to the copied source. Default is `.`. |
| `env` | No | Object of string values | Environment variables for the interface process. |
| `envFromHost` | No | Array of strings | Host environment variable names allowed through to the interface process. |
| `extraPorts` | No | Array of integers, each `1` to `65535` | Additional ports exposed by the interface. |
| `readyPath` | No | String | HTTP path used to detect readiness. |
| `readyTimeoutSeconds` | No | Integer, `0` to `600` | Maximum readiness wait. |

## Environment Config

An environment config defines the evaluation contract.

Required top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `apiVersion` | `optpilot.io/v1` | Config API version. |
| `config` | `environment` | Config kind. |
| `id` | `identifier` | Environment id. |
| `evaluator` | Evaluator object | How candidates are evaluated. |
| `candidate` | Candidate object | Candidate format and validation/materialization contract. |
| `metrics` | Metrics object | How metric values are obtained. |

Optional top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `description` | String | Human-readable description. |
| `tags` | Array of strings | Search and grouping tags. |
| `runtime` | Runtime object | Environment setup and execution runtime. |
| `interface` | Interface object | Optional GUI for the environment. |
| `trialWorkspace` | Array of copy entries | Files copied into each trial workspace before candidate materialization. |
| `methodContext` | Object | Instructions and references visible to compatible methods. |
| `records` | Array of record objects | Non-metric records collected from trials. |
| `outputFiles` | Array of output file specs | Files retained from trial workspaces. |
| `capabilities` | Array of capability objects | Environment capabilities a method may require. |

Allowed `evaluator` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `python` | One of `python`, `command`, `adapter` | `pythonImport` | Python callable evaluator. |
| `command` | One of `python`, `command`, `adapter` | `command` | External evaluator command. |
| `adapter` | One of `python`, `command`, `adapter` | `pythonImport` | Python adapter class. |
| `timeoutSeconds` | No | Integer, minimum `1` | Evaluator timeout. Defaults to study `execution.timeoutSeconds` when omitted. |
| `pythonPath` | No | Array of strings | Import paths resolved against the copied environment source. |
| `cwd` | No | String | Evaluator working directory, usually inside the trial workspace. |
| `env` | No | Object of string values | Narrow evaluator-process overrides. Prefer `runtime.env` for component-level variables. |
| `settings` | No | Object | Environment-owned evaluator settings. |

Allowed `trialWorkspace` entry fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `from` | Yes | String | Source path relative to the copied environment source. |
| `to` | Yes | String | Destination path inside the trial workspace. |

Allowed `methodContext` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `instructions` | No | Array of strings | Paths to instruction files relative to the copied environment source. |
| `references` | No | Array of reference objects | Method-readable reference files. |

Allowed `methodContext.references[]` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `name` | Yes | String | Reference name. |
| `path` | Yes | String | Reference path relative to the copied environment source. |
| `description` | No | String | Reference description. |
| `type` | No | String | Reference type, for example `markdown`, `json`, or `csv`. |
| `mimeType` | No | String | MIME type. |

Environment-owned and method-visible data are intentionally separate:

| Data | Visible to | Meaning |
| --- | --- | --- |
| `evaluator.settings` | Evaluator only | Opaque evaluator settings. OptPilot passes this object through and does not rewrite paths inside it. |
| `methodContext.instructions[]` | Compatible methods | Instruction files a method may read from the copied environment source. |
| `methodContext.references[]` | Compatible methods | Named reference files a method may read from the copied environment source. |
| Evidence and observations | Compatible methods during/after the run | Prior candidate results, metrics, retained files, and provenance that OptPilot chooses to expose through the method worker contract. |

Allowed `capabilities[]` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `id` | Yes | `identifier` | Capability id that a method may list in `accepts.requires.capabilities`. |
| `description` | No | String | Human-readable explanation. |

## Candidate Schema

`candidate` appears in environment configs. It is the environment-owned
candidate contract that compatible methods must satisfy.

Allowed candidate fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `format` | Yes | `parameters`, `files`, or `opaque` | Candidate format. |
| `description` | No | String | Candidate description. |
| `parameters` | When `format: parameters` | Parameters object | Parameter schema. |
| `files` | When `format: files` | Files object | File candidate contract. |
| `materialize` | No | Materialize object | How file candidates are materialized. |
| `opaque` | When `format: opaque` | Opaque object | Opaque candidate family metadata. |

Allowed `candidate.parameters` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `schema` | Yes | Object of parameter definitions | Named parameter definitions. |
| `constraints` | No | Array of constraint objects | Additional parameter constraints. |

Allowed parameter definition fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `valueType` | Yes | `float`, `int`, `bool`, `string`, `categorical`, `array`, or `object` | Parameter value type. |
| `min` | No | Number | Minimum numeric value. |
| `max` | No | Number | Maximum numeric value. |
| `values` | No | Array | Allowed categorical values. |
| `default` | No | Any JSON value | Default value. |
| `description` | No | String | Parameter description. |
| `unit` | No | String | Unit label. |
| `pattern` | No | String | String pattern. |
| `items` | No | Parameter definition | Array item definition. |
| `properties` | No | Object of parameter definitions | Object property definitions. |
| `required` | No | Array of strings | Required object properties. |
| `minItems` | No | Integer, minimum `0` | Minimum array length. |
| `maxItems` | No | Integer, minimum `0` | Maximum array length. |

Allowed constraint fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `id` | Yes | String | Constraint id. |
| `expr` | Yes | Object | Constraint expression. Must use an expression shape supported by OptPilot. |
| `description` | No | String | Constraint description. |

Allowed `candidate.files` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `editable` | Yes | Non-empty array of objects with `path` string | Files the method may edit. |
| `required` | No | Array of strings | Files that must be present after materialization. |
| `allow` | No | Array of strings | Allowed file globs. |
| `deny` | No | Array of strings | Denied file globs. |

Allowed `candidate.materialize` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `root` | No | String | Trial workspace subdirectory where file candidates are materialized. |

Allowed `candidate.opaque` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `family` | Yes | String | Opaque candidate family. |

## Metrics, Records, And Output Files

Allowed `metrics` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `source` | Yes | `return`, `file`, `stdout`, `sqlite`, or `custom` | Where metric values come from. |
| `keys` | No | Array of strings | Metric names expected. |
| `path` | No | String | File path for file-based extraction. |
| `database` | No | String | SQLite database path. |
| `query` | No | String | SQLite query. |
| `extractor` | No | `pythonImport` | Custom extractor. |
| `settings` | No | Object | Extractor-specific settings. |

Metrics have source-specific required fields:

| `metrics.source` | Required fields | Evaluator requirement | Expected payload |
| --- | --- | --- | --- |
| `return` | None beyond `source` | `evaluator.python` or `evaluator.adapter` | Python dict. Metric values may be top-level or under `metric_values` or `metrics`. |
| `file` | `path` | Any evaluator | JSON object in a trial-workspace file. Metric values may be top-level or under `metric_values` or `metrics`. |
| `stdout` | None beyond `source` | `evaluator.command` or `evaluator.adapter` | Evaluator stdout containing one JSON object. Metric values may be top-level or under `metric_values` or `metrics`. |
| `sqlite` | `database`, `query` | Any evaluator | First SQLite query row as a JSON object of metric names to values. |
| `custom` | `extractor` | Any evaluator | Custom extractor returns a dict with the same metric payload shape. |

Allowed `records[]` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `name` | Yes | String | Record name. |
| `source` | Yes | `jsonl`, `csv`, `sqlite_table`, `sqlite_query`, or `custom` | Record source. |
| `path` | No | String | Record file path. |
| `database` | No | String | SQLite database path. |
| `table` | No | String | SQLite table name. |
| `query` | No | String | SQLite query. |
| `extractor` | No | `pythonImport` | Custom extractor. |
| `settings` | No | Object | Extractor-specific settings. |

Records have source-specific required fields:

| `records[].source` | Required fields | Meaning |
| --- | --- | --- |
| `jsonl` | `name`, `path` | Retain records from a JSONL file in the trial workspace. |
| `csv` | `name`, `path` | Retain records from a CSV file in the trial workspace. |
| `sqlite_table` | `name`, `database`, `table` | Retain rows from a SQLite table. |
| `sqlite_query` | `name`, `database`, `query` | Retain rows returned by a SQLite query. |
| `custom` | `name`, `extractor` | Call a custom Python record extractor. |

JSONL record files contain one JSON object per line. CSV rows are converted to
JSON objects using the header row. SQLite rows become JSON objects keyed by
column name. Custom record extractors return a list of JSON objects or an object
with `rows` or `records`.

`outputFiles[]` may be either a string path or an object:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `path` | Yes | String | File path or glob inside the trial workspace. |
| `name` | No | String | Logical output name. |
| `required` | No | Boolean | Whether missing output should fail the trial. |

## Method Config

A method config defines how candidates are proposed.

Required top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `apiVersion` | `optpilot.io/v1` | Config API version. |
| `config` | `method` | Config kind. |
| `id` | `identifier` | Method id. |
| `entrypoint` | Entrypoint object | How OptPilot calls the method worker. |
| `accepts` | Accepts object | Environment contracts this method accepts. |

Optional top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `description` | String | Human-readable description. |
| `tags` | Array of strings | Search and grouping tags. |
| `runtime` | Runtime object | Method setup and execution runtime. |
| `interface` | Interface object | Optional GUI for the method. |
| `settings` | Object | Method-specific settings. |

Allowed `entrypoint` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `python` | One of `python` or `command` | `pythonImport` | Python method class or callable, run through a method worker. |
| `command` | One of `python` or `command` | `command` | External method command. |
| `pythonPath` | No | Array of strings | Import paths resolved against the copied method source. |
| `protocol` | No | `batch` or `session` | Method protocol. Default is `batch`. `session` is valid only with `entrypoint.python` in the initial public model. |

Protocol semantics:

- `batch` means each method proposal or observation call may use a fresh worker.
  The method should not rely on in-memory state between calls.
- `session` means OptPilot keeps one method worker alive for the run. The method
  may keep in-memory state until the run ends.

For `batch`, persistent method state should be written to the per-call
`method_calls/method-call-*` directory or returned through the method worker
contract. Shared writes to copied method source or the run directory need
package-owned locking because parallel calls may overlap.

Allowed `accepts` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `formats` | Yes | Non-empty array of `parameters`, `files`, or `opaque` | Candidate formats the method accepts. |
| `requires` | No | Requires object | Context and capability requirements. |

Allowed `accepts.requires` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `context` | No | Array of strings | Required context fields, such as `methodContext.instructions`. |
| `capabilities` | No | Array of strings | Required environment capability ids. |

## Study Config

A study binds one environment to one method. It should not duplicate component
setup, runtime, dependencies, or component-specific environment variables.

Required top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `apiVersion` | `optpilot.io/v1` | Config API version. |
| `config` | `study` | Config kind. |
| `name` | Non-empty string | Study name. |
| `environmentConfig` | String | Path to the environment config, relative to the study file. |
| `methodConfig` | String | Path to the method config, relative to the study file. |
| `objective` | Objective object | Primary optimization objective. |
| `budget` | Budget object | Trial and failure limits. |

Optional top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `description` | String | Human-readable description. |
| `tags` | Array of strings | Search and grouping tags. |
| `execution` | Execution object | Study-level run policy. |
| `evidence` | Evidence object | Evidence retention policy. |
| `reproducibility` | Reproducibility object | Seed and reproducibility settings. |

Allowed `objective` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `metric` | Yes | String | Metric to optimize. |
| `direction` | Yes | `maximize` or `minimize` | Optimization direction. |
| `aggregation` | No | `mean`, `median`, `min`, `max`, `sum`, `last`, or `weighted_mean` | Aggregation for repeated observations. |
| `secondaryMetrics` | No | Array of strings | Extra metrics to display and retain. |

Allowed `budget` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `maxTrials` | Yes | Integer, minimum `1` | Maximum number of trials. |
| `maxWallClockSeconds` | No | Integer, minimum `1` | Wall-clock budget for the whole run. |
| `maxFailures` | No | Integer, minimum `1` | Failure budget. |

Allowed `execution` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `parallelism` | No | Integer, minimum `1` | Maximum parallel trials. Default is `1`. |
| `timeoutSeconds` | No | Integer, minimum `1` | Default per-trial timeout when the environment evaluator does not provide one. |
| `retry` | No | Retry object | Retry policy. |

Allowed `execution.retry` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `maxRetries` | No | Integer, minimum `0` | Maximum retries per failed trial. Default is `0`. |

`maxRetries: 1` means one retry after the initial failed attempt, for two total
attempts. Internally this corresponds to `maxAttempts = maxRetries + 1`.

Allowed `evidence` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `level` | No | `minimal`, `standard`, or `full` | Evidence detail level. |
| `outputFileStorage` | No | `reference` or `copy` | Whether output files are referenced or copied into the evidence store. |
| `outputDir` | No | String | Run output root. Relative paths resolve against the launch working directory, not catalog source. When omitted, OptPilot writes under `./runs` from the launch working directory. Studio and CLI launchers must reject or redirect any resolved run root inside catalog source. |

Allowed `reproducibility` fields:

| Field | Required | Type or values | Meaning |
| --- | --- | --- | --- |
| `seed` | No | Integer | Study seed. |

## Resource Folders And Manifests

A resource is lightweight catalog support material. It can be displayed in
Studio and may optionally define a launchable interface. It is not referenced by
studies and is not part of the environment-method-study experiment contract.

A resource folder may be README-only. In that case, Studio may derive display
metadata from the folder name and README, and no YAML manifest is required.

Use `optpilot.resource.yaml` when the resource needs a stable id, tags, or a
launchable interface.

Required manifest fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `apiVersion` | `optpilot.io/v1` | Config API version. |
| `config` | `resource` | Config kind. |
| `id` | `identifier` | Resource id. |

Optional top-level fields:

| Field | Type or values | Meaning |
| --- | --- | --- |
| `name` | String | Display name. |
| `description` | String | Display description. |
| `tags` | Array of strings | Search and grouping tags. |
| `interface` | Interface object | Optional launchable resource UI. |

Do not put top-level `runtime` on a resource manifest. If a resource GUI needs
dependency installation, use `interface.setup`.

## Path Resolution

All component paths should remain copy-safe.

| Field | Written relative to | Resolved at execution against |
| --- | --- | --- |
| `study.environmentConfig` | Study file | Source config first, then copied environment config during launch. |
| `study.methodConfig` | Study file | Source config first, then copied method config during launch. |
| `runtime.workdir` | Owning config file | Copied component source. |
| `runtime.setup.steps[].cwd` | Owning component source root | Copied component source. |
| `runtime.container.build.context` | Owning config file | Copied component source. |
| `interface.cwd` | Owning environment, method, or resource source root | Copied source for that entry. |
| `interface.setup.steps[].cwd` | Owning environment, method, or resource source root | Copied source for that entry. |
| `evaluator.pythonPath` | Environment config file | Copied environment source. |
| `entrypoint.pythonPath` | Method config file | Copied method source. |
| `evaluator.cwd` | Trial workspace | Trial workspace. |
| `metrics.path` | Trial workspace | Trial workspace. |
| `metrics.database` | Trial workspace | Trial workspace. |
| `records[].path` | Trial workspace | Trial workspace. |
| `records[].database` | Trial workspace | Trial workspace. |
| `trialWorkspace[].from` | Environment config file | Copied environment source. |
| `trialWorkspace[].to` | Trial workspace | Trial workspace. |
| `methodContext.instructions[]` | Environment config file | Copied environment source. |
| `methodContext.references[].path` | Environment config file | Copied environment source. |
| `outputFiles[].path` | Trial workspace | Trial workspace. |
| `evidence.outputDir` | Launch working directory | User-selected run root outside catalog source. |

Run roots must never be inside catalog source. If a relative `outputDir` or the
default `./runs` would resolve inside `catalog/`, OptPilot should fail with a
clear message or redirect to a configured non-catalog run root such as
`.optpilot-ui/runs/` for Studio launches.

Run preparation follows a copy-then-rebase rule: OptPilot first copies
environment and method source into the run directory, then resolves executable
paths against those copied configs. Compiled run specs should not point
executable paths back to catalog source.

OptPilot treats `evaluator.settings` and `method.settings` as opaque component
data. If a component puts paths inside those objects, the component owns
resolving them, usually against its copied source or declared working directory.
Command arrays are not path-rewritten; relative command paths run from the
declared `cwd` or `runtime.workdir` and use the prepared `PATH`.

Prefer config-local imports:

```yaml
evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
```

instead of imports that hardcode the catalog package path:

```yaml
evaluator:
  python: catalog.example_package.environments.foo.evaluator:evaluate
```

Config-local imports keep the same config runnable from catalog source, Studio
editable copies, and run source copies.

## Copy Rules

When OptPilot creates an editable or execution copy, it copies the source root:

- for an environment, the folder containing the selected environment config
- for a method, the folder containing the selected method config
- for a resource launch, the folder containing `optpilot.resource.yaml`

The copy should exclude generated or local-only folders:

```text
.git/
.venv/
__pycache__/
node_modules/
runs/
resource/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

Setup may recreate needed generated folders inside the copied source.

## Target Environment Example

This example shows the intended public model and should validate against the
current schemas.

```yaml
apiVersion: optpilot.io/v1
config: environment

id: dispatch-rule-environment
description: Evaluate Python dispatch rules on two job-shop scheduling cases.
tags: [job-shop, scheduling, files]

runtime:
  sandbox: process
  workdir: .
  env:
    PYTHONUNBUFFERED: "1"
  setup:
    steps:
      - uses: python-venv
        cwd: .
        requirements: [requirements.txt]
        installProject: true
    env:
      PIP_DISABLE_PIP_VERSION_CHECK: "1"
    timeoutSeconds: 900

evaluator:
  python: evaluator:evaluate
  pythonPath: [.]
  timeoutSeconds: 120
  cwd: candidate
  settings:
    cases:
      - id: ft06_small
        path: cases/ft06_small.yaml
      - id: la01_tiny
        path: cases/la01_tiny.yaml

trialWorkspace:
  - from: template_dispatch_rule
    to: candidate

candidate:
  format: files
  description: A Python dispatching rule file edited by a method.
  materialize:
    root: candidate
  files:
    editable:
      - path: dispatch_rule.py
    required:
      - dispatch_rule.py
    allow:
      - dispatch_rule.py
    deny:
      - "**/__pycache__/**"

methodContext:
  instructions:
    - prompts/dispatch_rule_system_prompt.md
  references:
    - name: validation_cases
      path: cases/README.md
      description: Description of validation cases and metrics.
      type: markdown
      mimeType: text/markdown

metrics:
  source: return
  keys:
    - makespan
    - normalized_makespan
    - tardiness
    - utilization
    - feasible

outputFiles:
  - path: schedule_*.json
    name: schedules
    required: false

interface:
  label: Environment Dashboard
  description: Optional dashboard for inspecting cases and evaluator output.
  command: [bash, -lc, ./scripts/start_dashboard.sh]
  port: 5173
  cwd: .
  env:
    HOST: 0.0.0.0
  readyPath: /
  readyTimeoutSeconds: 90
```

## Target Method Example

This example shows the intended public model after the implementation checklist
is complete. It may not validate against the current schemas until method
workers and component setup land.

```yaml
apiVersion: optpilot.io/v1
config: method

id: llm-file-editor
description: Edit environment-provided files with an OpenAI-compatible model.
tags: [llm, files, code-edit]

runtime:
  sandbox: process
  workdir: .
  env:
    PYTHONUNBUFFERED: "1"
  envFromHost:
    - OPENAI_API_KEY
    - OPENAI_BASE_URL
  setup:
    steps:
      - uses: python-venv
        cwd: .
        requirements: [requirements.txt]
        installProject: true
    timeoutSeconds: 900

entrypoint:
  python: method:LLMFileEditorMethod
  pythonPath: [.]
  protocol: batch

settings:
  batchSize: 1
  model: openrouter/openai/gpt-5.4
  promptMessages:
    - role: system
      path: prompts/file_editor_system.md

accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
      - methodContext.instructions

interface:
  label: Method Trace Viewer
  description: Optional UI for inspecting prompts and generated edits.
  command: [bash, -lc, ./scripts/start_trace_viewer.sh]
  port: 5174
  cwd: .
  env:
    HOST: 0.0.0.0
  readyPath: /
  readyTimeoutSeconds: 90
```

## Target Study Example

This example shows the intended public model after the implementation checklist
is complete. Existing studies may still include legacy execution fields until
the catalog is migrated.

```yaml
apiVersion: optpilot.io/v1
config: study

name: dispatch-rule-llm-study
description: Use an LLM file-editing method on the dispatch-rule environment.
tags: [job-shop, llm, files]

environmentConfig: ../environments/dispatch_rule_env/environment.yaml
methodConfig: ../methods/llm_file_editor/method.yaml

objective:
  metric: normalized_makespan
  direction: minimize
  aggregation: mean
  secondaryMetrics:
    - makespan
    - tardiness
    - utilization

budget:
  maxTrials: 8
  maxWallClockSeconds: 3600
  maxFailures: 4

execution:
  parallelism: 2
  timeoutSeconds: 180
  retry:
    maxRetries: 1

evidence:
  level: full
  outputFileStorage: copy
  outputDir: runs

reproducibility:
  seed: 0
```

## Lightweight Resource Example

This manifest example is needed only for resources that require stable metadata
or a launchable interface. README-only resources do not need this YAML file.

```yaml
apiVersion: optpilot.io/v1
config: resource

id: devs-simulation-interface
name: DEVS Simulation Interface
description: Build discrete-event simulation projects from natural-language descriptions.
tags: [resource, simulation, frontend, devs]

interface:
  label: DEVS Interface
  description: Start the backend API and frontend, then open the UI.
  setup:
    steps:
      - uses: python-venv
        cwd: .
        requirements: [requirements.txt]
      - uses: npm
        cwd: devs_display/frontend
        install: ci
    env:
      PYTHONUNBUFFERED: "1"
      NPM_CONFIG_FUND: "false"
      NPM_CONFIG_AUDIT: "false"
    timeoutSeconds: 900
  command: [bash, -lc, ./scripts/start_interface.sh]
  port: 3000
  cwd: .
  env:
    HOST: 0.0.0.0
  envFromHost:
    - OPENAI_API_KEY
    - OPENAI_BASE_URL
  extraPorts:
    - 8000
  readyPath: /
  readyTimeoutSeconds: 420
```

## Required Implementation Changes

Before publishing this model as stable, implementation must match it:

- Done: update JSON schemas so every field above is accepted and every excluded
  field is rejected.
- Done: replace public `runtime.sandbox: host` with
  `runtime.sandbox: process`.
- Done: add `runtime.setup` for environment and method configs.
- Done: add `interface.setup` and `interface.envFromHost` for launchable
  interfaces.
- Done: remove public `execution.backend`, `execution.runtime`,
  `execution.resources`, `method.resourceProfile`, and public
  `method.produces`.
- Done: validate source-specific metric and record requirements with schema
  discriminators or equivalent semantic validation.
- Done: map public `records[].database` to the adapter path used for SQLite
  record extraction.
- Done: wire `execution.retry.maxRetries` to scheduler attempts as
  `maxAttempts = maxRetries + 1`.
- Done: resolve relative `evidence.outputDir` against the launch working
  directory or explicit output root, not the catalog study file.
- Done: enforce the runtime schema split: process runtimes may use
  `runtime.setup`; container runtimes use images/builds and container-only
  network policy.
- Done: ensure Studio's editable-copy and interface-launch paths can run
  declared setup on editable copies.
- Done: update Studio study YAML generation so it no longer emits public
  `execution.backend` or `execution.runtime` fields.
- Done: migrate the bundled example-package configs away from public
  `execution.backend` and public `method.produces`.
- Done: copy environment and method source into each run directory before
  execution.
- Done: run setup inside copied source and persist setup status under
  `.optpilot/setup-status.json` in that copied source for study launches.
- Done: rebase component-relative executable paths after source copy, while
  keeping opaque settings such as `evaluator.settings` under component control.
- Done: use setup fingerprints and `.optpilot/setup-status.json` to decide when
  setup can be reused instead of rerun.
- Done: store retry attempt artifacts under `trials/<trial>/attempt-*` and never
  overwrite earlier attempts.
- Done: run Python methods through a method worker process or container instead
  of importing them directly into the main OptPilot runner.
- Done: run environment evaluation through the declared environment runtime.
- Done: implement the documented `batch` and `session` method worker protocols.
- Done: enforce package-qualified catalog ids and reject duplicate component ids
  inside one package.
- Done: migrate older docs away from package-qualified imports,
  `runtime.sandbox: host`, and public `execution.backend`.

## Design Checklist

Before publishing a package, check:

- Catalog source contains curated source, configs, docs, and small sample data
  only.
- Process-runtime environment and method dependencies are declared in
  `runtime.setup`.
- Container-runtime dependencies are declared in the container image or
  `runtime.container.build`.
- Launch-only interface dependencies are declared in `interface.setup`.
- Setup commands are idempotent and write only inside the copied source.
- Interface commands start services only; they do not perform large installs.
- Python imports are config-local where possible.
- Process runtimes do not claim network isolation.
- Method-visible context is declared through `methodContext`, not hidden inside
  evaluator-only settings.
- Study files do not duplicate environment or method setup steps.
- Study files do not contain component-specific environment variables.
- Study files do not contain backend or resource-allocation fields.
- At least one study validates and runs from a fresh checkout.
- At least one Studio flow works: inspect source, create editable copy and
  install, or launch interface.
