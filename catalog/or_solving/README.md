# or_solving — natural-language OR solving with COOPA

One-time solve of a natural-language operations-research problem
(plan §5.3, W3): you describe the problem in plain language at launch time,
and the `coopa_solver` method drives the COOPA multi-agent pipeline
(formulation extraction with confidence-scored refinement → routing to a
mathematical / combinatorial / metaheuristic / general optimizer agent →
generated solver code → numeric answer). The full solution artifact —
formulation with provenance, per-dimension confidence, routing decision,
generated code, and the numeric solution — is retained with the Run.

## License status: COOPA is user-provisioned, not redistributed

The COOPA codebase has **no license file**, so this package does not include
or redistribute any COOPA source. Everything here (environment, evaluator,
method adapter, shim) is OptPilot-original code that *imports* COOPA from a
checkout you provide:

1. Obtain the COOPA code from its authors (the research checkout layout with
   `apps/`, `src/`, and `general_tools/` at its root).
2. Set `COOPA_HOME` to that root in the environment that runs OptPilot
   (shell for CLI runs; Studio Settings for Studio runs).
3. Install the pruned runtime dependencies into the Python environment that
   executes the method (see `methods/coopa_solver/requirements-pruned.txt`).
   Solver backends are user-provisioned extras: `ortools` and `pymoo` are
   pip-installable; GLPK/IPOPT come from your system package manager.
   Without them, keep `agentMode: mathematical-only` availability in mind —
   the paper reports the mathematical agent alone covers ~91% of benchmark
   dispatches.
4. Set `OPENROUTER_API_KEY` (or configure the model id in
   `settingsSchema.model` for a provider litellm can route with your keys).

## Run setups

- `solve-or-problem` — the real pipeline. Declares one per-launch input
  (`problem`, the natural-language statement); the Studio launch form
  renders it, or pass `--input problem="…"` on the CLI:

  ```bash
  uv run optpilot run catalog/or_solving/studies/solve_or_problem.yaml \
    --package-root catalog/or_solving \
    --method-request-timeout 900 \
    --input problem="A factory makes two products. Product A yields \$40 profit and takes 2 hours of labor; product B yields \$30 and takes 1 hour. With 100 labor hours available, maximize profit."
  ```

- `solve-or-problem-mock` — an explicitly labeled mock that exercises the
  exact same OptPilot machinery (per-launch inputs, command-protocol batch
  method, artifact retention, evaluator scoring) without COOPA, network, or
  API keys. Use it for smoke tests and CI. Its artifacts are marked
  `"mode": "mock"` and its answers are canned, never real solutions.

## What the evaluator checks

`or_problem`'s evaluator does not re-solve anything. It validates the
returned solution artifact's well-formedness (parses, carries the required
sections, objective value consistent with `answer_found`) and reports
`solved` (1/0), the `objective_value`, and the artifact size as metrics.
Trusting the numeric answer remains the user's judgment call over the
retained artifact — formulation confidence scores and generated code are
kept precisely so the answer can be audited.

Note the method executes LLM-generated solver code locally (COOPA's design).
Treat the method runtime as trusted-local-code; revisit under the container
slice per plan §5.3.
