#!/usr/bin/env python3
"""HAMLET Unified Experiment Runner.

Reads experiment_config.py and orchestrates generation + evaluation.
All results are saved into isolated, timestamped run directories under HAMLET_core/generated/.

Usage:
    # Single task (creates a new timestamped run folder)
    python run_simple.py --framework single_simpy --model gpt_5_2 --benchmark ABP

    # Custom run name (saves to generated/my_experiment_v1/)
    python run_simple.py --framework single_simpy --model gpt_5_2 --benchmark ABP --run-name my_experiment_v1

    # Batch (all tasks from experiment_config)
    python run_simple.py

    # Resume the most recent run (skips already completed tasks in that run folder)
    python run_simple.py --resume

    # Dry-run (show what would run in the latest/specified folder)
    python run_simple.py --resume --dry-run

    # List benchmarks / models / frameworks
    python run_simple.py --list-benchmarks
    python run_simple.py --list-models
    python run_simple.py --list-frameworks
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────
THIS_DIR       = Path(__file__).resolve().parent
HAMLET_CORE    = THIS_DIR.parent
BASE_GEN_DIR   = HAMLET_CORE / "generated"

# Import config & sub-runners
sys.path.insert(0, str(THIS_DIR))
from experiment_config import * # noqa: F401,F403
from gen_runner   import run_generation, FRAMEWORK_REGISTRY
from eval_runner  import run_eval_pipeline, BENCHMARKS as EVAL_BMS

# Load .env
try:
    from dotenv import dotenv_values
    _ef = HAMLET_CORE / ".env"
    if _ef.exists():
        for _k, _v in dotenv_values(_ef).items():
            if _k not in os.environ:
                os.environ[_k] = _v
except Exception:
    pass


# ── Logging ──────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


# ── Helper: resolve config paths ─────────────────────────────────────────
def resolve_bm(bm_name: str) -> dict:
    """Resolve a benchmark name to absolute config paths."""
    bm = BENCHMARKS.get(bm_name)
    if bm is None:
        raise ValueError(f"Unknown benchmark: {bm_name}")
    return {
        "gen_config":  str((HAMLET_CORE / bm["gen_config"]).resolve()),
        "test_config": str((HAMLET_CORE / bm["test_config"]).resolve()),
        "checker":     str((HAMLET_CORE / bm["checker"]).resolve()),
    }


# ── Check if task already completed ──────────────────────────────────────
def is_completed(fw: str, ms: str, bm: str, run_dir: Path) -> bool:
    """Check if task already has results with a score in the specific run dir."""
    meta = run_dir / f"{fw}_{ms}" / bm / "run_meta.json"
    if not meta.exists():
        return False
    try:
        d = json.load(open(meta))
        return d.get("evaluation", {}).get("total_score") is not None
    except Exception:
        return False


# ── Run single task ─────────────────────────────────────────────────────
def run_task(fw: str, ms: str, bm: str, run_dir: Path) -> dict:
    """Run one framework × model × benchmark task in the specified run_dir."""
    model_id = EXPERIMENT_LLMS[ms]
    timeout  = get_gen_timeout(fw)

    dest = run_dir / f"{fw}_{ms}" / bm
    dest.mkdir(parents=True, exist_ok=True)

    log(f"  RUN: {fw}/{bm}/{ms} (gen_timeout={timeout}s)")
    
    # ── Phase 1: Generation ─────────────────────────────────────────────
    gen_start = time.time()
    gen_result = run_generation(fw, model_id, bm, dest, timeout)
    gen_dur = round(time.time() - gen_start, 2)

    log(f"  GEN: {gen_result.get('status', 'unknown')} in {gen_dur}s")

    # Fallback check if it timed out but generated valid code
    if gen_result.get("status") != "success":
        is_timeout = "timeout" in str(gen_result.get("status", "")).lower()
        run_py = dest / "run.py"
        if is_timeout and run_py.exists():
            try:
                compile(run_py.read_text(), str(run_py), "exec")
                log(f"  GEN: timeout but run.py exists and syntax OK — treating as success")
                gen_result["status"] = "success"
            except SyntaxError:
                pass

    if gen_result.get("status") != "success":
        meta = {
            "experiment": {"framework": fw, "model_id": model_id, "benchmark": bm},
            "generation": gen_result,
            "evaluation": {"status": "skipped", "reason": "gen_failed"},
            "totals": {
                "generation_duration_sec": gen_dur,
                "evaluation_duration_sec": 0,
                "total_duration_sec": gen_dur,
                "total_score": None,
                "token_usage": gen_result.get("token_usage", {}),
            },
        }
        (dest / "run_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        return meta

    # ── Phase 2: Evaluation ─────────────────────────────────────────────
    sim_cwd = gen_result.get("sim_cwd")
    if sim_cwd:
        sim_cwd_path = Path(sim_cwd) if os.path.isabs(sim_cwd) else (dest / sim_cwd)

        if sim_cwd_path.exists():
            eval_start = time.time()
            ev_result = run_eval_pipeline(bm, str(sim_cwd_path), "run.py", dest, timeout=EVAL_TIMEOUT)
            eval_dur = round(time.time() - eval_start, 2)
            log(f"  EVAL: {ev_result['status']} in {eval_dur}s, score={ev_result.get('total_score')}")
        else:
            ev_result = {"status": "skipped", "reason": f"sim_cwd not found: {sim_cwd_path}"}
            eval_dur = 0
            log(f"  EVAL: skipped (sim_cwd not found)")
    else:
        ev_result = {"status": "skipped", "reason": "no sim_cwd"}
        eval_dur = 0
        log(f"  EVAL: skipped (no sim_cwd)")

    total_dur = round(gen_dur + eval_dur, 2)

    tokens = gen_result.get("token_usage", {})
    score = ev_result.get("total_score")
    meta = {
        "experiment": {"framework": fw, "model_id": model_id, "benchmark": bm},
        "generation": gen_result,
        "evaluation": ev_result,
        "totals": {
            "generation_duration_sec": gen_dur,
            "evaluation_duration_sec": eval_dur,
            "total_duration_sec": total_dur,
            "total_score": score,
            "token_usage": tokens,
        },
    }
    (dest / "run_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return meta


# ── Append to status file (thread-safe) ──────────────────────────────────
def append_status(record: dict, status_file: Path):
    lock_file = str(status_file) + ".lock"
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with open(status_file, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        fcntl.flock(lf, fcntl.LOCK_UN)


# ── Generate report ─────────────────────────────────────────────────────
def generate_report(run_dir: Path):
    """Generate summary_data.json and report.md inside the specific run directory."""
    all_results = {}
    for fw_dir in sorted(run_dir.iterdir()):
        if not fw_dir.is_dir() or fw_dir.name.startswith('.') or fw_dir.name == "batch_logs":
            continue
        for bm_dir in sorted(fw_dir.iterdir()):
            meta_path = bm_dir / "run_meta.json"
            if not meta_path.exists():
                continue
            try:
                d = json.load(open(meta_path))
                exp = d.get("experiment", {})
                fw, bm, mid = exp.get("framework"), exp.get("benchmark"), exp.get("model_id", "")
                if not fw or not bm:
                    continue
                ms = mid.split("/")[-1] if "/" in mid else mid
                key = f"{fw}_{ms}_{bm}"
                totals = d.get("totals", {})
                
                all_results[key] = {
                    "framework": fw, "model": ms, "benchmark": bm,
                    "score": totals.get("total_score"),
                    "status": "copied_exp3" if totals.get("copied_exp3") else d.get("evaluation", {}).get("status", "unknown"),
                    "wall": totals.get("total_duration_sec", 0),
                    "gen": totals.get("generation_duration_sec", 0),
                    "ti": sum(v.get("input", 0) for v in totals.get("token_usage", {}).values() if isinstance(v, dict)),
                    "to": sum(v.get("output", 0) for v in totals.get("token_usage", {}).values() if isinstance(v, dict)),
                }
            except Exception:
                pass

    summary = {"modes": {}}
    for fw in EXPERIMENT_FRAMEWORKS:
        success, timeout, fail = 0, 0, 0
        scores, walls, gens = [], [], []
        per_ms = {}
        for ms in list(EXPERIMENT_LLMS.keys()):
            ms_scores = []
            for bm in TARGET_BENCHMARKS:
                key = f"{fw}_{ms}_{bm}"
                if key not in all_results:
                    fail += 1
                    continue
                r = all_results[key]
                sc = r["score"]
                if sc is not None:
                    scores.append(sc)
                    ms_scores.append(sc)
                    success += 1
                elif (r.get("wall", 0) or 0) >= 1700:
                    timeout += 1
                    fail += 1
                else:
                    fail += 1
                
                if r.get("wall"): walls.append(r["wall"])
                if r.get("gen"): gens.append(r["gen"])
                
            per_ms[ms] = round(sum(ms_scores) / max(len(ms_scores), 1), 4) if ms_scores else 0

        summary["modes"][fw] = {
            "success": success, "total": success + fail - timeout,
            "timeouts": timeout, "failures": fail - timeout,
            "avg_score": round(sum(scores) / max(len(scores), 1), 4) if scores else 0,
            "avg_wall_s": round(sum(walls) / max(len(walls), 1), 1) if walls else 0,
            "avg_gen_s": round(sum(gens) / max(len(gens), 1), 1) if gens else 0,
            "per_llm": per_ms,
        }

    (run_dir / "summary_data.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # Build markdown report
    lines = [
        "# DEVS Framework Comparison Report", "", "```", "=" * 80,
        "DEVS Code Generation Framework Comparison Report", "=" * 80, "",
        f"Run Directory: {run_dir.name}",
        f"Generated: {datetime.now().isoformat()}",
        "=" * 80, "I. Overall Framework Performance", "=" * 80, "",
        f"{'Mode':<25s} | {'Success':>8} | {'TO':>4} | {'Fail':>4} | {'AvgScore':>8} | {'AvgWall':>8} | {'AvgGen':>8}",
        "-" * 90
    ]

    for fw in EXPERIMENT_FRAMEWORKS:
        sm = summary["modes"].get(fw, {})
        s, t, f = sm.get("success", 0), sm.get("timeouts", 0), sm.get("failures", 0)
        lines.append(f"{fw:<25s} | {s:>3}/{s+t+f:>3d}   | {t:>4} | {f:>4} | {sm.get('avg_score', 0):>8.4f} | {sm.get('avg_wall_s', 0):>8.1f}s | {sm.get('avg_gen_s', 0):>8.1f}s")

    lines.append("```")
    (run_dir / "report.md").write_text("\n".join(lines))
    log(f"\nReport generated at: {run_dir / 'report.md'}")
    return summary


# ── Main ────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="HAMLET Unified Experiment Runner")
    p.add_argument("--framework", type=str)
    p.add_argument("--model", type=str, help="Short model key (e.g. gpt_5_2)")
    p.add_argument("--benchmark", type=str)
    p.add_argument("--resume", action="store_true", help="Resume pending tasks in the latest/specified run folder")
    p.add_argument("--run-name", type=str, help="Custom name for the run folder (e.g. exp_v1)")
    p.add_argument("--dry-run", action="store_true", help="Show what would run")
    p.add_argument("--list-frameworks", action="store_true")
    p.add_argument("--list-models", action="store_true")
    p.add_argument("--list-benchmarks", action="store_true")
    args = p.parse_args()

    BASE_GEN_DIR.mkdir(parents=True, exist_ok=True)

    if args.list_frameworks or args.list_models or args.list_benchmarks:
        if args.list_frameworks:
            print("Frameworks:\n" + "\n".join(f"  {k}" for k in EXPERIMENT_FRAMEWORKS))
        if args.list_models:
            print("Models:\n" + "\n".join(f"  {k}" for k in EXPERIMENT_LLMS))
        if args.list_benchmarks:
            print("Benchmarks:\n" + "\n".join(f"  {k}" for k in TARGET_BENCHMARKS))
        return

    # Determine Run Directory
    if args.run_name:
        run_dir = BASE_GEN_DIR / args.run_name
    elif args.resume:
        # Auto-detect latest run folder starting with 'run_'
        runs = sorted([d for d in BASE_GEN_DIR.iterdir() if d.is_dir() and d.name.startswith("run_")], key=os.path.getmtime)
        if runs:
            run_dir = runs[-1]
            log(f"Resuming latest run directory: {run_dir.name}")
        else:
            run_dir = BASE_GEN_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        run_dir = BASE_GEN_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    run_dir.mkdir(parents=True, exist_ok=True)
    status_file = run_dir / "batch_status.jsonl"
    log_dir = run_dir / "batch_logs"
    log_dir.mkdir(exist_ok=True)

    # ── Single task mode ────────────────────────────────────────────────
    if args.framework and args.model and args.benchmark:
        fw, ms, bm = args.framework, args.model, args.benchmark
        if is_completed(fw, ms, bm, run_dir):
            log(f"Task {fw}/{ms}/{bm} already completed in {run_dir.name}.")
            return
        meta = run_task(fw, ms, bm, run_dir)
        
        sc = meta.get("totals", {}).get("total_score")
        log(f"DONE: score={sc} in {run_dir.name}")
        return

    # ── Batch mode ──────────────────────────────────────────────────────
    if not args.resume and not args.dry_run and not args.run_name:
        # If running full batch without resume, clear existing status in the new folder (it's new anyway)
        open(status_file, "w").close()

    tasks = make_task_list()
    task_info = []
    
    for fw, ms, bm in tasks:
        key = task_key(fw, ms, bm)
        if is_completed(fw, ms, bm, run_dir):
            task_info.append((key, fw, ms, bm, "done"))
        else:
            task_info.append((key, fw, ms, bm, "pending"))

    if args.dry_run:
        n_run = sum(1 for _, _, _, _, s in task_info if s == "pending")
        print(f"\nDry Run for: {run_dir.name}")
        print(f"Tasks pending: {n_run} / {len(tasks)}")
        return

    log(f"Starting Batch in: {run_dir.name}")
    log(f"Output: {run_dir}")
    log(f"Logs: {log_dir}")

    results = {}
    for i, (key, fw, ms, bm, status) in enumerate(task_info):
        log(f"\n{'=' * 70}")
        log(f"TASK [{i+1}/{len(tasks)}]: {key} [{status}]")
        log(f"{'=' * 70}")

        if status in ("done", "copied_exp3"):
            results[key] = {"key": key, "status": status}
            continue

        meta = run_task(fw, ms, bm, run_dir)
        sc = meta.get("totals", {}).get("total_score")
        w  = meta.get("totals", {}).get("total_duration_sec", 0)
        
        record = {"key": key, "status": "success" if sc is not None else "no_score", "score": sc, "wall": w, "timestamp": datetime.now().isoformat()}
        results[key] = record
        append_status(record, status_file)
        log(f"  DONE: score={sc} wall={w:.0f}s")
        time.sleep(1)

    log(f"\n{'=' * 70}\nBATCH COMPLETE\n{'=' * 70}")
    generate_report(run_dir)

if __name__ == "__main__":
    main()