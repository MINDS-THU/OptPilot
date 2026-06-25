"""
Structured DEVS Code Generation Pipeline (v6) — JSON-first approach with framework support.

Phase 1: opencode creates .json specification files
  - opencode plans component hierarchy
  - Creates directory tree with .json files (ModelSpecification schema)
  - No code generation, just specs

Phase 2: litellm reads .json → generates .py code (FULLY PARALLEL)
  - Bottom-up traversal of .json files
  - Each .json → 1 litellm call → .py file
  - All siblings generated in parallel
  - Interface mismatches left for Phase 3 to adjust

Phase 3: Generate run.py (handles interface mismatches)
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
import ast
import re
import concurrent.futures
from pathlib import Path
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PERMISSION_TEMPLATE = BASE_DIR / "opencode_permission.json"
RUN_TIMEOUT = 600

# ── LLM API ──────────────────────────────────────────────────────────────────
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
    for pattern in [
        r"```(?:py|python)?\s*\n(.*?)\n```",
        r"```(?:py|python)?\s*\\n(.*?)\\n```",
    ]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            code = match.group(1).strip()
            try:
                ast.parse(code)
                return code
            except SyntaxError:
                pass
    return text.strip()


# ── Workspace Setup ──────────────────────────────────────────────────────────


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

    subprocess.run(
        ["rm", "-rf", str(path)], check=False, timeout=30, capture_output=True
    )
    for _ in range(20):
        if not path.exists():
            return
        time.sleep(0.2)
    try:
        shutil.rmtree(path, onerror=on_error)
    except Exception:
        pass
    if path.exists():
        raise RuntimeError(f"Failed to remove: {path}")


def setup_workspace(workspace: Path, params: dict) -> str:
    _force_rmtree(workspace)
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


# ── Phase 1: opencode creates .json specs ────────────────────────────────────


def _json_creation_prompt(framework: str, requirements: str) -> str:
    if framework == "xdevs":
        fw_instruction = """4. Use **xdevs.py** framework — mention this in the function description
5. Atomic models use: `Atomic` base class, `initialize`, `lambdaf`, `deltint`, `deltext`, `exit` methods, `hold_in(phase, sigma)`
6. Coupled models use: `Coupled` base class, `add_component`, `add_coupling` (EIC/IC/EOC)
7. All models output JSONL to stdout: `print(json.dumps({"time": ..., "entity": "...", "event": "...", "payload": {...}}), file=sys.stdout, flush=True)`
8. Debug output goes to stderr"""
    else:
        fw_instruction = """4. Use **simpy** framework — mention this in the function description
5. All models output JSONL to stdout: `print(json.dumps({"time": env.now, "entity": "...", "event": "...", "payload": {}}), file=sys.stdout, flush=True)`
6. Debug output goes to stderr"""

    return f"""You are a DEVS System Architect. Your task is to create specification files for a simulation system.

## Requirements
{requirements}

## Workflow

### Step 1: Think
Analyze the requirements and plan the component hierarchy:
- What are the main components?
- How do they connect?
- What are the input/output ports?
- What parameters does each need?

### Step 2: Create .json Specification Files
For EACH component, create a `.json` file with this exact schema:

```json
{{
  "class_name": "ComponentName",
  "type": "atomic",
  "specification": {{
    "function": "Detailed description of responsibility, workflow, and logic",
    "logging": "What specific data should be logged for debugging/analysis",
    "model_init_args": [
      {{"name": "param1", "type": "int", "structure": "Description of this parameter"}},
      {{"name": "param2", "type": "float", "structure": "Description of this parameter"}}
    ],
    "input_ports": [
      {{"name": "port_name", "type": "dict", "structure": "Expected keys and value constraints", "protocol": "How this port is used"}}
    ],
    "output_ports": [
      {{"name": "port_name", "type": "dict", "structure": "Expected keys and value constraints", "protocol": "How this port is used"}}
    ]
  }}
}}
```

