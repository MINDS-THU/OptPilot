"""Ephemeral planning for one explicit local study-package boundary.

This module is intentionally the only host-path-facing part of the retained
study flow.  It validates a caller-declared package root and the three authored
configuration files, then invokes the public config compiler exactly once.  It
does not infer a root, guess content policy from filenames, resolve imports,
copy or capture content, rebase compiled semantics, or mutate the package.

``LocalStudyPackagePlan`` deliberately has no portable serialization.  It is a
UI/preflight result, not durable semantic authority: the package can change
after this function returns.  A production retention flow must seal bytes first
and compile from that exact immutable snapshot (or independently prove the live
tree unchanged).  Only normalized package-relative config paths and logical
import roots may cross that boundary as authored package identity.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NoReturn

import yaml

from ..config import compile_authoring_config
from ..config_errors import CodedConfigError
from ..package_settings import load_package_settings
from ..spec import StudySpec, study_spec_from_raw
from ..runtime_limits import MAX_ATTEMPT_INPUT_LAYERS
from .errors import ContentRejected
from .manifests import validate_portable_path, validate_portable_paths


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class LocalStudyPackagePlanError(ValueError):
    """A stable rejection at the ephemeral local-package boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> NoReturn:
    raise LocalStudyPackagePlanError(code, message)


@dataclass(frozen=True, slots=True)
class LocalStudyPackagePlan:
    """One non-authoritative, deliberately non-serializable local preflight."""

    study_spec: StudySpec
    package_root: Path
    study_config_path: str
    environment_config_path: str
    method_config_path: str
    environment_python_import_roots: tuple[str, ...]
    method_python_import_roots: tuple[str, ...]
    method_context_instruction_paths: tuple[str, ...] = ()
    method_context_reference_paths: tuple[str, ...] = ()
    trial_workspace_mappings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.study_spec, StudySpec):
            raise TypeError("study_spec must be a StudySpec.")
        if not isinstance(self.package_root, Path) or not self.package_root.is_absolute():
            raise ValueError("package_root must be an absolute Path.")
        for field_name in (
            "study_config_path",
            "environment_config_path",
            "method_config_path",
        ):
            value = getattr(self, field_name)
            try:
                normalized = validate_portable_path(value)
            except (TypeError, ValueError, RuntimeError) as error:
                raise ValueError(
                    f"{field_name} must be a canonical portable relative path."
                ) from error
            if normalized != value:
                raise ValueError(
                    f"{field_name} must be a canonical portable relative path."
                )
        validate_portable_paths(
            (
                self.study_config_path,
                self.environment_config_path,
                self.method_config_path,
            )
        )
        for field_name, config_path in (
            ("environment_python_import_roots", self.environment_config_path),
            ("method_python_import_roots", self.method_config_path),
        ):
            roots = getattr(self, field_name)
            if not isinstance(roots, tuple) or not roots:
                raise TypeError(f"{field_name} must be a non-empty tuple.")
            if any(not isinstance(value, str) for value in roots):
                raise TypeError(f"{field_name} must contain strings.")
            if len(set(roots)) != len(roots):
                raise ValueError(f"{field_name} must not contain duplicates.")
            expected_default = PurePosixPath(config_path).parent.as_posix()
            if roots[0] != expected_default:
                raise ValueError(
                    f"{field_name} must begin with its config directory "
                    f"{expected_default!r}."
                )
            for value in roots:
                if value == ".":
                    continue
                try:
                    validate_portable_path(value)
                except (ContentRejected, TypeError, ValueError) as error:
                    raise ValueError(
                        f"{field_name} entries must be '.' or canonical portable paths."
                    ) from error
        for field_name in (
            "method_context_instruction_paths",
            "method_context_reference_paths",
        ):
            paths = getattr(self, field_name)
            if not isinstance(paths, tuple) or any(
                not isinstance(value, str) for value in paths
            ):
                raise TypeError(f"{field_name} must be a tuple of strings.")
            for value in paths:
                try:
                    validate_portable_path(value)
                except (ContentRejected, TypeError, ValueError) as error:
                    raise ValueError(
                        f"{field_name} entries must be canonical portable paths."
                    ) from error
        mappings = self.trial_workspace_mappings
        if not isinstance(mappings, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in mappings
        ):
            raise TypeError(
                "trial_workspace_mappings must be a tuple of (source, destination) tuples."
            )
        if len(mappings) > MAX_ATTEMPT_INPUT_LAYERS:
            raise ValueError("trial_workspace_mappings contains too many entries.")
        for source, destination in mappings:
            if source != ".":
                try:
                    validate_portable_path(source)
                except (ContentRejected, TypeError, ValueError) as error:
                    raise ValueError(
                        "trial workspace sources must be '.' or canonical portable paths."
                    ) from error
            if destination != ".":
                try:
                    validate_portable_path(destination)
                except (ContentRejected, TypeError, ValueError) as error:
                    raise ValueError(
                        "trial workspace destinations must be '.' or canonical portable paths."
                    ) from error


