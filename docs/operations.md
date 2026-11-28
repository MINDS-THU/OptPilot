# Run and resource operations

Use these procedures when a run is waiting, a lab resource needs to be shared, or an operator needs to record a measurement. The commands assume you are in the directory containing `optpilot.experiment.yaml` and have the Core CLI installed. From a uv checkout, prefix each command with `uv run`. Replace uppercase identifiers with the IDs from your own review, run, work order or allocation.

## Continue or cancel a run

```sh
optpilot experiment detail RUN_ID
```

Look at `state`, `error`, `reviewId` and the latest attempt for the affected trial. A waiting run can need an instrument, a receipt, an operator or a remote worker. `experiment run` exits with status 1 when the run returns in a state other than `succeeded`, including `waiting`. The run's JSON record explains what to do next.

Once the stated condition is satisfied, resume with both original identifiers.

```sh
optpilot experiment run --review STUDY_ID --run-id RUN_ID
```

Completed trials retain their results. To repeat the comparison from the beginning, use the same review and omit `--run-id` to get a new run. To stop work on a particular run, use `optpilot experiment cancel RUN_ID` and check its detail. Cancellation is terminal once confirmed.

## Register a shared supply

Suppose a group has one concurrent solver license. The versioned task or candidate using it needs this manifest entry.

```yaml
resourceRequirements:
  - capability: solver.concurrent
    mode: capacity
    unit: seat
    minimum: 1
```

Register the group's actual allowance under a stable pool ID. The following POSIX-shell example reads the registration as JSON from standard input. You can also save the JSON in a file and pass its path to `resource-register`.

```sh
optpilot experiment resource-register - <<'JSON'
{
  "id": "lab-solver",
  "name": "Lab solver license",
  "capability": "solver.concurrent",
  "mode": "capacity",
  "unit": "seat",
  "capacity": 1,
  "access": "allocate",
  "owner": "Optimization lab"
}
JSON
optpilot experiment resource-inventory
```

Pick the mode that matches how supply becomes available again.

| Mode | Example | How to maintain it |
| --- | --- | --- |
| `capacity` | A solver seat or instrument | Register the number that can be used at once. The reservation is released when the work finishes. |
| `consumable` | Material stock | Register the gross amount supplied. Add replenishments to that total. Receipted withdrawals reduce availability. |
| `quota` | An allowance per hour | Register capacity in the provider's units. Use `windowSeconds` for a fixed accounting window. Endpoints sharing one allowance should draw from the same pool. |

`access: inspect` makes a supply visible for consultation. `access: allocate` authorizes reservations from the pool. Projects using the same registry coordinate with one another. When other software or people also use a device, the owner or adapter must account for them. The [calibration tutorial](resource-tutorial.md) shows an adapter synchronizing the registry with the device itself.

To withdraw an offer, use `optpilot experiment resource-delete RESOURCE_ID` after resolving its held or pending allocations. Completed runs retain the allocation history. An adapter that registers the device again can make it available for new plans.

## Plan and reconcile an allocation

Review the study after saving its requirements. Inspect its plan before you start the run.

```sh
optpilot experiment resource-plan STUDY_ID
```

`canStartNext` describes whether the next planned work can start. Inspect `items` and `coverageIssues` for claims and gaps across the study. A projection for the whole study can exceed the remaining inventory even if the next trial fits. A preview makes no reservation. At execution time the registry checks the supply again.

If a requirement has a matching resource under a different capability name, verify that it is the same pool, with compatible units and properties. The plan can return `linkCandidates`. To confirm a match, pass the item's `key`, `capability` and the selected resource ID.

```sh
optpilot experiment resource-link STUDY_ID CLAIM_KEY CAPABILITY RESOURCE_ID
optpilot experiment resource-plan STUDY_ID
```

Use `--claim-index N` when the same key and capability occur several times. In Desktop, open **资源** (Resources) to inspect supply. The run plan under **设置评测** (evaluation setup) can register a missing supply or link an existing one.

