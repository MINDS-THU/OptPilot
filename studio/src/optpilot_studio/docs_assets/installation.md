---
title: Installation
description: Install OptPilot, and what you get.
---

# Installation

## Prerequisites

- Python 3.10 or newer. (On macOS the built-in `python3` is older than this;
  install a current Python first, or the install will refuse.)
- Docker or Podman **only** if you use a package that declares a container
  image. Nothing that ships with OptPilot requires one.

## Install

```bash
python -m pip install optpilot-studio
```

That gives you three things: the `optpilot` command line, the Studio web
application, and five ready-made packages to look at and run.

To install only the command line, without the web application:

```bash
python -m pip install optpilot
```

## Start Studio

```bash
optpilot-studio --open-browser
```

The default address is `http://127.0.0.1:8765/`. Choose another port with
`--port`.

The first start takes a few seconds longer than later ones: OptPilot copies
the ready-made packages into a folder of your own and records a version of
each, so they can be run straight away. Those copies are yours — edit them,
move them, delete the ones you do not want. They live beside OptPilot's own
storage, in the standard per-user data location for your operating system, and
`OPTPILOT_PACKAGES_ROOT` overrides where they go.

## Run something without the web application

Every package that ships works from the command line too. This one needs no
container software and no model provider account:

```bash
optpilot run --package-root <packages>/devs_gallery \
  <packages>/devs_gallery/studies/seird_minimize_deaths.yaml
```

Replace `<packages>` with the folder Studio reports on its Catalog page, or
set `OPTPILOT_PACKAGES_ROOT` yourself so you know where it is.

## Packages that need more

Two of the ready-made packages need something extra before they run:

- Anything that asks a language model to write candidates needs an API key.
  Add it under Studio Settings → Local environment variables, or export it
  before a command-line launch. OptPilot passes the value to that run's method
  only, and never copies it into the run's record.
- A package that declares a container image needs Docker or Podman, and the
  image must be approved for execution first:

  ```bash
  optpilot image approve <image reference>
  ```

  Approving an image is how you say you are willing to run software someone
  else built. A refused launch names the exact image and this command.

## Core CLI/SDK


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

```bash
git clone https://github.com/MINDS-THU/OptPilot.git
cd OptPilot
uv sync --all-packages --group examples --group docs
uv run optpilot --help
uv run optpilot package validate catalog/production_agv_scheduling --check-source
```

The five packages that ship are described under
[Flagship Capabilities](devs-gallery.md). Most run with no extra setup; the
ones that call a language model need an API key, as described above.

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

## Optional test-catalog dependencies

```bash
uv sync --all-packages --group examples
```

Only needed to run the job-shop studies under `test_catalog/`, which are part
of OptPilot's own test material rather than something shipped to users. The
group pulls in a deep-learning stack and several hundred megabytes; nothing in
the five shipped packages needs it.

## Documentation server

```bash
uv run --group docs mkdocs serve
```

The local docs URL is usually `http://127.0.0.1:8000/OptPilot/`.
