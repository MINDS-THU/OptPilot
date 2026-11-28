# CLI reference

In a source checkout, prefix the commands on this page with `uv run`. Use `optpilot --version` to print the installed Core version and `optpilot experiment COMMAND --help` for a subcommand's arguments. Uppercase words below represent values to supply.

## Validate and inspect the schema

| Command | Result |
| --- | --- |
| `optpilot validate PATH [--json]` | Validates an existing file named `optpilot.experiment.yaml`, its referenced files and all its authored Studies in a temporary ledger. On success, writes a confirmation or a JSON object with `valid` and `path`. |
| `optpilot schema` | Writes the experiment manifest JSON Schema to standard output. |

## Address a project and ledger

All commands below begin with `optpilot experiment` and accept `--project DIR` and `--store DIR` after the subcommand. The project defaults to the current directory. The ledger defaults to `PROJECT/.optpilot/experiments`. A process bound to a Desktop project can inherit that project's ledger. An explicit `--store` takes precedence.

Experiment commands write JSON to standard output. Review returns an `id` for a frozen Study revision. Run returns an `id` for the execution and `reviewId` for its review. Copy the appropriate ID from the response when supplying a positional argument or flag.

## Discover and read versions

| Command | Result |
| --- | --- |
| `discover` | Reads the working project, saves versions, and lists them by group alongside authored Studies. |
| `save ARTIFACT_ID` | Returns the current saved version of an artifact from the working project. |
| `version VERSION_ID` | Returns the saved definition, dependencies, manifest digests and file metadata. Inline private Instance data is omitted. |
| `version-file VERSION_ID PATH` | Reads the exact saved path. Returns content for UTF-8 text up to 512 KiB, or an `unavailable` reason (`binary` or `too-large`). Includes size and SHA-256 in either case. |
| `graph` | Lists version summaries, derivations and Run summaries in the ledger. |

## Review and run

Review accepts exactly one selection source. Choose an authored Study, a YAML or JSON selection file, or a group of individual selection flags.

```sh
optpilot experiment review --project catalog/optpilot_tutorial --study toy_factory-search
optpilot experiment review --project catalog/document_search \
  --suite help-questions --candidate word-count --candidate bm25
```

| Selection options for `review` and `run` | Meaning |
| --- | --- |
| `--study ID` | Resolve an authored Study from the manifest. |
| `--selection FILE` | Read a selection mapping. Use this or an authored Study to set `timeoutSeconds`, `sshTarget` and `resourceBudgets`. |
| `--suite ID`, `--instance ID`, `--candidate ID` | Repeatable individual selection flags. Accept current artifact IDs or saved version IDs. |
| `--generator ID`, `--generation-budget N` | Select a native Generator and limit the number of generated Candidates. |
| `--metric NAME`, `--direction DIRECTION`, `--title TEXT` | Select a shared metric, ordering (`minimize` or `maximize`) and display title. |
| `--repetitions N`, `--seed N`, `--retries N` | Set the number of repetitions, Study seed and retry limit. |

`run` accepts the same three selection sources or an already frozen `--review REVIEW_ID`. With a new selection it performs review and execution together. Add `--run-id RUN_ID` to select an execution identity. Submit the original review and Run ID to continue waiting or interrupted work. If you omit `--run-id`, each invocation creates a new Run.

| Command | Result |
| --- | --- |
| `run --review REVIEW_ID [--run-id RUN_ID]` | Executes the frozen revision and returns Run detail. |
| `detail RUN_ID` | Returns Trial and Attempt records, aggregates, generation history, resource use and applicable external job details. |
| `cancel RUN_ID` | Requests cancellation and returns current status. Read detail for the terminal state or reconciliation reason. |

`run` exits with status 0 for `succeeded` and 1 for other returned states, including `waiting`. A nonzero status with valid Run JSON can therefore signal a resource wait or open work order. Command errors are written to standard error. [Run lifecycle](how-it-works.md)

## Register resources, plan and account for use

Supply a JSON file wherever `FILE` appears in the resource, manual and SSH commands below. These commands also accept `-` to read that JSON document from standard input. The resource registry is shared across projects for the current user. Set `OPTPILOT_RESOURCE_STORE` to a separate directory when isolating a registry.

| Command | Result |
| --- | --- |
| `resource-inventory` | Lists registered supply and its availability. |
| `resource-register FILE` | Creates or updates a supply. Required fields are `id`, `name`, `capability`, `unit`, `mode`, and `capacity`. Register `windowSeconds` for a usable windowed quota. |
| `resource-delete RESOURCE_ID` | Removes a supply from future allocation after active holds and outstanding usage are reconciled. |
| `resource-requirements ARTIFACT_ID EXPECTED_VERSION_ID FILE` | Saves the supplied requirements array for the current Task, Instance, Candidate or Generator. The expected version guards against concurrent edits. |
| `resource-plan REVIEW_ID` | Matches the reviewed requirements to available supply. Inspect `items`, `canStartNext`, `coverage`, `budgetIssues`, `coverageIssues` and `placement`. |
| `resource-link REVIEW_ID CLAIM_KEY CAPABILITY RESOURCE_ID [--claim-index N]` | Confirms that a registered supply provides the requested capability. Take the key and capability from the plan. Use its zero-based item index to disambiguate a repeated claim. |
| `run-resources RUN_ID` | Returns activities, allocations and observations for this Run. |
| `resource-observe RUN_ID OWNER KEY AMOUNT RECEIPT [--partial]` | Adds a usage measurement to the allocation's owner and key. Reuse the receipt to replay the same measurement. Supply `--partial` when further readings will follow. |

Supply records may carry `properties`, `location`, capability `aliases`, `owner`, a `note`, and an ISO 8601 `verifiedAt` timestamp with time zone. `access` is `allocate` (default) or `inspect`. `provider` is `manual` (default) or `managed`. The amount a Run can reserve depends on both its own budget and the registry's current supply. [Resource tutorial](resource-tutorial.md) · [Resource operations](operations.md)

## Manual measurements and remote workers

| Command | Result |
| --- | --- |
| `manual-tasks RUN_ID` | Lists the work orders for a manual Run. |
| `manual-claim RUN_ID TRIAL_ID OPERATOR` | Assigns an offered work order to the named operator. |
| `manual-complete RUN_ID TRIAL_ID METRICS_FILE RECEIPT [--evidence FILE]` | Records a claimed work order's metric mapping, measurement ID and optional JSON evidence. |
| `ssh-targets` | Lists registered native SSH worker hosts. |
| `ssh-register FILE` | Registers `id`, `name`, `host` (an SSH configuration alias) and optional `python` executable (default `python3`). |
| `ssh-probe TARGET_ID` | Checks remote Python using the SSH account. Returns a JSON readiness result and exits with status 1 when unavailable. |

## Import and refresh external results

| Command | Result |
| --- | --- |
| `inspect-results PATH` | Previews a Harbor job folder or exported JSON report, its source identifiers and content digest. |
| `import-results PATH --review REVIEW_ID --bindings FILE [--digest DIGEST]` | Attaches external evidence to the review using the JSON bindings file. Supply the preview digest to check the inputs at import time. |
| `refresh-results RUN_ID` | Re-reads original Harbor receipts for a terminal managed Harbor Run. Requires the job files in its ledger. |

[Bindings and report format](external-results.md) · [Manifest fields and defaults](configuration.md)
