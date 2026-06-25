import os
import sys
import json
import yaml
import uuid
import argparse
import subprocess
import shutil
import signal
import time
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

SKILL_DIR = Path(__file__).parent / "skills"
MATERIALS_DIR = Path(__file__).parent / "materials"
PERMISSION_TEMPLATE = Path(__file__).parent / "opencode_permission.json"

DEFAULT_WORKSPACE_BASE = (
    Path(__file__).resolve().parent.parent.parent / "devs_tester" / "devs_skill_runs"
)

RUN_TIMEOUT = 600  # hard timeout for subprocess in seconds

IO_STRICT_WARNING = """
## CRITICAL: I/O Format Compliance

The Input/Output format specified in the requirements is a STRICT CONTRACT that the checker will validate.
You MUST follow it exactly — field names, value formats, entity names, event types, and JSON structure.
Even minor deviations (capitalization, extra fields, missing fields) will cause the checker to fail.
When in doubt, copy the example output format verbatim and adapt only the values.
"""


def load_requirements(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        if config_path.suffix.lower() == ".json":
            params = json.load(f)
        else:
            params = yaml.safe_load(f)
    return params


def _build_requirements_text(requirements) -> str:
    if isinstance(requirements, dict):
        parts = []
        for key, val in requirements.items():
            if val:
                parts.append(f"### {key.replace('_', ' ').title()}\n{val}")
        return "\n\n".join(parts)
    return str(requirements)


def _framework_instruction(framework: str) -> str:
    if framework == "simpy":
        return """
## Framework Requirement

You MUST use the **simpy** library for this simulation. Do NOT use xdevs.py.
- Use `simpy.Environment`, `simpy.Resource`, `simpy.Store` as needed.
- Model each component as a class with a `process()` generator method.
- Use `yield env.timeout()` for delays, `yield resource.request()` for contention.
"""
    if framework == "xdevs":
        return """
## Framework Requirement

You MUST use the **xdevs.py** library for this simulation. Do NOT use simpy.
- Use `Atomic` models with `initialize`, `lambdaf`, `deltint`, `deltext`, `exit` methods.
- Use `Coupled` models with `add_component` and `add_coupling` (EIC/IC/EOC).
- Use `hold_in(phase, sigma)` for event scheduling.
- Follow the DEVS event sequence: lambdaf sends output BEFORE deltint schedules next event.
"""
    return """
## Framework Selection

Use either `simpy` or `xdevs.py` framework — choose based on what fits the requirements best. Default to `simpy` if unsure.
"""


def _force_rmtree(path: Path):
    """Force-remove a directory, handling read-only files and race conditions."""
    import stat

    if not path.exists():
        return

    def on_error(func, path, exc_info):
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR)
            func(path)
        else:
            raise

    # Use rm -rf directly — it's the most reliable on Linux
    subprocess.run(
        ["rm", "-rf", str(path)],
        capture_output=True,
        timeout=30,
    )
    # Wait for filesystem to settle
    for _ in range(10):
        if not path.exists():
            return
        time.sleep(0.1)

    # Fallback to shutil
    shutil.rmtree(path, onerror=on_error)

    # Final verification
    if path.exists():
        raise RuntimeError(f"Failed to remove workspace: {path}")


def setup_workspace(workspace: Path, params: dict, mode: str, framework: str) -> str:
    # Clean workspace if it already exists
    _force_rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    requirements_text = _build_requirements_text(params.get("requirements", {}))
    root_name = params.get("root_model_name", "DEVSModel")

    # Copy skills (skip for bare mode)
    if mode != "bare":
        opencode_skills = workspace / ".opencode" / "skills"
        opencode_skills.mkdir(parents=True, exist_ok=True)
        for skill_folder in SKILL_DIR.iterdir():
            if skill_folder.is_dir():
                dest = opencode_skills / skill_folder.name
                shutil.copytree(skill_folder, dest, dirs_exist_ok=True)

    # Copy materials
    materials_dest = workspace / "_devs_skill_materials"
    shutil.copytree(MATERIALS_DIR, materials_dest, dirs_exist_ok=True)

    # Create opencode.json with permission restrictions
    if PERMISSION_TEMPLATE.exists():
        with open(PERMISSION_TEMPLATE, "r", encoding="utf-8") as f:
            perm_config = json.load(f)
    else:
        perm_config = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"*": "allow"},
        }

    # Disable snapshot to avoid git issues in workspace
    perm_config["snapshot"] = False
    # Disable autoupdate
    perm_config["autoupdate"] = False

    with open(workspace / "opencode.json", "w", encoding="utf-8") as f:
        json.dump(perm_config, f, indent=2, ensure_ascii=False)

    # Build prompt based on mode + framework
    prompt = _build_prompt(mode, framework, root_name, requirements_text)

    agents_md = workspace / "AGENTS.md"
    agents_md.write_text(prompt, encoding="utf-8")

    return prompt


