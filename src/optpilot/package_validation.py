"""Package-level validation for OptPilot catalog packages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .config import validate_authoring_config
from .locked_python_runtime_contract import (
    LockedPythonRuntimeError,
    validate_locked_python_setup_declaration,
)
from .method_protocol_limits import RETAINED_COMMAND_METHOD_INTERPRETERS
from .package_index import PackageEntry, index_package
from .retained_study_compiler import (
    RetainedStudyCompileError,
    preflight_retained_process_study,
)
from .schema_validation import validate_public_config_schema
from .spec import load_study_spec


JsonDict = Dict[str, Any]


def uses_locked_python_runtime_setup(raw: Mapping[str, Any]) -> bool:
    """Whether a component declares the retained exact-preparation boundary."""

    if not isinstance(raw, Mapping):
        return False
    runtime = raw.get("runtime")
    setup = runtime.get("setup") if isinstance(runtime, Mapping) else None
    if setup in (None, {}):
        return False
    try:
        validate_locked_python_setup_declaration(setup)
    except LockedPythonRuntimeError:
        return False
    return True


@dataclass
class PackageValidationEntry:
    path: str
    config: str
    id: str
    qualified_id: str
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    capabilities: JsonDict = field(default_factory=dict)
    synthesized: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path,
            "config": self.config,
            "id": self.id,
            "qualified_id": self.qualified_id,
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "capabilities": dict(self.capabilities),
            "synthesized": self.synthesized,
        }


def validate_package(
    package_root: str | Path,
    *,
    check_imports: bool = False,
    check_source: bool = False,
    check_setup_files: bool = False,
) -> JsonDict:
    """Validate all recognized public OptPilot configs in a package folder."""

    index = index_package(package_root)
    entries: List[PackageValidationEntry] = []
    for entry in index.entries:
        entries.append(
            _validate_entry(
                entry,
                package_root=index.package_root,
                check_imports=check_imports,
                check_source=check_source,
                check_setup_files=check_setup_files,
            )
        )

    valid = not index.errors and all(entry.valid for entry in entries)
    return {
        "valid": valid,
        "package": str(index.package_root),
        "package_id": index.package_id,
        "counts": index.counts(),
        "errors": list(index.errors),
        "ignored_yaml": [str(path) for path in index.ignored_yaml],
        "entries": [entry.to_dict() for entry in entries],
        "capabilities": _package_capabilities(
            index.entries,
            entries,
        ),
    }


def _validate_entry(
    entry: PackageEntry,
    *,
    package_root: Path,
    check_imports: bool,
    check_source: bool,
    check_setup_files: bool,
) -> PackageValidationEntry:
    errors: List[str] = []
    warnings: List[str] = []
    if entry.synthesized and entry.config == "resource":
        schema_result = validate_public_config_schema(entry.raw, config_path=entry.path)
        if not schema_result.valid:
            errors.extend(f"{issue.path}: {issue.message}" for issue in schema_result.errors)
    else:
        result = validate_authoring_config(entry.path)
        errors.extend(result.get("errors", []) or [])

    if check_source:
        errors.extend(_check_source_paths(entry, package_root))
    if check_setup_files:
        errors.extend(_check_setup_files(entry, package_root))
    import_errors: List[str] = []
    if check_imports:
        import_errors = _check_imports(entry, package_root)
        errors.extend(import_errors)

    capabilities = _entry_capabilities(
        entry,
        package_root=package_root,
        check_imports=check_imports,
        import_errors=import_errors,
    )

    return PackageValidationEntry(
        path=str(entry.path),
        config=entry.config,
        id=entry.id,
        qualified_id=entry.qualified_id,
        valid=not errors,
        errors=errors,
        warnings=warnings,
        capabilities=capabilities,
        synthesized=entry.synthesized,
    )


def _entry_capabilities(
    entry: PackageEntry,
    *,
    package_root: Path,
    check_imports: bool,
    import_errors: List[str],
) -> JsonDict:
    if entry.config != "method":
        return {}
    return {
        "retained_execution": _retained_method_execution_capability(
            entry,
            package_root=package_root,
            check_imports=check_imports,
            import_errors=import_errors,
        )
    }


def _retained_method_execution_capability(
    entry: PackageEntry,
    *,
    package_root: Path,
    check_imports: bool,
    import_errors: List[str],
) -> JsonDict:
    """Describe whether one authored method can use the retained public runner.

    Authoring validity and retained execution eligibility are deliberately
    separate.  The public schema describes future protocol targets too, while
    the retained process-study compiler accepts Python and command batch
    methods.
    """

    entrypoint = (
        entry.raw.get("entrypoint", {})
        if isinstance(entry.raw.get("entrypoint"), dict)
        else {}
    )
    protocol = str(entrypoint.get("protocol") or "batch")
    is_command = isinstance(entrypoint.get("command"), list) and not isinstance(
        entrypoint.get("python"), str
    )
    if not is_command and not isinstance(entrypoint.get("python"), str):
        return _execution_capability(
            supported=False,
            eligible=False,
            code="method_mode_unsupported",
            reason=(
                "The retained process-study runner requires a Python or "
                "command batch method entrypoint."
            ),
        )
    if protocol != "batch":
        return _execution_capability(
            supported=False,
            eligible=False,
            code="method_mode_unsupported",
            reason=(
                f"The retained process-study runner does not support method "
                f"protocol {protocol!r}; use a batch method for this execution slice."
            ),
        )
    if is_command:
        command = [str(item) for item in entrypoint.get("command", [])]
        if not command or command[0] not in RETAINED_COMMAND_METHOD_INTERPRETERS:
            return _execution_capability(
                supported=False,
                eligible=False,
                code="method_command_unsupported",
                reason=(
                    "The retained process slice executes command methods with "
                    "its prepared Python runtime; command[0] must be the "
                    "logical interpreter name python or python3."
                ),
            )
    runtime = (
        entry.raw.get("runtime", {})
        if isinstance(entry.raw.get("runtime"), dict)
        else {}
    )
    setup = runtime.get("setup")
    if setup not in (None, {}):
        try:
            validate_locked_python_setup_declaration(setup)
        except LockedPythonRuntimeError as error:
            return _execution_capability(
                supported=False,
                eligible=False,
                code=error.code,
                reason=str(error),
            )
        return _execution_capability(
            supported=True,
            eligible=False,
            code="runtime_verification_required",
            reason=(
                "Static checks accepted this Method's exact prepared dependency "
                "declaration. Run a retained Study test to prepare the locked "
                "layer and verify the method in its real execution runtime."
            ),
        )
    if is_command:
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_command_unchecked",
            reason=(
                "Static validation does not execute commands; run the explicit "
                "package smoke to verify the command batch method exchange."
            ),
        )
    if not check_imports:
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_callable_unchecked",
            reason=(
                "Static validation does not execute Python; run the explicit "
                "package smoke to verify the batch method callable."
            ),
        )
    if import_errors:
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_callable_unavailable",
            reason="The retained batch method callable could not be imported.",
        )

    ref = str(entrypoint["python"])
    python_paths = _python_path_roots(
        entry.path, entrypoint.get("pythonPath", []) or []
    )
    return _run_retained_method_shape_check(ref, python_paths, package_root)


def _execution_capability(
    *,
    supported: bool,
    eligible: bool,
    code: str,
    reason: str | None,
) -> JsonDict:
    payload = {
        "supported": supported,
        "eligible": eligible,
        "code": code,
        "reason": reason,
    }
    payload["smoke_eligible"] = bool(eligible) or (
        code in _SMOKE_VERIFIED_CAPABILITY_CODES
    )
    return payload


def _package_capabilities(
    indexed_entries: List[PackageEntry],
    entries: List[PackageValidationEntry],
) -> JsonDict:
    """Summarize retained execution for exact studies when they exist.

    A method callable is only a component capability.  Run capability belongs
    to an exact study/environment/method pairing, so packages containing study
    configs are evaluated through the same pure semantic preflight as the
    retained compiler.  This function never imports authored Python.
    """

    methods: List[JsonDict] = []
    methods_by_path: Dict[Path, JsonDict] = {}
    for entry in entries:
        capability = entry.capabilities.get("retained_execution")
        if not isinstance(capability, dict):
            continue
        item = {
            "path": entry.path,
            "id": entry.id,
            **capability,
        }
        methods.append(item)
        methods_by_path[Path(entry.path).resolve()] = item

    validations_by_path = {
        Path(entry.path).resolve(): entry for entry in entries
    }
    studies = [
        _retained_study_execution_capability(
            entry,
            validation=validations_by_path.get(entry.path.resolve()),
            methods_by_path=methods_by_path,
        )
        for entry in indexed_entries
        if entry.config == "study"
    ]
    if studies:
        eligible_studies = [item for item in studies if item.get("eligible") is True]
        if eligible_studies:
            summary = _execution_capability(
                supported=True,
                eligible=True,
                code="ready",
                reason=None,
            )
        elif len(studies) == 1:
            summary = {
                key: studies[0][key]
                for key in (
                    "supported",
                    "eligible",
                    "code",
                    "reason",
                    "smoke_eligible",
                )
            }
        else:
            summary = _execution_capability(
                supported=any(item.get("supported") is True for item in studies),
                eligible=False,
                code="no_eligible_retained_study",
                reason=(
                    "No study in the package is currently eligible for retained "
                    "execution; inspect the per-study capability reasons."
                ),
            )
        summary["methods"] = methods
        summary["studies"] = studies
        return {"retained_execution": summary}

    has_environment = any(entry.config == "environment" for entry in indexed_entries)
    if has_environment and methods:
        hard_blockers = [
            item for item in methods if retained_execution_blocks_smoke(item)
        ]
        if len(methods) == 1 and hard_blockers:
            summary = {
                key: methods[0][key]
                for key in (
                    "supported",
                    "eligible",
                    "code",
                    "reason",
                    "smoke_eligible",
                )
            }
            summary["methods"] = methods
            summary["studies"] = []
            return {"retained_execution": summary}
        summary = _execution_capability(
            supported=any(item.get("supported") is True for item in methods),
            eligible=False,
            code="study_required",
            reason=(
                "Executable eligibility requires an exact study that pairs one "
                "environment with one method; validate components now, then add "
                "and smoke that study."
            ),
        )
        summary["methods"] = methods
        summary["studies"] = []
        return {"retained_execution": summary}

    eligible = [item for item in methods if item.get("eligible") is True]
    if eligible:
        summary = _execution_capability(
            supported=True,
            eligible=True,
            code="ready",
            reason=None,
        )
    elif not methods:
        summary = _execution_capability(
            supported=True,
            eligible=False,
            code="method_required",
            reason="A retained study requires a compatible method.",
        )
    elif len(methods) == 1:
        summary = {
            key: methods[0][key]
            for key in (
                "supported",
                "eligible",
                "code",
                "reason",
                "smoke_eligible",
            )
        }
    else:
        summary = _execution_capability(
            supported=any(item.get("supported") is True for item in methods),
            eligible=False,
            code="no_eligible_retained_method",
            reason="No method in the package is eligible for retained execution.",
        )
    summary["methods"] = methods
    summary["studies"] = []
    return {"retained_execution": summary}


_SMOKE_VERIFIED_CAPABILITY_CODES = frozenset(
    {
        "method_callable_unchecked",
        "method_command_unchecked",
        "method_factory_requires_smoke",
        "runtime_verification_required",
        "study_required",
    }
)


def retained_execution_blocks_smoke(capability: Mapping[str, Any]) -> bool:
    """Return whether a capability is a known blocker, not an unchecked gate."""

    if capability.get("eligible") is True:
        return False
    return str(capability.get("code") or "") not in _SMOKE_VERIFIED_CAPABILITY_CODES


def _retained_study_execution_capability(
    entry: PackageEntry,
    *,
    validation: PackageValidationEntry | None,
    methods_by_path: Dict[Path, JsonDict],
) -> JsonDict:
    base = {
        "path": str(entry.path),
        "id": entry.id,
    }
    if validation is None or not validation.valid:
        return {
            **base,
            **_execution_capability(
                supported=False,
                eligible=False,
                code="study_invalid",
                reason="The study or one of its referenced configs is invalid.",
            ),
        }

    method_value = entry.raw.get("methodConfig")
    if not isinstance(method_value, str) or not method_value:
        return {
            **base,
            **_execution_capability(
                supported=False,
                eligible=False,
                code="study_method_required",
                reason="The study does not identify a method config.",
            ),
        }
    method_path = (
        Path(method_value).resolve()
        if Path(method_value).is_absolute()
        else (entry.path.parent / method_value).resolve()
    )
    method = methods_by_path.get(method_path)
    base.update(
        {
            "method_path": str(method_path),
            "method_id": method.get("id") if method is not None else None,
        }
    )
    if method is None:
        return {
            **base,
            **_execution_capability(
                supported=False,
                eligible=False,
                code="study_method_unavailable",
                reason="The study's exact method config is not in this package.",
            ),
        }

    try:
        study_spec = load_study_spec(str(entry.path))
        preflight_retained_process_study(study_spec)
    except RetainedStudyCompileError as error:
        return {
            **base,
            **_execution_capability(
                supported=False,
                eligible=False,
                code=error.code,
                reason=str(error),
            ),
        }
    except (OSError, TypeError, ValueError) as error:
        return {
            **base,
            **_execution_capability(
                supported=False,
                eligible=False,
                code="study_invalid",
                reason=str(error),
            ),
        }

    if method.get("eligible") is True:
        capability = _execution_capability(
            supported=True,
            eligible=True,
            code="ready",
            reason=None,
        )
    else:
        capability = {
            key: method.get(key)
            for key in (
                "supported",
                "eligible",
                "code",
                "reason",
                "smoke_eligible",
            )
        }
    return {
        **base,
        **capability,
    }


def _run_retained_method_shape_check(
    ref: str, python_paths: List[Path], package_root: Path
) -> JsonDict:
    """Inspect a method in an isolated interpreter without constructing it."""

    script = r"""
