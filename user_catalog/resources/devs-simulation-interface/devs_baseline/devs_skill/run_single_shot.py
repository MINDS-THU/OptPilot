"""
Single-shot direct API code generation baseline.

One API call → one run.py file. No opencode, no agent loop, no multi-file.
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


def build_prompt(requirements_text: str, benchmark_name: str) -> str:
    return f"""Write a complete, runnable Python simulation program that satisfies the following requirements.

## Requirements
{requirements_text}

## Environment
- Python 3.10+
- Available packages: simpy, numpy, random, json, sys, argparse, logging, time, math
- Use `import simpy` for discrete-event simulation if needed
- Use standard library only (no pip install)

## Output Contract
- The program MUST be a single file: `run.py`
- Use `argparse` for CLI arguments
- Output results as JSONL to stdout (one JSON object per line)
- All debug/logging output goes to stderr
- The program must run without errors when called with the required arguments

## Instructions
- Write COMPLETE PYTHON CODE that implements the simulation
- Do NOT use placeholder comments or partial code
- Include ALL necessary imports, classes, and functions
- Return the code enclosed in <python_code> and </python_code> tags

<python_code>
"""


def run_single_shot(config_path: str, workspace: str, model_id: str, benchmark_name: str) -> dict:
    start_time = time.time()
    workspace_path = Path(workspace).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    with open(config_path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)

    requirements_text = _build_requirements_text(params.get("requirements", {}))
    prompt = build_prompt(requirements_text, benchmark_name)

    # Call LLM
    print(f"[SingleShot] Calling {model_id}...")
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
            "agent": "single_shot",
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
        print(f"[SingleShot] Syntax error: {e}")

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
        "agent": "single_shot",
        "mode": "single_shot",
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

    print(f"[SingleShot] API call: {api_duration:.1f}s")
    print(f"[SingleShot] Raw response: {len(raw_response)} chars")
    print(f"[SingleShot] Extracted code: {len(code)} chars")
    print(f"[SingleShot] Syntax OK: {syntax_ok}")

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
    parser = argparse.ArgumentParser(description="Single-shot direct API code generation")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--workspace", required=True, help="Path to workspace directory")
    parser.add_argument("--model_id", required=True, help="LLM model ID")
    parser.add_argument("--benchmark", required=True, help="Benchmark name")
    args = parser.parse_args()

    run_single_shot(args.config, args.workspace, args.model_id, args.benchmark)


if __name__ == "__main__":
    main()
