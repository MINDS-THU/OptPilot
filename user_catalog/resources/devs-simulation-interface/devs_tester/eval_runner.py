#!/usr/bin/env python3
"""HAMLET Evaluation Runner — runs evaluation on generated code, collects scores.

Embeds the full eval pipeline logic (simulation + checker) without nesting into
another subprocess.  Supports single-run and batch modes.

Usage:
    # Single evaluation
    python eval_runner.py --benchmark ABP \
        --sim_cwd /tmp/ws/abp_model --sim_script run.py \
        --workspace /tmp/results

    # Batch evaluation
    python eval_runner.py --batch-manifest tasks.json \
        --workspace-prefix /results/

    # List
    python eval_runner.py --list-benchmarks
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union

# ── Paths ────────────────────────────────────────────────────────────────────
HAMLET_CORE   = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = HAMLET_CORE / "benchmark"

# Bound subprocess output while keeping enough data for normal evaluations.
EVAL_CAPTURE_LIMIT_BYTES = 128 * 1024 * 1024
ARCHIVE_LOG_LIMIT_BYTES = 2 * 1024 * 1024

# ── Benchmarks — use experiment_config.BENCHMARKS if available ───────────
BENCHMARKS = {}
try:
    from experiment_config import BENCHMARKS as _EC_BMS, HAMLET_CORE as _EC_CORE
    # experiment_config defines paths relative to _EC_CORE
    for _k, _v in _EC_BMS.items():
        BENCHMARKS[_k] = {
            "gen_config":     str(_EC_CORE / _v.get("gen_config", "")),
            "test_config":    str(_EC_CORE / _v.get("test_config", "")),
            "checker_script": str(_EC_CORE / _v.get("checker", "")),
        }
    _benchmark_source = "experiment_config"
except (ImportError, AttributeError) as _e:
    # Auto-discovery fallback (legacy)
    _discover_dir = HAMLET_CORE / "benchmark"
    def _discover():
        bms = {}
        if not _discover_dir.exists():
            return bms
        for d in sorted(_discover_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            yamls   = list(d.glob("*.yaml"))
            configs = list(d.glob("*config*.json"))
            checkers = sorted(d.glob("*checker*.py"))
            if yamls and configs and checkers:
                bms[d.name] = {
                    "gen_config":     str(yamls[0]),
                    "test_config":    str(configs[0]),
                    "checker_script": str(checkers[0]),
                }
        return bms
    BENCHMARKS = _discover()
    _benchmark_source = "auto-discovery"


# ── Logging ──────────────────────────────────────────────────────────────────
def log(tag, msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] [{tag}] {msg}", flush=True)


# ======================================================================
# Embedded eval pipeline helpers (merged from eval_pipeline.py)
# ======================================================================

def run_command(
    cmd: List[str],
    stdin_content: Optional[Union[str, bytes]] = None,
    stdout_file: Optional[str] = None,
    stderr_file: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute a subprocess, capturing output to files or memory."""
    out_f: Optional[BinaryIO] = None
    err_f: Optional[BinaryIO] = None
    process: Optional[subprocess.Popen] = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    capture_stats = {
        "stdout": {"observed_bytes": 0, "captured_bytes": 0, "truncated": False},
        "stderr": {"observed_bytes": 0, "captured_bytes": 0, "truncated": False},
    }

    def _drain_stream(stream, sink: Optional[BinaryIO], buffer: bytearray, stats: Dict[str, Any]):
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                stats["observed_bytes"] += len(chunk)
                remaining = EVAL_CAPTURE_LIMIT_BYTES - stats["captured_bytes"]
                if remaining > 0:
                    captured = chunk[:remaining]
                    if sink:
                        sink.write(captured)
                    else:
                        buffer.extend(captured)
                    stats["captured_bytes"] += len(captured)
        finally:
            stats["truncated"] = stats["observed_bytes"] > stats["captured_bytes"]
            stream.close()

    def _write_stdin(stream: BinaryIO, content: bytes):
        try:
            stream.write(content)
        except BrokenPipeError:
            pass
        finally:
            stream.close()

    try:
        if stdout_file:
            out_f = open(stdout_file, "wb")
        if stderr_file:
            err_f = open(stderr_file, "wb")

        input_bytes: Optional[bytes] = None
        if stdin_content is not None:
            input_bytes = stdin_content.encode("utf-8") if isinstance(stdin_content, str) else stdin_content

        proc_start = time.time()
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_bytes is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=(os.name == "posix"),
        )
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, out_f, stdout_buffer, capture_stats["stdout"]),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, err_f, stderr_buffer, capture_stats["stderr"]),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        stdin_thread = None
        if process.stdin:
            stdin_thread = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, input_bytes or b""),
                daemon=True,
            )
            stdin_thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
        finally:
            stdout_thread.join()
            stderr_thread.join()
            if stdin_thread:
                stdin_thread.join()

        duration = time.time() - proc_start

        stdout_content = None
        if not stdout_file and stdout_buffer:
            stdout_content = stdout_buffer.decode("utf-8", errors="replace")
        stderr_content = None
        if not stderr_file and stderr_buffer:
            stderr_content = stderr_buffer.decode("utf-8", errors="replace")

        result = {
            "returncode": -1 if timed_out else process.returncode,
            "timed_out": timed_out,
            "duration": duration,
            "stdout": stdout_content, "stderr": stderr_content,
            "capture": capture_stats,
        }
        if timed_out:
            result["error"] = f"Timed out after {timeout} seconds"
        return result
    except Exception as e:
        return {
            "returncode": -1,
            "timed_out": False,
            "error": str(e),
            "duration": time.time() - (proc_start if 'proc_start' in dir() else time.time()),
            "capture": capture_stats,
        }
    finally:
        if out_f: out_f.close()
        if err_f: err_f.close()


