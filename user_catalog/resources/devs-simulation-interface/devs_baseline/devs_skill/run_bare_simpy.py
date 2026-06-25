"""
Bare simpy mode with structural guidance.

Uses opencode agent loop (full edit capability) but forces simpy framework.
Allows both single-file and multi-file approaches (LLM decides).
Provides simpy usage guidance in the prompt.

This is the simpy counterpart to run_bare_xdevs.py.
"""

import os
import sys
import json
import yaml
import argparse
import subprocess
import shutil
import signal
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent
PERMISSION_TEMPLATE = BASE_DIR / "opencode_permission.json"
RUN_TIMEOUT = 600


def _force_rmtree(path: Path):
    import stat

    if not path.exists():
        return

    def on_error(func, path, exc_info):
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR)
            func(path)
        else:
            raise

    try:
        shutil.rmtree(path, onerror=on_error)
    except OSError:
        subprocess.run(["rm", "-rf", str(path)], check=False, timeout=30)
    for _ in range(10):
        if not path.exists():
            return
        time.sleep(0.1)
    if path.exists():
        raise RuntimeError(f"Failed to remove: {path}")


def setup_workspace(workspace: Path, params: dict) -> str:
    if workspace.exists():
        for item in workspace.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
    else:
        workspace.mkdir(parents=True, exist_ok=True)

    requirements_text = _build_requirements_text(params.get("requirements", {}))
    if PERMISSION_TEMPLATE.exists():
        with open(PERMISSION_TEMPLATE, "r", encoding="utf-8") as f:
            perm_config = json.load(f)
    else:
        perm_config = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"*": "allow"},
        }
    perm_config["snapshot"] = False
    perm_config["autoupdate"] = False
    with open(workspace / "opencode.json", "w", encoding="utf-8") as f:
        json.dump(perm_config, f, indent=2, ensure_ascii=False)
    return requirements_text


def _build_requirements_text(requirements: dict) -> str:
    parts = []
    for key in ["general", "scenario", "args_input_output"]:
        val = requirements.get(key, "")
        if val:
            parts.append(f"## {key.replace('_', ' ').title()}\n{val}")
    return "\n\n".join(parts) if parts else "No specific requirements provided."


SIMPY_FRAMEWORK_GUIDE = """
## simpy Framework Guide

You MUST use **simpy** (process-based discrete-event simulation) for this simulation.

### Core API
```python
import simpy
import json
import sys
import argparse
```

### Process-based Model Template
```python
class ModelName:
    def __init__(self, env: simpy.Environment, <explicit_config_args>):
        self.env = env
        # Initialize resources, stores, state
        # simpy.Resource(env, capacity=N) for contention
        # simpy.Store(env, capacity=N) for message passing

    def process(self):
        # Main process loop.
        while True:
            yield self.env.timeout(<duration>)
            # Process logic
            self._log(<event_data>)

    def _log(self, data: dict):
        # Log event as JSONL to stdout.
        data["time"] = self.env.now
        print(json.dumps(data), file=sys.stdout, flush=True)
```

### Entry Point (run.py)
```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate_time", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    env = simpy.Environment()
    model = ModelName(env, <config>)
    env.process(model.process())
    env.run(until=args.simulate_time)

if __name__ == "__main__":
    main()
```

### Key Patterns
- **Resource**: `simpy.Resource(env, capacity=N)` — `yield resource.request()` / `resource.release()`
- **Store**: `simpy.Store(env, capacity=N)` — `yield store.put(item)` / `item = yield store.get()`
- **Timeout**: `yield env.timeout(duration)`
- **Event**: `yield event1 | event2` (OR), `yield event1 & event2` (AND)
- **Interrupt**: `process.interrupt()` for preemption

### DEVS → simpy Mapping
| DEVS Concept | simpy Equivalent |
|---|---|
| Atomic Model | `class` with `process()` generator |
| Coupled Model | Composition: multiple processes sharing `Environment` |
| Input Port | `simpy.Store` (receive) |
| Output Port | `simpy.Store` (send) |
| hold_in(phase, sigma) | `yield env.timeout(sigma)` |
| lambdaf (output) | `yield store.put(payload)` |
| deltext (input) | `yield store.get()` |
| deltint (internal) | Internal state update after timeout |

### Critical Rules
1. **JSONL output**: `print(json.dumps({"time": env.now, "entity": "...", "event": "...", "payload": {...}}), file=sys.stdout, flush=True)`
2. **Debug to stderr**, JSONL to stdout.
3. **Use `argparse`** for CLI arguments.
4. **All processes share one `simpy.Environment`** — no need for explicit coupling.
5. **File organization**: Single-file or multi-file is fine. If multi-file, use standard Python imports.
"""

