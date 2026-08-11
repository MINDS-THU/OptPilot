#!/usr/bin/env python3
"""Run one study across many benchmark tasks and tabulate the results.

The 32 tasks are selected per launch (``--input task_id=...``), so a sweep is
just the same Run setup launched once per task. Defaults sweep the zero-LLM
smoke study, which needs no API key and is what CI uses.

    python scripts/run_task_sweep.py                       # all 32, zero LLM
    python scripts/run_task_sweep.py --tasks iron_plate_low_easy inserter_low_hard
    python scripts/run_task_sweep.py --study studies/factory_design_task.yaml

Run it from the package root.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TASK_CONFIGS = PACKAGE_ROOT / "environments" / "factory_design" / "fd_core" / "tasks" / "configs"


def all_task_ids() -> list[str]:
    return sorted(p.stem for p in TASK_CONFIGS.glob("*.json"))


def run_one(study: Path, task_id: str) -> dict:
    completed = subprocess.run(
        [
            "optpilot", "run", str(study),
            "--package-root", str(PACKAGE_ROOT),
            "--input", f"task_id={task_id}",
        ],
        capture_output=True,
        text=True,
        cwd=str(PACKAGE_ROOT.parent.parent),
    )
    payload = {}
    stdout = completed.stdout
    if "{" in stdout:
        try:
            payload = json.loads(stdout[stdout.index("{"):])
        except json.JSONDecodeError:
            payload = {}
    return {
        "task_id": task_id,
        "returncode": completed.returncode,
        "run_status": payload.get("run_status", "unknown"),
        "run_id": payload.get("run_id", ""),
        "objective": (payload.get("best") or {}).get("metric"),
        "error": "" if completed.returncode == 0 else (completed.stderr or stdout)[-300:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", default="studies/factory_design_smoke.yaml")
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--json", dest="json_out", default="")
    args = parser.parse_args()

    study = (PACKAGE_ROOT / args.study).resolve()
    tasks = args.tasks if args.tasks else all_task_ids()
    if not tasks:
        print("no task configs found", file=sys.stderr)
        return 2

    results = []
    for index, task_id in enumerate(tasks, start=1):
        result = run_one(study, task_id)
        results.append(result)
        status = result["run_status"]
        objective = result["objective"]
        print(
            f"[{index:2}/{len(tasks)}] {task_id:34} {status:10} "
            f"failed_check_count={objective}"
            + (f"  {result['error']}" if result["error"] else "")
        )

    succeeded = [r for r in results if r["run_status"] == "succeeded"]
    print(f"\n{len(succeeded)}/{len(results)} runs succeeded")
    scored = [r["objective"] for r in succeeded if isinstance(r["objective"], (int, float))]
    if scored:
        print(f"failed_check_count: min={min(scored)} max={max(scored)} "
              f"mean={sum(scored) / len(scored):.2f}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 0 if len(succeeded) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