import importlib
import inspect
import json
import sys

ref = sys.argv[1]
sentinel = "__OPTPILOT_RETAINED_METHOD_CAPABILITY__"
try:
    module_name, sep, attr_path = ref.partition(":")
    if not sep:
        raise ValueError("Python reference must use module:attribute")
    module = importlib.import_module(module_name)
    value = module
    for part in attr_path.split("."):
        value = getattr(value, part)
    if not callable(value):
        result = {
            "supported": True,
            "eligible": False,
            "code": "method_callable_unavailable",
            "reason": "The retained method entrypoint must be callable.",
        }
    else:
        try:
            inspect.signature(value).bind({}, object(), object())
        except (TypeError, ValueError) as exc:
            result = {
                "supported": True,
                "eligible": False,
                "code": "method_constructor_incompatible",
                "reason": f"The retained method entrypoint must accept definition, study_spec, and rng: {exc}",
            }
        else:
            result = None
    if result is None and callable(getattr(value, "propose", None)):
        result = {"supported": True, "eligible": True, "code": "ready", "reason": None}
    elif result is None and inspect.isclass(value) and all(callable(getattr(value, name, None)) for name in ("start", "poll", "finalize")):
        result = {
            "supported": True,
            "eligible": False,
            "code": "method_propose_required",
            "reason": "The retained Python batch worker requires propose; start/poll/finalize-only methods use a legacy runtime shape that this runner does not execute.",
        }
    elif result is None and not inspect.isclass(value):
        result = {
            "supported": True,
            "eligible": False,
            "code": "method_factory_requires_smoke",
            "reason": "Callable method factories are verified by constructing the method during the explicit package smoke.",
        }
    elif result is None:
        result = {
            "supported": True,
            "eligible": False,
            "code": "method_propose_required",
            "reason": "The retained Python batch worker requires the method object to implement propose.",
        }
    print(sentinel + json.dumps(result, sort_keys=True))
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
"""
    env = dict(os.environ)
    # Import validation must not mutate the package being inspected by
    # materializing interpreter-specific ``__pycache__`` files into it.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    path_values = [str(path) for path in python_paths]
    if path_values:
        env["PYTHONPATH"] = os.pathsep.join(path_values)
    else:
        env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", script, ref],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(package_root),
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit code {completed.returncode}"
        )
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_callable_unavailable",
            reason=f"The retained batch method callable could not be inspected: {detail}",
        )
    sentinel = "__OPTPILOT_RETAINED_METHOD_CAPABILITY__"
    encoded = next(
        (
            line[len(sentinel) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(sentinel)
        ),
        "",
    )
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_callable_unavailable",
            reason="The retained batch method shape check returned malformed output.",
        )
    if not isinstance(payload, dict):
        return _execution_capability(
            supported=True,
            eligible=False,
            code="method_callable_unavailable",
            reason="The retained batch method shape check returned malformed output.",
        )
    return _execution_capability(
        supported=payload.get("supported") is True,
        eligible=payload.get("eligible") is True,
        code=str(payload.get("code") or "method_callable_unavailable"),
        reason=(
            str(payload["reason"])
            if payload.get("reason") not in (None, "")
            else None
        ),
    )


def _check_imports(entry: PackageEntry, package_root: Path) -> List[str]:
    """Best-effort import checks for Python callables referenced by a config."""

    raw = entry.raw
    # A host import is neither truthful nor reproducible when the component's
    # imports intentionally come from its exact prepared layer. The ordinary
    # retained Study smoke constructs that layer and verifies the callable.
    if uses_locked_python_runtime_setup(raw):
        return []
    refs: List[Tuple[str, List[Path]]] = []
    if entry.config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        for key in ("python", "adapter"):
            if isinstance(evaluator.get(key), str):
                refs.append((evaluator[key], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or [])))
    elif entry.config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        if isinstance(entrypoint.get("python"), str):
            refs.append((entrypoint["python"], _python_path_roots(entry.path, entrypoint.get("pythonPath", []) or [])))

    if not refs:
        return []

    errors: List[str] = []
    for ref, python_paths in refs:
        completed = _run_import_check(ref, python_paths, package_root)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            errors.append(f"Could not import {ref!r}: {detail}")
    return errors


def _check_source_paths(entry: PackageEntry, package_root: Path) -> List[str]:
    raw = entry.raw
    errors: List[str] = []
    config_dir = entry.source_root if entry.config == "resource" and entry.source_root is not None else _config_dir(entry.path)

    def check_path(value: Any, location: str, *, must_exist: bool = True) -> None:
        if not isinstance(value, str) or not value:
            return
        path = _resolve_package_path(value, config_dir, package_root, location, errors)
        if path is None:
            return
        if must_exist and not path.exists():
            errors.append(f"{location} does not exist: {value}")

    def check_python_paths(values: Iterable[Any], location: str) -> None:
        for index, value in enumerate(values):
            if isinstance(value, str):
                check_path(value, f"{location}[{index}]")

    if entry.config == "environment":
        evaluator = raw.get("evaluator", {}) if isinstance(raw.get("evaluator"), dict) else {}
        check_python_paths(evaluator.get("pythonPath", []) or [], "evaluator.pythonPath")
        if evaluator.get("pythonPath") and isinstance(evaluator.get("python"), str):
            errors.extend(_check_python_ref_source(evaluator["python"], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or []), package_root, "evaluator.python"))
        if evaluator.get("pythonPath") and isinstance(evaluator.get("adapter"), str):
            errors.extend(_check_python_ref_source(evaluator["adapter"], _python_path_roots(entry.path, evaluator.get("pythonPath", []) or []), package_root, "evaluator.adapter"))
        if isinstance(evaluator.get("cwd"), str):
            check_path(evaluator["cwd"], "evaluator.cwd")
        if isinstance(evaluator.get("command"), list):
            errors.extend(_check_command_file_tokens(evaluator["command"], config_dir, package_root, "evaluator.command"))
        for index, item in enumerate(raw.get("trialWorkspace", []) or []):
            if isinstance(item, dict):
                check_path(item.get("from"), f"trialWorkspace[{index}].from")
        method_context = raw.get("methodContext", {}) if isinstance(raw.get("methodContext"), dict) else {}
        for index, value in enumerate(method_context.get("instructions", []) or []):
            check_path(value, f"methodContext.instructions[{index}]")
        for index, item in enumerate(method_context.get("references", []) or []):
            if isinstance(item, dict):
                check_path(item.get("path"), f"methodContext.references[{index}].path")
    elif entry.config == "method":
        entrypoint = raw.get("entrypoint", {}) if isinstance(raw.get("entrypoint"), dict) else {}
        check_python_paths(entrypoint.get("pythonPath", []) or [], "entrypoint.pythonPath")
        if entrypoint.get("pythonPath") and isinstance(entrypoint.get("python"), str):
            errors.extend(_check_python_ref_source(entrypoint["python"], _python_path_roots(entry.path, entrypoint.get("pythonPath", []) or []), package_root, "entrypoint.python"))
        if isinstance(entrypoint.get("command"), list):
            errors.extend(_check_command_file_tokens(entrypoint["command"], config_dir, package_root, "entrypoint.command"))
    elif entry.config == "resource":
        pass
    elif entry.config == "study":
        check_path(raw.get("environmentConfig"), "environmentConfig")
        check_path(raw.get("methodConfig"), "methodConfig")
    if entry.config in {"environment", "method", "resource"}:
        interface = raw.get("interface", {}) if isinstance(raw.get("interface"), dict) else {}
        for interface_label, profile in _interface_authoring_profiles(interface):
            if isinstance(profile.get("cwd"), str):
                check_path(profile["cwd"], f"{interface_label}.cwd")
            if isinstance(profile.get("command"), list):
                interface_dir = (
                    _resolve_package_path(
                        profile.get("cwd", "."),
                        config_dir,
                        package_root,
                        f"{interface_label}.cwd",
                        errors,
                    )
                    if isinstance(profile.get("cwd", "."), str)
                    else config_dir
                )
                if interface_dir is not None:
                    errors.extend(
                        _check_command_file_tokens(
                            profile["command"],
                            interface_dir,
                            package_root,
                            f"{interface_label}.command",
                        )
                    )
    return errors


def _check_setup_files(entry: PackageEntry, package_root: Path) -> List[str]:
    errors: List[str] = []
    raw = entry.raw
    config_base = entry.source_root if entry.config == "resource" and entry.source_root is not None else _config_dir(entry.path)
    if entry.config in {"environment", "method"}:
        runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}
        errors.extend(_check_runtime_setup_files(runtime, config_base, package_root, "runtime"))
    if entry.config in {"environment", "method", "resource"}:
        interface = raw.get("interface", {}) if isinstance(raw.get("interface"), dict) else {}
        for interface_label, profile in _interface_authoring_profiles(interface):
            runtime = profile.get("runtime", {}) if isinstance(profile.get("runtime"), dict) else {}
            errors.extend(
                _check_runtime_setup_files(
                    runtime,
                    config_base,
                    package_root,
                    f"{interface_label}.runtime",
                )
            )
    return errors


def _interface_authoring_profiles(interface: JsonDict) -> List[Tuple[str, JsonDict]]:
    if not interface:
        return []
    launch_profiles = interface.get("launchProfiles")
    if isinstance(launch_profiles, list):
        return [
            (f"interface.launchProfiles[{index}]", profile)
            for index, profile in enumerate(launch_profiles)
            if isinstance(profile, dict)
        ]
    return [("interface", interface)]


def _check_runtime_setup_files(runtime: JsonDict, base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    if not runtime:
        return errors
    errors.extend(_check_setup_block_files(runtime.get("setup"), base_dir, package_root, f"{location}.setup"))
    container = runtime.get("container", {}) if isinstance(runtime.get("container"), dict) else {}
    build = container.get("build", {}) if isinstance(container.get("build"), dict) else {}
    if build:
        context_path = _resolve_package_path(build.get("context", "."), base_dir, package_root, f"{location}.container.build.context", errors)
        if context_path is not None and not context_path.exists():
            errors.append(f"{location}.container.build.context does not exist: {build.get('context', '.')}")
        dockerfile = build.get("dockerfile")
        if context_path is not None and isinstance(dockerfile, str) and dockerfile:
            dockerfile_path = Path(dockerfile).expanduser()
            resolved = dockerfile_path.resolve() if dockerfile_path.is_absolute() else (context_path / dockerfile_path).resolve()
            if not _is_relative_to(resolved, package_root):
                errors.append(f"{location}.container.build.dockerfile must stay inside package: {dockerfile}")
                return errors
            if not resolved.exists():
                errors.append(f"{location}.container.build.dockerfile does not exist: {dockerfile}")
    return errors


def _check_setup_block_files(setup: Any, base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(setup, dict):
        return errors
    for index, step in enumerate(setup.get("steps", []) or []):
        if not isinstance(step, dict):
            continue
        step_location = f"{location}.steps[{index}]"
        cwd_value = step.get("cwd", ".")
        cwd = _resolve_package_path(cwd_value, base_dir, package_root, f"{step_location}.cwd", errors)
        if cwd is None:
            continue
        if not cwd.exists():
            errors.append(f"{step_location}.cwd does not exist: {cwd_value}")
            continue
        kind = step.get("uses")
        if kind == "uv":
            if not ((cwd / "pyproject.toml").exists() or (cwd / "uv.lock").exists()):
                errors.append(f"{step_location} uses uv but no pyproject.toml or uv.lock exists in {cwd_value}")
        elif kind == "python-venv":
            for req_index, requirement in enumerate(step.get("requirements", []) or []):
                requirement_path = _resolve_package_path(requirement, cwd, package_root, f"{step_location}.requirements[{req_index}]", errors)
                if requirement_path is None:
                    continue
                if not requirement_path.exists():
                    errors.append(f"{step_location}.requirements[{req_index}] does not exist: {requirement}")
        elif kind == "npm":
            if not ((cwd / "package.json").exists() or (cwd / "package-lock.json").exists()):
                errors.append(f"{step_location} uses npm but no package.json or package-lock.json exists in {cwd_value}")
        elif kind == "command":
            command = step.get("command", []) if isinstance(step.get("command"), list) else []
            errors.extend(_check_command_file_tokens(command, cwd, package_root, f"{step_location}.command"))
    return errors


def _check_command_file_tokens(command: Iterable[Any], base_dir: Path, package_root: Path, location: str) -> List[str]:
    errors: List[str] = []
    tokens = list(command)
    inline_payload_indexes = _command_inline_payload_indexes(tokens)
    for index, token in enumerate(tokens):
        if index in inline_payload_indexes:
            continue
        if not isinstance(token, str) or not _looks_like_file_token(token):
            continue
        path = _resolve_package_path(token, base_dir, package_root, f"{location}[{index}]", errors)
        if path is None:
            continue
        if not path.exists():
            errors.append(f"{location}[{index}] file does not exist: {token}")
    return errors


def _command_inline_payload_indexes(command: List[Any]) -> set[int]:
    if not command or not isinstance(command[0], str):
        return set()
    executable = Path(command[0]).name
    indexes: set[int] = set()
    if executable.startswith("python"):
        for index, token in enumerate(command[1:], start=1):
            if token == "-c" and index + 1 < len(command):
                indexes.add(index + 1)
    if executable in {"sh", "bash", "zsh"}:
        for index, token in enumerate(command[1:], start=1):
            if isinstance(token, str) and token.startswith("-") and "c" in token and index + 1 < len(command):
                indexes.add(index + 1)
    return indexes


def _looks_like_file_token(value: str) -> bool:
    if not value or value.startswith("{") or "://" in value:
        return False
    if value in {"python", "python3", "uv", "npm", "node", "bash", "sh", "cmd", "powershell"}:
        return False
    suffix = Path(value).suffix.lower()
    if suffix in {".py", ".js", ".mjs", ".cjs", ".sh", ".bash", ".json", ".yaml", ".yml", ".toml", ".txt"}:
        return True
    return "/" in value or "\\" in value


def _check_python_ref_source(ref: str, python_paths: List[Path], package_root: Path, location: str) -> List[str]:
    module_name = ref.partition(":")[0]
    if not module_name:
        return []
    module_path = Path(*module_name.split("."))
    for root in python_paths:
        if not _is_relative_to(root.resolve(), package_root.resolve()):
            continue
        if (root / f"{module_path}.py").exists() or (root / module_path / "__init__.py").exists():
            return []
    return [f"{location} module file not found under declared pythonPath: {ref}"]


def _run_import_check(ref: str, python_paths: List[Path], package_root: Path) -> subprocess.CompletedProcess[str]:
    script = """
