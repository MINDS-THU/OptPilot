# Build a factory staffing experiment

Should a production line use four workers or six? That depends on the orders it has to fill. You will turn a small simulator into an OptPilot project, compare the two staffing levels on quiet and busy shifts, and inspect how the choice changes with demand.

Start in the root of the [Core checkout](installation.md). These commands use a POSIX shell. Keep the terminal in the checkout so `uv run` uses the installed Core.

## Connect the simulator to a grader

Create an empty project and copy the toy factory's existing production model into it.

```sh
mkdir my-factory
cp catalog/optpilot_tutorial/environments/toy_factory/evaluator.py my-factory/factory_model.py
```

The model calculates throughput and operating cost. Its `score` is throughput minus 0.55 times operating cost. Only staffing will vary between candidates. Save the following as `my-factory/grading.py`.

```python
from factory_model import evaluate as simulate


def evaluate(candidate, context):
    settings = {
        "workers": candidate["workers"],
        "buffer_capacity": 12,
        "mode": "balanced",
    }
    demand = context["environment"]["demand"]
    result = simulate(settings, {**context, "settings": {"demand": demand}})
    return {
        "metrics": result["metric_values"],
        "evidence": {"demand": demand, "settings": settings},
    }
```

OptPilot calls this function once per trial. The candidate supplies a worker count. The instance supplies demand. The grader records both alongside the metrics, so someone reviewing a score can see how the line was configured.

## Define the comparison

Save the following as `my-factory/optpilot.experiment.yaml`.

```yaml
environments:
  - id: production-line
    settings: {demand: 72}

tasks:
  - id: staff-line
    environment: production-line
    evaluator: grading.py:evaluate
    files: [factory_model.py]
    decisionContract:
      format: parameters
      schema:
        workers: {valueType: int, min: 2, max: 8}
    parameters:
      orders: {valueType: int, min: 1}
    environmentParameters: {demand: orders}
    metrics: [score, throughput, operating_cost]

instances:
  - id: quiet
    task: staff-line
    parameters: {orders: 45}
  - id: busy
    task: staff-line
    parameters: {orders: 72}

suites:
  - id: shifts
    instances: [quiet, busy]

candidates:
  - id: four-workers
    format: parameters
    value: {workers: 4}
  - id: six-workers
    format: parameters
    value: {workers: 6}

studies:
  - id: choose-staffing
    suiteIds: [shifts]
    candidateIds: [four-workers, six-workers]
    metric: score
    direction: maximize
```

`decisionContract` constrains the decision to an integer worker count. `environmentParameters` maps each case's `orders` to the simulator's `demand`. The grader entrypoint is captured automatically. Listing `factory_model.py` under `files` includes the imported simulator in the saved task version.

## Run both shifts

```sh
uv run optpilot validate my-factory/optpilot.experiment.yaml
uv run optpilot experiment review --study choose-staffing --project my-factory
```

Check that `plannedTrials` is 4. Copy the review's `id` into the next command in place of `STUDY_ID`.

```sh
uv run optpilot experiment run --review STUDY_ID --run-id staffing-01 --project my-factory
uv run optpilot experiment detail staffing-01 --project my-factory
```

For each trial, read `evidence.demand` and `evidence.settings.workers` to locate its score.

| Worker count | Score with 45 orders | Score with 72 orders | Mean score |
| ---: | ---: | ---: | ---: |
| 4 | 25.09 | 35.69 | 30.39 |
| 6 | 16.29 | 43.29 | 29.79 |

On the quiet shift, both candidates meet demand, so lower labor cost wins. On the busy shift, six workers deliver more orders. Four workers score slightly higher when those shifts count equally. If you expect mostly busy shifts, the per-case results give you a reason to reconsider how often each shift should occur in a representative evaluation.

To try automated proposals, open `catalog/optpilot_tutorial/optpilot.experiment.yaml`. Its `toy_factory-search` study starts with one fixed candidate and gives a seeded random generator a budget of five proposals. That example opens three decisions to search, including buffer capacity and operating mode. [Candidate generators](methods.md) · [Manifest reference](configuration.md)
