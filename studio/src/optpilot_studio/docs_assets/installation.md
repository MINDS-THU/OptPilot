---
title: Installation
description: Install OptPilot, and what you get.
---

# Installation

## Prerequisites

- Python 3.10 or newer. (On macOS the built-in `python3` is older than this;
  install a current Python first, or the install will refuse.)
- Docker or Podman **only** for packages or interfaces that declare a
  container image.

## Install the core CLI/SDK

```bash
python -m pip install optpilot
optpilot --help
```

Validate a package and study:

```bash
optpilot package validate path/to/package
optpilot validate path/to/package/studies/my_study.yaml
```

Run a supported retained study:

```bash
optpilot run path/to/package/studies/my_study.yaml \
  --package-root path/to/package
```

`--package-root` is required. It is the complete source authority OptPilot
captures before compilation; referenced configs, Python roots, and source-backed
callables must stay inside it.

Without `--realm-root`, the command uses OptPilot's private per-user Realm in
the OS user-data location. Use an explicit Realm root only for deliberate local
isolation/testing. It is not a Workspace or generated-output directory.

OptPilot supports parameter and bounded file candidates, Python batch methods
and evaluators, command methods, process and container runtimes, and
package-backed, environment-owned `methodContext.references`. Those references are captured
with the package and projected read-only into the method worker. Package-owned
`trialWorkspace` files/directories are also supported as retained seed layers
for attempts; every attempt receives a fresh writable trial volume. File
candidates are frozen, sealed, and atomically admitted before their immutable
tree is projected into that volume. Running a package's code still assumes you trust that package: an image is
executed only after you approve it, and authored code is never sandboxed
against deliberate hostility.
A process Method may declare `runtime.envFromHost`; the launcher selects only
those names. Studio binds their current saved revisions to the new Run and
sends the values transiently to its Method worker, leaving them out of the
durable process request and Run evidence. A direct CLI launch instead uses its
exported process environment as a process-lifetime binding. Unsupported
authoring configs fail during retained compilation.

A package normally contains:

```text
my_package/
  environments/
  methods/
  resources/
  studies/
```

See [Packages and Catalogs](catalog.md).

## Source checkout and Studio

Studio, the public research catalog, documentation, and contributor tooling
are currently distributed from the source repository rather than PyPI.

```bash
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync --all-packages --group examples --group docs
uv run optpilot --help
uv run optpilot package validate catalog/production_agv_scheduling --check-source
```

The source checkout contains three paper-backed research packages and one
small tutorial package. They are described under [Research Packages](devs-gallery.md).
Pre-generated examples run with no model key. Generation and language-model
search need an API key; add it under Studio Settings → Local environment
variables, or export it before a command-line launch. OptPilot passes only
declared values to that launch and does not copy their contents into Run
evidence.

Launch Studio:

```bash
uv run optpilot ui --open-browser
```

The default URL is `http://127.0.0.1:8765/`. To choose another port:

```bash
uv run optpilot ui --host 127.0.0.1 --port 8866 --open-browser
```

Studio scans `catalog/` by default and reads runs from the same default Realm.
See [Studio UI](ui.md), [Workspace Management](studio-workspaces.md), and
[OptPilot Assistant](assistant.md).

Packages that declare a container image also need Docker or Podman. Approve
the exact digest before execution as described in
[Local Operations and Security](operations.md#environment-preview-image-approvals).

## Optional test-catalog dependencies

```bash
uv sync --all-packages --group examples
```

Only needed to run the job-shop studies under `test_catalog/`, which are part
of OptPilot's own test material rather than something shipped to users. The
group pulls in a deep-learning stack and several hundred megabytes; nothing in
the four public catalog packages needs it.

## Documentation server

```bash
uv run --group docs mkdocs serve
```

The local docs URL is usually `http://127.0.0.1:8000/OptPilot/`.
