"""Command line entrypoint for OptPilot."""

from __future__ import annotations

import argparse
import importlib.metadata
import json

from .config import validate_authoring_config
from .package_validation import validate_package
from .runner import run_study


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run an OptPilot study config")
    run_parser.add_argument("spec", help="Path to the study YAML file")
    run_parser.add_argument("--output-root", help="Directory to place study runs (default: ./runs)")
    run_parser.add_argument("--resume-run-dir", help="Append more trials to an existing run directory")
    run_parser.add_argument("--branch-from-run-dir", help="Start a new run that records an existing run as its parent")
    run_parser.set_defaults(handler=_run_command)

    validate_parser = subparsers.add_parser("validate", help="Validate an OptPilot public config")
    validate_parser.add_argument("spec", help="Path to an environment, method, or study YAML file")
    validate_parser.add_argument("--json", action="store_true", help="Print machine-readable validation output")
    validate_parser.set_defaults(handler=_validate_command)

    package_parser = subparsers.add_parser("package", help="Work with OptPilot package folders")
    package_subparsers = package_parser.add_subparsers(dest="package_command", required=True)
    package_validate_parser = package_subparsers.add_parser("validate", help="Validate an OptPilot package folder")
    package_validate_parser.add_argument("package", help="Path to a package folder")
    package_validate_parser.add_argument("--json", action="store_true", help="Print machine-readable validation output")
    package_validate_parser.add_argument("--check-imports", action="store_true", help="Best-effort import checks for Python callables")
    package_validate_parser.set_defaults(handler=_package_validate_command)

    _load_command_providers(subparsers)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"Unsupported command: {args.command}")
        return 2
    return int(handler(args) or 0)


def _run_command(args) -> int:
    summary = run_study(
        args.spec,
        output_root=args.output_root,
        resume_run_dir=args.resume_run_dir,
        branch_from_run_dir=args.branch_from_run_dir,
    )
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    return 0


def _validate_command(args) -> int:
    result = validate_authoring_config(args.spec)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["valid"]:
        print(f"Valid: {result['path']}")
    else:
        print(f"Invalid: {result['path']}")
        for error in result["errors"]:
            print(f"- {error}")
    return 0 if result["valid"] else 1


def _package_validate_command(args) -> int:
    result = validate_package(args.package, check_imports=args.check_imports)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["valid"]:
        print(f"Valid package: {result['package']}")
        print(f"Configs: {result['counts']}")
    else:
        print(f"Invalid package: {result['package']}")
        for error in result.get("errors", []):
            print(f"- {error}")
        for entry in result.get("entries", []):
            if entry.get("valid"):
                continue
            print(f"- {entry['path']}")
            for error in entry.get("errors", []):
                print(f"  - {error}")
    return 0 if result["valid"] else 1


def _load_command_providers(subparsers) -> None:
    try:
        entry_points = importlib.metadata.entry_points()
    except Exception:
        return
    if hasattr(entry_points, "select"):
        providers = entry_points.select(group="optpilot.commands")
    else:
        providers = entry_points.get("optpilot.commands", [])
    for entry_point in providers:
        provider = entry_point.load()
        provider(subparsers)


if __name__ == "__main__":
    raise SystemExit(main())
