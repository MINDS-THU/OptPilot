# OptPilot

An optimization score is useful only when you can tell what produced it. The candidate, objective, evaluation cases, metric and run conditions all shape the result. Change one of them and a score may no longer support a fair comparison.

OptPilot makes those conditions explicit and keeps them with the observations, failures and evidence from each run. Review freezes the selected project versions before execution. The resulting record lets you inspect what was measured, compare candidates under shared conditions, and resume interrupted work without losing completed trials.

OptPilot Core is an open-source Python package and CLI for versioned experiments. Define an evaluation in `optpilot.experiment.yaml`, then run it with the CLI or the separately distributed OptPilot Desktop client. Both use the same project contract and local experiment ledger.

```sh
uv sync
uv run optpilot validate catalog/document_search/optpilot.experiment.yaml
uv run optpilot experiment run --project catalog/document_search --suite help-questions --candidate word-count --candidate bm25
```

The document-search example compares two local ranking candidates on the same five questions. It shows how OptPilot records an evaluation; it is not evidence about a live assistant or service.

## What OptPilot records

- The exact saved versions selected for evaluation, so later source edits do not silently change a reviewed experiment.
- Per-trial inputs, metric values, failures and supporting evidence. A measured zero remains distinct from a missing score or failed attempt.
- Repetitions, retries and generated candidates under the same reviewed conditions. An interrupted run can resume with its original identity and completed trials.
- Resource requirements, reservations and usage receipts when an experiment needs registered resources.

These records make comparisons inspectable. They do not make an evaluator correct by themselves. The project author is responsible for choosing representative cases and a grading contract that measures the intended objective.

## Core and Desktop

The public repository contains the Python Core, CLI, schemas, examples and technical documentation. Desktop is a separately distributed client with private source. Installers for macOS, Windows and Linux are published as assets on the public [GitHub Releases](https://github.com/MINDS-THU/OptPilot/releases) page when a release is ready. Consult the release notes for signing and platform-specific installation status.

Local Python execution runs trusted project code and is not a hostile-code sandbox. Harbor can delegate agent execution and grading to Docker. External results can be imported with explicit bindings to reviewed versions, while retaining their original job and trial identities.

## Learn more

[Project website](https://minds-thu.github.io/OptPilot/) · [Technical documentation](https://minds-thu.github.io/OptPilot/reference/) · [First experiment](docs/getting-started.md) · [Desktop](docs/ui.md) · [Experiment model](docs/concepts.md) · [Configuration](docs/configuration.md) · [Generators](docs/methods.md) · [External results](docs/external-results.md) · [Development](AGENTS.md)

The maintained CLI workflow uses `optpilot.experiment.yaml` and `optpilot experiment ...`. The former `optpilot run`, standalone Assistant, web Studio and work-card workflow are retired. Legacy runtime modules and fixtures remain for regression coverage and are not a second public workflow.
