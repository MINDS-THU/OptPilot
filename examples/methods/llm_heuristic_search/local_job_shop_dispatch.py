"""Tiny local upstream command for the LLM heuristic-search adapter example.

Real LLM heuristic-search repositories own their own search loop and write a
generated heuristic file. This script gives the OptPilot adapter the same
shape without requiring a provider key or external clone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "dispatch_rule.py"
    target.write_text(_dispatch_rule_source(request), encoding="utf-8")
    return 0


def _dispatch_rule_source(request: dict) -> str:
    study_name = str(request.get("study_name", "job-shop-local-heuristic-search"))
    return f'''"""Generated dispatch rule from the local heuristic-search example.

Source study: {study_name}
"""


def score(operation, machine, state):
    remaining_work = float(operation.get("remaining_work", 0.0))
    duration = float(operation.get("duration", 1.0))
    job_ready = float(operation.get("job_ready_time", 0.0))
    machine_ready = float(machine.get("ready_time", 0.0))
    return (1.4 * remaining_work) - (0.6 * duration) - (0.05 * job_ready) - (0.05 * machine_ready)
'''


if __name__ == "__main__":
    raise SystemExit(main())
