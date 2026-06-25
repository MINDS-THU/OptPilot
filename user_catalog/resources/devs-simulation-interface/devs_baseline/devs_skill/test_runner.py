#!/usr/bin/env python3
"""
Test runner for all devs_skill modes.
Tests each mode with a simple benchmark (ABP_D1) to verify functionality.
"""

import os
import sys
import json
import argparse
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEST_RUNS_DIR = BASE_DIR / "test_runs"

# Available modes
MODES = {
    "single_shot": {
        "script": "run_single_shot.py",
        "args": ["--benchmark", "ABP_D1"],
    },
    "single_shot_xdevs": {
        "script": "run_single_shot_xdevs.py",
        "args": ["--benchmark", "ABP_D1"],
    },
    "bare_simpy": {
        "script": "run_bare_simpy.py",
        "args": [],
    },
    "bare_xdevs": {
        "script": "run_bare_xdevs.py",
        "args": [],
    },
    "guided": {
        "script": "run_guided_pipeline.py",
        "args": [],
    },
    "guided_xdevs": {
        "script": "run_guided_xdevs.py",
        "args": [],
    },
    "structured_simpy": {
        "script": "run_structured_pipeline.py",
        "args": ["--framework", "simpy"],
    },
    "structured_xdevs": {
        "script": "run_structured_pipeline.py",
        "args": ["--framework", "xdevs"],
    },
}

DEFAULT_BENCHMARK = str(Path(__file__).resolve().parent.parent.parent / "benchmark" / "ABP" / "ABP_D1.yaml")


def run_test(mode: str, model_id: str, config: str, timeout: int = 600) -> dict:
    """Run a single mode test."""
    mode_info = MODES[mode]
    script = BASE_DIR / mode_info["script"]
    workspace = TEST_RUNS_DIR / f"abp_{mode}_{model_id.split('/')[-1]}"

    # Clean workspace
    if workspace.exists():
        import shutil
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(script),
        "--config", config,
        "--workspace", str(workspace),
        "--model_id", model_id,
    ] + mode_info["args"]

    print(f"\n{'='*60}")
    print(f"Testing: {mode}")
    print(f"Model: {model_id}")
    print(f"Script: {script.name}")
    print(f"Workspace: {workspace}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
        )
        duration = time.time() - start

        # Parse GENERATION_RESULT from stdout
        output = result.stdout
        gen_result = None
        if "<<<GENERATION_RESULT>>>" in output:
            parts = output.split("<<<GENERATION_RESULT>>>")
            if len(parts) >= 3:
                try:
                    gen_result = json.loads(parts[-2].strip())
                except json.JSONDecodeError:
                    pass

        return {
            "mode": mode,
            "model": model_id,
            "status": "success" if result.returncode == 0 else "failed",
            "duration": round(duration, 2),
            "exit_code": result.returncode,
            "gen_result": gen_result,
            "workspace": str(workspace),
            "run_py_exists": (workspace / "run.py").exists(),
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        return {
            "mode": mode,
            "model": model_id,
            "status": "timeout",
            "duration": round(duration, 2),
            "exit_code": -1,
            "gen_result": None,
            "workspace": str(workspace),
            "run_py_exists": (workspace / "run.py").exists(),
        }


def main():
    parser = argparse.ArgumentParser(description="Test all devs_skill modes")
    parser.add_argument("--config", default=DEFAULT_BENCHMARK, help="Benchmark config")
    parser.add_argument("--model_id", default="openrouter/openai/gpt-5.2", help="LLM model")
    parser.add_argument("--modes", nargs="*", default=None, help="Specific modes to test")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout per mode (seconds)")
    args = parser.parse_args()

    modes_to_test = args.modes if args.modes else list(MODES.keys())

    results = []
    for mode in modes_to_test:
        if mode not in MODES:
            print(f"Unknown mode: {mode}")
            continue
        result = run_test(mode, args.model_id, args.config, args.timeout)
        results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        status = r["status"]
        run_py = "run.py" if r["run_py_exists"] else "NO run.py"
        gen_status = r["gen_result"].get("status", "N/A") if r["gen_result"] else "N/A"
        print(f"  {r['mode']:25s} | {status:10s} | {gen_status:15s} | {run_py:10s} | {r['duration']:.1f}s")


if __name__ == "__main__":
    main()