def _build_prompt(
    mode: str, framework: str, root_name: str, requirements_text: str
) -> str:
    common_constraints = f"""
## Constraints

1. You MUST create a complete, runnable Python project.
2. The entry point MUST be named `run.py` and be executable via `python run.py`.
3. Use `argparse` for CLI arguments as specified in the requirements.
4. Output MUST be JSONL to stdout — one JSON object per line.
5. Debug/info output goes to stderr.
6. Use simulation time, NOT real time.
7. The simulation MUST end within a reasonable real time (under 10 seconds).
8. Create all necessary files in the workspace directory.
9. After completing all files, verify that `run.py` exists and is runnable.
{IO_STRICT_WARNING}
"""

    if mode == "bare":
        return f"""# Task: Build a DEVS Simulation Project

You are building a discrete-event simulation system from requirements.

## System Name
{root_name}

## Requirements

{requirements_text}
{common_constraints}

10. You may use any Python simulation framework (simpy, xdevs.py, or custom). Choose what works best.

Begin now."""

    framework_instr = _framework_instruction(framework)

    skill_guidance = """
## Available Skills

Load the following skills for detailed guidance:
- `task-decomposition`: How to decompose requirements into DEVS model hierarchies
- `code-writing`: How to write DEVS simulation code (both simpy and xdevs.py)

## Process

1. First, read and load the `task-decomposition` skill to understand how to structure the system.
2. Analyze the requirements and plan the model hierarchy.
3. Then, read and load the `code-writing` skill for coding standards.
4. Implement all model files and the `run.py` entry point.
5. Verify the project is complete and runnable.
"""

    return f"""# Task: Build a DEVS Simulation Project

You are building a discrete-event simulation system from requirements.

## System Name
{root_name}

## Requirements

{requirements_text}
{common_constraints}
{framework_instr}
{skill_guidance}

Begin now."""