### Directory Structure Rules
1. Root component: `<SystemName>.json` at workspace root
2. Sub-components: `<SystemName>_libs/<ComponentName>.json`
3. Create `__init__.py` in each `_libs/` directory
{fw_instruction}

### Example Structure
```
workspace/
├── ABPSystem.json              ← Root coupled model spec
├── ABPSystem_libs/
│   ├── __init__.py
│   ├── Sender.json             ← Atomic model spec
│   ├── Receiver.json           ← Atomic model spec
│   └── Subnet.json             ← Atomic model spec
```

## Important
- Create .json files only, NO Python code
- Be specific in function descriptions — this is what the code generator will use
- Keep type hints simple (int, float, str, dict, list)
- For complex structures, describe them in the "structure" field
- Do NOT output any GENERATION_RESULT — just create the files and exit.
"""


def _safe_run_opencode(cmd: list, workspace: Path) -> tuple:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["OPENCODE_CONFIG"] = str(workspace / "opencode.json")
    output_lines = []
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
            output_lines.append(f"\n[Timeout] Process timed out after {RUN_TIMEOUT}s\n")
    except Exception as e:
        return_code = -2
        output_lines.append(f"\n[Error] {e}\n")
    return return_code, "".join(output_lines)


def create_json_specs(
    workspace: Path, requirements_text: str, model_id: str, framework: str
) -> tuple:
    """Phase 1: opencode creates .json specification files."""
    prompt = _json_creation_prompt(framework, requirements_text)
    prompt_file = workspace / "_json_creation_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    cmd = [
        "opencode",
        "run",
        "Read the instructions from _json_creation_prompt.txt and create the .json specification files. Follow all instructions carefully.",
    ]

    print(f"[Phase 1] opencode creating .json specs (framework={framework})...")
    return_code, output = _safe_run_opencode(cmd, workspace)

    json_files = list(workspace.glob("*.json"))
    json_files = [
        f
        for f in json_files
        if f.name != "opencode.json" and not f.name.startswith("_")
    ]
    if not json_files:
        raise RuntimeError("No .json specification files created by opencode")

    root_json = json_files[0]
    print(f"[Phase 1] Found root spec: {root_json.name}")
    return root_json, return_code


# ── Phase 2: litellm generates code from .json ───────────────────────────────

# ── simpy prompts ──

SIMPY_ATOMIC_PROMPT = """Write a complete Python file for a DEVS simulation model.

## Model Specification
{spec_json}

## Sub-Models (already implemented)
{sub_models_info}

{import_section}

## Framework: simpy
- `env = simpy.Environment()`
- `yield env.timeout(duration)` for delays
- `store = simpy.Store(env, capacity=N)` for message passing
- `yield store.put(item)` / `item = yield store.get()` for communication
- JSONL output: `print(json.dumps({{"time": env.now, "entity": "...", "event": "...", "payload": {{}}}}), file=sys.stdout, flush=True)`
- Debug output to stderr
- Use `argparse` for CLI arguments

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""

SIMPY_COUPLED_PROMPT = """Write a complete Python file for a DEVS simulation model.

## Model Specification
{spec_json}

## Sub-Models (already implemented)
{sub_models_info}

{import_section}

## Framework: simpy (Coupled Model)
- This is a container that composes sub-models
- Create simpy Environment
- Instantiate sub-models and start their processes
- Run simulation: `env.run(until=simulate_time)`
- Use argparse for CLI arguments
- JSONL output to stdout, debug to stderr

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""

# ── xdevs.py prompts ──

XDEVS_ATOMIC_PROMPT = """Write a complete Python file for a DEVS simulation model using xdevs.py.

## Model Specification
{spec_json}

## Sub-Models (already implemented)
{sub_models_info}

{import_section}

