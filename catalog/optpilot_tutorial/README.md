# Build Your First OptPilot Package

This package is intentionally small enough to read in one sitting. It contains
one example of each public building block:

| Building block | File | What it does |
| --- | --- | --- |
| Environment | `environments/toy_factory/environment.yaml` | Declares the simulator, Candidate parameters, and returned metrics. |
| Method | `methods/random_search/method.yaml` | Proposes Candidate parameter combinations. |
| Run setup | `studies/find_best_settings.yaml` | Pairs the Method and Environment, chooses an objective, and sets the trial budget. |
| Resource | `resources/package_guide/optpilot.resource.yaml` | Declares a small launchable web interface. |

The evaluator in `environments/toy_factory/evaluator.py` is ordinary Python.
It receives one Candidate and returns metrics. The Method is OptPilot's simple
seeded random-search reference, so the tutorial needs no API key or external
service.

Validate and run it from the repository root:

```bash
uv run optpilot package validate catalog/optpilot_tutorial --check-source
uv run optpilot run catalog/optpilot_tutorial/studies/find_best_settings.yaml \
  --package-root catalog/optpilot_tutorial
```

Use this folder as a template: copy it, replace the package identity, then
change one building block at a time.