def archive_log_file(path: Union[str, Path], capture_stats: Optional[Dict[str, Any]] = None):
    """Shrink a captured log after evaluation and record any truncation."""
    log_path = Path(path)
    if not log_path.exists():
        return

    captured_size = log_path.stat().st_size
    archive_truncated = captured_size > ARCHIVE_LOG_LIMIT_BYTES
    if archive_truncated:
        with open(log_path, "r+b") as f:
            f.truncate(ARCHIVE_LOG_LIMIT_BYTES)

    capture_truncated = bool(capture_stats and capture_stats.get("truncated"))
    if capture_truncated or archive_truncated:
        record = {
            "log_file": log_path.name,
            "capture_limit_bytes": EVAL_CAPTURE_LIMIT_BYTES,
            "archive_limit_bytes": ARCHIVE_LOG_LIMIT_BYTES,
            "observed_bytes": capture_stats.get("observed_bytes") if capture_stats else captured_size,
            "captured_bytes": capture_stats.get("captured_bytes") if capture_stats else captured_size,
            "archived_bytes": log_path.stat().st_size,
            "capture_truncated": capture_truncated,
            "archive_truncated": archive_truncated,
        }
        marker_path = Path(f"{log_path}.truncation.json")
        marker_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def dict_to_cli_args(args_dict: Dict[str, Any]) -> List[str]:
    """Convert a dict to CLI argument list (--key value)."""
    cli_list = []
    if not args_dict:
        return cli_list
    for key, value in args_dict.items():
        prefix = "" if key.startswith("-") else "--"
        flag = f"{prefix}{key}"
        if isinstance(value, bool):
            if value: cli_list.append(flag)
        elif isinstance(value, (list, tuple)):
            for v in value:
                cli_list.append(flag)
                cli_list.append(str(v))
        else:
            cli_list.append(flag)
            cli_list.append(str(value))
    return cli_list


def _normalize_seed_flag(seed_arg: Any) -> str:
    if not isinstance(seed_arg, str) or not seed_arg.strip():
        return "--seed"
    return seed_arg if seed_arg.startswith("-") else f"--{seed_arg}"