## Framework: xdevs.py (Atomic Model)
- Import: `from xdevs.models import Atomic, Coupled, Port`
- Inherit from `Atomic`
- Constructor: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
  - Call `super().__init__(name)`, set `self.parent = parent`
  - Register ports: `self.add_in_port("port_name")`, `self.add_out_port("port_name")`
  - Initialize state, call `self.hold_in("passive", float('inf'))`
- Required methods:
  - `initialize(self)`: Set initial phase/sigma via `hold_in`, log. Can schedule immediate event with `hold_in("INIT", 0)`
  - `lambdaf(self)`: ONLY output via `self.output["port"].add(payload)`. NO state modification.
  - `deltint(self)`: Handle internal timeout, update state, prepare next payload, call `hold_in(phase, sigma)`
  - `deltext(self, e)`: Handle incoming input from `self.input["port"].values`, update state, call `hold_in(phase, sigma)`
  - `exit(self)`: Final cleanup and KPI logging
- CRITICAL event sequence: `lambdaf` sends output BEFORE `deltint` schedules next event. Payload must be prepared in previous `deltint`/`deltext`/`initialize`.
- JSONL output: `print(json.dumps({{"time": ..., "entity": "...", "event": "...", "payload": {{}}}}), file=sys.stdout, flush=True)`
- Debug output to stderr

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""

XDEVS_COUPLED_PROMPT = """Write a complete Python file for a DEVS simulation model using xdevs.py.

## Model Specification
{spec_json}

## Sub-Models (already implemented)
{sub_models_info}

{import_section}

## Framework: xdevs.py (Coupled Model)
- Import: `from xdevs.models import Atomic, Coupled, Port`
- Inherit from `Coupled`
- ONLY `__init__` is implemented — no `deltint`, `deltext`, `lambdaf`, `initialize`
- Constructor: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
  - Call `super().__init__(name)`, set `self.parent = parent`
  - Register ports: `self.add_in_port("port_name")`, `self.add_out_port("port_name")`
  - Instantiate sub-models and call `self.add_component(sub_model)`
  - Define couplings via `self.add_coupling(from_port, to_port)`:
    - EIC (External Input): `self.input["port"]` -> `child.input["port"]`
    - IC (Internal): `child_a.output["port"]` -> `child_b.input["port"]`
    - EOC (External Output): `child.output["port"]` -> `self.output["port"]`
  - IMPORTANT: `add_component` must be called BEFORE `add_coupling`
- JSONL output: `print(json.dumps({{"time": ..., "entity": "...", "event": "...", "payload": {{}}}}), file=sys.stdout, flush=True)`
- Debug output to stderr

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""

# ── run.py prompts ──

SIMPY_RUN_PY_PROMPT = """Create a `run.py` entry point for a DEVS simulation system.

## System Info
- Root model class: `{root_class}`
- Root model file: `{root_file}`
- Import: `from {root_module} import {root_class}`
- Sub-models: {sub_models}

## Root Model Spec
{root_spec}

## Rules
1. Use argparse for CLI arguments
2. Use simpy for simulation
3. Import and instantiate the root model
4. Run simulation with `env.run(until=args.simulate_time)`
5. JSONL output to stdout, debug to stderr
6. Use system time to set random seed

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""

XDEVS_RUN_PY_PROMPT = """Create a `run.py` entry point for a DEVS simulation system using xdevs.py.

## System Info
- Root model class: `{root_class}`
- Root model file: `{root_file}`
- Import: `from {root_module} import {root_class}`
- Sub-models: {sub_models}

## Root Model Spec
{root_spec}

## Rules
1. Use argparse for CLI arguments
2. Use xdevs.py for simulation
3. Import the root model
4. Create simulation: `from xdevs.simulator import Simulator` → `sim = Simulator(root_model)` → `sim.initialize()` → `sim.run(until=simulate_time)`
5. JSONL output to stdout, debug to stderr
6. Use system time to set random seed

## Output
Return ONLY the Python code in <python_code> tags.

<python_code>
# Your code here
</python_code>
"""


