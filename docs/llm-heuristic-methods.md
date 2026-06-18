---
title: LLM Heuristic Methods
description: How to connect existing LLM-based heuristic-search repositories to OptPilot.
---

# LLM Heuristic Methods

This guide explains how to connect an existing LLM-based heuristic-search repository to OptPilot as a method.

Use this pattern when the upstream repository already has its own search loop. Examples include FunSearch-style systems, evolutionary heuristic search, reflection-based heuristic improvement, and repositories that repeatedly generate, evaluate, rank, and revise candidate heuristics internally.

The important boundary is simple:

```text
upstream method runs its own loop -> produces a generated file -> OptPilot evaluates that file as a candidate
```

OptPilot does not need to rewrite the upstream method into its internal control flow. OptPilot only needs a method config that says how to launch the upstream command and where to find the generated file.

For a concrete environment target, use the job-shop file-candidate configs:

- `examples/environments/job_shop_scheduling/environment_dispatch_rule.yaml` for generated `dispatch_rule.py`
- `examples/environments/job_shop_scheduling/environment_solver_code.yaml` for generated `solver.py`

## When This Pattern Fits

Use the `llm_heuristic_search` example when the upstream project has:

- a top-level command or script that starts a complete search run
- its own generation, evaluation, ranking, reflection, or evolution loop
- a final generated heuristic file that can be evaluated by an OptPilot environment
- enough repository-specific logic that rewriting it into a tiny `propose()` method would mostly add glue code

Use a normal OptPilot-native method instead when your optimizer is already a small Python class or command that can directly propose candidates from the current study state.

## The Adapter

The shared adapter is:

```text
examples.methods.llm_heuristic_search.method:LLMHeuristicSearchMethod
```

The adapter does five things:

1. creates a method workspace for one OptPilot method call
2. writes a JSON request file into that workspace
3. runs the configured upstream command
4. captures `stdout.log`, `stderr.log`, and `request.json` for debugging
5. stores the configured generated file as an OptPilot file candidate

The generated file then follows the same runtime path as any other file candidate: OptPilot validates it, stores it in the candidate store, materializes it into a fresh trial workspace, and asks the environment evaluator to score it.

## Method Config

Each upstream repository gets a small method config.

```yaml
apiVersion: optpilot.io/v1
config: method
id: my-llm-heuristic-search
description: Wrapper around an upstream heuristic-search repository.

entrypoint:
  python: examples.methods.llm_heuristic_search.method:LLMHeuristicSearchMethod
  protocol: batch

settings:
  command:
    - "{python}"
    - run_upstream.py
    - --request
    - "{request_file}"
    - --output-dir
    - "{method_workspace}"
  repoRoot: ../../../resource/upstream_repo
  workdir: ../../../resource/upstream_repo
  generatedFile: best_heuristic.py
  timeoutSeconds: 1800

accepts:
  formats: [files]
  requires:
    context:
      - candidate.files.editable
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `settings.command` | Command used to launch the upstream search run. It is passed to `subprocess.run(...)`. |
| `settings.repoRoot` | Local checkout of the upstream repository. Relative paths resolve from the method config file. |
| `settings.workdir` | Working directory for the command. Defaults conceptually to the repository root in these examples. |
| `settings.generatedFile` | File produced by the upstream command and returned to OptPilot as the candidate content. |
| `settings.candidatePath` | Optional target path inside the environment candidate contract. Omit it when the environment exposes exactly one editable file. |
| `settings.timeoutSeconds` | Optional command timeout. |

`generatedFile` can be absolute or relative. A relative value is searched in this order:

1. the OptPilot method workspace
2. `settings.workdir`
3. `settings.repoRoot`

This lets upstream repositories write either to OptPilot's provided workspace or to their normal repository output paths.

Command placeholders:

| Placeholder | Resolves to |
| --- | --- |
| `{python}` | The Python interpreter running OptPilot. |
| `{request_file}` | JSON request file written by OptPilot for this method call. |
| `{method_workspace}` | Writable OptPilot workspace for this method call. |
| `{repo_root}` | Resolved `settings.repoRoot`. |

## Candidate Path

The upstream repository produces a file. The environment decides where that file is applied during evaluation.

For the common single-file case, the environment has exactly one editable file:

```yaml
candidate:
  format: files
  materialize:
    root: heuristic
  files:
    editable:
      - path: priority.py
    required:
      - priority.py
