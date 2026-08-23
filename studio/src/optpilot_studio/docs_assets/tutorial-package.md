---
title: Build Your First OptPilot Package
description: Learn the four package building blocks from one tiny runnable example.
---

# Build Your First OptPilot Package

`catalog/optpilot_tutorial` is the release catalog's teaching package. It is
small on purpose: one Environment, one Method, one Run setup, and one Resource.
Nothing needs an API key, network access, or third-party dependency.

| Building block | Responsibility |
| --- | --- |
| Environment | Defines the evaluator, Candidate parameter schema, and metrics. |
| Method | Proposes Candidates compatible with that schema. |
| Run setup | Pairs the two and declares the objective, budget, and seed. |
| Resource | Demonstrates a reusable launchable interface with a visual package map. |

Run the complete example:

```bash
uv run optpilot package validate catalog/optpilot_tutorial --check-source
uv run optpilot run catalog/optpilot_tutorial/studies/find_best_settings.yaml \
  --package-root catalog/optpilot_tutorial
```

The six deterministic trials tune a toy factory's worker count, buffer capacity,
and operating mode. The Method is deliberately implemented in one short Python
file so it can be copied and changed. Each trial retains a small
`evaluation.json` artifact.

To start your own package, copy the folder, replace the 32-character `identity`
in `optpilot.package.yaml`, and change one component at a time. Keep the same
identity when merely moving or renaming your package.
