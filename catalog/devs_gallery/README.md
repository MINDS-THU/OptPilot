# Devs Gallery

Open this directory in OptPilot Desktop. `optpilot.experiment.yaml` is the public definition; source files are frozen by Core when reviewed.

```sh
optpilot validate optpilot.experiment.yaml
optpilot experiment review --project . --study abp-search
optpilot experiment run --project . --study abp-search
```

The original simulator/solver implementation and provenance files remain in this package. Old environment/method/study YAML files are source references for retained-runtime regression, not another launch workflow.

The ABP and SEIRD tasks require the declared xDEVS dependencies. Their two suites have different metrics and should be evaluated separately.
