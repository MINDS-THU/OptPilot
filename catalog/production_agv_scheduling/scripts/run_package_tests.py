#!/usr/bin/env python3
"""Run every production/AGV package suite in an isolated Python process."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    ("package contracts", "tests", "test_package_contract.py"),
    ("rule grid", "methods/rule_grid", "test_method.py"),
    ("evolutionary search", "methods/evolutionary_rule_search", "test_method.py"),
    ("process-aware LLM", "methods/process_aware_llm/tests", "test_method.py"),
    ("rolling MILP", "methods/rolling_milp/tests", "test_rolling_milp_method.py"),
    (
        "environment evaluator",
        "environments/production_agv_scheduling/tests",
        "test_environment.py",
    ),
    (
        "interactive interface",
        "environments/production_agv_scheduling/tests",
        "test_interface.py",
    ),
)


def main() -> int:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    failures: list[str] = []
    for label, relative_directory, test_file in SUITES:
        print(f"\n=== {label} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", test_file],
            cwd=PACKAGE_ROOT / relative_directory,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            failures.append(label)
    if failures:
        print(f"\nFailed package suites: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nAll production/AGV package suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
