# Use measured results to generate candidates

A fixed comparison can tell you which of the candidates you supplied scored highest. A search also has to decide what to try next. In OptPilot, the Task defines the measurement and the Generator makes that decision. You can change the search strategy while keeping the same cases, baseline and grader.

## Start with a baseline and a budget

From the Core checkout, run the toy factory's search. It needs only Core.

```sh
uv run optpilot experiment run --project catalog/optpilot_tutorial --study toy_factory-search
```

The baseline uses five workers, a buffer of twelve and balanced operation. The grader measures throughput and operating cost at a demand of 72. The first Trial produces a throughput of 67.1 and a cost of 44.2. Its score is `throughput - 0.55 × operating_cost`, or 42.79 to two decimal places.

The generation budget of five counts **new candidates**. With one instance and one repetition, the baseline plus five proposals gives six trials. In `generations`, the first call contains the baseline's metrics in `input.feedback`. Later calls contain measurements from the earlier proposals too. Each call lists the candidate versions it produced. The bundled random search uses the parameter ranges and a seed to choose values, even though the scores are available to it.

## Try a feedback-driven proposal

Work in a copy of `catalog/optpilot_tutorial`. In that copy, replace the contents of `generate.py` with the following function. It first tries one additional worker. It then takes the higher-scoring configuration and tests whether switching it to efficient operation helps.

```python
def propose(context):
    measured = context['feedback']
    if not measured or context['generatedCount'] >= 2:
        return []

    best = max(measured, key=lambda row: row['metrics']['score'])
    proposal = dict(best['value'])
    if context['generatedCount'] == 0:
        proposal['workers'] += 1
    else:
        proposal['mode'] = 'efficient'
    return proposal
```

Run the same command with `--project` pointing to your copy. The Task makes `score`, `throughput` and `operating_cost` available to this generator through `feedbackMetrics`. Inspect the second call's feedback and compare its chosen parent configuration with its output. With the supplied model, adding a worker raises the score from 42.79 to 43.29. The generator therefore tries efficient operation with six workers and measures 44.418. It returns an empty list after those two proposals, ending the Run after three scored trials. A new review captures the changed generator code. [Generator contract](methods.md) · [Read a Run](evidence.md)

## Which route to use

Use measured search when you can run a candidate and measure its consequences, as in the factory simulation. If you can express the decisions, objective and constraints explicitly, you can instead supply that problem to COOPA. Moving from a simulator to a mathematical formulation requires choosing which physical constraints and costs to model.

For example, put this statement in the `problem` field of the `or-report` Task's `observations` in `catalog/or_solving/optpilot.experiment.yaml`.

```text
Minimise the cost of shipping five units to a customer.
Decide how many units go by each route. Route A costs 2 per unit and route B costs 3.
Constraints: route A carries at most 3 units, route B at most 4.
Ship exactly 5 units and use non-negative integer quantities.
```

Review your saved problem with `uv run optpilot experiment review --project catalog/or_solving --study solve`. The OR guide explains the runtime prerequisites and how to check the resulting answer independently. [Inspect a COOPA solution](or-solving.md)
