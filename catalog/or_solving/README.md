# or_solving — natural-language OR solving with COOPA

One-time solve of a natural-language operations-research problem: you
describe the problem in plain language at launch time,
and the `coopa_solver` method drives the COOPA multi-agent pipeline
(formulation extraction with confidence-scored refinement → routing to a
mathematical / combinatorial / metaheuristic / general optimizer agent →
generated solver code → numeric answer). The full solution artifact —
formulation with provenance, per-dimension confidence, routing decision,
generated code, and the numeric solution — is retained with the Run.

## Setup

COOPA is **bundled** with this package under
`methods/coopa_solver/coopa_home/` (Apache-2.0, see its `LICENSE`). You do not
need to obtain it separately, and a retained Run always uses that captured
copy. The optional `COOPA_HOME` grant belongs only to the interactive console;
interface developers can use it to point the console at another checkout
without changing the retained Method.

Two things are still yours to provide, because neither can be locked into an
OptPilot process runtime (which accepts pure `py3-none-any` wheels only):

1. Install the pruned runtime dependencies into the Python environment that
   executes the method (see `methods/coopa_solver/requirements-pruned.txt`).
   Solver backends are user-provisioned extras: `ortools` and `pymoo` are
   pip-installable; GLPK/IPOPT come from your system package manager.
   Without them, keep `agentMode: mathematical-only` availability in mind —
   the paper reports the mathematical agent alone covers ~91% of benchmark
   dispatches.
2. Set `OPENROUTER_API_KEY` and choose a model id that OpenRouter can route.
   The retained Method declares only this provider credential.

## Run setups

- `solve-or-problem` — the real pipeline. Declares one per-launch input
  (`problem`, the natural-language statement); the Studio launch form
  renders it, or pass `--input problem="…"` on the CLI:

  ```bash
  uv run optpilot run catalog/or_solving/studies/solve_or_problem.yaml \
    --package-root catalog/or_solving \
    --input problem="A factory makes two products. Product A yields \$40 profit and takes 2 hours of labor; product B yields \$30 and takes 1 hour. With 100 labor hours available, maximize profit."
  ```

To check the package's wiring without COOPA, a network, or API keys, run
`optpilot package validate catalog/or_solving`.

## What the evaluator checks

`or_problem`'s evaluator does not re-solve anything. It validates the
returned solution artifact's well-formedness (parses, carries the required
sections, objective value consistent with `answer_found`) and reports
`solved` (1/0), the `objective_value`, and the artifact size as metrics.
Trusting the numeric answer remains the user's judgment call over the
retained artifact — formulation confidence scores and generated code are
kept precisely so the answer can be audited.

Note that the method executes LLM-generated solver code locally (COOPA's
design). Treat the method runtime as trusted local code.
