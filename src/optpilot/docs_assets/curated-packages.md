---
title: Curated Packages
description: How OptPilot should distribute application-specific environments, methods, and resources outside the core repository.
---

# Curated Packages

OptPilot core should stay small and runnable from a clean checkout. Application-specific environments, methods, resources, adapters, datasets, simulator wrappers, and heavyweight dependencies should be released as separate curated packages.

A curated package is a normal codebase that has been prepared to work with OptPilot. It may contain:

- `environments/` with one or more `config: environment` files and evaluator code
- `methods/` with one or more `config: method` files and method code
- `resources/` with reusable reference material, simulator interfaces, datasets, or launchable apps
- `studies/` with runnable study plans
- package-local tests, smoke scripts, dependency files, and documentation

The local `catalog/` directory is a shelf of packages. The repository ships
`catalog/example_package/`, and Studio creates `catalog/local_package/` only
when a user registers their own files. New curated packages should be added as
new sibling folders, not merged into or written over existing packages:

```text
catalog/
  example_package/
  local_package/
  my_curated_package/
```

OptPilot Studio can load packages under `catalog/` by default. It can also load
an external package path as an additional catalog root. The package remains
user-visible and editable, but it does not become part of the OptPilot
framework source tree.

```bash
uv run optpilot ui --catalog catalog/example_package --catalog path/to/optpilot-package
```

## Merge Policy

Adding a package should be additive. Do not overwrite the existing catalog and
do not copy files into `example_package` unless you are intentionally editing
that package.

Use one folder per package so users can:

- inspect where each environment, method, resource, and study came from
- update or remove a package without touching other packages
- keep user-owned work separate from bundled examples and curated case studies
- compare packages that expose similar method or environment ids

## Package Release Contract

Every package should be curated before it is featured in a case study, blog post, or package gallery:

- it has a short README that explains what it contains and which studies are runnable first
- each advertised catalog entry has the source code and resources needed to run it
- external dependencies are declared in the package, not hidden in the framework repo
- heavyweight or licensed assets are either excluded with clear download instructions or replaced by a small runnable sample
- at least one smoke command validates the package from a fresh checkout
- the package states which OptPilot version or commit it was verified against

## User-Owned Code

Users do not need to wait for an official package. They can attach their own repository or folder in Studio, then use the assistant to discover candidate environment and method boundaries, draft OptPilot configs, validate compatibility, and register the result into their local catalog.

The same rule applies to user code and curated packages: OptPilot owns the contract and runtime; the package owns application-specific implementation.
