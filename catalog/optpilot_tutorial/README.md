# Optpilot Tutorial

Open this directory in OptPilot Desktop. `optpilot.experiment.yaml` is the public definition; source files are frozen by Core when reviewed.

```sh
optpilot validate optpilot.experiment.yaml
optpilot experiment review --project . --study toy_factory-search
optpilot experiment run --project . --study toy_factory-search
```

The original simulator/solver implementation and provenance files remain in this package. Old environment/method/study YAML files are source references for retained-runtime regression, not another launch workflow.

The baseline and seeded random generator use the original deterministic toy-factory evaluator.
