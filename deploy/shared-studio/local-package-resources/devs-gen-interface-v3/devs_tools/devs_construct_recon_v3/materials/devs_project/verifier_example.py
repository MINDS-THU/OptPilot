"""Minimal verifier template used as prompt material.

The checker stages the simulation log as ``stdout.txt`` beside the verifier.
Generated verifiers should exit with status zero only when their checks pass.
"""

import json
from pathlib import Path


def load_json_records(path: Path) -> list[dict]:
    records = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def main() -> int:
    records = load_json_records(Path("stdout.txt"))
    passed = bool(records)
    detail = f"parsed {len(records)} structured records"
    print(json.dumps({"passed": passed, "detail": detail}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
