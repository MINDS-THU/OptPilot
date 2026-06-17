"""CLI wrapper around the toy factory test evaluator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .toy_factory_env import evaluate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-toy-factory-env")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--instance-index", type=int, default=0)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    candidate_path = Path(args.candidate)
    instance_path = Path(args.instance)
    output_path = Path(args.output)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    result = evaluate(
        candidate,
        instance,
        {
            "workspace": str(output_path.parent),
            "instance_index": args.instance_index,
        },
    )
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
