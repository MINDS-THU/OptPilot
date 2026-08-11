---
title: Natural-Language OR Solving
description: Describe an operations-research problem in plain language, solve it once with the COOPA pipeline, and keep the whole audit trail as Run evidence.
---

# Natural-Language OR Solving

This tutorial runs the `or_solving` package: you type an operations-research
problem in plain language, one method solves it **once**, and OptPilot retains
the full artifact — formulation with provenance, confidence scores, routing
decision, generated solver code, numeric answer. It demonstrates *applying one
method to one problem, one time*, rather than searching a space.

## The one-time-solve shape

No search loop: the Run setup declares a budget of one trial and carries the
problem itself as a per-launch input.

```yaml
# catalog/or_solving/studies/solve_or_problem.yaml (excerpt)
inputs:
  problem: {valueType: string}
objective: {metric: solved, direction: maximize}
budget: {maxTrials: 1}
```

The method reads that value from `settings["inputs"]["problem"]`, solves, and
returns a single candidate whose parameters are `objective_value`,
`answer_found` and `report_json`. Launch inputs are bound into the retained
contracts, so the problem statement you typed is part of the Run's evidence and
of its run-definition digest — same problem, same digest.

!!! warning "COOPA is user-provisioned, not redistributed"
    The COOPA research codebase carries **no license file**, so this package
    ships no COOPA source at all. Every file under `catalog/or_solving/` is
    OptPilot-original code that *imports* a checkout you supply through
    `COOPA_HOME`. Its solver backends are native (GLPK/IPOPT binaries,
    `ortools`, `pymoo`) and cannot be locked into an OptPilot process runtime,
    which accepts pure `py3-none-any` wheels only. This path is
    user-provisioned by design, and will stay that way until the license
    question is resolved with COOPA's authors.

## Start with the mock twin — no COOPA, no API key

`solve-or-problem-mock` (`studies/solve_or_problem_mock.yaml`) uses the
explicitly labeled `coopa-solver-mock` method. It exercises the identical
machinery — per-launch input, command-protocol batch exchange, artifact
retention, evaluator scoring — with no COOPA checkout, no network, and no keys.
Its `problem` input has a placeholder default, so this runs as-is:

```bash
uv run optpilot run catalog/or_solving/studies/solve_or_problem_mock.yaml \
  --package-root catalog/or_solving
```

The Run succeeds with one trial: `solved = 1.0`, `objective_value = 0.0`. Add
`--input problem="A bakery makes cakes and pies..."` to watch your own statement
land inside the retained artifact.

!!! note "Mock answers are placeholders, never solutions"
    Every artifact this method emits is marked `"mode": "mock"` and its
    `predicted` value is a constant. `solved = 1.0` here means "a well-formed
    artifact came back", which is exactly what the smoke test is for.

## The real pipeline

`solve-or-problem` pairs the same `or-problem` environment with the
`coopa-solver` method, which drives the COOPA pipeline: confidence-scored
formulation extraction with refinement, routing to one of four optimizer agents
(mathematical / combinatorial / metaheuristic / general), LLM-generated solver
code executed locally, and a numeric answer. Four prerequisites are yours to
provide:

| Prerequisite | How |
| --- | --- |
| COOPA checkout | Obtain from its authors; the research layout with `apps/`, `src/`, `general_tools/` at the root. Set `COOPA_HOME` to it (shell for CLI, Studio Settings for Studio). |
| Pruned runtime deps | `uv pip install -r catalog/or_solving/methods/coopa_solver/requirements-pruned.txt` into the **same** Python environment that runs `optpilot`. |
| Solver backends | `ortools` and `pymoo` come with the requirements file; GLPK/IPOPT binaries come from your system package manager (e.g. `brew install glpk ipopt`). |
| Model access | `OPENROUTER_API_KEY`, or a `model` setting litellm can route with your own keys. |

```bash
uv run optpilot run catalog/or_solving/studies/solve_or_problem.yaml \
  --package-root catalog/or_solving \
  --input problem="A factory makes two products. Product A yields \$40 profit and takes 2 hours of labor; product B yields \$30 and takes 1 hour. With 100 labor hours available, maximize profit."
```

