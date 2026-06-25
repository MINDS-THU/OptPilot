"""
Bare xdevs.py mode with structural guidance.

Uses opencode agent loop (full edit capability) but forces xdevs.py framework.
Allows both single-file and multi-file approaches (LLM decides).
Provides xdevs.py usage guidance in the prompt.
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


XDEVS_FRAMEWORK_GUIDE = """
## xdevs.py Framework Guide

You MUST use **xdevs.py** (formal DEVS framework) for this simulation.

### Core API
```python
from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock
```

### Atomic Model Template
```python
class ModelName(Atomic):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        self.hold_in("<INITIAL_PHASE>", <sigma>)

    def initialize(self):
        self.hold_in("<PHASE>", <sigma>)

    def lambdaf(self):
        # Output ONLY. Do NOT modify state.
        self.output["<port_name>"].add(<payload>)

    def deltint(self):
        # Internal transition (timeout). Update state.
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def deltext(self, e):
        # External transition (input). Process self.input["port"].values
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def exit(self):
        # Cleanup and final KPI logging
        pass
```

### Coupled Model Template (ONLY __init__)
```python
class SystemName(Coupled):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        # Instantiate sub-models
        child = ChildModel(name="child_0", parent=self, <config>)
        self.add_component(child)
        # Define couplings
        self.add_coupling(self.input["port"], child.input["port"])        # EIC
        self.add_coupling(child_a.output["port"], child_b.input["port"])  # IC
        self.add_coupling(child.output["port"], self.output["port"])      # EOC
```

### Entry Point (run.py)
```python
from xdevs.sim import Coordinator, SimulationClock

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate_time", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    root = SystemName(name="system", parent=None, <config>)
    coord = Coordinator(root, clock=SimulationClock(0))
    coord.initialize()
    coord.simulate_time(args.simulate_time)

if __name__ == "__main__":
    main()
```

### Critical Rules
1. **ALL Atomic models MUST implement all 5 abstract methods**: `initialize`, `lambdaf`, `deltint`, `deltext`, `exit`. Missing any one will cause `TypeError: Can't instantiate abstract class`.
2. **Use `Coordinator`** for running simulations with Coupled models, NOT `Simulator` (which is for Atomic-only models).
3. **Event sequence**: `lambdaf` sends output BEFORE `deltint` schedules next event. Payload must be prepared in previous `deltint`/`deltext`/`initialize`.
4. **lambdaf purity**: Only output, no state modification.
5. **hold_in**: Always call at end of `initialize`, `deltint`, `deltext`.
6. **Initial signals**: Use `self.hold_in("INIT", 0)` to schedule immediate event.
7. **Coupled models**: ONLY `__init__` — no `deltint`, `deltext`, `lambdaf`, `initialize`.
8. **add_component** BEFORE **add_coupling**.
9. **JSONL output**: `print(json.dumps({"time": ..., "entity": "...", "event": "...", "payload": {...}}), file=sys.stdout, flush=True)`
10. **Debug to stderr**, JSONL to stdout.

### File Organization
You may choose single-file or multi-file:
- **Single-file**: All models in `run.py` (no imports between model files)
- **Multi-file**: `run.py` + `<SystemName>_libs/` directory with `__init__.py` and sub-model files
  - Use relative imports: `from <SystemName>_libs.ChildModel import ChildModel`
"""

BARE_XDEVS_PROMPT = """You are a DEVS simulation expert. Build a complete Python simulation system using **xdevs.py** (formal DEVS framework).

## Requirements
{requirements}

{xdevs_framework_guide}

## Workflow

### Step 1: Plan the Component Hierarchy
Think about the system structure:
- What are the main components (Atomic models)?
- How do they connect (Coupled model with EIC/IC/EOC)?
- What are the input/output ports for each component?
- What parameters does each component need?
- Decide: single-file or multi-file organization

### Step 2: Implement
1. Create all Atomic model classes with proper state machines
2. Create the Coupled model that composes sub-models
3. Write `run.py` as the entry point with argparse and Simulator
4. Ensure JSONL output matches the specification exactly

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
  "agent": "bare_xdevs",
  "mode": "bare_xdevs"
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


def run_bare_xdevs(config_path: str, workspace: str, model_id: str) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = setup_workspace(workspace_path, params)

    prompt = BARE_XDEVS_PROMPT.format(
        requirements=requirements_text,
        xdevs_framework_guide=XDEVS_FRAMEWORK_GUIDE,
        workspace=str(workspace_path),
    )

    # Write prompt to a file so opencode can read it
    prompt_file = workspace_path / "_bare_xdevs_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    cmd = [
        "opencode",
        "run",
        f"Read the requirements from _bare_xdevs_prompt.txt and implement the DEVS simulation system using xdevs.py. Follow all instructions carefully.",
    ]

    print(f"[Bare-xdevs] Running opencode with xdevs guidance...")
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
        "agent": "bare_xdevs",
        "mode": "bare_xdevs",
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }

    return result


def main():
    parser = argparse.ArgumentParser(description="Bare xdevs.py Code Generation with Guidance")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--workspace", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    args = parser.parse_args()

    result = run_bare_xdevs(args.config, args.workspace, args.model_id)
    print("\n<<<GENERATION_RESULT>>>")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("<<<GENERATION_RESULT>>>")


if __name__ == "__main__":
    main()
