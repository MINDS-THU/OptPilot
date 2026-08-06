"""Command line entrypoint for OptPilot."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import tempfile
import uuid
from pathlib import Path

from .config import validate_authoring_config
from .package_index import index_package
from .package_validation import validate_package
from .runner import run_study
from .realm.config import default_realm_root
from .realm.provider_trust_policy import RealmProviderTrustPolicyService
from .realm.provider_trust_records import (
    PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE,
    PROVIDER_TRUST_GATEWAY_CONTRACT,
    ProviderTrustDecision,
    ProviderTrustHead,
    validate_provider_image_ref,
)
from .setup import run_process_setup


_ENVIRONMENT_PREVIEW_TRUST_OUTPUT_SCHEMA = (
    "optpilot.environment-preview-trust-command.v1"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run an OptPilot study config")
    run_parser.add_argument("spec", help="Path to the study YAML file")
    run_parser.add_argument(
        "--package-root",
        required=True,
        help="Explicit root of the package captured for this study",
    )
    run_parser.add_argument(
        "--realm-root",
        help="Private local Realm root (default: secure OS user-data location)",
    )
    run_parser.add_argument(
        "--operation-id",
        help="Stable launch identity for replay and pre-launch run monitoring",
    )
    run_parser.add_argument(
        "--method-request-timeout",
        type=float,
        default=10.0,
        help=(
            "Maximum seconds for one retained method callback "
            "(increase for external model calls; default: 10)"
        ),
    )
    run_parser.add_argument(
        "--input",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        dest="inputs",
        help=(
            "Per-launch value for a study-declared input (repeatable). "
            "Values are parsed as YAML scalars: 30 is an int, true a bool, "
            "quoted values stay strings."
        ),
    )
    run_parser.add_argument(
        "--inputs-file",
        default=None,
        help=(
            "YAML file with a mapping of per-launch study input values; "
            "--input wins on conflicting keys"
        ),
    )
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
    package_smoke_parser.add_argument(
        "--realm-root",
        help="Private Realm root for retained smoke evidence; defaults to a temporary Realm",
    )
    package_smoke_parser.add_argument("--json", action="store_true", help="Print machine-readable smoke output")
    package_smoke_parser.set_defaults(handler=_package_smoke_command)

    preview_parser = subparsers.add_parser(
        "environment-preview",
        help="Manage local Environment Preview settings",
    )
    preview_subparsers = preview_parser.add_subparsers(
        dest="environment_preview_command",
        required=True,
    )
    trust_parser = preview_subparsers.add_parser(
        "trust",
        help="Manage exact container images approved for Environment Preview",
    )
    trust_subparsers = trust_parser.add_subparsers(
        dest="environment_preview_trust_command",
        required=True,
    )
    trust_approve_parser = trust_subparsers.add_parser(
        "approve",
        help="Approve one digest-pinned container image",
    )
    _add_environment_preview_trust_arguments(
        trust_approve_parser,
        image=True,
        confirmation=True,
    )
    trust_approve_parser.set_defaults(
        handler=_environment_preview_trust_approve_command
    )
    trust_revoke_parser = trust_subparsers.add_parser(
        "revoke",
        help="Revoke approval for one digest-pinned container image",
    )
    _add_environment_preview_trust_arguments(
        trust_revoke_parser,
        image=True,
        confirmation=True,
    )
    trust_revoke_parser.set_defaults(
        handler=_environment_preview_trust_revoke_command
    )
    trust_list_parser = trust_subparsers.add_parser(
        "list",
        help="List active Environment Preview image approvals",
    )
    _add_environment_preview_trust_arguments(
        trust_list_parser,
        image=False,
        confirmation=False,
    )
    trust_list_parser.set_defaults(handler=_environment_preview_trust_list_command)

    _load_command_providers(subparsers)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"Unsupported command: {args.command}")
        return 2
    try:
        return int(handler(args) or 0)
    except (OSError, RuntimeError, ValueError) as error:
        # Command-line users need one actionable message. The public library
        # API remains exception-based for callers that need structured
        # recovery or a developer traceback.
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _parse_launch_inputs(args) -> dict | None:
    """Merge --inputs-file and repeatable --input into one mapping.

    Returns ``None`` when neither flag was used, so studies without declared
    inputs launch exactly as before. Values are parsed as YAML scalars.
    """

    import yaml

    if args.inputs_file is None and args.inputs is None:
        return None
    merged: dict = {}
    if args.inputs_file is not None:
        with open(args.inputs_file, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise SystemExit(
                f"--inputs-file {args.inputs_file} must contain a YAML mapping."
            )
        merged.update(loaded)
    for item in args.inputs or []:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit(
                f"--input {item!r} must use key=value form."
            )
        try:
            merged[key] = yaml.safe_load(value)
        except yaml.YAMLError as error:
            raise SystemExit(f"--input {item!r} value is not a YAML scalar: {error}")
    return merged


def _run_command(args) -> int:
    summary = run_study(
        args.spec,
        package_root=args.package_root,
        realm_root=args.realm_root,
        operation_id=args.operation_id,
        method_request_timeout=args.method_request_timeout,
        launch_inputs=_parse_launch_inputs(args),
    )
    print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    return 0 if summary.run_status == "succeeded" else 1


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
    result = _package_smoke(args.package, study=args.study, realm_root=args.realm_root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["valid"]:
        print(f"Smoke passed: {result['study']}")
        print(f"Run: {result['run_id']}")
    else:
        print(f"Smoke failed: {result.get('study') or args.package}")
        for error in result.get("errors", []):
            print(f"- {error}")
    return 0 if result["valid"] else 1


def _add_environment_preview_trust_arguments(
    parser: argparse.ArgumentParser,
    *,
    image: bool,
    confirmation: bool,
) -> None:
    if image:
        parser.add_argument(
            "image",
            help=(
                "Exact digest-pinned image reference "
                "(for example, registry.example/preview@sha256:<64 hex>)"
            ),
        )
    parser.add_argument(
        "--realm-root",
        help="Private local Realm root (default: secure OS user-data location)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable trust-policy output",
    )
    if confirmation:
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Apply the trust change without an interactive confirmation",
        )


def _environment_preview_trust_approve_command(args) -> int:
    args.image = validate_provider_image_ref(args.image)
    if not _confirm_environment_preview_trust_change(
        action="approve",
        image_ref=args.image,
        assume_yes=bool(args.yes),
    ):
        return _print_environment_preview_trust_cancelled(args, action="approve")
    return _apply_environment_preview_trust_change(args, action="approve")


def _environment_preview_trust_revoke_command(args) -> int:
    args.image = validate_provider_image_ref(args.image)
    if not _confirm_environment_preview_trust_change(
        action="revoke",
        image_ref=args.image,
        assume_yes=bool(args.yes),
    ):
        return _print_environment_preview_trust_cancelled(args, action="revoke")
    return _apply_environment_preview_trust_change(args, action="revoke")


def _environment_preview_trust_list_command(args) -> int:
    root = _environment_preview_trust_realm_root(args.realm_root)
    with RealmProviderTrustPolicyService.open_local(root) as service:
        active = [
            _environment_preview_trust_head_payload(record)
            for record in service.list_active()
        ]
    payload = {
        "schema": _ENVIRONMENT_PREVIEW_TRUST_OUTPUT_SCHEMA,
        "action": "list",
        "realm_root": str(root),
        "active": active,
        "count": len(active),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif not active:
        print("No Environment Preview container images are approved.")
    else:
        print("Approved Environment Preview container images:")
        for record in active:
            print(f"- {record['image_ref']}")
    return 0


def _apply_environment_preview_trust_change(args, *, action: str) -> int:
    root = _environment_preview_trust_realm_root(args.realm_root)
    with RealmProviderTrustPolicyService.open_local(root) as service:
        operation = getattr(service, action)
        current = next(
            (
                head
                for head in service.list_heads()
                if head.image_ref == args.image
            ),
            None,
        )
        python_executable = (
            current.python_executable
            if action == "revoke" and current is not None
            else PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE
        )
        contract = (
            current.contract
            if action == "revoke" and current is not None
            else PROVIDER_TRUST_GATEWAY_CONTRACT
        )
        decision = operation(
            operation_id=(
                f"cli/environment-preview/trust/{action}/"
                f"{uuid.uuid4().hex}"
            ),
            image_ref=args.image,
            python_executable=python_executable,
            contract=contract,
            reason=f"Requested by optpilot environment-preview trust {action}.",
        )
        active = service.read_active(image_ref=args.image)
    payload = {
        "schema": _ENVIRONMENT_PREVIEW_TRUST_OUTPUT_SCHEMA,
        "action": action,
        "realm_root": str(root),
        "studio_restart_required": True,
        "decision": _environment_preview_trust_decision_payload(decision),
        "active": (
            None
            if active is None
            else _environment_preview_trust_head_payload(active)
        ),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        past_tense = "Approved" if action == "approve" else "Revoked"
        print(f"{past_tense} Environment Preview image: {args.image}")
        print("Restart Studio to load the updated Realm trust snapshot.")
    return 0


def _environment_preview_trust_head_payload(
    record: ProviderTrustHead,
) -> dict[str, object]:
    return {
        "image_ref": record.image_ref,
        "python_executable": record.python_executable,
        "contract": record.contract,
        "decision_id": record.decision_id,
        "sequence": record.sequence,
        "approved_at": record.created_at,
    }


def _environment_preview_trust_decision_payload(
    record: ProviderTrustDecision,
) -> dict[str, object]:
    return {
        "image_ref": record.image_ref,
        "python_executable": record.python_executable,
        "contract": record.contract,
        "decision_id": record.decision_id,
        "sequence": record.sequence,
        "state": record.state.value,
        "decided_at": record.created_at,
    }


def _environment_preview_trust_realm_root(value: str | None) -> Path:
    if value is None:
        return default_realm_root()
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("--realm-root must be an absolute path.")
    return root


def _confirm_environment_preview_trust_change(
    *,
    action: str,
    image_ref: str,
    assume_yes: bool,
) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Environment Preview trust changes require confirmation; "
            "re-run with --yes in a noninteractive session."
        )
    confirmation = action.upper()
    print(
        f"Type {confirmation} to {action} this exact Environment Preview image:\n"
        f"  {image_ref}\n> ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    return sys.stdin.readline().strip() == confirmation


def _print_environment_preview_trust_cancelled(args, *, action: str) -> int:
    if args.json:
        print(
            json.dumps(
                {
                    "schema": _ENVIRONMENT_PREVIEW_TRUST_OUTPUT_SCHEMA,
                    "action": action,
                    "cancelled": True,
                    "image_ref": args.image,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("No Environment Preview trust change was made.")
    return 0


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
    launch_profiles = interface.get("launchProfiles")
    profiles = (
        [
            (f"interface.launchProfiles[{index}]", profile)
            for index, profile in enumerate(launch_profiles)
            if isinstance(profile, dict)
        ]
        if isinstance(launch_profiles, list)
        else [("interface", interface)]
    )
    for label, profile in profiles:
        interface_runtime = (
            profile.get("runtime", {})
            if isinstance(profile.get("runtime"), dict)
            else {}
        )
        setup = (
            interface_runtime.get("setup")
            if isinstance(interface_runtime.get("setup"), dict)
            else None
        )
        if setup:
            blocks.append((f"{label}.runtime.setup", setup))
    return blocks


def _package_smoke(package: str, *, study: str | None, realm_root: str | None) -> dict:
    package_root = Path(package).expanduser().resolve()
    validation = validate_package(package_root, check_source=True, check_setup_files=True)
    if not validation.get("valid"):
        return {
            "valid": False,
            "package": str(package_root),
            "errors": ["Package validation failed."],
            "validation": validation,
        }
    try:
        study_path = _select_package_smoke_study(package_root, study)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "valid": False,
            "package": str(package_root),
            "errors": [str(exc)],
        }
    if study_path is None:
        return {
            "valid": False,
            "package": str(package_root),
            "errors": [
                "No smoke study selected and package does not contain exactly one study."
            ],
        }
    study_validation = validate_authoring_config(study_path)
    if not study_validation.get("valid"):
        return {
            "valid": False,
            "package": str(package_root),
            "study": str(study_path),
            "errors": ["Study validation failed."],
            "validation": study_validation,
        }
    try:
        if realm_root is not None:
            summary = run_study(
                str(study_path),
                package_root=str(package_root),
                realm_root=realm_root,
            )
            return _package_smoke_summary_result(package_root, study_path, summary)
        with tempfile.TemporaryDirectory(prefix="optpilot-package-smoke-") as tmp_dir:
            summary = run_study(
                str(study_path),
                package_root=str(package_root),
                realm_root=str(Path(tmp_dir) / "realm"),
            )
            return _package_smoke_summary_result(package_root, study_path, summary)
    except Exception as exc:
        return {
            "valid": False,
            "package": str(package_root),
            "study": str(study_path),
            "errors": [str(exc)],
        }


def _package_smoke_summary_result(package_root: Path, study_path: Path, summary) -> dict:
    payload = summary.to_dict()
    valid = summary.run_status == "succeeded" and summary.final_logical_failures == 0
    return {
        "valid": valid,
        "package": str(package_root),
        "study": str(study_path),
        "run_id": summary.run_id,
        "summary": payload,
        "errors": []
        if valid
        else [
            "Smoke run did not complete cleanly: "
            f"run_status={summary.run_status}, stop_code={summary.stop_code}, "
            f"final_logical_failures={summary.final_logical_failures}."
        ],
    }


def _select_package_smoke_study(package_root: Path, study: str | None) -> Path | None:
    try:
        canonical_root = package_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Package root does not exist: {package_root}.") from exc
    if not canonical_root.is_dir():
        raise ValueError(f"Package root is not a directory: {canonical_root}.")

    if study:
        requested = Path(study).expanduser()
        selected = requested if requested.is_absolute() else canonical_root / requested
        return _contained_smoke_study(canonical_root, selected)
    index = index_package(canonical_root)
    studies = [entry.path for entry in index.entries if entry.config == "study"]
    if len(studies) == 1:
        return _contained_smoke_study(canonical_root, studies[0])
    smoke_named = [path for path in studies if "smoke" in path.stem.lower()]
    if len(smoke_named) == 1:
        return _contained_smoke_study(canonical_root, smoke_named[0])
    return None


def _contained_smoke_study(package_root: Path, selected: Path) -> Path:
    try:
        canonical = selected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Smoke study does not exist: {selected}.") from exc
    try:
        canonical.relative_to(package_root)
    except ValueError as exc:
        raise ValueError(
            "Smoke study must be a file inside the explicit package root."
        ) from exc
    if not canonical.is_file():
        raise ValueError(f"Smoke study is not a file: {canonical}.")
    return canonical


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