def load_json_spec(json_path: Path) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_children(workspace: Path, parent_json: Path) -> List[Path]:
    """Find child .json files in the _libs directory."""
    parent_name = parent_json.stem
    libs_dir = workspace / f"{parent_name}_libs"
    if not libs_dir.exists():
        return []
    children = sorted(libs_dir.glob("*.json"))
    return [c for c in children if not c.name.startswith("_")]


def _build_code_prompt(framework: str, spec: dict, children_specs: List[dict]) -> str:
    """Build the appropriate prompt based on framework and model type."""
    spec_json = json.dumps(spec, indent=2)
    model_type = spec.get("type", "atomic")

    sub_models_info = ""
    import_section = ""
    if children_specs:
        sub_lines = []
        import_lines = []
        for child in children_specs:
            child_name = child.get("class_name", "Unknown")
            child_func = child.get("specification", {}).get("function", "")
            child_file = child.get("_file_path", "")
            sub_lines.append(f"- {child_name}: {child_func[:100]}")

            if child_file:
                rel_path = child_file.replace("/", ".").replace("\\", ".")
                if rel_path.endswith(".py"):
                    rel_path = rel_path[:-3]
                import_lines.append(f"from {rel_path} import {child_name}")

        sub_models_info = "\n".join(sub_lines)
        import_section = (
            f"## Required Imports\n```python\n" + "\n".join(import_lines) + "\n```"
        )

    if framework == "xdevs":
        if model_type == "coupled":
            prompt = XDEVS_COUPLED_PROMPT.format(
                spec_json=spec_json,
                sub_models_info=sub_models_info,
                import_section=import_section,
            )
        else:
            prompt = XDEVS_ATOMIC_PROMPT.format(
                spec_json=spec_json,
                sub_models_info=sub_models_info,
                import_section=import_section,
            )
    else:
        if model_type == "coupled":
            prompt = SIMPY_COUPLED_PROMPT.format(
                spec_json=spec_json,
                sub_models_info=sub_models_info,
                import_section=import_section,
            )
        else:
            prompt = SIMPY_ATOMIC_PROMPT.format(
                spec_json=spec_json,
                sub_models_info=sub_models_info,
                import_section=import_section,
            )

    return prompt


def generate_code_from_spec(
    model_id: str,
    framework: str,
    spec: dict,
    workspace: Path,
    children_specs: List[dict] = None,
) -> str:
    """Generate Python code from a .json specification."""
    prompt = _build_code_prompt(framework, spec, children_specs or [])

    for attempt in range(2):
        try:
            response = call_llm(
                model_id, [{"role": "user", "content": prompt}], temperature=0.5
            )
            code = extract_code(response)
            ast.parse(code)
            return code
        except Exception as e:
            print(
                f"  Code gen attempt {attempt + 1} failed for {spec.get('class_name', 'unknown')}: {e}"
            )
    raise RuntimeError(
        f"Failed to generate code for {spec.get('class_name', 'unknown')}"
    )


def _generate_single_child(
    model_id: str, framework: str, child_json: Path, workspace: Path
) -> dict:
    """Generate code for a single child and its descendants (fully parallel)."""
    spec = load_json_spec(child_json)

    # Find and generate grandchildren first (parallel)
    grandchildren_jsons = find_children(workspace, child_json)
    children_specs = []
    if grandchildren_jsons:
        libs_dir = child_json.parent / f"{child_json.stem}_libs"
        if not libs_dir.exists():
            libs_dir = workspace / f"{spec['class_name']}_libs"
            libs_dir.mkdir(parents=True, exist_ok=True)
            (libs_dir / "__init__.py").write_text("")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(grandchildren_jsons))
        ) as executor:
            futures = {
                executor.submit(
                    _generate_single_child, model_id, framework, gc, workspace
                ): gc
                for gc in grandchildren_jsons
            }
            for future in concurrent.futures.as_completed(futures):
                gc_path = futures[future]
                try:
                    child_spec = future.result()
                    children_specs.append(child_spec)
                except Exception as e:
                    print(f"  ✗ Failed to generate {gc_path.stem}: {e}")

    # Generate code for this child
    code = generate_code_from_spec(model_id, framework, spec, workspace, children_specs)

    # Save as .py
    py_path = child_json.with_suffix(".py")
    py_path.write_text(code, encoding="utf-8")

    # Add file path for parent's import
    spec["_file_path"] = str(py_path.relative_to(workspace))

    print(f"  ✓ Generated {spec['class_name']} from {child_json.name}")
    return spec


