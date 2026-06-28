"""Command line entrypoint for OptPilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import validate_authoring_config
from .runner import run_study
from .ui.server import add_ui_arguments, run_ui



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run an OptPilot study config")
    run_parser.add_argument("spec", help="Path to the study YAML file")
    run_parser.add_argument("--output-root", help="Directory to place study runs (default: ./runs)")
    run_parser.add_argument("--resume-run-dir", help="Append more trials to an existing run directory")
    run_parser.add_argument("--branch-from-run-dir", help="Start a new run that records an existing run as its parent")

    validate_parser = subparsers.add_parser("validate", help="Validate an OptPilot public config")
    validate_parser.add_argument("spec", help="Path to an environment, method, or study YAML file")
    validate_parser.add_argument("--json", action="store_true", help="Print machine-readable validation output")

    ui_parser = subparsers.add_parser("ui", help="Start the lightweight local web UI")
    add_ui_arguments(ui_parser)

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
    if args.command == "validate":
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
    if args.command == "ui":
        run_ui(
            host=args.host,
            port=args.port,
            catalog_roots=args.catalog,
            run_roots=args.runs,
            code_server_bin=args.code_server_bin,
            code_server_host=args.code_server_host,
            code_server_port=args.code_server_port,
            code_server_auth=args.code_server_auth,
            code_server_password=args.code_server_password,
            workspace_runtime_executable=args.workspace_runtime_bin,
            workspace_runtime_image=args.workspace_runtime_image,
            workspace_runtime_network=args.workspace_runtime_network,
            workspace_runtime_port_start=args.workspace_runtime_port_start,
            open_browser=args.open_browser,
        )
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