def _as_path(value: Any, label: str) -> Path:
    if not isinstance(value, Path):
        _fail("path_type_invalid", f"{label} must be a Path.")
    if ".." in value.parts:
        _fail(
            "package_boundary_ambiguous",
            f"{label} must not contain parent traversal: {value}.",
        )
    return value


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _prepare_package_root(value: Any) -> tuple[Path, Path]:
    requested = _as_path(value, "package_root")
    lexical_root = _absolute_without_following(requested)
    try:
        linked = os.lstat(lexical_root)
    except FileNotFoundError:
        _fail("package_root_missing", f"Package root does not exist: {lexical_root}.")
    except OSError as error:
        _fail("package_root_unreadable", f"Cannot inspect package root {lexical_root}: {error}.")
    if stat.S_ISLNK(linked.st_mode):
        _fail("symlink_rejected", f"Package root must not be a symlink: {lexical_root}.")
    if not stat.S_ISDIR(linked.st_mode):
        _fail("package_root_not_directory", f"Package root is not a directory: {lexical_root}.")
    try:
        canonical_root = lexical_root.resolve(strict=True)
    except OSError as error:
        _fail("package_root_unreadable", f"Cannot resolve package root {lexical_root}: {error}.")
    if canonical_root.parent == canonical_root:
        _fail(
            "package_boundary_ambiguous",
            "The filesystem root cannot be used as a study package boundary.",
        )
    return lexical_root, canonical_root


