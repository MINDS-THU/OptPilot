#!/usr/bin/env python3
"""HAMLET Generation Runner — runs a specified framework + model + benchmark.

Each framework in the registry specifies:
  - group:    which runner type (devs_native, baseline_skill, baseline_struct,
              baseline_guided, baseline_runner)
  - conda_env: conda environment to activate (default: hamlet_env)
  - cwd:      working directory for the subprocess (relative to HAMLET_CORE
              if not starting with /)
  - script:   the Python script to invoke (for non-devs_native groups)
  - args/extra: additional CLI flags

Usage:
    python gen_runner.py --framework devs_fast --model openai/qwen3.6-plus \
        --benchmark ABP --workspace /tmp/ws
    python gen_runner.py --list-frameworks
    python gen_runner.py --list-benchmarks
"""
import argparse
import json
import os
import re
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────
HAMLET_CORE   = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = HAMLET_CORE / "benchmark"

# Load .env so API keys are available to child processes
try:
    from dotenv import dotenv_values
    _ef = HAMLET_CORE / ".env"
    if _ef.exists():
        for _k, _v in dotenv_values(_ef).items():
            if _k not in os.environ:
                os.environ[_k] = _v
except Exception:
    pass

# ── Benchmark auto-discovery ─────────────────────────────────────────────────
def discover_benchmarks():
    bms = {}
    if not BENCHMARK_DIR.exists():
        return bms
    for d in sorted(BENCHMARK_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        yamls = list(d.glob("*.yaml"))
        configs = list(d.glob("*config*.json"))
        checkers = sorted(d.glob("*checker*.py"))
        if yamls and configs and checkers:
            bms[d.name] = {
                "gen_config":     str(yamls[0]),
                "test_config":    str(configs[0]),
                "checker_script": str(checkers[0]),
            }
    return bms

BENCHMARKS = discover_benchmarks()

try:
    from experiment_config import BENCHMARKS as _EC_BMS, HAMLET_CORE as _EC_CORE

    for _k, _v in _EC_BMS.items():
        _gen_cfg = _v.get("gen_config")
        _test_cfg = _v.get("test_config")
        _checker = _v.get("checker")
        if not (_gen_cfg and _test_cfg and _checker):
            continue
        BENCHMARKS[_k] = {
            "gen_config": str((_EC_CORE / _gen_cfg).resolve()),
            "test_config": str((_EC_CORE / _test_cfg).resolve()),
            "checker_script": str((_EC_CORE / _checker).resolve()),
        }
except Exception:
    pass


# ── Framework registry ───────────────────────────────────────────────────────
#
# Fields per framework:
#   group      – runner category (see groups below)
#   conda_env  – conda environment name (defaults to "hamlet_env" if omitted)
#   cwd        – subprocess working dir; relative to HAMLET_CORE if not absolute
#   script     – path to the runner script (relative to HAMLET_CORE)
#   mode       – for devs_native: --mode value
#   args       – extra flags appended to the command
#   extra_cfg  – free-form dict (fw_choice, skill_mode, skill_fw, etc.)
#
# Groups:
#   devs_native     → python -m devs_app.run --mode ...
#   baseline_skill  → python devs_baseline/devs_skill/<script>
#   baseline_struct → python devs_baseline/devs_skill/run_structured_pipeline.py
#   baseline_guided → python devs_baseline/devs_skill/run_guided_pipeline.py /
#                     run_guided_xdevs.py / run_bare_simpy.py / etc.
#   baseline_runner → python devs_baseline/<runner_dir>/<script>
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_cwd(cwd_raw: str) -> str:
    """Return absolute cwd.  Relative paths are resolved against HAMLET_CORE."""
    p = Path(cwd_raw)
    if p.is_absolute():
        return str(p)
    return str(HAMLET_CORE / p)


FRAMEWORK_REGISTRY: dict[str, dict] = {
    # ═══════════════ DEVS native (devs_app.run -m) ═══════════════
    "devs_tool": {
        "group": "devs_native", "mode": "tool_debug",
        "args": [],
        "desc": "DEVS construct with check loop",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_fast": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check", "--concur_generate", "--concur_num", "4"],
        "desc": "DEVS fast (no check, concurrent ×4)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_fast_plan": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check", "--concur_generate", "--concur_num", "4",
                 "--construct_variant", "fast_plan"],
        "desc": "DEVS fast plan-then-construct",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_recon": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check", "--concur_generate", "--concur_num", "8",
                 "--construct_variant", "recon"],
        "desc": "DEVS fast plan-then-construct",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_fast_robust": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check", "--concur_generate", "--concur_num", "4",
                 "--construct_variant", "fast_robust"],
        "desc": "DEVS fast robust variant",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_fast_debug": {
        "group": "devs_native", "mode": "tool_debug_agent",
        "args": ["--disable_check", "--concur_generate", "--concur_num", "4",
                 "--manager_use_strong", "--agent_planning_interval", "3",
                 "--agent_max_steps", "90", "--agent_log_level", "INFO"],
        "desc": "DEVS fast with integrated debug loop (strong model)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_concur": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check", "--concur_generate"],
        "desc": "DEVS concurrent (no check, concurrency)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "devs_test": {
        "group": "devs_native", "mode": "tool_debug",
        "args": ["--disable_check"],
        "desc": "DEVS test mode (no check, single)",
        "conda_env": "hamlet_env", "cwd": ".",
    },

    # ═══════════════ Opencode skill (devs_baseline/devs_skill) ═══════════════
    "opencode_skill": {
        "group": "baseline_skill",
        "script": "devs_baseline/devs_skill/run_opencode_skill.py",
        "extra_cfg": {"mode": None, "fw": None},
        "desc": "Opencode + DEVS skill guidance",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "bare": {
        "group": "baseline_skill",
        "script": "devs_baseline/devs_skill/run_opencode_skill.py",
        "extra_cfg": {"mode": "bare", "fw": None},
        "desc": "Opencode bare (no skill guidance)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "bare_simpy": {
        "group": "baseline_skill",
        "script": "devs_baseline/devs_skill/run_bare_simpy.py",
        "extra_cfg": {},
        "desc": "Opencode bare, force simpy",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "bare_xdevs": {
        "group": "baseline_skill",
        "script": "devs_baseline/devs_skill/run_bare_xdevs.py",
        "extra_cfg": {},
        "desc": "Opencode bare, force xdevs",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    # ═══════════════ Single-shot direct API (litellm call → code) ═══════════
    "single": {
        "group": "baseline_single",
        "script": "devs_baseline/devs_skill/run_opencode_skill.py",
        "extra_cfg": {"mode": "single", "fw": "auto"},
        "desc": "Opencode single-shot + skill (auto framework)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "single_simpy": {
        "group": "baseline_single",
        "script": "devs_baseline/devs_skill/run_single_shot.py",
        "extra_cfg": {},
        "desc": "Single-shot direct API → simpy code",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "single_xdevs": {
        "group": "baseline_single",
        "script": "devs_baseline/devs_skill/run_single_shot_xdevs.py",
        "extra_cfg": {},
        "desc": "Single-shot direct API → xdevs code",
        "conda_env": "hamlet_env", "cwd": ".",
    },

    # ═══════════════ Structured pipeline ═══════════════
    "structured": {
        "group": "baseline_struct",
        "script": "devs_baseline/devs_skill/run_structured_pipeline.py",
        "extra_cfg": {"fw_choice": None},
        "desc": "Structured pipeline (plan → code, auto framework)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "structured_simpy": {
        "group": "baseline_struct",
        "script": "devs_baseline/devs_skill/run_structured_pipeline.py",
        "extra_cfg": {"fw_choice": "simpy"},
        "desc": "Structured pipeline, force simpy",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "structured_xdevs": {
        "group": "baseline_struct",
        "script": "devs_baseline/devs_skill/run_structured_pipeline.py",
        "extra_cfg": {"fw_choice": "xdevs"},
        "desc": "Structured pipeline, force xdevs",
        "conda_env": "hamlet_env", "cwd": ".",
    },

    # ═══════════════ Guided pipeline ═══════════════
    "guided": {
        "group": "baseline_guided",
        "script": "devs_baseline/devs_skill/run_guided_pipeline.py",
        "desc": "Guided pipeline (opencode + structural guidance)",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "guided_simpy": {
        "group": "baseline_guided",
        "script": "devs_baseline/devs_skill/run_guided_pipeline.py",
        "extra_cfg": {"framework_instruction": "simpy"},
        "desc": "Guided pipeline, force simpy",
        "conda_env": "hamlet_env", "cwd": ".",
    },
    "guided_xdevs": {
        "group": "baseline_guided",
        "script": "devs_baseline/devs_skill/run_guided_xdevs.py",
        "desc": "Guided pipeline, force xdevs",
        "conda_env": "hamlet_env", "cwd": ".",
    },

    # ═══════════════ External runners (own conda env + subdir) ═══════════════
    "meta_gpt": {
        "group": "baseline_runner",
        "script": "devs_baseline/meta_gpt_run/run_metagpt.py",
        "desc": "MetaGPT multi-agent",
        "conda_env": "metagpt", "cwd": "devs_baseline/meta_gpt_run",
    },
    "swe_agent": {
        "group": "baseline_runner",
        "script": "devs_baseline/swe_agent_run/run_swe_agent.py",
        "desc": "SWE-Agent standard",
        "conda_env": "sweagent", "cwd": "devs_baseline/swe_agent_run",
    },
    "swe_agent_fast": {
        "group": "baseline_runner",
        "script": "devs_baseline/swe_agent_run/run_swe_agent_fast.py",
        "desc": "SWE-Agent fast",
        "conda_env": "sweagent", "cwd": "devs_baseline/swe_agent_run",
    },
    "swe_agent_spd": {
        "group": "baseline_runner",
        "script": "devs_baseline/swe_agent_run/run_swe_agent.py",
        "extra_cfg": {"spd_mode": True},
        "desc": "SWE-Agent speed mode",
        "conda_env": "sweagent", "cwd": "devs_baseline/swe_agent_run",
    },
    "openhands": {
        "group": "baseline_runner",
        "script": "devs_baseline/openhands_run/run_openhands.py",
        "desc": "OpenHands standard",
        "conda_env": "openhands", "cwd": "devs_baseline/openhands_run",
    },
    "openhands_xdevs": {
        "group": "baseline_runner",
        "script": "devs_baseline/openhands_run/run_openhands_xdevs.py",
        "desc": "OpenHands standard",
        "conda_env": "openhands", "cwd": "devs_baseline/openhands_run",
    },
    "openhands_fast": {
        "group": "baseline_runner",
        "script": "devs_baseline/openhands_run/run_openhands_fast.py",
        "desc": "OpenHands fast",
        "conda_env": "openhands", "cwd": "devs_baseline/openhands_run",
    },
    "openhands_spd": {
        "group": "baseline_runner",
        "script": "devs_baseline/openhands_run/run_openhands.py",
        "extra_cfg": {"spd_mode": True},
        "desc": "OpenHands speed mode",
        "conda_env": "openhands", "cwd": "devs_baseline/openhands_run",
    },
}


# ── Logging ──────────────────────────────────────────────────────────────────
def log(tag, msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] [{tag}] {msg}", flush=True)


# ── Helper: conda-aware python command ───────────────────────────────────────
def cmd_python(conda_env: Optional[str] = None):
    if conda_env:
        return ["conda", "run", "-n", conda_env, "--no-capture-output", "python"]
    return [sys.executable]


# ── Build generation command ─────────────────────────────────────────────────
def build_gen_cmd(
    fw_name: str, model_id: str, benchmark_name: str, workspace_dir: Path
) -> list[str]:
    """Build the full command to run generation for the given framework.

    Returns (cmd, cwd_str) where cwd_str is the absolute working directory.
    """
    cfg = FRAMEWORK_REGISTRY[fw_name]
    bm  = BENCHMARKS[benchmark_name]
    group = cfg["group"]
    conda = cfg.get("conda_env")
    cwd   = _resolve_cwd(cfg.get("cwd", "."))

    # ── Group: devs_native ───────────────────────────────────────────────
    if group == "devs_native":
        cmd = cmd_python(conda) + [
            "-m", "devs_app.run",
            "--mode",           cfg["mode"],
            "--debug_args_file", bm["gen_config"],
            "--target_tool",    "devs_construct_tree",
            "--working_directory", str(workspace_dir),
            "--model_id",       model_id,
            "--model_id_strong", model_id,
        ] + cfg.get("args", [])
        return cmd, cwd

    # ── Group: baseline_skill ────────────────────────────────────────────
    if group == "baseline_skill":
        script = HAMLET_CORE / cfg["script"]
        extra  = cfg.get("extra_cfg", {})
        cmd = cmd_python(conda) + [str(script),
            "--config",    bm["gen_config"],
            "--workspace", str(workspace_dir / "devs_project"),
            "--model_id",  model_id,
        ]
        mode = extra.get("mode")
        fw   = extra.get("fw")
        if mode is not None: cmd += ["--mode", mode]
        if fw   is not None: cmd += ["--framework", fw]
        return cmd, cwd

    # ── Group: baseline_single ─────────────────────────────────────────
    if group == "baseline_single":
        # Direct API call scripts — flat workspace, need --benchmark
        script = HAMLET_CORE / cfg["script"]
        cmd = cmd_python(conda) + [str(script),
            "--config",     bm["gen_config"],
            "--workspace",  str(workspace_dir),
            "--model_id",   model_id,
            "--benchmark",  benchmark_name,
        ]
        return cmd, cwd

    # ── Group: baseline_struct ───────────────────────────────────────────
    if group == "baseline_struct":
        script = HAMLET_CORE / cfg["script"]
        extra  = cfg.get("extra_cfg", {})
        cmd = cmd_python(conda) + [str(script),
            "--config",    bm["gen_config"],
            "--workspace", str(workspace_dir / "devs_project"),
            "--model_id",  model_id,
        ]
        fc = extra.get("fw_choice")
        if fc: cmd += ["--framework", fc]
        return cmd, cwd

    # ── Group: baseline_guided ───────────────────────────────────────────
    if group == "baseline_guided":
        script = HAMLET_CORE / cfg["script"]
        cmd = cmd_python(conda) + [str(script),
            "--config",    bm["gen_config"],
            "--workspace", str(workspace_dir / "devs_project"),
            "--model_id",  model_id,
        ]
        return cmd, cwd

    # ── Group: baseline_runner ───────────────────────────────────────────
    if group == "baseline_runner":
        script = HAMLET_CORE / cfg["script"]
        cmd = cmd_python(conda) + [str(script),
            "--config",    bm["gen_config"],
            "--workspace", str(workspace_dir / "devs_project"),
            "--model_id",  model_id,
        ]
        return cmd, cwd

    raise ValueError(f"Unknown group for {fw_name!r}: {group!r}")


# ── Run single generation ────────────────────────────────────────────────────
def run_generation(
    fw_name: str,
    model_id: str,
    benchmark_name: str,
    workspace_dir: Path,
    timeout: int = 1800,
    verbose: bool = True,
) -> dict:
    """Run one generation task.  Returns metadata dict."""
    cfg = FRAMEWORK_REGISTRY.get(fw_name)
    if not cfg:
        return {"status": "unknown_fw", "error": f"Framework {fw_name!r} not found"}
    if benchmark_name not in BENCHMARKS:
        return {"status": "unknown_bm", "error": f"Benchmark {benchmark_name!r} not found"}

    tag = f"{fw_name}/{benchmark_name}"
    log(tag, f"Starting (fw={fw_name}, model={model_id})")

    try:
        cmd, run_cwd = build_gen_cmd(fw_name, model_id, benchmark_name, workspace_dir)
    except Exception as e:
        log(tag, f"Cmd build failed: {e}", "ERROR")
        return {"status": "cmd_error", "error": str(e)}

    workspace_dir.mkdir(parents=True, exist_ok=True)
    stdout_p = workspace_dir / "gen_stdout.log"
    stderr_p = workspace_dir / "gen_stderr.log"
    meta_p   = workspace_dir / "gen_meta.json"

    start = time.time()
    log(tag, f"CWD: {run_cwd}")
    if verbose:
        log(tag, f"CMD: {' '.join(cmd)}")

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        with open(stdout_p, "w") as fo, open(stderr_p, "w") as fe:
            proc = subprocess.run(
                cmd, stdout=fo, stderr=fe,
                cwd=run_cwd, timeout=timeout, env=env,
            )
        dur = round(time.time() - start, 2)

        # Parse handshake signal from stdout+stderr
        output = stdout_p.read_text() + "\n" + stderr_p.read_text()
        sim_cwd = entry_file = None
        token_usage = {}
        m = re.search(
            r"<<<GENERATION_RESULT>>>\s*(.*?)\s*<<<GENERATION_RESULT>>>",
            output, re.DOTALL,
        )
        if m:
            try:
                d = json.loads(m.group(1))
                sim_cwd     = d.get("sim_cwd")
                entry_file  = d.get("sim_entry")
                token_usage = d.get("token_usage", {})
            except json.JSONDecodeError:
                pass

        ok = proc.returncode == 0 and sim_cwd
        status = "success" if ok else "failed"

        meta = {
            "status":       status,
            "returncode":   proc.returncode,
            "duration_sec": dur,
            "start_time":   datetime.now().isoformat(),
            "sim_cwd":      sim_cwd,
            "entry_file":   entry_file,
            "token_usage":  token_usage,
            "framework":    fw_name,
            "model_id":     model_id,
            "benchmark":    benchmark_name,
            "command":      cmd,
            "run_cwd":      run_cwd,
        }
        meta_p.write_text(json.dumps(meta, indent=2))
        log(tag, f"Done ({status}) in {dur}s, sim_cwd={sim_cwd}")
        return meta

    except subprocess.TimeoutExpired:
        dur = round(time.time() - start, 2)
        log(tag, f"TIMEOUT after {dur}s", "ERROR")
        return {"status": "timeout", "duration_sec": dur}
    except Exception as e:
        dur = round(time.time() - start, 2)
        log(tag, f"CRASH: {e}", "ERROR")
        return {"status": "crash", "duration_sec": dur, "error": str(e)}


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="HAMLET Generation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--framework", type=str)
    p.add_argument("--model", type=str, default=None)
    p.add_argument("--benchmark", type=str, default=None)
    p.add_argument("--workspace", type=str, default=None)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--verbose", action="store_true", default=True, dest="verbose")
    p.add_argument("--silent", action="store_false", dest="verbose")
    p.add_argument("--list-frameworks", action="store_true")
    p.add_argument("--list-benchmarks", action="store_true")
    args = p.parse_args()

    if args.list_frameworks:
        print("Frameworks:")
        for n, i in FRAMEWORK_REGISTRY.items():
            env = i.get("conda_env", "hamlet_env")
            cdw = i.get("cwd", ".")
            print(f"  {n:25s} [{env:12s}] cwd={cdw:35s}  {i['desc']}")
        return
    if args.list_benchmarks:
        print("Benchmarks:")
        for n in BENCHMARKS:
            print(f"  {n}")
        return

    if not args.framework or not args.model or not args.benchmark:
        p.error("--framework, --model, --benchmark required")
    if args.framework not in FRAMEWORK_REGISTRY:
        p.error(f"Unknown framework: {args.framework}")
    if args.benchmark not in BENCHMARKS:
        p.error(f"Unknown benchmark: {args.benchmark}")

    ws = Path(args.workspace) if args.workspace else \
         (HAMLET_CORE / "devs_tester" / "gen_workspace")
    ws = ws.resolve()

    result = run_generation(args.framework, args.model, args.benchmark,
                            ws, args.timeout, args.verbose)
    print(json.dumps(result, indent=2))
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
