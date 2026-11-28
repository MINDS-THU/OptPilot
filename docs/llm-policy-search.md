# Compare scheduling policies on a factory shift

A scheduling policy decides what to do when orders arrive, machines change state or an automated guided vehicle (AGV) becomes available. In `catalog/production_agv_scheduling`, the candidate receives a snapshot and returns commands. The grader runs the factory simulator, applies those commands and computes performance from the resulting shift. A policy proposed by an engineer or a coding assistant can enter the same comparison.

## Measure the supplied baseline

From the Core checkout, run the `smoke` Study. Its single case spans 30 simulated minutes at half-minute decision intervals, with equipment faults disabled. The package supplies its simulation runtime. This Study needs no model account.

```sh
uv run optpilot experiment run --project catalog/production_agv_scheduling --study smoke
```

Find `total_score` in the Trial's `metrics`. Its components track production efficiency, quality and cost, and AGV operation. In the shipped short shift, the baseline scored 4.3548 while `mean_order_completion` was 0. Inspecting `mean_device_utilization` and `mean_agv_score` helps explain how the score can rise before any order finishes. `evidence.report` contains the simulator's JSON report, including settings, the actual replication seed and its performance measures.

## Add a policy to the comparison

Make a working copy of `catalog/production_agv_scheduling`. Add `idle.py` at its root. This control sends no commands, which lets you measure what the factory does without dispatch decisions from the policy.

```python
def decide(operation, snapshot=None):
    if operation == 'reset':
        return None
    return []
```

Append the following entry to the existing `candidates` list in the copy's `optpilot.experiment.yaml`. Keep the supplied baseline entry.

```yaml
- id: idle
  name: Issue no scheduling commands
  format: program
  entrypoint: idle.py:decide
```

Set `PROJECT` to the copy's location. Run both policies on the same short shift and compare the two per-candidate records.

```sh
PROJECT=/path/to/your/factory-copy
uv run optpilot validate "$PROJECT/optpilot.experiment.yaml"
uv run optpilot experiment run --project "$PROJECT" --suite smoke \
  --candidate baseline --candidate idle --metric total_score
```

On the supplied short shift, the idle control scores 0 and the baseline scores 4.3548. Both have zero order completion. The difference comes from other components of the factory score.

To compare a policy of your own, change `decide('run', snapshot)` to return a list of command objects. Each command specifies a `line_id` and fields accepted by the simulator's command handler. The bundled policy in `environments/production_agv_scheduling/initial/scheduler.py` gives working examples. Declare any supporting Python files in the candidate's `files` list. Keep the selected cases and grader fixed when comparing revisions. [Candidate contracts](candidate-contracts.md)

## Test a different operating condition

The `shift-with-faults` instance spans 180 minutes and enables equipment faults. Use `--instance shift-with-faults` in place of `--suite smoke` to measure both candidates in that setting. Select `--repetitions` when you want more than one trial per candidate and case. Each Trial runs one simulation seed, which the grader records in its report. Desktop can open the factory viewer with a compatible Trial's settings for visual inspection. A new scored comparison requires a Run. [Desktop workflow](ui.md)

The supplied Study evaluates a fixed policy. To automate the design of new policies, add a program Generator with a generation budget and explicitly choose which Task metrics it can see in `feedbackMetrics`. [Generate and evaluate candidates](generate-and-optimize.md) · [Generator contract](methods.md)