The method declares `entrypoint.exchangeTimeoutSeconds: 900` and the runner
honors that declaration, so no timeout flag is needed (`--method-request-timeout`
remains a launch-time override). In Studio the same launch is a **Launch inputs**
form on the Run setup, blocked with `runtime_environment_missing` until
`COOPA_HOME` and `OPENROUTER_API_KEY` exist as Settings values.

!!! warning "Generated solver code runs locally"
    COOPA's design is to execute the solver code its agents write. In this
    package that happens in the method's process runtime on your machine. Treat
    the method runtime as trusted-local-code.

### Method settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `model` | `openrouter/deepseek/deepseek-v4-pro` | litellm model id for formulation and all agents |
| `agentMode` | `manager` | `manager` routes across all four optimizer agents; `mathematical-only` runs the Pyomo agent directly and needs no combinatorial/metaheuristic extras |
| `skipFormulation` | `false` | Skip formulation extraction and prompt the agents with raw text |
| `maxRefinementIterations` | `2` | Formulation refinement rounds (1–5) |
| `batchSize` | `1` | Pinned to 1 — one proposal per Run |

!!! note "The 91% figure is not checkable here"
    The package README repeats the COOPA paper's claim that the mathematical
    agent alone covers ~91% of benchmark dispatches, as the rationale for
    `mathematical-only` being a usable degraded mode. No paper artifact exists
    in this repository, so that number is quoted, not verified.

## What the evaluator scores

`or-problem` **does not re-solve anything**. Correctness of a natural-language
OR answer is not machine-checkable in general, so the evaluator scores whether
the artifact is a complete, parseable record of what the solver did:

| Metric | Meaning |
| --- | --- |
| `solved` | 1.0 when an answer was found *and* the artifact is well-formed. **The objective.** |
| `objective_value` | The reported objective value (0.0 when no answer was found) |
| `artifact_bytes` | Size of the retained JSON artifact |

Well-formed means: `report_json` parses, its schema is
`optpilot.or-solving-report.v1`, it carries `schema`/`mode`/`problem`/
`agent_response`, and a claimed answer comes with a finite `objective_value`
and a `predicted` field. So `solved = 1.0` asserts *"an auditable answer
exists"*, never *"the answer is optimal"*. Judging the number is your call over
the retained formulation, confidence and generated code — which is precisely
why all of it is kept. The release plan
(`designs/initial-release-plan.md` §5.3) records two real runs: an LP problem
through the retained runner at `predicted = 36.0` (the exact optimum, 8.9 KB
artifact, manager routing, deepseek-v4-pro via OpenRouter), and a product-mix
LP through the console at 2160 (also exact). Single recorded runs, not a
benchmark.

## The COOPA Solve Console

The `coopa-solver` method declares an `interface`, so the human path is a
launchable app rather than a form: open the method in **Catalog** and choose
**Open interface**. Studio prepares a cached Python runtime from
`requirements-pruned.txt` (via `launch_console.sh --prepare-only`), then serves
the console as a web presentation on port 8000 with `COOPA_HOME` and
`OPENROUTER_API_KEY` granted from Settings. It runs four steps —
**Formulate → Review → Solve → Result** — in three modes:

- **Interactive** (default): extract a confidence-scored formulation, present
  objective/variables/constraints/parameters as tables with the source quote
  behind each element and per-dimension confidence bars, then either
  **Request revision** with free-text feedback (which re-extracts, incorporating
  your guidance) or **Approve & solve**. Feedback rounds accumulate on the job.
- **Automatic**: formulate and solve unattended, showing everything at the end.
- **Mock** (a checkbox under Advanced options): canned formulation, confidence,
  solver code and result, so the console can be demonstrated with no COOPA, no
  network and no key. Mock runs are labelled in the UI.

!!! note "Inside the launch runtime, host paths do not exist"
    A container-launched interface cannot see your host `COOPA_HOME`. The shim
    therefore also looks for a checkout at
    `catalog/or_solving/methods/coopa_solver/coopa_home/` — a gitignored
    location you populate yourself; COOPA is never committed. Without either, a
    non-mock start refuses with an explicit message, as it does when
    `OPENROUTER_API_KEY` is not granted.

## Validating without running

Everything except an actual solve is checkable offline, which is what CI does:

```bash
uv run optpilot package validate catalog/or_solving --check-source
```

It reports the package's one environment, two methods and two Run setups. The
mock study is the executable half of this story; the real pipeline stays
validate-only until you provision COOPA yourself.
