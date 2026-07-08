"""Command line entrypoint for OptPilot."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import tempfile
from pathlib import Path

from .config import validate_authoring_config
from .package_index import index_package
from .package_validation import validate_package
from .runner import run_study
from .setup import run_process_setup


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
    package_validate_parser.add_argument("--check-source", action="store_true", help="Check public source paths referenced by package configs")
    package_validate_parser.add_argument("--check-imports", action="store_true", help="Import Python callables in isolated subprocesses")
    package_validate_parser.add_argument("--check-setup-files", action="store_true", help="Check files needed by runtime and interface setup declarations")
    package_validate_parser.set_defaults(handler=_package_validate_command)
    package_setup_parser = package_subparsers.add_parser("setup-check", help="Check or execute package setup declarations")
    package_setup_parser.add_argument("package", help="Path to a package folder")
    package_setup_parser.add_argument("--run-setup", action="store_true", help="Execute runtime.setup and interface.setup declarations")
    package_setup_parser.add_argument("--json", action="store_true", help="Print machine-readable setup check output")
    package_setup_parser.set_defaults(handler=_package_setup_check_command)
    package_smoke_parser = package_subparsers.add_parser("smoke", help="Run a package smoke study")
    package_smoke_parser.add_argument("package", help="Path to a package folder")
    package_smoke_parser.add_argument("--study", help="Study config path, relative to package root or absolute")
    package_smoke_parser.add_argument("--output-root", help="Directory for smoke run output; defaults to a temporary directory")
    package_smoke_parser.add_argument("--json", action="store_true", help="Print machine-readable smoke output")
    package_smoke_parser.set_defaults(handler=_package_smoke_command)

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
    result = validate_package(
        args.package,
        check_imports=args.check_imports,
        check_source=args.check_source,
        check_setup_files=args.check_setup_files,
    )
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
            for warning in entry.get("warnings", []):
                print(f"  - warning: {warning}")
    return 0 if result["valid"] else 1


def _package_setup_check_command(args) -> int:
    result = _package_setup_check(args.package, run_setup=bool(args.run_setup))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["valid"]:
        verb = "Executed" if args.run_setup else "Checked"
        print(f"{verb} setup declarations: {result['package']}")
        print(f"Setup blocks: {result['counts']['setup_blocks']}")
    else:
        print(f"Invalid setup declarations: {result['package']}")
        for error in result.get("errors", []):
            print(f"- {error}")
        for entry in result.get("entries", []):
            if entry.get("valid"):
                continue
            print(f"- {entry['path']}")
            for error in entry.get("errors", []):
                print(f"  - {error}")
    return 0 if result["valid"] else 1


def _package_smoke_command(args) -> int:
    result = _package_smoke(args.package, study=args.study, output_root=args.output_root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["valid"]:
        print(f"Smoke passed: {result['study']}")
        print(f"Run directory: {result['run_dir']}")
    else:
        print(f"Smoke failed: {result.get('study') or args.package}")
        for error in result.get("errors", []):
            print(f"- {error}")
    return 0 if result["valid"] else 1


def _package_setup_check(package: str, *, run_setup: bool) -> dict:
    index = index_package(package)
    setup_file_validation = validate_package(index.package_root, check_setup_files=True)
    entries = []
    errors = list(setup_file_validation.get("errors", []))
    validation_entries = {entry.get("path"): entry for entry in setup_file_validation.get("entries", [])}
    for entry in index.entries:
        entry_validation = validation_entries.get(str(entry.path), {})
        entry_errors = list(entry_validation.get("errors", []) or [])
        setup_blocks = _entry_setup_blocks(entry.raw)
        if run_setup and not entry_errors:
            for label, setup in setup_blocks:
                try:
                    run_process_setup(setup, entry.source_root or entry.path.parent)
                except Exception as exc:
                    entry_errors.append(f"{label} failed: {exc}")
        entries.append(
            {
                "path": str(entry.path),
                "config": entry.config,
                "id": entry.id,
                "setup_blocks": [label for label, _setup in setup_blocks],
                "valid": not entry_errors,
                "errors": entry_errors,
            }
        )
    valid = not errors and all(entry["valid"] for entry in entries)
    return {
        "valid": valid,
        "package": str(index.package_root),
        "package_id": index.package_id,
        "counts": {"setup_blocks": sum(len(entry["setup_blocks"]) for entry in entries)},
        "errors": errors,
        "entries": entries,
    }


def _entry_setup_blocks(raw: dict) -> list[tuple[str, dict]]:
    blocks = []
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
    setup = runtime.get("setup") if isinstance(runtime.get("setup"), dict) else None
    if setup:
        blocks.append(("runtime.setup", setup))
    interface = raw.get("interface", {}) if isinstance(raw.get("interface"), dict) else {}
    setup = interface.get("setup") if isinstance(interface.get("setup"), dict) else None
    if setup:
        blocks.append(("interface.setup", setup))
    return blocks


def _package_smoke(package: str, *, study: str | None, output_root: str | None) -> dict:
    package_root = Path(package).expanduser().resolve()
    validation = validate_package(package_root, check_source=True, check_setup_files=True)
    if not validation.get("valid"):
        return {"valid": False, "package": str(package_root), "errors": ["Package validation failed."], "validation": validation}
    study_path = _select_package_smoke_study(package_root, study)
    if study_path is None:
        return {"valid": False, "package": str(package_root), "errors": ["No smoke study selected and package does not contain exactly one study."]}
    study_validation = validate_authoring_config(study_path)
    if not study_validation.get("valid"):
        return {"valid": False, "package": str(package_root), "study": str(study_path), "errors": ["Study validation failed."], "validation": study_validation}
    try:
        if output_root:
            summary = run_study(study_path, output_root=output_root)
            return {"valid": True, "package": str(package_root), "study": str(study_path), "run_dir": str(summary.run_dir), "summary": summary.to_dict()}
        tmp_dir = tempfile.mkdtemp(prefix="optpilot-package-smoke-")
        summary = run_study(study_path, output_root=tmp_dir)
        return {"valid": True, "package": str(package_root), "study": str(study_path), "run_dir": str(summary.run_dir), "summary": summary.to_dict()}
    except Exception as exc:
        return {"valid": False, "package": str(package_root), "study": str(study_path), "errors": [str(exc)]}


def _select_package_smoke_study(package_root: Path, study: str | None) -> Path | None:
    if study:
        path = Path(study).expanduser()
        return path.resolve() if path.is_absolute() else (package_root / path).resolve()
    index = index_package(package_root)
    studies = [entry.path for entry in index.entries if entry.config == "study"]
    if len(studies) == 1:
        return studies[0]
    smoke_named = [path for path in studies if "smoke" in path.stem.lower()]
    if len(smoke_named) == 1:
        return smoke_named[0]
    return None


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
