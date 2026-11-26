# OptPilot

OptPilot Core is a Python package and public CLI for versioned, measured experiments. OptPilot Desktop is a separately distributed client with private source. Both use the same `optpilot.experiment.yaml` project contract and `.optpilot/experiments` ledger, so saved artifact versions and results can be inspected from either entry point.

The project model separates Environment behavior, Task grading contracts, concrete Instances, Suites, Candidates, optional feedback-driven Generators, reviewed Studies, Runs, Trials and Attempts. Review freezes the exact selected versions before execution. A Run retains actual inputs, scores, failures and evidence.

[Install](installation.md) · [First experiment](getting-started.md) · [Desktop](ui.md) · [Configuration](configuration.md) · [External results](external-results.md)
