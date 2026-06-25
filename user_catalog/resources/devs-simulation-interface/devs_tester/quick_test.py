#!/usr/bin/env python3
"""
Quick test runner for HAMLET fast_plan variant.
Usage:
    python quick_test.py --model qwen3_coder_30b --benchmark ABP
    python quick_test.py --model gpt_5_2 --benchmark ABP
    python quick_test.py --model qwen3_coder_30b --benchmark ABP --workspace /tmp/my_test_ws
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
THIS_DIR    = Path(__file__).resolve().parent
HAMLET_CORE = THIS_DIR.parent

sys.path.insert(0, str(THIS_DIR))
from experiment_config import BENCHMARKS, EXPERIMENT_LLMS
from gen_runner import run_generation

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


def main():
    p = argparse.ArgumentParser(description="Quick HAMLET fast_plan test runner")
    p.add_argument("--model", type=str, required=True,
                   choices=list(EXPERIMENT_LLMS.keys()),
                   help="Model key (e.g. qwen3_coder_30b_a3b_instruct, gpt_5_2)")
    p.add_argument("--benchmark", type=str, default="ABP",
                   choices=list(BENCHMARKS.keys()),
                   help="Benchmark name")
    p.add_argument("--workspace", type=str, default=None,
                   help="Workspace directory (default: ./quick_test_ws/{model_short}_{bm})")
    p.add_argument("--timeout", type=int, default=1800,
                   help="Generation timeout in seconds")
    args = p.parse_args()

    model_id = EXPERIMENT_LLMS[args.model]
    bm_name = args.benchmark
    bm_conf = BENCHMARKS[bm_name]

    # Resolve workspace
    if args.workspace:
        ws = Path(args.workspace)
    else:
        model_short = args.model.replace("_", "")[:20]
        ws = THIS_DIR / "quick_test_ws" / f"{model_short}_{bm_name}"
    
    ws = ws.resolve()

    ws.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Quick Test: {args.model} / {bm_name}")
    print(f"Workspace:  {ws}")
    print(f"Model ID:   {model_id}")
    print(f"Timeout:    {args.timeout}s")
    print(f"{'='*60}\n")

    start = time.time()
    result = run_generation(
        fw_name="devs_fast_plan",
        model_id=model_id,
        benchmark_name=bm_name,
        workspace_dir=ws,
        timeout=args.timeout,
        verbose=True,
    )
    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"RESULT (wall time: {elapsed:.1f}s)")
    print(f"{'='*60}")
    print(json.dumps(result, indent=2))

    # Copy LLM call logs to workspace for easy access
    llm_log_src = ws / "abp_model" / "devs_project" / "_analysis_logs" / "llm_calls"
    # Try to find the actual project dir (might not be abp_model)
    if not llm_log_src.exists():
        for d in ws.iterdir():
            candidate = d / "devs_project" / "_analysis_logs" / "llm_calls"
            if candidate.exists():
                llm_log_src = candidate
                break

    if llm_log_src.exists():
        llm_log_dst = ws / "llm_call_logs"
        if llm_log_dst.exists():
            import shutil
            shutil.rmtree(llm_log_dst)
        import shutil
        shutil.copytree(llm_log_src, llm_log_dst)
        print(f"\nLLM call logs copied to: {llm_log_dst}")

        # Print summary
        summary_path = llm_log_dst / "llm_calls_summary.jsonl"
        if summary_path.exists():
            from collections import defaultdict
            calls = [json.loads(line) for line in open(summary_path) if line.strip()]
            print(f"\nLLM Call Summary:")
            print(f"  Total raw calls: {len(calls)}")
            print(f"  Total duration: {sum(c['duration_sec'] for c in calls):.1f}s")
            print(f"  Total input chars: {sum(c['input_chars'] for c in calls):,}")
            print(f"  Total output chars: {sum(c['output_chars'] for c in calls):,}")

            # Deduplicate
            unique_map = {}
            for c in calls:
                key = (c['phase'], c['target'], c['attempt'])
                if key not in unique_map:
                    unique_map[key] = c
            unique_calls = list(unique_map.values())
            print(f"\n  Deduplicated calls: {len(unique_calls)}")
            print(f"  Dedup duration: {sum(c['duration_sec'] for c in unique_calls):.1f}s")
            print(f"  Dedup input chars: {sum(c['input_chars'] for c in unique_calls):,}")
            print(f"  Dedup output chars: {sum(c['output_chars'] for c in unique_calls):,}")

            api_in = sum(c.get('token_usage', {}).get('prompt_tokens', 0) for c in unique_calls)
            api_out = sum(c.get('token_usage', {}).get('completion_tokens', 0) for c in unique_calls)
            if api_in > 0:
                print(f"  API tokens: {api_in:,} in + {api_out:,} out = {api_in+api_out:,} total")

            print(f"\n  Per-phase breakdown:")
            phases = defaultdict(list)
            for c in unique_calls:
                phases[c['phase']].append(c)
            for phase_name in sorted(phases.keys()):
                pc = phases[phase_name]
                print(f"    {phase_name}: {len(pc)} calls, {sum(c['duration_sec'] for c in pc):.1f}s, "
                      f"input={sum(c['input_chars'] for c in pc):,} chars")
    else:
        print(f"\nNo LLM call logs found (logger may not be initialized)")

    print(f"\n{'='*60}")
    if result.get("status") == "success":
        print("SUCCESS")
    else:
        print(f"FAILED: {result.get('status', 'unknown')}")
    print(f"{'='*60}\n")

    sys.exit(0 if result.get("status") == "success" else 1)


if __name__ == "__main__":
    main()