def generate_code_bottom_up(
    model_id: str, framework: str, root_json: Path, workspace: Path
) -> dict:
    """Phase 2: Generate code bottom-up from .json specs (fully parallel)."""
    spec = load_json_spec(root_json)

    # Find and generate children (parallel)
    children_jsons = find_children(workspace, root_json)
    children_specs = []
    if children_jsons:
        libs_dir = root_json.parent / f"{root_json.stem}_libs"
        if libs_dir.exists():
            (libs_dir / "__init__.py").write_text("")

        max_workers = min(8, len(children_jsons))
        print(
            f"  Generating {len(children_jsons)} children in parallel (workers={max_workers})..."
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _generate_single_child, model_id, framework, cj, workspace
                ): cj
                for cj in children_jsons
            }
            for future in concurrent.futures.as_completed(futures):
                cj_path = futures[future]
                try:
                    child_spec = future.result()
                    children_specs.append(child_spec)
                except Exception as e:
                    print(f"  ✗ Failed to generate {cj_path.stem}: {e}")

    # Generate root model code
    code = generate_code_from_spec(model_id, framework, spec, workspace, children_specs)
    py_path = root_json.with_suffix(".py")
    py_path.write_text(code, encoding="utf-8")
    spec["_file_path"] = str(py_path.relative_to(workspace))
    spec["_children"] = children_specs

    print(f"  ✓ Generated {spec['class_name']} from {root_json.name}")
    return spec


def generate_run_py(
    model_id: str, framework: str, workspace: Path, root_spec: dict
) -> str:
    """Generate run.py entry point."""
    root_class = root_spec.get("class_name", "System")
    root_file = root_spec.get("_file_path", f"{root_class}.py")
    root_module = root_file.replace(".py", "").replace("/", ".").replace("\\", ".")
    sub_models = ", ".join(
        [c.get("class_name", "?") for c in root_spec.get("_children", [])]
    )

    if framework == "xdevs":
        prompt = XDEVS_RUN_PY_PROMPT.format(
            root_class=root_class,
            root_file=root_file,
            root_module=root_module,
            sub_models=sub_models,
            root_spec=json.dumps(root_spec.get("specification", {}), indent=2),
        )
    else:
        prompt = SIMPY_RUN_PY_PROMPT.format(
            root_class=root_class,
            root_file=root_file,
            root_module=root_module,
            sub_models=sub_models,
            root_spec=json.dumps(root_spec.get("specification", {}), indent=2),
        )

    for attempt in range(2):
        try:
            response = call_llm(
                model_id, [{"role": "user", "content": prompt}], temperature=0.5
            )
            code = extract_code(response)
            ast.parse(code)
            run_path = workspace / "run.py"
            run_path.write_text(code, encoding="utf-8")
            return code
        except Exception as e:
            print(f"  run.py attempt {attempt + 1} failed: {e}")
    raise RuntimeError("Failed to generate run.py")


# ── Main Pipeline ────────────────────────────────────────────────────────────