Read the allocations and observations after a run has started. For an outstanding consumable or quota measurement, take the `owner` and `key` from the relevant allocation. Here the recorded withdrawal is seven units and `receipt-17` is the source measurement's identifier.

```sh
optpilot experiment run-resources RUN_ID
optpilot experiment resource-observe RUN_ID OWNER KEY 7 receipt-17
```

Use `--partial` while more readings are due. Consumption readings are increments, so each additional withdrawal needs its own receipt. Resubmitting an identical receipt is safe. A final zero records that no further quantity was used. A started allocation whose withdrawal remains unknown stays pending. If a run was waiting for the reading, resume it using its original review and run IDs.

## Submit a human measurement

For a task with `runner: manual`, a parameter candidate and declared metrics, starting the run offers a work order. Locate the trial, inspect the instructions and claim it under the operator's name.

```sh
optpilot experiment manual-tasks RUN_ID
optpilot experiment manual-claim RUN_ID TRIAL_ID "Lab operator"
```

Create `metrics.json` with a finite number for every metric listed in the order. For a task that declares only `latency_ms`, a reading could be `{"latency_ms": 42.5}`. Use an identifier from the measurement as the receipt. Include a JSON evidence file when you have supporting details.

```sh
optpilot experiment manual-complete RUN_ID TRIAL_ID metrics.json MEASUREMENT_ID --evidence evidence.json
optpilot experiment manual-tasks RUN_ID
```

Completion records the operator, receipt and readings on the trial and advances the run. After a disconnect, look up the same work order. Submitting the identical measurement again returns its recorded state.

## Use an SSH worker

For native Python grading on a trusted POSIX computer, configure its SSH alias, verify its host key and arrange non-interactive login from the controller. Install the grader's dependencies in the target's Python environment. Save `target.json` with the following fields, substituting your SSH configuration alias.

```json
{"id":"lab-worker","name":"Lab worker","host":"lab-node","python":"python3"}
```

```sh
optpilot experiment ssh-register target.json
optpilot experiment ssh-probe lab-worker
```

When the probe returns `"ready": true`, add `sshTarget: lab-worker` to the authored study and review it, or select the registered target during Desktop evaluation setup. The controller sends the selected frozen files and grading inputs, including any grader-private instance data, to the worker. The controller and generator stay on the local machine. If the connection drops, reconnect and resume the same run so Core can collect the existing remote job's receipt. Remote job data lives under `~/.local/share/optpilot/ssh-trials` in the worker account.

## Back up project and resource records

The default project ledger lives in `.optpilot/experiments` inside the project directory. When you choose `--store DIRECTORY`, use that directory for all later commands that read or resume its runs. The resource registry is shared separately. Choose it with `OPTPILOT_RESOURCE_STORE` or use the per-user default.

| Platform | Default resource registry |
| --- | --- |
| macOS | `~/Library/Application Support/OptPilot/experiment-resources` |
| Linux | `$XDG_DATA_HOME/OptPilot/experiment-resources`, usually `~/.local/share/OptPilot/experiment-resources` |
| Windows | `%LOCALAPPDATA%\OptPilot\experiment-resources` |

If `OPTPILOT_DATA_DIR` is set, its `experiment-resources` subdirectory supplies the default. An explicit `OPTPILOT_RESOURCE_STORE` takes precedence.

Stop active controllers and close Desktop before making a filesystem backup. Copy the project and its complete ledger, the registry used by those runs, and any external stores or device receipts needed to interpret them. To preserve Desktop conversations, include its user-data workspace. Restore the corresponding directories together and use `experiment detail RUN_ID` to inspect a known run before starting new work.

Local Python executes with the permissions of the current user. Use it for trusted projects. For agent evaluations that require a sandbox, choose a Harbor task and configure its environment and credentials. [Execution options](capabilities.md) · [CLI reference](cli.md)
