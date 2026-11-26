# OptPilot

OptPilot Core is a Python package and public CLI for versioned, measured experiments. OptPilot Desktop is a separately distributed client with private source. Both use the same project contract and experiment ledger. Define Tasks and Suites, evaluate fixed Candidates or feedback-driven Generators, compare measured results, and continue work in a coding-assistant conversation.

Desktop keeps conversations on the left and actual artifact/version/evaluation relationships on the right. All entry points share `optpilot.experiment.yaml` and the project ledger at `.optpilot/experiments`.

```sh
uv sync
uv run optpilot validate catalog/document_search/optpilot.experiment.yaml
uv run optpilot experiment run --project catalog/document_search --suite help-questions --candidate word-count --candidate bm25
```

To develop Desktop, use the private OptPilot-Desktop repository. Downloadable clients are intended to be published as assets on public OptPilot GitHub Releases after builds and acceptance checks pass. The Apple Silicon local trial bundles its own Python Core; other builds may need a matching Python environment. Coding assistants and Harbor use installed tools and their own authentication.

[Project website](https://minds-thu.github.io/OptPilot/) · [Technical documentation](https://minds-thu.github.io/OptPilot/reference/) · [Get started](docs/getting-started.md) · [Desktop](docs/ui.md) · [Experiment model](docs/concepts.md) · [Configuration](docs/configuration.md) · [Generators](docs/methods.md) · [Harbor results](docs/external-results.md) · [Development](AGENTS.md)

Official examples live under `catalog/`, with one versioned manifest per project. The former `optpilot run`, standalone Assistant, web Studio and work-card workflow are retired. Internal retained-runtime modules and old fixtures remain for regression; they are not a second public product.

Local Python runs trusted project code. Harbor delegates execution and grading to Docker. Passing fixtures does not qualify a real model run, and missing scores are never treated as successful zeros.
