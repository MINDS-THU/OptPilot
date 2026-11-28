# Experiment manifest reference

Place `optpilot.experiment.yaml` in the project root. The CLI and Desktop both read this file. It contains JSON-compatible values written in YAML. Run these commands from the checkout to print the schema and validate a project, including its file references, dependency versions and authored Studies.

```sh
uv run optpilot schema
uv run optpilot validate catalog/document_search/optpilot.experiment.yaml --json
```

## Top-level fields and saved files

`environments`, `tasks`, `instances`, `suites`, `candidates`, `generators` and `studies` are arrays. Omit unused groups. The optional project metadata fields are `apiVersion`, `kind`, `name` and `description`. The schema rejects unknown top-level fields.

The first six groups contain versioned artifacts. Each artifact requires an `id` unique across those groups. Optional common fields are `name` (defaults to the ID), `description` and `files`. An authored Study has its own ID and fields described below.

`files` lists individual paths relative to the manifest. Use forward slashes and regular files inside the project. Absolute paths, parent traversal and symlinks are rejected. The limit is 32 MiB per file and 128 MiB of declared files per artifact. Python evaluator and callable entrypoint files are included automatically. Declare local helper modules and data explicitly. A native grader combines Environment, Task and Instance files in one workspace. Keep distinct content at distinct paths there. The candidate program gets its own files in a separate directory. Third-party Python packages must be installed on the machine executing that code.

## Environments and graphical interfaces

| Field | Meaning |
| --- | --- |
| `settings` | Object containing reusable environmental values, available to the native grader in `context["environment"]`. |
| `entrypoint` | Optional callable reference in `file.py:function` form. Its file is saved with the Environment. The Task's `evaluator` selects the callable to run for grading. |
| `interface` | Optional Desktop graphical viewer definition. Requires a Python **script path** as its `entrypoint`. |

A viewer may set `title`, `path` (local page) and `readyPath` (local health check). Both paths default to `/`. The script receives `--context` with the path of the JSON launch context and listens on loopback at `OPTPILOT_INTERFACE_PORT`. Use `interface.files` for viewer assets and `filePrefixes` to select groups of files already saved with the Environment. List the Environment setting names the viewer needs in `interface.settings`.

`defaultSeed` sets the visual session's default. Set `supportsTrialSettings: true` for a viewer that can honor the recorded public Trial parameters, settings and seed. Desktop can offer those original settings for a compatible completed execution. The graphical session produces no scored Trial. The AGV scheduling project includes a working example. [Example projects](catalog.md)

## Tasks

A Task requires `id`, `decisionContract` and a nonempty `metrics` list. Execution fields depend on the runner.