def _scan_clean_package_tree(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            _fail("package_tree_unreadable", f"Cannot inspect package directory {directory}: {error}.")
        entries.sort(key=lambda item: os.fsencode(item.name))
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                linked = entry.stat(follow_symlinks=False)
            except OSError as error:
                _fail("package_tree_unreadable", f"Cannot inspect package entry {entry_path}: {error}.")
            if stat.S_ISLNK(linked.st_mode):
                _fail("symlink_rejected", f"Package trees must not contain symlinks: {entry_path}.")
            is_directory = stat.S_ISDIR(linked.st_mode)
            if is_directory:
                pending.append(entry_path)
            elif not stat.S_ISREG(linked.st_mode):
                _fail(
                    "package_entry_unsupported",
                    f"Package entry must be a regular file or directory: {entry_path}.",
                )


def _lexically_contained(
    candidate: Path,
    *,
    lexical_root: Path,
    canonical_root: Path,
    label: str,
) -> tuple[Path, str]:
    lexical_candidate = _absolute_without_following(candidate)
    try:
        lexical_candidate.relative_to(lexical_root)
    except ValueError:
        _fail(
            "config_outside_package",
            f"{label} is outside the explicit package root: {candidate}.",
        )

    try:
        canonical_candidate = lexical_candidate.resolve(strict=False)
        relative = canonical_candidate.relative_to(canonical_root)
    except (OSError, ValueError):
        _fail(
            "config_outside_package",
            f"{label} resolves outside the explicit package root: {candidate}.",
        )
    if not relative.parts:
        _fail("config_not_regular_file", f"{label} names the package directory, not a file.")
    portable = relative.as_posix()
    try:
        validate_portable_path(portable)
    except (ContentRejected, TypeError, ValueError) as error:
        _fail(
            "config_path_not_portable",
            f"{label} does not have a canonical portable package-relative path: {portable!r}.",
        )
    return canonical_candidate, portable


def _require_regular_config(path: Path, label: str) -> None:
    try:
        linked = os.lstat(path)
    except FileNotFoundError:
        _fail("config_missing", f"{label} does not exist: {path}.")
    except OSError as error:
        _fail("config_unreadable", f"Cannot inspect {label} {path}: {error}.")
    if stat.S_ISLNK(linked.st_mode):
        _fail("symlink_rejected", f"{label} must not be a symlink: {path}.")
    if not stat.S_ISREG(linked.st_mode):
        _fail("config_not_regular_file", f"{label} is not a regular file: {path}.")


def _load_study_references(path: Path) -> tuple[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        _fail("study_config_unreadable", f"Cannot read public study config {path}: {error}.")
    if not isinstance(raw, Mapping):
        _fail("study_config_invalid", f"Public study config must be a mapping: {path}.")
    references: list[str] = []
    for field in ("environmentConfig", "methodConfig"):
        value = raw.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            _fail(
                "config_reference_invalid",
                f"Public study config {field} must be a non-empty relative path.",
            )
        if "\x00" in value or "\\" in value or Path(value).is_absolute() or _WINDOWS_ABSOLUTE_PATH.match(value):
            _fail(
                "config_reference_not_portable",
                f"Public study config {field} must be a portable relative path: {value!r}.",
            )
        references.append(value)
    return references[0], references[1]


def _resolve_reference(
    reference: str,
    *,
    study_path: Path,
    canonical_root: Path,
    label: str,
) -> tuple[Path, str]:
    return _lexically_contained(
        study_path.parent / Path(reference),
        # ``study_path`` is canonical.  Comparing it with the canonical root
        # avoids false outside-boundary rejections on hosts where an ancestor
        # such as /var is itself a platform symlink.
        lexical_root=canonical_root,
        canonical_root=canonical_root,
        label=label,
    )


def _cross_check_compiled_config_paths(
    compiled: Mapping[str, Any],
    *,
    study_path: Path,
    environment_path: Path,
    method_path: Path,
) -> None:
    extensions = compiled.get("extensions")
    authoring = extensions.get("authoringConfig") if isinstance(extensions, Mapping) else None
    if not isinstance(authoring, Mapping):
        _fail(
            "compiled_config_path_mismatch",
            "Compiled study omitted extensions.authoringConfig path anchors.",
        )
    expected = {
        "studyConfigPath": study_path,
        "environmentConfigPath": environment_path,
        "methodConfigPath": method_path,
    }
    for field, expected_path in expected.items():
        value = authoring.get(field)
        if not isinstance(value, str) or Path(value) != expected_path:
            _fail(
                "compiled_config_path_mismatch",
                f"Compiled {field} does not match the validated package path.",
            )


def _compiled_python_path_values(
    compiled: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    environment = compiled.get("environment")
    adapter = environment.get("adapter") if isinstance(environment, Mapping) else None
    adapter_config = adapter.get("config") if isinstance(adapter, Mapping) else None
    evaluate = (
        adapter_config.get("evaluate")
        if isinstance(adapter_config, Mapping)
        else None
    )
    if not isinstance(evaluate, Mapping):
        _fail(
            "compiled_python_import_roots_invalid",
            "Compiled environment does not expose an exact evaluator definition.",
        )
    method = compiled.get("method")
    implementation = (
        method.get("implementation") if isinstance(method, Mapping) else None
    )
    if not isinstance(implementation, Mapping):
        _fail(
            "compiled_python_import_roots_invalid",
            "Compiled method does not expose an exact entrypoint definition.",
        )

    def values(mapping: Mapping[str, Any], label: str) -> tuple[str, ...]:
        raw = mapping.get("pythonPath", ())
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            _fail(
                "compiled_python_import_roots_invalid",
                f"Compiled {label} pythonPath must be a sequence.",
            )
        result = tuple(raw)
        if any(not isinstance(value, str) or not value for value in result):
            _fail(
                "compiled_python_import_roots_invalid",
                f"Compiled {label} pythonPath entries must be non-empty strings.",
            )
        return result

    return (
        values(evaluate, "environment evaluator"),
        values(implementation, "method entrypoint"),
    )


def _logical_python_import_roots(
    *,
    config_directory: Path,
    compiled_python_paths: tuple[str, ...],
    package_root: Path,
    label: str,
) -> tuple[str, ...]:
    candidates = (config_directory, *(Path(value) for value in compiled_python_paths))
    seen_paths: set[Path] = set()
    logical_roots: list[str] = []
    for candidate in candidates:
        if not candidate.is_absolute():
            _fail(
                "compiled_python_import_roots_invalid",
                f"Compiled {label} import root is not absolute: {candidate}.",
            )
        try:
            canonical = candidate.resolve(strict=False)
        except OSError as error:
            _fail(
                "python_import_root_unreadable",
                f"Cannot resolve {label} import root {candidate}: {error}.",
            )
        if canonical != candidate:
            _fail(
                "python_import_root_not_canonical",
                f"Compiled {label} import root is not canonical: {candidate}.",
            )
        try:
            relative = canonical.relative_to(package_root)
        except ValueError:
            _fail(
                "python_import_root_outside_package",
                f"{label} import root is outside the explicit package: {canonical}.",
            )
        if canonical in seen_paths:
            continue
        try:
            linked = os.lstat(canonical)
        except FileNotFoundError:
            _fail(
                "python_import_root_missing",
                f"{label} import root does not exist: {canonical}.",
            )
        except OSError as error:
            _fail(
                "python_import_root_unreadable",
                f"Cannot inspect {label} import root {canonical}: {error}.",
            )
        if stat.S_ISLNK(linked.st_mode):
            _fail(
                "symlink_rejected",
                f"{label} import root must not be a symlink: {canonical}.",
            )
        if not stat.S_ISDIR(linked.st_mode):
            _fail(
                "python_import_root_not_directory",
                f"{label} import root is not a directory: {canonical}.",
            )
        logical = relative.as_posix()
        if logical != ".":
            try:
                validate_portable_path(logical)
            except (ContentRejected, TypeError, ValueError) as error:
                _fail(
                    "python_import_root_not_portable",
                    f"{label} import root is not portable: {logical!r}: {error}",
                )
        seen_paths.add(canonical)
        logical_roots.append(logical)

    try:
        validate_portable_paths(
            value for value in logical_roots if value != "."
        )
    except (ContentRejected, TypeError, ValueError) as error:
        _fail(
            "python_import_roots_ambiguous",
            f"{label} import roots are not jointly portable: {error}",
        )
    return tuple(logical_roots)


def _compiled_method_context_paths(
    compiled: Mapping[str, Any], *, package_root: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return package-relative identities for environment-owned method inputs.

    The expanded public representation contains operational absolute paths for
    the legacy runner.  This host-boundary planner is the only retained-study
    component allowed to interpret them.  Durable compilation receives only
    the corresponding portable paths inside the already sealed package.
    """

    candidate = compiled.get("candidate")
    context = candidate.get("context") if isinstance(candidate, Mapping) else None
    method_context = (
        context.get("methodContext") if isinstance(context, Mapping) else None
    )
    if not isinstance(method_context, Mapping):
        return (), ()

    instructions = method_context.get("instructions", ())
    references = method_context.get("references", ())
    if isinstance(instructions, (str, bytes)) or not isinstance(
        instructions, Sequence
    ):
        _fail(
            "compiled_method_context_invalid",
            "Compiled methodContext.instructions must be a sequence.",
        )
    if isinstance(references, (str, bytes)) or not isinstance(references, Sequence):
        _fail(
            "compiled_method_context_invalid",
            "Compiled methodContext.references must be a sequence.",
        )

    def logical(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            _fail(
                "compiled_method_context_invalid",
                f"Compiled {label} must be a non-empty path.",
            )
        candidate_path = Path(value)
        if not candidate_path.is_absolute():
            _fail(
                "compiled_method_context_invalid",
                f"Compiled {label} must be an absolute legacy path.",
            )
        try:
            canonical = candidate_path.resolve(strict=True)
        except FileNotFoundError:
            _fail(
                "method_context_missing",
                f"Compiled {label} is absent from the explicit package.",
            )
        except (OSError, RuntimeError):
            _fail(
                "compiled_method_context_invalid",
                f"Compiled {label} cannot be resolved safely.",
            )
        try:
            relative = canonical.relative_to(package_root)
        except ValueError:
            _fail(
                "method_context_outside_package",
                f"Compiled {label} is outside the explicit package.",
            )
        if canonical != candidate_path or not canonical.is_file():
            _fail(
                "compiled_method_context_invalid",
                f"Compiled {label} must name one canonical regular package file.",
            )
        portable = relative.as_posix()
        try:
            return validate_portable_path(portable)
        except (ContentRejected, TypeError, ValueError) as error:
            _fail(
                "compiled_method_context_invalid",
                f"Compiled {label} is not a portable package path.",
            )

    instruction_paths = tuple(
        logical(value, f"methodContext.instructions[{index}]")
        for index, value in enumerate(instructions)
    )
    reference_paths: list[str] = []
    for index, reference in enumerate(references):
        if not isinstance(reference, Mapping):
            _fail(
                "compiled_method_context_invalid",
                f"Compiled methodContext.references[{index}] must be a mapping.",
            )
        reference_paths.append(
            logical(
                reference.get("path"),
                f"methodContext.references[{index}].path",
            )
        )
    return instruction_paths, tuple(reference_paths)


def _compiled_trial_workspace_mappings(
    compiled: Mapping[str, Any], *, package_root: Path
) -> tuple[tuple[str, str], ...]:
    """Replace expanded host copy sources with exact package-relative identities."""

    candidate = compiled.get("candidate")
    candidate_context = (
        candidate.get("context") if isinstance(candidate, Mapping) else None
    )
    environment = compiled.get("environment")
    adapter = environment.get("adapter") if isinstance(environment, Mapping) else None
    adapter_config = adapter.get("config") if isinstance(adapter, Mapping) else None
    adapter_context = (
        adapter_config.get("context") if isinstance(adapter_config, Mapping) else None
    )
    if not isinstance(candidate_context, Mapping) or not isinstance(
        adapter_config, Mapping
    ) or not isinstance(adapter_context, Mapping):
        _fail(
            "compiled_trial_workspace_invalid",
            "Compiled environment does not expose an exact trial workspace declaration.",
        )
    declarations = candidate_context.get("trialWorkspace", ())
    mirrors = (
        candidate_context.get("workspace", {}).get("copy", ())
        if isinstance(candidate_context.get("workspace"), Mapping)
        else None,
        adapter_context.get("trialWorkspace", ()),
        adapter_context.get("workspace", {}).get("copy", ())
        if isinstance(adapter_context.get("workspace"), Mapping)
        else None,
        adapter_config.get("workspace", {}).get("copy", ())
        if isinstance(adapter_config.get("workspace"), Mapping)
        else None,
    )
    if isinstance(declarations, (str, bytes)) or not isinstance(
        declarations, Sequence
    ):
        _fail(
            "compiled_trial_workspace_invalid",
            "Compiled trialWorkspace must be a sequence.",
        )
    if len(declarations) > MAX_ATTEMPT_INPUT_LAYERS:
        _fail(
            "trial_workspace_too_large",
            "Compiled trialWorkspace contains too many mappings.",
        )
    if any(value != declarations for value in mirrors):
        _fail(
            "compiled_trial_workspace_mismatch",
            "Compiled candidate and evaluator trial workspace declarations differ.",
        )

    result: list[tuple[str, str]] = []
    for index, raw in enumerate(declarations):
        if not isinstance(raw, Mapping) or set(raw) != {"from", "to"}:
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}] must contain only from and to.",
            )
        source_value = raw.get("from")
        destination_value = raw.get("to")
        if not isinstance(source_value, str) or not source_value:
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from must be a non-empty path.",
            )
        source = Path(source_value)
        if not source.is_absolute():
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from must be an absolute legacy path.",
            )
        try:
            canonical = source.resolve(strict=True)
        except FileNotFoundError:
            _fail(
                "trial_workspace_missing",
                f"Compiled trialWorkspace[{index}].from is absent from the explicit package.",
            )
        except (OSError, RuntimeError):
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from cannot be resolved safely.",
            )
        try:
            relative = canonical.relative_to(package_root)
        except ValueError:
            _fail(
                "trial_workspace_outside_package",
                f"Compiled trialWorkspace[{index}].from is outside the explicit package.",
            )
        if canonical != source:
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from must be canonical.",
            )
        try:
            linked = os.lstat(canonical)
        except OSError:
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from cannot be inspected.",
            )
        if stat.S_ISLNK(linked.st_mode):
            _fail(
                "symlink_rejected",
                f"Compiled trialWorkspace[{index}].from must not be a symlink.",
            )
        if not (stat.S_ISDIR(linked.st_mode) or stat.S_ISREG(linked.st_mode)):
            _fail(
                "compiled_trial_workspace_invalid",
                f"Compiled trialWorkspace[{index}].from must be a regular file or directory.",
            )
        portable_source = relative.as_posix()
        if portable_source != ".":
            try:
                portable_source = validate_portable_path(portable_source)
            except (ContentRejected, TypeError, ValueError):
                _fail(
                    "compiled_trial_workspace_invalid",
                    f"Compiled trialWorkspace[{index}].from is not a portable package path.",
                )
        if destination_value == ".":
            portable_destination = "."
        else:
            try:
                portable_destination = validate_portable_path(destination_value)
            except (ContentRejected, TypeError, ValueError):
                _fail(
                    "compiled_trial_workspace_invalid",
                    f"Compiled trialWorkspace[{index}].to is not a portable destination.",
                )
        if stat.S_ISREG(linked.st_mode) and portable_destination == ".":
            _fail(
                "trial_workspace_destination_invalid",
                "A file trialWorkspace source requires an explicit destination file path.",
            )
        result.append((portable_source, portable_destination))
    return tuple(result)


def plan_local_study_package(
    study_config_path: Path,
    package_root: Path,
    *,
    launch_inputs: Mapping[str, Any] | None = None,
) -> LocalStudyPackagePlan:
    """Validate and compile one caller-declared local study package.

    The public compiler is invoked exactly once, after all paths and the whole
    package tree pass this host-boundary preflight.  The returned object is an
    ephemeral preflight only; no filesystem bytes have been captured or
    retained, so its ``StudySpec`` must not be persisted as if snapshot-backed.
    """

    requested_study = _as_path(study_config_path, "study_config_path")
    lexical_root, canonical_root = _prepare_package_root(package_root)
    _scan_clean_package_tree(canonical_root)

    study_path, study_relative = _lexically_contained(
        requested_study,
        lexical_root=lexical_root,
        canonical_root=canonical_root,
        label="study config",
    )
    _require_regular_config(study_path, "study config")
    environment_reference, method_reference = _load_study_references(study_path)
    environment_path, environment_relative = _resolve_reference(
        environment_reference,
        study_path=study_path,
        canonical_root=canonical_root,
        label="environment config",
    )
    method_path, method_relative = _resolve_reference(
        method_reference,
        study_path=study_path,
        canonical_root=canonical_root,
        label="method config",
    )
    _require_regular_config(environment_path, "environment config")
    _require_regular_config(method_path, "method config")
    try:
        validate_portable_paths(
            (study_relative, environment_relative, method_relative)
        )
    except (ContentRejected, TypeError, ValueError) as error:
        _fail(
            "config_paths_ambiguous",
            f"Config paths are not jointly portable: {error}",
        )

    try:
        try:
            package_settings = load_package_settings(canonical_root)
        except (OSError, TypeError, ValueError) as error:
            _fail(
                "package_settings_invalid",
                f"Package settings are invalid: {error}",
            )
        compiled = compile_authoring_config(
            study_path,
            launch_inputs=launch_inputs,
            package_settings=package_settings,
        )
        _cross_check_compiled_config_paths(
            compiled,
            study_path=study_path,
            environment_path=environment_path,
            method_path=method_path,
        )
        environment_python_paths, method_python_paths = (
            _compiled_python_path_values(compiled)
        )
        environment_python_import_roots = _logical_python_import_roots(
            config_directory=environment_path.parent,
            compiled_python_paths=environment_python_paths,
            package_root=canonical_root,
            label="environment evaluator",
        )
        method_python_import_roots = _logical_python_import_roots(
            config_directory=method_path.parent,
            compiled_python_paths=method_python_paths,
            package_root=canonical_root,
            label="method entrypoint",
        )
        (
            method_context_instruction_paths,
            method_context_reference_paths,
        ) = _compiled_method_context_paths(compiled, package_root=canonical_root)
        trial_workspace_mappings = _compiled_trial_workspace_mappings(
            compiled, package_root=canonical_root
        )
        study_spec = study_spec_from_raw(study_path, compiled)
    except LocalStudyPackagePlanError:
        raise
    except CodedConfigError:
        # The compiler already assigned a stable, caller-actionable code (for
        # example study_inputs_required, which carries the input names a
        # caller must collect). Re-coding it as a generic compile failure
        # would discard exactly the information the caller needs.
        raise
    except (OSError, TypeError, ValueError) as error:
        _fail("config_compile_failed", f"Public study config compilation failed: {error}")

    return LocalStudyPackagePlan(
        study_spec=study_spec,
        package_root=canonical_root,
        study_config_path=study_relative,
        environment_config_path=environment_relative,
        method_config_path=method_relative,
        environment_python_import_roots=environment_python_import_roots,
        method_python_import_roots=method_python_import_roots,
        method_context_instruction_paths=method_context_instruction_paths,
        method_context_reference_paths=method_context_reference_paths,
        trial_workspace_mappings=trial_workspace_mappings,
    )


__all__ = [
    "LocalStudyPackagePlan",
    "LocalStudyPackagePlanError",
    "plan_local_study_package",
]
