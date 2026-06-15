# SA Code Optimization Example

This example shows how OptPilot can wrap an external simulator codebase and a user-owned LLM file-editing method.

The study:

- evaluates the SA simulator through `sa_eval.py`
- exposes selected simulator files through the environment candidate contract
- uses `OpenAIFileEditMethod` from `user_methods/openai_file_edit_method.py`
- records official trial observations and artifacts in the run directory

## Files

- `environments/sa_simulator.yaml`: environment contract for the simulator.
- `methods/openai_file_editor.yaml`: method config for the LLM file editor.
- `studies/sa_code_optimization.yaml`: runnable study binding the two.
- `user_methods/openai_file_edit_method.py`: user-owned method implementation.
- `prompts/sa_file_edit_system_prompt.md`: prompt text used by the method.

The shipped environment edits `MissionController.py` because that is the strongest initial intervention point in the simulator. This is an environment-specific file name, not an OptPilot abstraction.

## Run

Set an API key compatible with the method config:

```bash
export OPENROUTER_API_KEY=...
```

Then run:

```bash
uv run optpilot run examples/opt_devs_gen_sims/studies/sa_code_optimization.yaml
```

The run directory contains:

- `study_spec.json`
- `summary.json`
- `observations.jsonl`
- `trials.jsonl`
- `artifacts.jsonl`
- `method_calls.jsonl`
- `scheduler_events.jsonl`
- trial workspaces under `trials/`
- prompt records under `prompts/` when the method calls the LLM

## Boundary

OptPilot does not understand or visualize SA simulator internals. The environment config declares what can be copied, edited, evaluated, and saved. The method reads that candidate context and returns a code-bundle artifact manifest. OptPilot materializes that artifact into a clean trial workspace and runs the evaluator.

