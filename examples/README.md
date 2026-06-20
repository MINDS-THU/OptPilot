# Examples

This folder contains runnable OptPilot example environments, methods, and studies.

For explanations, use the public docs:

- `docs/getting-started.md` for the first local run
- `docs/candidate-contracts.md` for the environment/method boundary
- `docs/examples.md` for the full example catalog
- `docs/job-shop-environment.md` for the main tutorial environment

## Quick Runs

Dependency-free job-shop baselines:

```bash
uv run optpilot run examples/studies/job_shop_rule_parameters_baseline.yaml
uv run optpilot run examples/studies/job_shop_dispatch_rule_baseline.yaml
uv run optpilot run examples/studies/job_shop_solver_code_baseline.yaml
```

JobShopLib and Stable-Baselines examples:

```bash
uv sync --extra examples
uv run optpilot run examples/studies/job_shop_lib_dispatching_rule.yaml
uv run optpilot run examples/studies/job_shop_simulated_annealing.yaml
uv run optpilot run examples/studies/job_shop_ortools_cpsat.yaml
uv run optpilot run examples/studies/job_shop_rl_stable_baselines.yaml
```

LLM and external-command examples:

```bash
uv run optpilot run examples/studies/job_shop_openai_dispatch_rule.yaml
uv run optpilot run examples/studies/job_shop_local_heuristic_search.yaml
uv sync --extra sa
uv run optpilot run examples/studies/sa_baseline.yaml
```

The Strategic Airlift sample also requires the generated simulator tree under `resource/`. The OpenAI-compatible editing studies require provider credentials for real LLM edits.

## Layout

```text
examples/
  environments/
  methods/
  studies/
```

Environment and method directories contain reusable components. Study files bind one environment, one method, objective, budget, runtime, and evidence settings into a concrete run.
