"""Command line entrypoint for OptPilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .importers import build_frontier_unified_study_config
from .runner import run_study



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a StudyConfig")
    run_parser.add_argument("spec", help="Path to the StudyConfig YAML file")
    run_parser.add_argument("--output-root", help="Directory to place study runs")
    run_parser.add_argument("--resume-run-dir", help="Append more trials to an existing run directory")
    run_parser.add_argument("--branch-from-run-dir", help="Start a new run that records an existing run as its parent")

    frontier_parser = subparsers.add_parser(
        "import-frontier",
        help="Create a StudyConfig draft from a Frontier unified benchmark",
    )
    frontier_parser.add_argument("benchmark", help="Benchmark directory or frontier_eval metadata directory")
    frontier_parser.add_argument("-o", "--output", required=True, help="Path for the generated integration draft YAML")
    frontier_parser.add_argument("--repo-root", help="Frontier-Engineering repository root")
    frontier_parser.add_argument("--study-name", help="Override generated study name")
    frontier_parser.add_argument(
        "--engine-implementation",
        default="python:my_lab.engines:FrontierCodeEngine",
        help="User-owned engine implementation to place in the draft",
    )
    frontier_parser.add_argument("--max-trials", type=int, default=20, help="Study stopping.maxTrials value")
    frontier_parser.add_argument(
        "--candidate-parallelism",
        type=int,
        default=1,
        help="Study execution.parallelism.candidateParallelism value",
    )
    frontier_parser.add_argument("--force", action="store_true", help="Overwrite output if it already exists")
    return parser



def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        summary = run_study(
            args.spec,
            output_root=args.output_root,
            resume_run_dir=args.resume_run_dir,
            branch_from_run_dir=args.branch_from_run_dir,
        )
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        return 0
    if args.command == "import-frontier":
        spec = build_frontier_unified_study_config(
            args.benchmark,
            repo_root=args.repo_root,
            study_name=args.study_name,
            engine_implementation=args.engine_implementation,
            max_trials=args.max_trials,
            candidate_parallelism=args.candidate_parallelism,
        )
        output_path = Path(args.output).resolve()
        if output_path.exists() and not args.force:
            parser.error(f"Output already exists: {output_path}. Use --force to overwrite.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        print(str(output_path))
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