| Field | Type and use |
| --- | --- |
| `runner` | Omit for local trusted Python, or use `native` or `trusted-local`. Use `manual` for an operator and `harbor` for a Harbor job. |
| `evaluator` | Required for native Python. Callable reference such as `grading.py:evaluate`. |
| `harborTask` | Required for Harbor. Relative path to the local task package containing `task.toml` and `instruction.md`. Core saves the package files. |
| `decisionContract` | Required `format` of `parameters`, `program` or `agent`. Optional `description`. A parameter contract can supply a typed `schema`. A program can supply `input` and `output` JSON Schemas. [Candidate contracts](candidate-contracts.md) |
| `metrics` | Distinct names as strings or objects with required `name` and optional `description`, `unit`, `direction`. Direction accepts `minimize` or `maximize`. |
| `feedbackMetrics` | Subset of declared metric names a Generator can receive. Defaults to an empty list. |
| `parameters` | Definitions for **case** parameters. Instance values are validated against these definitions. |
| `environment`, `environmentParameters` | Environment artifact ID and an optional mapping from Environment setting names to declared Task parameter names. At execution, the concrete Instance values override those settings. |
| `observations` | Public JSON-compatible description or data exposed to a Generator in the Task context. |
| `resourceRequirements` | Resource claims for work on this Task. See [resource fields](#resource-requirements-and-budgets). |
| `isolation` | Optional native setting accepting `trusted-local` or `none`. Native execution uses trusted Python processes. |

A Harbor Task uses an agent contract and keeps its environment, task settings and grading in the Harbor package. Omit native fields such as `evaluator`, `parameters`, `environment`, `environmentParameters`, `feedbackMetrics` and `isolation`. A manual Task accepts a parameters contract and obtains metrics from the operator. Omit `evaluator`, `harborTask` and `isolation` for manual work.

## Parameter definitions

The same typed definitions apply to Task parameters and to `decisionContract.schema` for parameter Candidates. Each named parameter requires a `valueType`. Supply all declared names that lack a `default`. Undeclared names in a value mapping are rejected. Definitions may include a `description` and `unit`.

| `valueType` | Value and constraints |
| --- | --- |
| `int`, `float` | Numeric value with optional inclusive `min` and `max`. A float parameter also accepts integers. Booleans have their own type. |
| `bool`, `string` | A boolean or a string. A string can use `pattern` for a regular expression search. |
| `categorical` | One member of the supplied `values` array. |
| `array` | A list with optional `minItems`, `maxItems` and a recursive `items` definition. |
| `object` | A mapping with optional recursive `properties` and a `required` list. Declaring properties restricts the keys to those names. |

## Instances and Suites

An Instance requires `id` and `task`. Optional `parameters` set the case values. Optional `private` holds grader-only data such as reference answers. An Instance may also declare `files` and `resourceRequirements`. A Harbor Instance obtains its case data from the Task package. Put grading inputs there.

`visualSeed` stores a visual preset seed in the range 0–9007199254740991. Study repetitions use the reviewed Study seed. A Suite requires `id` and a nonempty `instances` list of distinct Instance IDs. Saving the Suite records the corresponding Instance versions.

## Candidates and Generators

| Group and format | Required fields beyond `id` | Optional fields of interest |
| --- | --- | --- |
| Candidate, `format: parameters` | `value` object | `resourceRequirements` |
| Candidate, `format: program` | `entrypoint` callable | `files`, `resourceRequirements` |
| Candidate, `format: agent` | `agent` object with `name` | Model and runner settings under `agent`, plus `resourceRequirements`. Agent configurations accept no attached files. |
| Generator | `entrypoint` callable | `files`, `resourceRequirements`, `outputFormat` (`value` or `candidate`) |

[Candidate and grader contracts](candidate-contracts.md) · [Generator inputs and outputs](methods.md)

## Studies

An authored Study requires an `id` unique within `studies`. Choose it with `experiment review --study ID`. An independent `--selection FILE` takes the same selection fields and omits `id`. References may use current artifact IDs or immutable version IDs already in the ledger.

| Field | Rule |
| --- | --- |
| `title` | Optional display name. Defaults to `Evaluate candidates`. |
| `suiteIds`, `instanceIds` | Select at least one case through either or both lists. Overlapping Instances are counted once per version. |
| `candidateIds`, `generatorId` | Select fixed Candidates, one native Generator, or both. Fixed Candidates must satisfy every selected Task. |
| `generationBudget` | Maximum new Candidates, 1–10000. Requires a Generator. Defaults to 1 when a Generator is selected. |
| `metric`, `direction` | A selected metric must exist on every selected Task. By default, Core uses the first Task's first shared metric if one exists, with its declared direction or `maximize`. A mixed-Task Study can retain per-Task measurements even if no metric is common to all Tasks. |
| `repetitions`, `seed` | 1–10000 repetitions (default 1). Study seed 0–9007199254740991 (default 0). Harbor owns its randomness and accepts only the default seed value. |
| `retries`, `timeoutSeconds` | 0–20 additional attempts (default 0). Native per-call timeout 1–86400 seconds (default 60). Manual work requires zero retries. Harbor handles retries and applies its own timeout settings. |
| `sshTarget` | Registered target ID for native Python trial execution. |
| `resourceBudgets` | Per-capability limits under the reviewed Study. |

A Study uses one runner. Selecting a Generator requires native Python Tasks with a common Candidate format. Review accepts at most 100000 planned Trials, including the generation budget.

## Resource requirements and budgets

A Task, Instance, Candidate or Generator may declare up to 128 `resourceRequirements`. Each claim requires `capability`, `unit` and `mode`. A reusable instrument uses `capacity`, stock uses `consumable`, and an allowance that renews in a fixed window uses `quota`.

```yaml
resourceRequirements:
  - capability: demo.display.colorimeter
    resourceId: demo.display.colorimeter
    unit: seat
    mode: capacity
    minimum: 1
```

Use `maximum` to authorize an upper bound. For capacity, `minimum` alone can specify the reservation. Consumables and quotas need a known upper bound. An optional `expected` amount must fall within declared bounds and serves as an estimate. Alternatively, `parameter` takes the exact amount from a numeric Instance parameter. Use it on Task, Instance or Candidate claims and omit the fixed quantities.

Optional `resourceId`, `location` and `properties` narrow the eligible supplies. The registry supports `constraints.sameLocation` and `constraints.distinctResources` with a shared group name for related claims on the same activity. Other relationships can yield an unsupported plan. Managed Harbor jobs reserve at job scope and cannot verify per-Trial relationships.

A Study accepts up to 128 `resourceBudgets`, each containing `capability`, `unit` and a nonnegative `limit`. Each capability/unit pair may appear once. For capacity the planner checks simultaneous demand. For consumables and quotas it accounts for cumulative demand. Requirements and budgets describe the experiment. Register the actual supply in the shared registry before execution. [Resource tutorial](resource-tutorial.md) · [Resource commands](cli.md)
