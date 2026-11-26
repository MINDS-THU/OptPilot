# Or Solving

Open this directory in OptPilot Desktop. `optpilot.experiment.yaml` is the public definition; source files are frozen by Core when reviewed.

```sh
optpilot validate optpilot.experiment.yaml
optpilot experiment review --project . --study solve
optpilot experiment run --project . --study solve
```

The original simulator/solver implementation and provenance files remain in this package. Old environment/method/study YAML files are source references for retained-runtime regression, not another launch workflow.

The generator requires an actual COOPA installation (`COOPA_HOME`) and its model credentials. The score checks report completeness, not mathematical optimality; missing authentication is a failure, never a synthetic answer.
