# Share a colorimeter between trials

Two display calibrations need the same instrument. What happens if another researcher has already reserved it? You will run an offline experiment against a simulated colorimeter, observe a run wait for a seat, and trace one score back to the instrument reading that produced it.

This tutorial requires macOS or Linux and the [Core checkout](installation.md). The instrument runs as a local process. Open a fresh shell in the checkout's root and keep the commands in that shell.

## Identify the resource and the experiment

Make a working copy. The adapter writes a local instrument database, connection settings and a report into this copy.

```sh
bench_project="$(mktemp -d)/display_calibration"
cp -R catalog/display_calibration "$bench_project"
uv run python "$bench_project/demo.py" --prepare
```

The adapter reads the simulated instrument's status over MCP and registers the usable supply. The instrument has **two physical channels** and a **one-seat team limit**. Its registered capacity reflects the team's allowance and occupancy by other users. OptPilot tracks reservations against that supply.

The example uses its own registry. In this shell, select it and inspect the instrument.

```sh
export OPTPILOT_RESOURCE_STORE="$bench_project/.demo/registry"
uv run optpilot experiment resource-inventory
uv run optpilot experiment review --project "$bench_project" --study calibration
```

Look for `id: demo.display.colorimeter`, `mode: capacity`, `unit: seat` and `available: 1` in the inventory. The study compares two sets of RGB gains at two gray levels. The task requires one seat per attempt, and the study budget permits one seat at a time. For a preview, replace `STUDY_ID` below with the review's `id`.

```sh
uv run optpilot experiment resource-plan STUDY_ID --project "$bench_project"
```

The plan indicates whether work can start with the current supply. The actual reservation happens when the work is admitted. In this project the grader also requests a lease from the instrument before measuring.

## Observe contention and recovery

```sh
uv run python "$bench_project/demo.py" --step
```

The driver prints prompts in Chinese. At the first prompt, press **Enter** to simulate a colleague reserving the team seat. The instrument refuses a second reservation. The adapter refreshes the registry, and the OptPilot run enters `waiting` with zero completed trials. Although one physical channel is still free, both users share the same team allowance.

The colleague then releases the seat. At the second prompt, press **Enter** to resume the original run and take four measurements. The driver saves its human-readable report in `演示报告.md` and its receipts in `演示结果.json`. For a noninteractive run, use `--auto` in place of `--step`.

## Follow a score to its measurement

The completed report should show the following values, rounded to three decimals. `rgb_mae` is the mean absolute difference between the three measured channels and their target values. Smaller is better.

| Channel gains | Error at gray 64 | Error at gray 192 | Mean error |
| --- | ---: | ---: | ---: |
| Factory settings | 4.480 | 13.440 | 8.960 |
| Compensated settings | 0.150 | 0.453 | 0.302 |

The source project selected the compensated gains using the simulated panel's known bias. The measurements describe this panel at these two gray levels.

Copy the report's `runId` into these commands in place of `RUN_ID`.

```sh
uv run optpilot experiment run-resources RUN_ID --project "$bench_project"
uv run optpilot experiment detail RUN_ID --project "$bench_project"
uv run optpilot experiment resource-inventory
```

1. In `allocations`, find a one-seat allocation for `demo.display.colorimeter`. Each completed attempt has a released allocation. The inventory shows one available seat again.
2. In `trials`, find the factory settings with `evidence.target` equal to `[64, 64, 64]`. Its readings are `[70.4, 60.16, 67.2]`. The absolute errors average to 4.480.
3. In that trial's `evidence`, match the `leaseId` in `reservation`, `measurement` and `release`. The measurement also has a `readingId`. Those device receipts explain the RGB score. The resource allocation explains who held the seat while the measurement took place.

If you move the copy, run `--prepare` again to update the instrument connection for new reviews. Close the tutorial shell to return to your usual registry settings. To register lab supplies by hand, reconcile withdrawals or inspect a waiting run, see [Run and resource operations](operations.md).
