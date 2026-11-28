# Run COOPA and inspect its solution report

Use `catalog/or_solving` when you can describe the decision variables, objective and constraints of an operations research problem. The `solve` Study asks COOPA to formulate the problem and produce an answer. OptPilot saves the resulting report as a generated candidate, then grades the completeness of that record.

## Configure the solver

From the Core checkout, install the project's pruned COOPA dependencies into the Python environment that runs Core. Set `COOPA_HOME` to an absolute path so the generator can import COOPA while executing in its temporary workspace. The repository includes a COOPA source tree you can use.

```sh
uv sync
export COOPA_HOME="$PWD/catalog/or_solving/methods/coopa_solver/coopa_home"
uv pip install -r catalog/or_solving/methods/coopa_solver/requirements-pruned.txt
```

Set the credentials for the model named in the Task's `observations.model` in the launching process. The shipped model uses OpenRouter (`OPENROUTER_API_KEY`). Use an account with access to that model, or change `model` to one available to you. COOPA invokes a live provider and may generate and execute solver code. Install any solver executable needed by your chosen backend, for example GLPK for a Pyomo model that selects GLPK.

## State the problem and review it

Edit `observations.problem` in a working copy of `catalog/or_solving/optpilot.experiment.yaml`. Include the objective direction, constraints, units and domain of each decision variable. The shipped example maximizes `3x + 2y` subject to `x + y ≤ 4` and `x, y ≥ 0`. Its optimum can be checked by hand, which makes it suitable for an initial review.

Set `PROJECT` to the folder containing your manifest, then run the review from the Core checkout.

```sh
PROJECT=catalog/or_solving
uv run --no-sync optpilot validate "$PROJECT/optpilot.experiment.yaml"
uv run --no-sync optpilot experiment review --project "$PROJECT" --study solve
```

Check that the review shows `metric` set to `solved`, a generation budget of one and one planned trial. Copy its `id` into `REVIEW_ID` below. Starting this step calls the configured provider.

```sh
uv run --no-sync optpilot experiment run --project "$PROJECT" --review REVIEW_ID
```

## Read the answer and its evidence

Look at `generations` first. A successful call lists a generated candidate version. Inspect that version to read `definition.value.report_json`, the JSON text containing the problem, model, routing, agent response and extracted answer. The report can include a formulation, confidence information and generated files, subject to size limits.

```sh
uv run --no-sync optpilot experiment version CANDIDATE_VERSION_ID --project "$PROJECT"
```

The `or-report` grader reads the submission. `solved = 1` means it found the expected report fields, a reported prediction and a finite numerical answer. The reported number appears in `objective_value`. A value of zero can also be the fallback when no answer was found, so inspect `answer_found`, `solved` and the report together. A failed generator call has its own error record.

For the shipped problem, `3x + 2y = 3(x + y) − y ≤ 12`, and `(x, y) = (4, 0)` attains 12. This independent bound and feasible assignment establish optimality for this particular problem. For another problem, supply a feasibility check, a known solution or a suitable optimality certificate if the conclusion depends on it. To compare policies through simulation, use the search guide. [Generate and evaluate candidates](generate-and-optimize.md)
