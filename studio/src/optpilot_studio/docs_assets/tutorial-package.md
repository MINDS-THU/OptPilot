---
title: Build Your First OptPilot Package
description: Create, validate, run, register, and update a portable OptPilot package.
---

# Build Your First OptPilot Package

`catalog/optpilot_tutorial` is a small, deterministic teaching package: one
Environment, one Method, one Run setup, and one Resource. It needs no API key,
network access, or third-party dependency.

The repository's `catalog/` is shipped example source. Keep your own package in
an external project or in OptPilot's editable per-user packages folder; do not
author a new package inside the tracked release catalog.

## 1. Choose an editable package root

Studio automatically scans the per-user packages folder when it contains
packages:

| Platform | Default folder |
| --- | --- |
| macOS | `~/Library/Application Support/OptPilot/packages` |
| Windows | `%LOCALAPPDATA%\OptPilot\packages` |
| Linux | `$XDG_DATA_HOME/optpilot/packages`, or `~/.local/share/optpilot/packages` |

Set `OPTPILOT_PACKAGES_ROOT` before starting Studio to use another folder. A
normal project-local catalog is also fine. The commands below use an external
sibling folder so they do not modify the checkout:

```bash
mkdir -p ../optpilot-packages
cp -R catalog/optpilot_tutorial ../optpilot-packages/my_package
```

Set a **new** package identity because this copy is a separate package:

```bash
uv run python -c "import secrets; print(secrets.token_hex(16))"
```

Open `../optpilot-packages/my_package/optpilot.package.yaml`, replace its
`identity` with the printed 32-character lowercase hexadecimal value, and give
the package its own `title` and `description`.

Keep an identity unchanged when moving or renaming the same package. Generate a
new identity only when a copy should have an independent publication history.

## 2. Understand the package

The copied package has this portable layout:

```text
my_package/
  optpilot.package.yaml
  README.md
  environments/
    toy_factory/
      environment.yaml
      evaluator.py
  methods/
    random_search/
      method.yaml
      method.py
  resources/
    package_guide/
      optpilot.resource.yaml
      serve.py
  studies/
    find_best_settings.yaml
```

The four public component roles are:

| Building block | Responsibility |
| --- | --- |
| Environment | Defines the Candidate contract, evaluator, metrics, and runtime. |
| Method | Proposes Candidates compatible with an Environment contract. |
| Run setup (`study`) | Pairs the two and declares objective, budget, inputs, and seed. |
| Resource | Supplies optional support material, actions, or a launchable interface. |

Every package should have `optpilot.package.yaml`:

```yaml
apiVersion: optpilot.io/v1
config: package
identity: 0123456789abcdef0123456789abcdef
title: My OptPilot package
category: local
description: What this package lets a user accomplish.
```

`category` is `research`, `tutorial`, or `local`. A research package may add a
`paper` mapping with a title and an `https://arxiv.org/abs/...` URL. A package
may also declare one digest-pinned default container image and platform under
`runtime.container`; see [Executable Capabilities](capabilities.md) before using
container execution.

Use human-facing metadata on every Catalog entry:

```yaml
id: toy-factory                 # stable technical identifier
name: Toy factory               # label shown to people
description: Evaluates one set of factory settings.
tasks: [learn-optpilot, tune-parameters]
tags: [tutorial, deterministic]
```

A Study uses `name` as its identifier and optional `title` as its human-facing
label. Built-in task slugs include `generate-simulator`, `optimize-policy`,
`solve-or-problem`, `tune-parameters`, `evaluate-design`, `benchmark-method`,
`learn-optpilot`, and `build-package`. Third-party packages may use another
lowercase verb-object slug such as `route-vehicles`. Tasks improve search and
routing; tags remain free-form labels.

## 3. Make one controlled change

Start by changing only one layer:

- edit the accepted fields or metric contract in
  `environments/toy_factory/environment.yaml` and its evaluator together
- edit proposal behavior in `methods/random_search/method.py`
- edit objective, budget, seed, or component selection in
  `studies/find_best_settings.yaml`
- edit the Resource only when you need a reusable support surface

Keep all referenced source, setup inputs, and fixture data inside the package.
Relative paths resolve from the config that declares them. Choose only a
combination marked executable in [Executable Capabilities](capabilities.md).

## 4. Validate and smoke-test

From the OptPilot checkout, run the deep package checks:

```bash
uv run optpilot package validate ../optpilot-packages/my_package \
  --check-source \
  --check-setup-files \
  --check-imports
```

Then validate and smoke-run the advertised Study:

```bash
uv run optpilot validate \
  ../optpilot-packages/my_package/studies/find_best_settings.yaml

uv run optpilot package smoke ../optpilot-packages/my_package \
  --study studies/find_best_settings.yaml
```

If the package declares locked dependencies, run these before the smoke test:

```bash
uv run optpilot package setup-check ../optpilot-packages/my_package
uv run optpilot package setup-check ../optpilot-packages/my_package --run-setup
```

`setup-check --run-setup` installs declared inputs, so review them first. A
successful schema check is not enough: the smoke Study is the executable proof
that the Method and Environment work together on this machine.

## 5. Run directly

Run the full six-trial example through an explicit package root:

```bash
uv run optpilot run \
  ../optpilot-packages/my_package/studies/find_best_settings.yaml \
  --package-root ../optpilot-packages/my_package
```

The original tutorial tunes worker count, buffer capacity, and operating mode.
It should finish with `run_status: succeeded`, a canonical `run_id`, and a small
`evaluation.json` artifact per trial.

## 6. Link and publish in Studio

Start Studio with the external catalog folder in addition to the shipped
examples:

```bash
uv run optpilot ui --open-browser \
  --catalog catalog \
  --catalog ../optpilot-packages
```

In **Catalog**:

1. Find the package under **Configured sources** and choose **Link local
   folder**. Studio connects that folder as an editable Workspace; it does not
   copy it over the shipped examples.
2. Open **Publish** and choose **Check files**.
3. Run the offered optional or required test.
4. Choose **Publish checked version**.

Registration captures the checked bytes as an immutable Realm package
revision. The configured-source card remains visible for future edits; the
registered Catalog entry is the stable revision used for Runs and inspection.

## 7. Update the package safely

For a normal update:

1. Keep `optpilot.package.yaml`'s `identity` unchanged.
2. Edit the external source folder and update its README when behavior or setup
   changes.
3. Repeat deep validation and the smallest meaningful smoke Study.
4. Reopen the linked Workspace, then choose **Publish** → **Check files**,
   rerun the offered test, and choose **Publish checked version**.
5. Confirm Studio shows the new immutable revision; existing Runs continue to
   reference the exact older revision they used.

Moving the folder does not create a new package as long as its identity stays
the same. Copying it for a different project does require a fresh identity.
Never reuse another package's identity, and never edit Realm storage directly.

## Release checklist

Before sharing a package:

- every advertised config has a clear `name`/`title`, `description`, and useful
  `tasks`
- the README states prerequisites, trust/network implications, first validation
  command, first smoke command, and which studies need keys or native software
- public source paths, setup files, and imports pass deep validation
- at least one small deterministic smoke Study runs without private data
- dependency and third-party license files travel inside the package
- large, licensed, or secret inputs are not committed
- the documented runtime combination matches
  [Executable Capabilities](capabilities.md)

For field-by-field YAML help, continue with
[Configuration Reference](configuration.md). For package publication and
security details, use [Packages and Catalogs](catalog.md) and
[Local Operations and Security](operations.md).