def _safe_run_opencode(cmd: list, workspace: Path, model_id: str) -> tuple:
    """Run opencode subprocess with timeout. Returns (return_code, output_text).

    Safety: child runs in its own process group (os.setsid).
    On timeout we kill the entire child group via os.killpg, never the parent.
    """
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
            preexec_fn=os.setsid,  # new process group → safe to killpg
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
            # Kill the entire child process group, never the parent
            try:
                os.killpg(os.getpgid(child_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already exited
            process.wait()
            return_code = -1
            timed_out = True
            output_lines.append(
                "\n[Error] Process timed out after {}s\n".format(RUN_TIMEOUT)
            )
    except Exception as e:
        return_code = -2
        output_lines.append(f"\n[Error] {e}\n")

    return return_code, "".join(output_lines)


def get_opencode_cmd(model_id: str) -> list:
    cmd = ["opencode", "run"]
    if model_id:
        cmd.extend(["--model", model_id])
    # Permission control is handled by opencode.json in the workspace
    return cmd


def _check_run_py(workspace: Path) -> tuple:
    """Check for run.py. Returns (sim_cwd, sim_entry)."""
    run_py = workspace / "run.py"
    if run_py.exists():
        return str(run_py.parent), run_py.name
    found = list(workspace.rglob("run.py"))
    if found:
        best = min(found, key=lambda p: len(p.parts))
        return str(best.parent), best.name
    return "", ""


def run_single_mode(
    workspace: Path, prompt: str, model_id: str, mode_label: str
) -> dict:
    cmd = get_opencode_cmd(model_id)
    cmd.append(prompt)

    print(f"[Info] Running opencode in single-call mode ({mode_label})...")
    print(f"[Info] Model: {model_id}")
    print(f"[Info] Workspace: {workspace}")

    start_time = datetime.now()
    return_code, output = _safe_run_opencode(cmd, workspace, model_id)

    status = "success" if return_code == 0 else "failed"
    duration = (datetime.now() - start_time).total_seconds()
    sim_cwd, sim_entry = _check_run_py(workspace)

    if status == "success" and not sim_cwd:
        status = "failed"

    return {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": ""
        if status == "success"
        else f"opencode exited with code {return_code}"
        if return_code >= 0
        else "timeout or crash",
        "agent": "opencode_skill",
        "mode": mode_label,
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }


def run_two_stage_mode(
    workspace: Path, params: dict, model_id: str, framework: str
) -> dict:
    requirements_text = _build_requirements_text(params.get("requirements", {}))
    root_name = params.get("root_model_name", "DEVSModel")

    plan_prompt = f"""# Task: DEVS Architecture Plan

Analyze the following requirements and produce a DEVS model hierarchy plan.

## System Name
{root_name}

## Requirements
{requirements_text}

## Instructions

1. Load the `task-decomposition` skill for guidance.
2. Decompose the system into Atomic and Coupled models.
3. For each model, specify: function, logging, input_ports, output_ports, model_init_args.
4. For Coupled models, describe the coupling_specification (EIC, IC, EOC).
5. Output the plan as a structured JSON.

Do NOT write any code yet. Only produce the decomposition plan."""

    print(f"[Info] Stage 1: Generating architecture plan...")
    start_time = datetime.now()

    cmd1 = get_opencode_cmd(model_id)
    cmd1.append(plan_prompt)

    rc1, plan_output = _safe_run_opencode(cmd1, workspace, model_id)

    framework_instr = _framework_instruction(framework)

    code_prompt = f"""# Task: Implement DEVS Simulation Code

Implement the complete simulation system based on the architecture plan from Stage 1.

{IO_STRICT_WARNING}

## Instructions

1. Load the `code-writing` skill for coding standards.
2. Create all model files as specified in the plan.
3. Create the `run.py` entry point with argparse and simulation loop.
4. {framework_instr.strip()}
5. Output MUST be JSONL to stdout.
6. Verify `run.py` exists and is runnable.

## Architecture Plan (from Stage 1)

{plan_output}

## Original Requirements

{requirements_text}

Begin implementation now."""

    print(f"\n[Info] Stage 2: Implementing code...")

    cmd2 = get_opencode_cmd(model_id)
    cmd2.append(code_prompt)

    rc2, output2 = _safe_run_opencode(cmd2, workspace, model_id)

    status = "success" if rc2 == 0 else "failed"
    duration = (datetime.now() - start_time).total_seconds()
    sim_cwd, sim_entry = _check_run_py(workspace)

    if status == "success" and not sim_cwd:
        status = "failed"

    return {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": "" if status == "success" else f"Stage 1 rc={rc1}, Stage 2 rc={rc2}",
        "agent": "opencode_skill",
        "mode": f"two-stage+{framework}",
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }


def run_bare_mode(workspace: Path, prompt: str, model_id: str) -> dict:
    cmd = get_opencode_cmd(model_id)
    cmd.append(prompt)

    print(f"[Info] Running opencode in BARE mode (no skill guidance)...")
    print(f"[Info] Model: {model_id}")
    print(f"[Info] Workspace: {workspace}")

    start_time = datetime.now()
    return_code, output = _safe_run_opencode(cmd, workspace, model_id)

    status = "success" if return_code == 0 else "failed"
    duration = (datetime.now() - start_time).total_seconds()
    sim_cwd, sim_entry = _check_run_py(workspace)

    if status == "success" and not sim_cwd:
        status = "failed"

    return {
        "status": status,
        "sim_cwd": sim_cwd,
        "sim_entry": sim_entry,
        "duration": duration,
        "error": ""
        if status == "success"
        else f"opencode exited with code {return_code}",
        "agent": "opencode_skill",
        "mode": "bare",
        "token_usage": {model_id: {"input": 0, "output": 0, "thinking": 0, "calls": 0}},
    }


def main():
    parser = argparse.ArgumentParser(description="OpenCode Skill Automation Wrapper")
    parser.add_argument(
        "--config", required=True, help="Task YAML/JSON file with requirements"
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Output workspace root (default: auto-generated under devs_tester/devs_skill_runs/)",
    )
    parser.add_argument(
        "--model_id", default="openrouter/qwen/qwen3-coder", help="LLM model name"
    )
    parser.add_argument(
        "--mode",
        choices=["single", "two-stage", "bare"],
        default="single",
        help="Generation mode: single-call, two-stage (plan then code), or bare (no skills)",
    )
    parser.add_argument(
        "--framework",
        choices=["auto", "simpy", "xdevs"],
        default="auto",
        help="Simulation framework: auto-select, force simpy, or force xdevs.py",
    )
    args = parser.parse_args()

    # Determine workspace path
    if args.workspace:
        workspace = Path(args.workspace).resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:6]
        fw_label = args.framework if args.mode != "bare" else "none"
        workspace = DEFAULT_WORKSPACE_BASE / f"{args.mode}_{fw_label}_{ts}_{uid}"

    # Load requirements
    config_path = Path(args.config).resolve()
    try:
        params = load_requirements(config_path)
    except Exception as e:
        print(f"[Error] Failed to load config: {e}", file=sys.stderr)
        sys.exit(1)

    # Set up workspace
    try:
        prompt = setup_workspace(workspace, params, args.mode, args.framework)
    except Exception as e:
        print(f"[Error] Failed to set up workspace: {e}", file=sys.stderr)
        sys.exit(1)

    # Run based on mode
    if args.mode == "single":
        mode_label = f"single+{args.framework}"
        result = run_single_mode(workspace, prompt, args.model_id, mode_label)
    elif args.mode == "two-stage":
        result = run_two_stage_mode(workspace, params, args.model_id, args.framework)
    else:
        result = run_bare_mode(workspace, prompt, args.model_id)

    print(
        f"\n<<<GENERATION_RESULT>>>\n{json.dumps(result, indent=2)}\n<<<GENERATION_RESULT>>>"
    )

    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
