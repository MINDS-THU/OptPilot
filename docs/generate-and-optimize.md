# Generate and improve

Use a fixed baseline and Candidate Generator in one Study. The Task explicitly lists feedback metrics. Each proposed program is frozen and evaluated before the next generator call. See [Generators](methods.md) for the protocol and [Evidence](evidence.md) for retained feedback.

## Which route to use

Search with measured feedback when you can execute and grade a policy, but cannot express its objective and constraints as a tractable mathematical model. Use the [OR solver](or-solving.md) when the decisions, objective and constraints can be stated precisely. A solver's reported objective is not a proof of feasibility or optimality; add an independent Task grader when those claims matter.

In `catalog/or_solving/optpilot.experiment.yaml`, fill the Task's `observations.problem` (`problem` in the generator context) and model before reviewing Study `solve`. For example:

```text
Minimise the total cost of shipping five units to a customer.
Decide how many units to send by each of two routes: route A costs 2 per unit,
route B costs 3. Constraints: route A carries at most 3 units, route B at most 4;
ship exactly 5 units, with non-negative integer quantities.
```

Run `optpilot experiment review --study solve --project catalog/or_solving`, then start the returned Study revision through the CLI or choose the same versions in Desktop. The COOPA generator calls an actual configured model; credentials and its dependencies must be installed separately. The bundled grader records answer completeness, not mathematical correctness. The [OR guide](or-solving.md) describes that limit.