def run_structured_pipeline(
    config_path: str, workspace: str, model_id: str, framework: str = "auto"
) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = setup_workspace(workspace_path, params)

    # Phase 1: opencode creates .json specs
    print(f"\n{'=' * 60}")
    print(
        f"[Phase 1] opencode creating .json specification files (framework={framework})..."
    )
    print(f"{'=' * 60}")
    phase1_start = time.time()
    try:
        root_json, return_code = create_json_specs(
            workspace_path, requirements_text, model_id, framework
        )
        phase1_duration = time.time() - phase1_start
        print(f"[Phase 1] Completed in {phase1_duration:.1f}s")
    except Exception as e:
        phase1_duration = time.time() - phase1_start
        print(f"[Phase 1] Failed: {e}")
        return {
            "status": "failed",
            "phase": "json_creation",
            "error": str(e),
            "phase1_duration": round(phase1_duration, 2),
            "total_duration": round(time.time() - start_time, 2),
        }

    # Phase 2: litellm generates code from .json (fully parallel)
    print(f"\n{'=' * 60}")
    print(
        f"[Phase 2] litellm generating code from .json specs (framework={framework})..."
    )
    print(f"{'=' * 60}")
    phase2_start = time.time()
    try:
        root_spec = generate_code_bottom_up(
            model_id, framework, root_json, workspace_path
        )
        phase2_duration = time.time() - phase2_start
        print(f"[Phase 2] Completed in {phase2_duration:.1f}s")
    except Exception as e:
        phase2_duration = time.time() - phase2_start
        print(f"[Phase 2] Failed: {e}")
        return {
            "status": "failed",
            "phase": "code_generation",
            "error": str(e),
            "phase1_duration": round(phase1_duration, 2),
            "phase2_duration": round(phase2_duration, 2),
            "total_duration": round(time.time() - start_time, 2),
        }

    # Phase 3: Generate run.py (handles interface mismatches)
    print(f"\n{'=' * 60}")
    print(f"[Phase 3] Generating run.py (framework={framework})...")
    print(f"{'=' * 60}")
    phase3_start = time.time()
    try:
        generate_run_py(model_id, framework, workspace_path, root_spec)
        phase3_duration = time.time() - phase3_start
        print(f"[Phase 3] Completed in {phase3_duration:.1f}s")
    except Exception as e:
        phase3_duration = time.time() - phase3_start
        print(f"[Phase 3] Failed: {e}")
        return {
            "status": "failed",
            "phase": "runpy_generation",
            "error": str(e),
            "phase1_duration": round(phase1_duration, 2),
            "phase2_duration": round(phase2_duration, 2),
            "phase3_duration": round(phase3_duration, 2),
            "total_duration": round(time.time() - start_time, 2),
        }

    total_duration = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"[Done] Structured pipeline (v6) completed in {total_duration:.1f}s")
    print(
        f"  Phase 1 (json): {phase1_duration:.1f}s | Phase 2 (code): {phase2_duration:.1f}s | Phase 3 (run.py): {phase3_duration:.1f}s"
    )
    print(f"{'=' * 60}")

    return {
        "status": "success",
        "sim_cwd": str(workspace_path),
        "sim_entry": "run.py",
        "duration": round(total_duration, 2),
        "error": "",
        "agent": "structured_v6",
        "mode": "structured",
        "framework": framework,
        "phase1_duration": round(phase1_duration, 2),
        "phase2_duration": round(phase2_duration, 2),
        "phase3_duration": round(phase3_duration, 2),
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }


def main():
    parser = argparse.ArgumentParser(
        description="Structured DEVS Code Generation Pipeline (v6)"
    )
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument(
        "--workspace", required=True, help="Path to workspace directory"
    )
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    parser.add_argument(
        "--framework",
        default="auto",
        choices=["auto", "simpy", "xdevs"],
        help="Framework to use",
    )
    args = parser.parse_args()

    result = run_structured_pipeline(
        args.config, args.workspace, args.model_id, args.framework
    )
    print("\n<<<GENERATION_RESULT>>>")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print("<<<GENERATION_RESULT>>>")


if __name__ == "__main__":
    main()
