# Production Agv Scheduling

Open this directory in OptPilot Desktop. `optpilot.experiment.yaml` is the public definition; source files are frozen by Core when reviewed.

```sh
optpilot validate optpilot.experiment.yaml
optpilot experiment review --project . --study smoke
optpilot experiment run --project . --study smoke
```

The original simulator/solver implementation and provenance files remain in this package. Old environment/method/study YAML files are source references for retained-runtime regression, not another launch workflow.

The grader owns the simulator. The program candidate returns scheduling commands from public snapshots. Edit the policy source and save a new candidate version before comparing on the same suite. The trial retains simulator KPI evidence.