def resolve_sim_args_for_run(case: Dict[str, Any], run_idx: int) -> Dict[str, Any]:
    """Resolve per-run simulation args, including optional seed strategies."""
    sim_args = dict(case.get("sim_args", {}) or {})

    seed_flag = _normalize_seed_flag(case.get("seed_arg", "--seed"))
    run_seeds = case.get("run_seeds", None)
    if isinstance(run_seeds, list) and run_idx < len(run_seeds):
        sim_args[seed_flag] = run_seeds[run_idx]
        return sim_args

    seed_mode = case.get("seed_mode", None)
    if seed_mode == "incremental":
        try:
            start_seed = int(case.get("seed_start", 42))
        except Exception:
            start_seed = 42
        sim_args[seed_flag] = start_seed + run_idx
    elif seed_mode == "fixed":
        try:
            fixed_seed = int(case.get("seed_value", case.get("seed_start", 42)))
        except Exception:
            fixed_seed = 42
        sim_args[seed_flag] = fixed_seed

    return sim_args


def run_eval_for_benchmark(
    benchmark: str,
    sim_cwd: str,
    sim_script: str,
    output_dir: Path,
    checker_override: Optional[str] = None,
    sim_timeout_default: float = 10.0,
    checker_timeout_default: float = 60.0,
) -> dict:
    """Core evaluation: iterate test cases → run simulator → run checker → collect scores.

    Returns a dict with summary info (total_score, per-entry results, etc.).
    """
    bm = BENCHMARKS.get(benchmark, {})
    if not bm:
        return {"status": "unknown_bm", "error": f"Benchmark {benchmark!r} not found"}

    checker = checker_override or bm["checker_script"]
    config_path = bm["test_config"]
    config_dir  = str(Path(config_path).resolve().parent)

    with open(config_path, "r", encoding="utf-8") as f:
        data_entries = json.load(f)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_report = {"total_entries": len(data_entries), "results": []}
    all_scores = []
    type_avg_accum = {}

    sim_cmd_base = [sys.executable, sim_script]

    for entry_idx, entry in enumerate(data_entries):
        entry_name = entry.get("name", f"entry_{entry_idx}")
        tag = f"eval/{benchmark}/{entry_name}"
        log(tag, f"Processing ({entry_idx+1}/{len(data_entries)})")

        entry_output_dir = output_dir / entry_name
        entry_output_dir.mkdir(parents=True, exist_ok=True)

        # Expand cases by num
        raw_cases = entry.get("cases", [])
        cases = []
        for case in raw_cases:
            cases.extend([case] * case.get("num", 1))

        generated_jsonl_paths = []
        captured_log_files = []

        # ── Phase 1: Simulation ────────────────────────────────────────
        for run_idx, case in enumerate(cases):
            run_id = f"run{run_idx}"
            out_base = str(entry_output_dir / f"model_output_{run_id}")
            path_stdout = f"{out_base}.jsonl"
            path_stderr = f"{out_base}.stderr"
            path_meta   = f"{out_base}.meta.json"
            path_extra  = f"{out_base}.extra.json"

            # stdin
            if "sim_stdin_file" in case and case["sim_stdin_file"]:
                try:
                    full_stdin_path = os.path.join(config_dir, case["sim_stdin_file"])
                    with open(full_stdin_path, "r", encoding="utf-8") as sf:
                        final_stdin = sf.read()
                except Exception:
                    final_stdin = ""
            else:
                final_stdin = case.get("sim_stdin", "")

            # meta
            resolved_sim_args = resolve_sim_args_for_run(case, run_idx)

            meta_info = {
                "sim_args":       resolved_sim_args,
                "sim_stdin":      final_stdin,
                "checker_config": case.get("checker_config", {}),
            }
            with open(path_meta, "w", encoding="utf-8") as f:
                json.dump(meta_info, f, indent=2, ensure_ascii=False)

            # extra file
            if "checker_extra_file" in case and case["checker_extra_file"]:
                try:
                    extra_path = os.path.join(config_dir, case["checker_extra_file"])
                    with open(extra_path, "r", encoding="utf-8") as ef:
                        extra_data = json.load(ef)
                    with open(path_extra, "w", encoding="utf-8") as ef:
                        json.dump(extra_data, ef, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            # run simulator
            sim_args = resolved_sim_args
            sim_timeout = float(case.get("sim_timeout", entry.get("sim_timeout", sim_timeout_default)))
            full_sim_cmd = sim_cmd_base + dict_to_cli_args(sim_args)

            sim_res = run_command(
                cmd=full_sim_cmd, stdin_content=final_stdin,
                stdout_file=path_stdout, stderr_file=path_stderr,
                cwd=sim_cwd, timeout=sim_timeout,
            )
            meta_info["simulation_result"] = {
                "timeout_sec": sim_timeout,
                "returncode": sim_res.get("returncode"),
                "timed_out": sim_res.get("timed_out", False),
                "duration_sec": sim_res.get("duration"),
                "error": sim_res.get("error"),
                "capture": sim_res.get("capture", {}),
            }
            with open(path_meta, "w", encoding="utf-8") as f:
                json.dump(meta_info, f, indent=2, ensure_ascii=False)
            if sim_res.get("timed_out"):
                log(tag, f"Simulation {run_id} timed out after {sim_timeout}s", "WARN")
            generated_jsonl_paths.append(path_stdout)
            captured_log_files.extend([
                (path_stdout, sim_res.get("capture", {}).get("stdout")),
                (path_stderr, sim_res.get("capture", {}).get("stderr")),
            ])

        log(tag, f"Simulation complete ({len(cases)} runs), running checker...")

        # ── Phase 2: Checker ───────────────────────────────────────────
        checker_out_json = entry_output_dir / "checker_output.json"
        checker_out_txt  = entry_output_dir / "checker_output.txt"
        checker_out_err  = entry_output_dir / "checker_output.stderr"
        checker_meta_json = entry_output_dir / "checker_output.meta.json"

        checker_args = entry.get("checker_args", {})
        checker_cmd = (
            [sys.executable, Path(checker).name]
            + generated_jsonl_paths
            + dict_to_cli_args(checker_args)
        )

        checker_timeout = float(entry.get("checker_timeout", checker_timeout_default))
        check_res = run_command(
            cmd=checker_cmd,
            cwd=str(Path(checker).parent),
            stderr_file=str(checker_out_err),
            timeout=checker_timeout,
        )
        checker_meta_json.write_text(json.dumps({
            "timeout_sec": checker_timeout,
            "returncode": check_res.get("returncode"),
            "timed_out": check_res.get("timed_out", False),
            "duration_sec": check_res.get("duration"),
            "error": check_res.get("error"),
            "capture": check_res.get("capture", {}),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        if check_res.get("timed_out"):
            log(tag, f"Checker timed out after {checker_timeout}s", "WARN")
        checker_stdout_capture = check_res.get("capture", {}).get("stdout")
        if checker_stdout_capture and checker_stdout_capture.get("truncated"):
            marker_path = entry_output_dir / "checker_output.stdout.truncation.json"
            marker_path.write_text(json.dumps({
                "log_file": "checker stdout captured in memory",
                "capture_limit_bytes": EVAL_CAPTURE_LIMIT_BYTES,
                "observed_bytes": checker_stdout_capture.get("observed_bytes"),
                "captured_bytes": checker_stdout_capture.get("captured_bytes"),
                "capture_truncated": True,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        captured_log_files.append((
            checker_out_err,
            check_res.get("capture", {}).get("stderr"),
        ))

        raw_stdout = check_res.get("stdout", "") or ""
        parsed_result = {}

        try:
            parsed_result = json.loads(raw_stdout)
            with open(checker_out_json, "w", encoding="utf-8") as f:
                json.dump(parsed_result, f, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            # Invalid JSON → 0 score
            with open(checker_out_txt, "w", encoding="utf-8") as f:
                f.write(raw_stdout)
            log(tag, f"Checker output not valid JSON, saved as txt", "WARN")
            parsed_result = {
                "success": False, "run_count": len(cases),
                "total_score": 0.0, "type_averages": {},
                "rule_scores": {}, "rule_details": {"error": "Output parse failed"},
            }
            captured_log_files.append((
                checker_out_txt,
                check_res.get("capture", {}).get("stdout"),
            ))

        # Checker reads the full evaluation captures above. Keep smaller logs
        # for archival after scoring is complete.
        for log_path, capture_stats in captured_log_files:
            archive_log_file(log_path, capture_stats)

        # ── Phase 3: Reporting ─────────────────────────────────────────
        score   = parsed_result.get("total_score", 0.0)
        success = parsed_result.get("success", False)
        t_avg   = parsed_result.get("type_averages", {})
        all_scores.append(score)

        for k, v in t_avg.items():
            type_avg_accum.setdefault(k, []).append(v)

        icon = "OK" if (success and score > 0) else ("WARN" if score == 0 else "FAIL")
        log(tag, f"Score={score:.4f} [{icon}] valid_runs={parsed_result.get('run_count', 0)}")

        summary_report["results"].append({
            "name": entry_name,
            "description": entry.get("description", ""),
            "score": score, "success": success,
            "valid_json_output": True,
            "details": parsed_result,
        })

    # ── Final summary ──────────────────────────────────────────────────
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    t_avg_final = {k: sum(v)/len(v) for k, v in type_avg_accum.items()} if type_avg_accum else {}

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_report, f, indent=2, ensure_ascii=False)

    # Also write a flat checker_output.json at the top level for easy parsing
    top_ck = {
        "total_score": avg_score,
        "success": avg_score > 0,
        "run_count": sum(r["details"].get("run_count", 0) for r in summary_report["results"]),
        "type_averages": t_avg_final,
        "entry_count": len(summary_report["results"]),
    }
    top_ck_path = output_dir / "checker_output.json"
    with open(top_ck_path, "w", encoding="utf-8") as f:
        json.dump(top_ck, f, indent=2, ensure_ascii=False)

    log(f"eval/{benchmark}", f"Average score={avg_score:.4f}, type_averages={t_avg_final}")
    return {
        "status": "success",
        "total_score": avg_score,
        "scores_per_entry": all_scores,
        "type_averages": t_avg_final,
        "eval_results_dir": str(output_dir),
        "checker_output": str(top_ck_path),
        "summary": str(summary_path),
    }


# ======================================================================
# Public API
# ======================================================================

def run_eval_pipeline(
    benchmark: str,
    sim_cwd: str,
    sim_script: str,
    workspace_dir: Path,
    checker_override: Optional[str] = None,
    timeout: int = 600,
) -> dict:
    """Evaluate a single generated project.  Wrapper around run_eval_for_benchmark."""
    tag = f"eval/{benchmark}"
    bm  = BENCHMARKS.get(benchmark, {})
    if not bm:
        return {"status": "unknown_bm", "error": f"Benchmark {benchmark!r} not found"}

    eval_dir = workspace_dir / "eval_results"
    log(tag, f"Starting evaluation → {eval_dir}")
    start = time.time()

    try:
        result = run_eval_for_benchmark(
            benchmark, sim_cwd, sim_script, eval_dir,
            checker_override=checker_override,
            sim_timeout_default=timeout,
            checker_timeout_default=timeout,
        )
        dur = round(time.time() - start, 2)
        result["duration_sec"] = dur
        log(tag, f"Done in {dur}s, score={result.get('total_score')}")
        return result
    except subprocess.TimeoutExpired:
        dur = round(time.time() - start, 2)
        log(tag, f"TIMEOUT after {dur}s", "ERROR")
        return {"status": "eval_timeout", "duration_sec": dur}
    except Exception as e:
        dur = round(time.time() - start, 2)
        log(tag, f"CRASH: {e}", "ERROR")
        return {"status": "eval_crash", "duration_sec": dur, "error": str(e)}


def eval_batch(manifest: list[dict], workspace_prefix: Path,
               timeout: int = 600, concurrency: int = 1) -> dict:
    """Run a batch of evaluations.

    manifest entries have keys: benchmark, sim_cwd, sim_script,
    workspace (optional subdir), checker (optional override).
    """
    results = []
    start_all = time.time()

    def _run_one(entry):
        ws = workspace_prefix / entry.get(
            "workspace",
            f"{entry['benchmark']}_{entry['sim_cwd'].replace('/', '_')}",
        )
        return run_eval_pipeline(
            entry["benchmark"], entry["sim_cwd"], entry["sim_script"],
            ws, entry.get("checker"), timeout,
        )

    if concurrency > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_run_one, e) for e in manifest]
            for i, f in enumerate(concurrent.futures.as_completed(futures)):
                r = f.result()
                results.append(r)
                print(f"  [{i+1}/{len(manifest)}] {r.get('benchmark','?')}: "
                      f"{r['status']}, score={r.get('total_score')}")
    else:
        for i, entry in enumerate(manifest):
            r = _run_one(entry)
            results.append(r)
            print(f"  [{i+1}/{len(manifest)}] {r.get('benchmark','?')}: "
                  f"{r['status']}, score={r.get('total_score')}")

    total_dur = round(time.time() - start_all, 2)
    scores = [r.get("total_score") for r in results if r.get("total_score") is not None]
    return {
        "total_tasks":        len(manifest),
        "completed":          sum(1 for r in results if r.get("status") == "success"),
        "failed":             sum(1 for r in results if r.get("status") != "success"),
        "mean_score":         round(sum(scores) / len(scores), 4) if scores else None,
        "median_score":       None if not scores else sorted(scores)[len(scores)//2],
        "total_duration_sec": total_dur,
        "results":            results,
    }


# ======================================================================
# CLI
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="HAMLET Evaluation Runner")
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--benchmark", type=str, help="Benchmark name")
    g.add_argument("--batch-manifest", type=str, help="JSON manifest file")
    g.add_argument("--list-benchmarks", action="store_true")

    p.add_argument("--model", type=str, help="Model ID (for logging)")
    p.add_argument("--sim_cwd", type=str, help="Generated code directory")
    p.add_argument("--sim_script", type=str, default="run.py", help="Entry script name")
    p.add_argument("--checker", type=str, default=None, help="Checker override")
    p.add_argument("--workspace", type=str, default=None, help="Results output dir")
    p.add_argument("--workspace-prefix", type=str, default=None, help="Prefix for batch")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--concurrency", type=int, default=1)
    args = p.parse_args()

    if args.list_benchmarks:
        print("Benchmarks:")
        for n, info in BENCHMARKS.items():
            print(f"  {n:25s}  checker={info['checker_script']}")
        return

    if args.batch_manifest:
        with open(args.batch_manifest) as f:
            manifest = json.load(f)
        wp = Path(args.workspace_prefix) if args.workspace_prefix else Path("eval_results")
        summary = eval_batch(manifest, wp, args.timeout, args.concurrency)
        print(f"\n{'='*40}\nBatch Summary:")
        for k, v in summary.items():
            if k != "results": print(f"  {k}: {v}")
        wp.mkdir(parents=True, exist_ok=True)
        (wp / "batch_summary.json").write_text(json.dumps(summary, indent=2))
        print(f"  Saved to {wp / 'batch_summary.json'}")

    elif args.benchmark:
        if not args.sim_cwd:
            p.error("--sim_cwd required for single evaluation")
        ws = Path(args.workspace) if args.workspace else Path(f"eval_results/{args.benchmark}")
        result = run_eval_pipeline(
            args.benchmark, args.sim_cwd, args.sim_script,
            ws, args.checker, args.timeout,
        )
        print(json.dumps(result, indent=2))
        if result.get("status") != "success":
            sys.exit(1)
    else:
        p.error("Either --benchmark or --batch-manifest required")


if __name__ == "__main__":
    main()
