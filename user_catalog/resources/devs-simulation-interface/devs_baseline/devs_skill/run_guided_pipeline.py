"""
Guided DEVS Code Generation Pipeline

Approach: opencode with structural guidance prompt.
- Tells opencode to plan component hierarchy and interface contracts first
- Then implement bottom-up
- Still uses opencode's agent loop (has edit loop risk, but better guided than bare)

This is essentially bare mode + structural guidance prompt.
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
    # Don't delete the directory — just clean its contents
    # This avoids opencode's "working directory was deleted" error
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


GUIDED_PROMPT = """You are a DEVS simulation expert. Build a complete Python simulation system using **simpy**.

## Requirements
{requirements}

## CRITICAL: Structural Guidance

Before writing any code, follow this workflow:

### Step 1: Plan the Component Hierarchy
Think about the system structure:
- What are the main components (atomic models)?
- How do they connect (coupling)?
- What are the input/output ports for each component?
- What parameters does each component need?

Use this interface specification schema as a guide:
```python
class TypedEntity:
    name: str          # Variable or port name (valid Python identifier)
    type: str          # Python type hint (e.g., 'int', 'str', 'Dict[str, float]')
    structure: str     # Detailed format description for complex types

class PortEntity(TypedEntity):
    protocol: str      # Protocol for this port (initiation and data exchange)

class ModelSpecification:
    function: str           # Responsibility & Workflow & Logic
    logging: str            # What to log for debugging
    model_init_args: list   # Constructor parameters
    input_ports: list       # Data inputs
    output_ports: list      # Data outputs
```

### Step 2: Implement Bottom-Up
1. Create a `<SystemName>_libs/` directory for sub-models
2. Write each atomic model as a separate `.py` file in the libs directory
3. Write the top-level coupled model that composes the sub-models
4. Write `run.py` as the entry point

### Step 3: Test
Run the simulation with basic parameters to verify it works.

## Framework: simpy
- `env = simpy.Environment()`
- `yield env.timeout(duration)` for delays
- `store = simpy.Store(env, capacity=N)` for message passing
- `yield store.put(item)` / `item = yield store.get()` for communication
- JSONL output: `print(json.dumps({{"time": env.now, "entity": "...", "event": "...", "payload": {{}}}}), file=sys.stdout, flush=True)`
- Debug output to stderr

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
  "agent": "guided",
  "mode": "guided"
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


def run_guided_pipeline(config_path: str, workspace: str, model_id: str) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = setup_workspace(workspace_path, params)

    prompt = GUIDED_PROMPT.format(
        requirements=requirements_text,
        workspace=str(workspace_path),
    )

    # Write prompt to a file so opencode can read it
    prompt_file = workspace_path / "_guided_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    cmd = [
        "opencode",
        "run",
        f"Read the requirements from _guided_prompt.txt and implement the DEVS simulation system. Follow all instructions carefully.",
    ]

    print(f"[Guided] Running opencode with structural guidance...")
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
        "agent": "guided",
        "mode": "guided",
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Guided DEVS Code Generation Pipeline")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--workspace", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    args = parser.parse_args()

    result = run_guided_pipeline(args.config, args.workspace, args.model_id)
    print("\n<<<GENERATION_RESULT>>>")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("<<<GENERATION_RESULT>>>")


if __name__ == "__main__":
    main()