BARE_SIMPY_PROMPT = """You are a DEVS simulation expert. Build a complete Python simulation system using **simpy** (process-based discrete-event simulation).

## Requirements
{requirements}

{simpy_framework_guide}

## Workflow

### Step 1: Plan the Component Hierarchy
Think about the system structure:
- What are the main components (processes)?
- How do they communicate (Store for message passing, Resource for contention)?
- What parameters does each component need?
- Decide: single-file or multi-file organization

### Step 2: Implement
1. Create all model classes with `process()` generator methods
2. Use `simpy.Store` for inter-component communication
3. Use `simpy.Resource` for shared resource contention
4. Write `run.py` as the entry point with argparse and simulation loop
5. Ensure JSONL output matches the specification exactly

### Step 3: Test
Run the simulation with basic parameters to verify it works.

## CRITICAL: I/O Format Compliance
The Input/Output format is a STRICT CONTRACT. Follow it exactly — field names, value formats, entity names, event types, and JSON structure.

## Output
When done, output the result in this format:
<<<GENERATION_RESULT>>>
{{
  "status": "success",
  "sim_cwd": "{workspace}",
  "sim_entry": "run.py",
  "duration": 0.0,
  "error": "",
  "agent": "bare_simpy",
  "mode": "bare_simpy"
}}
<<<GENERATION_RESULT>>>
"""


def _safe_run_opencode(cmd: list, workspace: Path, model_id: str) -> tuple:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["OPENCODE_CONFIG"] = str(workspace / "opencode.json")

    output_lines = []
    timed_out = False
    child_pid = None
    try:
        process = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        child_pid = process.pid

        if process.stdout:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                output_lines.append(line)
        try:
            return_code = process.wait(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(child_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return_code = -1
            timed_out = True
            output_lines.append(f"\n[Timeout] Process timed out after {RUN_TIMEOUT}s\n")
    except Exception as e:
        return_code = -2
        output_lines.append(f"\n[Error] {e}\n")

    return return_code, "".join(output_lines)


def run_bare_simpy(config_path: str, workspace: str, model_id: str) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = setup_workspace(workspace_path, params)

    prompt = BARE_SIMPY_PROMPT.format(
        requirements=requirements_text,
        simpy_framework_guide=SIMPY_FRAMEWORK_GUIDE,
        workspace=str(workspace_path),
    )

    # Write prompt to a file so opencode can read it
    prompt_file = workspace_path / "_bare_simpy_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    cmd = [
        "opencode",
        "run",
        f"Read the requirements from _bare_simpy_prompt.txt and implement the DEVS simulation system using simpy. Follow all instructions carefully.",
    ]

    print(f"[Bare-simpy] Running opencode with simpy guidance...")
    return_code, output = _safe_run_opencode(cmd, workspace_path, model_id)

    duration = time.time() - start_time

    # Check for success
    if return_code == 0 and (workspace_path / "run.py").exists():
        status = "success"
        error = ""
    else:
        status = "failed"
        error = (
            f"opencode exited with code {return_code}"
            if return_code != 0
            else "run.py not found"
        )

    result = {
        "status": status,
        "sim_cwd": str(workspace_path) if status == "success" else "",
        "sim_entry": "run.py" if status == "success" else "",
        "duration": round(duration, 2),
        "error": error,
        "agent": "bare_simpy",
        "mode": "bare_simpy",
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Bare simpy Code Generation with Guidance")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--workspace", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    args = parser.parse_args()

    result = run_bare_simpy(args.config, args.workspace, args.model_id)
    print("\n<<<GENERATION_RESULT>>>")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("<<<GENERATION_RESULT>>>")


if __name__ == "__main__":
    main()