```

In that case, the adapter can infer the candidate target path and the method config does not need `settings.candidatePath`.

If the environment exposes multiple editable files, set `settings.candidatePath` explicitly:

```yaml
settings:
  generatedFile: output/best_policy.py
  candidatePath: policies/priority.py
```

`candidatePath` is not where the upstream project writes its output. It is the path inside the environment candidate contract where OptPilot should apply the generated file.

## Workflow

1. Clone the upstream repository into a stable local path.
2. Run the upstream repository once using its own README instructions.
3. Identify the launch command, working directory, and generated file.
4. Create or edit one OptPilot method config using the fields above.
5. Bind the method to an environment whose `candidate.format: files` contract matches the generated file.
6. Validate the method config and then the study config.

Example validation:

```bash
uv run optpilot validate examples/methods/llm_heuristic_search/reevo_command.yaml
uv run optpilot validate path/to/your_study.yaml
```

If the upstream command fails, inspect the adapter logs in the run directory. The method workspace contains `request.json`, `stdout.log`, and `stderr.log`.

## Included Templates

The repository includes template configs for several upstream projects:

```text
examples/methods/llm_heuristic_search/
  funsearch_command.yaml
  eoh_command.yaml
  reevo_command.yaml
  heuragenix_command.yaml
  eohs_command.yaml
```

These templates are not guaranteed turnkey studies. They document the OptPilot-side shape for connecting those repositories. You still need to clone the upstream repository, install its dependencies, confirm its real command, and bind it to an environment whose file-candidate contract matches the produced file. The job-shop dispatch-rule environment is the recommended first target for generated heuristic files.

## Repository Notes

### FunSearch

Repository: <https://github.com/google-deepmind/funsearch>

FunSearch is naturally connected as a coarse-grained method command because the public repository is centered on reference implementations and notebooks rather than a small reusable method class. The OptPilot template expects a command that writes one generated heuristic file.

Template:

- `examples/methods/llm_heuristic_search/funsearch_command.yaml`

### EoH

Repository: <https://github.com/FeiLiu36/EoH>

EoH has a cleaner Python structure than FunSearch and examples are commonly launched through scripts such as `runEoH.py`. The template treats EoH as a command method that owns its internal evolution loop and returns one generated heuristic file.

Template:

- `examples/methods/llm_heuristic_search/eoh_command.yaml`

### ReEvo

Repository: <https://github.com/ai4co/reevo>

ReEvo is configured through `main.py` and Hydra configs under `cfg/`. Problems live under `problems/`, and generated heuristics are often written into a problem directory such as `problems/bpp_online/gpt.py`. That makes ReEvo a good fit for `generatedFile` relative to `repoRoot` or `workdir`.

Template:

- `examples/methods/llm_heuristic_search/reevo_command.yaml`

### HeurAgenix

Repository: <https://github.com/microsoft/HeurAgenix>

HeurAgenix has structured problem directories and scripts for problem-state generation, seed heuristic generation, evolution, and hyper-heuristics. From OptPilot's perspective, the simplest boundary is still one upstream command plus one generated file.

Template:

- `examples/methods/llm_heuristic_search/heuragenix_command.yaml`

### EoH-S

Repository: <https://github.com/FeiLiu36/EoH-S>

EoH-S focuses on heuristic-set evolution. Some integrations may eventually return multiple files or an opaque payload. The template keeps the single-file case because it is the easiest bridge to understand and is compatible with normal file-candidate environments.

Template:

- `examples/methods/llm_heuristic_search/eohs_command.yaml`

## Why This Boundary Is Useful

This pattern keeps responsibility clear:

- the upstream repository owns its optimization algorithm and dependencies
- the OptPilot method config owns how that repository is launched
- the OptPilot environment config owns what kind of candidate can be evaluated
- the OptPilot study config owns the concrete pairing, budget, objective, and runtime policy

That is the same design used by the rest of OptPilot. The only special thing about this example is that the method is a wrapper around a larger existing repository instead of a small in-process optimizer.