import importlib
import json
import sys

ref = sys.argv[1]
try:
    module_name, sep, attr_path = ref.partition(":")
    if not sep:
        raise ValueError("Python reference must use module:attribute")
    module = importlib.import_module(module_name)
    value = module
    for part in attr_path.split("."):
        value = getattr(value, part)
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
"""
    env = dict(os.environ)
    # This is a read-only validation boundary. Bytecode caches would both
    # modify user source and later become accidental retained package input.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    path_values = [str(path) for path in python_paths]
    if path_values:
        env["PYTHONPATH"] = os.pathsep.join(path_values)
    else:
        env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", script, ref],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(package_root),
        timeout=30,
        check=False,
    )


def _python_path_roots(config_path: Path, values: Iterable[Any]) -> List[Path]:
    roots = [_config_dir(config_path)]
    for value in values:
        if isinstance(value, str) and value:
            roots.append(_resolve_config_path(value, config_path))
    return _dedupe_paths(roots)


def _resolve_config_path(value: Any, config_path: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_config_dir(config_path) / path).resolve()


def _resolve_relative_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def _resolve_package_path(value: Any, base_dir: Path, package_root: Path, location: str, errors: List[str]) -> Path | None:
    path = Path(str(value)).expanduser()
    resolved = path.resolve() if path.is_absolute() else (base_dir / path).resolve()
    if not _is_relative_to(resolved, package_root.resolve()):
        errors.append(f"{location} must stay inside package: {value}")
        return None
    return resolved


def _config_dir(path: Path) -> Path:
    return path.parent if path.is_file() else path


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
