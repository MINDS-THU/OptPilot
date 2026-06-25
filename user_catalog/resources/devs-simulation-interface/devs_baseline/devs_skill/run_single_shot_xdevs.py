"""
Single-shot xdevs.py code generation baseline.

One API call → one run.py file containing ALL xdevs models inline (no multi-file imports).
Adapts xdevs.py patterns from devs_construct_pure for single-file usage.
Records both the raw LLM response and the extracted code.
"""

import os
import sys
import json
import yaml
import argparse
import subprocess
import shutil
import time
import ast
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

import litellm
litellm.drop_params = True
from litellm import completion


def call_llm(model_id: str, messages: list, temperature: float = 0.5) -> str:
    response = completion(model=model_id, messages=messages, temperature=temperature)
    return response.choices[0].message.content


def extract_code(text: str) -> str:
    if "<python_code>" in text and "</python_code>" in text:
        start = text.rindex("<python_code>") + len("<python_code>")
        end = text.index("</python_code>", start)
        return text[start:end].strip()
    for pattern in [r"```(?:py|python)?\s*\n(.*?)\n```", r"```(?:py|python)?\s*\\n(.*?)\\n```"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            code = match.group(1).strip()
            try:
                ast.parse(code)
                return code
            except SyntaxError:
                pass
    return text.strip()


XDEVS_SINGLE_FILE_GUIDE = """
## xdevs.py Single-File Usage Guide

All models must be defined in ONE file (run.py). No multi-file imports.

### Core API

```python
from xdevs.models import Atomic, Coupled, Port
from xdevs.sim import Coordinator, SimulationClock
```

### Atomic Model Pattern

```python
class MyModel(Atomic):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        # Register ports
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        # Initialize state
        self.hold_in("<INITIAL_PHASE>", <sigma>)

    def initialize(self):
        # Set initial state, schedule first event via hold_in(phase, sigma).
        self.hold_in("<PHASE>", <sigma>)

    def lambdaf(self):
        # Output ONLY. Do NOT modify state.
        self.output["<port_name>"].add(<payload>)

    def deltint(self):
        # Internal transition (timeout). Update state, schedule next.
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def deltext(self, e):
        # External transition (input). Process self.input["port"].values.
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def exit(self):
        # Cleanup and final KPI logging. MUST be implemented (abstract method).
        pass
```

### Coupled Model Pattern (ONLY __init__)

```python
class System(Coupled):
    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        super().__init__(name)
        self.parent = parent
        # Instantiate sub-models
        child = ChildModel(name="child_0", parent=self, <config>)
        self.add_component(child)
        # Define couplings
        self.add_coupling(self.input["port"], child.input["port"])   # EIC
        self.add_coupling(child_a.output["port"], child_b.input["port"])  # IC
        self.add_coupling(child.output["port"], self.output["port"])  # EOC
```

### Entry Point Pattern

```python
from xdevs.sim import Coordinator, SimulationClock

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate_time", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    root = System(name="system", parent=None, <config>)
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
10. **All models in ONE file** — no relative imports between model files.
"""


def build_prompt(requirements_text: str, benchmark_name: str) -> str:
    return f"""You are a Python simulation expert. Write a complete, runnable simulation program using **xdevs.py** (formal DEVS framework).

## Requirements
{requirements_text}

{XDEVS_SINGLE_FILE_GUIDE}

## Environment
- Python 3.10+
- Available packages: xdevs, numpy, random, json, sys, argparse, logging, time, math
- Use standard library + xdevs only (no pip install)

## Output Contract
- The program MUST be a single file: `run.py`
- ALL model classes (Atomic and Coupled) must be defined in this ONE file
- Use `argparse` for CLI arguments
- Output results as JSONL to stdout (one JSON object per line)
- All debug/logging output goes to stderr
- The program must run without errors when called with the required arguments

## Critical
- Return ONLY the Python code, enclosed in <python_code> tags
- Do NOT include any explanation before or after the code
- The code must be complete and runnable
- Do NOT use multi-file imports — all models inline

<python_code>
# Your complete run.py implementation here
</python_code>
"""


def run_single_shot_xdevs(config_path: str, workspace: str, model_id: str, benchmark_name: str) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = _build_requirements_text(params.get("requirements", {}))
    prompt = build_prompt(requirements_text, benchmark_name)

    # Call LLM
    print(f"[SingleShot-xdevs] Calling {model_id}...")
    api_start = time.time()
    try:
        raw_response = call_llm(model_id, [{"role": "user", "content": prompt}], temperature=0.5)
    except Exception as e:
        api_duration = time.time() - api_start
        total_duration = time.time() - start_time
        result = {
            "status": "failed",
            "error": str(e),
            "api_duration": round(api_duration, 2),
            "total_duration": round(total_duration, 2),
            "raw_response": "",
            "extracted_code": "",
            "agent": "single_shot_xdevs",
        }
        print(f"\n<<<GENERATION_RESULT>>>")
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"<<<GENERATION_RESULT>>>")
        return result

    api_duration = time.time() - api_start

    # Save raw response
    raw_path = workspace_path / "raw_response.txt"
    raw_path.write_text(raw_response, encoding="utf-8")

    # Extract code
    code = extract_code(raw_response)

    # Save code as run.py
    run_path = workspace_path / "run.py"
    run_path.write_text(code, encoding="utf-8")

    # Validate syntax
    try:
        ast.parse(code)
        syntax_ok = True
    except SyntaxError as e:
        syntax_ok = False
        print(f"[SingleShot-xdevs] Syntax error: {e}")

    total_duration = time.time() - start_time

    # Estimate tokens (rough: ~4 chars per token)
    input_tokens = len(prompt) // 4
    output_tokens = len(raw_response) // 4

    result = {
        "status": "success" if syntax_ok else "syntax_error",
        "sim_cwd": str(workspace_path),
        "sim_entry": "run.py",
        "duration": round(total_duration, 2),
        "error": "" if syntax_ok else "Syntax error in generated code",
        "agent": "single_shot_xdevs",
        "mode": "single_shot_xdevs",
        "api_duration": round(api_duration, 2),
        "syntax_ok": syntax_ok,
        "raw_response_length": len(raw_response),
        "code_length": len(code),
        "token_usage": {
            model_id: {
                "input": input_tokens,
                "output": output_tokens,
                "thinking": 0,
                "calls": 1,
            }
        },
    }

    print(f"[SingleShot-xdevs] API call: {api_duration:.1f}s")
    print(f"[SingleShot-xdevs] Raw response: {len(raw_response)} chars")
    print(f"[SingleShot-xdevs] Extracted code: {len(code)} chars")
    print(f"[SingleShot-xdevs] Syntax OK: {syntax_ok}")

    print(f"\n<<<GENERATION_RESULT>>>")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"<<<GENERATION_RESULT>>>")

    # Also save result metadata
    meta_path = workspace_path / "generation_result.json"
    meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return result


def _build_requirements_text(requirements: dict) -> str:
    parts = []
    for key in ["general", "scenario", "args_input_output"]:
        val = requirements.get(key, "")
        if val:
            parts.append(f"## {key.replace('_', ' ').title()}\n{val}")
    return "\n\n".join(parts) if parts else "No specific requirements provided."


def main():
    parser = argparse.ArgumentParser(description="Single-shot xdevs.py code generation")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--workspace", required=True, help="Path to workspace directory")
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    parser.add_argument("--benchmark", required=True, help="Benchmark name")
    args = parser.parse_args()

    run_single_shot_xdevs(args.config, args.workspace, args.model_id, args.benchmark)


if __name__ == "__main__":
    main()
