# Local operation

Keep project files and `.optpilot/experiments` together. Desktop transcripts and settings live in Electron's user-data workspace (or `OPTPILOT_DATA_DIR`). Credentials use operating-system encryption or the selected agent's own login. Do not publish private profiles or authentication files.

Back up the closed application and project ledgers before replacing the app. A reviewed Study can be rerun from frozen versions even after source files change. Cancellation affects only the requested Run. Trusted local Python can access host resources; use a suitable external sandbox for untrusted submissions.

Harbor must be installed and Docker running. `OPTPILOT_HARBOR_EXECUTABLE` selects its absolute executable for CLI use. `OPTPILOT_HARBOR_CODEX_LOGIN=1` and `OPTPILOT_HARBOR_CLAUDE_SETTINGS=1` explicitly select local authentication. Failures and missing scores must be reported rather than replaced with baselines.

## Register experimental resources and run a reviewed evaluation

Declare resource requirements on a versioned Task, Instance, Candidate or Generator. For example, a candidate calling a licensed tool can use the following entry in its manifest.

```yaml
resourceRequirements:
  - capability: solver.concurrent
    mode: capacity
    unit: seat
    minimum: 1
```

Register the available supply once for all projects sharing this OptPilot registry. This example represents a concurrency allowance administered by the user. For consumables, `capacity` is the gross quantity supplied, including subsequent replenishments; confirmed withdrawals remain recorded. For quotas, specify `windowSeconds` for the fixed accounting window and put shared endpoints in the same pool. OptPilot can reserve its own allowance; check provider limits if other applications draw from that account.

Remove a supply from future plans with **资源 → 实验资源 → 删除** or `optpilot experiment resource-delete RESOURCE_ID`. Release active reservations and reconcile outstanding usage first. Completed evaluations retain their allocation and measurement records. An adapter that explicitly registers the device again can make it available to future plans.

On the same page, **运行项目命令** runs a script on a selected computer or configured backend. **作业记录** shows its status, logs and outputs. To compare candidate scores, open the evaluation in the project graph.

```sh
optpilot experiment resource-register - <<'JSON'
{"id":"lab-seat","name":"Lab solver allowance","capability":"solver.concurrent","mode":"capacity","unit":"seat","capacity":1}
JSON
optpilot experiment resource-inventory
optpilot experiment resource-plan STUDY_REVISION --project .
optpilot experiment run --review STUDY_REVISION --run-id RUN_ID --project .
optpilot experiment run-resources RUN_ID --project .
```

In the inventory, `owner` identifies the person or team responsible for the supply. Use `access: inspect` to register a source whose availability can be consulted. Allocation requires `access: allocate`. An optional `verifiedAt` timestamp records when a person confirmed the entry. In the desktop app, the **资源** toolbar button opens the inventory. **设置评测 → 运行计划** shows gaps and allows the user to register matching resources in place.

In the desktop editor, select a registered resource by name to require a particular pool, or choose a common requirement or enter a description such as “翻译评审员”. The review carries that requirement into the supply form. Enter the actual available total separately. When an existing pool matches the units and physical requirements, **使用已有资源** lets you confirm that the two names refer to the same capability. Future evaluations share the same balance. Experiment budgets can select requirements already declared by the chosen versions.

A Run in `waiting` can be waiting for a missing or occupied resource, a usage receipt or a human work order. Read the Run's reason. Repeat the run command with its original identifiers to continue. A capacity reservation is released after the work finishes. For stock used by a started attempt, record the actual amount against its allocation's owner and key. Reuse the same receipt when submitting a measurement again. A cancelled but unused reservation releases its hold. An uncertain withdrawal remains pending until reconciled. Run details preserve the evidence.

```sh
optpilot experiment resource-observe RUN_ID OWNER KEY 7 receipt-17
```

For evaluations performed by a person, set `runner: manual` on a Task with a parameter decision contract and declare its metrics. Inspect offered work, claim it under the operator name, and enter a JSON object with one numeric value for each declared metric. The original Trial retains the operator, receipt, readings and optional evidence. Continue with the same work order after reconnecting.

```sh
optpilot experiment manual-tasks RUN_ID
optpilot experiment manual-claim RUN_ID TRIAL_ID OPERATOR
optpilot experiment manual-complete RUN_ID TRIAL_ID metrics.json MEASUREMENT_ID --evidence evidence.json
```

For a trusted POSIX SSH worker, register a target with fields `id`, `name`, `host` (an SSH configuration alias) and `python` (default `python3`). Verify the host key and login using the account that runs OptPilot. Install the project's Python dependencies there. Run `optpilot experiment ssh-probe TARGET_ID`, then select the target when reviewing a native Python Study or set `sshTarget` in the Study. The remote job receives the chosen versions' frozen files and instance grading inputs. The local controller orchestrates the Study and each optional generator call. Submitted worker processes retain a job ID and their result in the remote account under `~/.local/share/optpilot/ssh-trials`. Resume the original Run after a connection interruption to collect the same receipt. Quota and usage estimates in the registry reflect work known to OptPilot.

Set `OPTPILOT_RESOURCE_STORE` to a directory to use a separate resource registry. On one machine Desktop and CLI otherwise share the user's default registry. A manual registry coordinates OptPilot participants using that same authority; external equipment calendars and third-party license usage require independent confirmation.
