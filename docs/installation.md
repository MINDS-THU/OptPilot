# Install OptPilot

Use Core to run experiments from a shell or script. Desktop provides a graphical view of the same project, including its saved versions, trial results and editable files.

## Install Core and the examples

You need Git, Python 3.10 or newer, and uv. A source checkout includes the example projects used throughout these guides. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)

```sh
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync
uv run optpilot --help
```

The help lists `experiment`, `validate` and `schema`. Run `uv run optpilot --version` to check the installed Core version. In a Python virtual environment managed with pip, `python -m pip install .` from the checkout installs the same CLI as `optpilot`.

You can now [compare two local document rankers](getting-started.md). The rankers and grading inputs are included in the checkout and run without a model account.

## Get Desktop

When a Desktop build is published, its installer appears on the project's public Releases page for macOS, Windows or Linux. Choose a release with an asset for your system and architecture, and consult that release's installation and signing notes. If the page lists no matching asset, use the Core instructions above. Desktop is distributed separately from the open-source Core. [OptPilot releases](https://github.com/MINDS-THU/OptPilot/releases) · [Desktop walkthrough](ui.md)

## Requirements for other workflows

| Workflow | What to prepare |
| --- | --- |
| Simulated display calibration | A macOS or Linux shell and the Core checkout. The bundled driver uses a POSIX file lock and starts the simulated instrument locally. [Resource tutorial](resource-tutorial.md) |
| Harbor agent evaluation | A working Harbor executable, Docker, and the credentials required by your chosen agent and model. Hosted model calls can incur provider charges. [Execution options](capabilities.md) |
| Native grading on another machine | A reachable POSIX SSH host, a verified host key, non-interactive login, and Python with the grader's dependencies on that host. [Operations](operations.md) |
| Research packages | Use the package guide to identify simulator dependencies, solver installations or model credentials for your chosen study. [Example projects](catalog.md) |
