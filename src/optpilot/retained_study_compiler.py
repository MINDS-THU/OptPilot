"""Compile one retained whole-package process study without filesystem access.

This is the narrow source/runtime-preparation bridge for the first Realm study
slice.  Its input is already-expanded study semantics plus one exact retained
package membership.  It never resolves a host path, copies content, builds a
runtime, or talks to the ledger.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, NoReturn, Tuple

from .realm._validation import required_text, thaw_json
from .realm.errors import RealmIntegrityError
from .realm.manifests import TreeEntry, TreeManifest, validate_portable_path
from .realm.owner_derivation import Binding, OwnerDerivationManifest, SourceAnchor
from .realm.process_provider import ProcessProviderIdentity
from .realm.refs import SnapshotRef, canonical_json_bytes
from .realm.run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    EnvironmentRevisionManifest,
    InterfaceLaunchProfile,
    PreparedEnvironmentRuntimeManifest,
    ScopeLayer,
    ScopePath,
)
from .realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
    MethodRevisionManifest,
    PreparedMethodRuntimeManifest,
    RunDefinitionManifest,
)
from .locked_python_runtime_contract import (
    LockedPythonRuntimeError,
    PreparedPythonRuntime,
    validate_locked_python_setup_declaration,
)
from .method_launch_environment import (
    MethodLaunchEnvironmentError,
    normalize_method_environment_names,
)
from .retained_file_candidates import validate_file_candidate_contract
from .realm.study_definition import (
    STUDY_DEFINITION_OWNER_KIND,
    StudyDefinitionManifest,
)
from .spec import StudySpec
from .runtime_limits import MAX_ATTEMPT_INPUT_LAYERS, TRIAL_FILESYSTEM_QUOTA
from .study_realm_compiler import (
    StudyRealmCompileError,
    compile_study_run_definition,
    expected_retained_environment_contract,
    expected_retained_method_contract,
)


JsonDict = Dict[str, Any]

RETAINED_PROCESS_STUDY_COMPILER_ID = "optpilot.retained-process-study-compiler"
RETAINED_PROCESS_STUDY_COMPILER_VERSION = "1"
LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA = (
    "optpilot.logical-python-process-runtime-settings.v1"
)
PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA = (
    "optpilot.retained-package-input-projection.v1"
)
METHOD_CONTEXT_SCOPE = "method-context"
TRIAL_INPUT_SCOPE = "trial"

_BATCH_PROTOCOL = "optpilot.method.batch.v1"
_PACKAGE_SCOPE = "study-package-source"


class RetainedStudyCompileError(StudyRealmCompileError):
    """A stable first-slice feature or semantic rejection."""


def _fail(code: str, message: str) -> NoReturn:
    raise RetainedStudyCompileError(code, message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _portable_config_path(value: Any, label: str) -> str:
    try:
        result = validate_portable_path(value)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(f"{label} must be a canonical portable relative path.") from error
    return result


def _portable_root_or_path(value: Any, label: str) -> str:
    if value == ".":
        return "."
    return _portable_config_path(value, label)


def _ordered_package_paths(value: Any, label: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence.")
    result = tuple(
        _portable_root_or_path(item, f"{label} entry") for item in value
    )
    if not result:
        raise ValueError(f"{label} must not be empty.")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates.")
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("study_shape_invalid", f"{label} must be a mapping.")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail("study_shape_invalid", f"{label} must be a sequence.")
    return value


def _present(mapping: Mapping[str, Any], key: str) -> bool:
    return mapping.get(key) not in (None, "", (), [], {})


def package_projection_contract_features(
    contract: Mapping[str, Any],
) -> frozenset[str]:
    """Validate the shared retained-package projection-contract envelope."""

    if not isinstance(contract, Mapping):
        raise ValueError("retained package projection contract must be a mapping.")
    if not contract:
        return frozenset()
    allowed = {"method_context", "schema", "trial_workspace"}
    if (
        not set(contract) <= allowed
        or contract.get("schema") != PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA
    ):
        raise ValueError("retained package projection contract shape is unsupported.")
    features = frozenset(set(contract) - {"schema"})
    if not features:
        raise ValueError("retained package projection contract has no features.")
    if "method_context" in features:
        binding = contract["method_context"]
        if not isinstance(binding, Mapping) or set(binding) != {
            "logical_scope",
            "source",
        }:
            raise ValueError("retained method-context projection binding is invalid.")
        source = binding.get("source")
        if (
            binding.get("logical_scope") != METHOD_CONTEXT_SCOPE
            or not isinstance(source, Mapping)
            or set(source) != {"path", "scope"}
            or source.get("scope") != _PACKAGE_SCOPE
        ):
            raise ValueError("retained method-context projection source is invalid.")
        _portable_root_or_path(
            source.get("path"), "retained method-context projection source path"
        )
    if "trial_workspace" in features:
        binding = contract["trial_workspace"]
        if not isinstance(binding, Mapping) or dict(binding) != {
            "collision_policy": "identical-effective-entries-only",
            "destination_scope": TRIAL_INPUT_SCOPE,
            "source_scope": _PACKAGE_SCOPE,
        }:
            raise ValueError("retained trial workspace projection binding is invalid.")
    return features


@dataclass(frozen=True)
class RetainedStudyPackage:
    """One exact whole-package owner membership and its authored configs."""

    source_anchor: SourceAnchor
    store_id: str
    snapshot_ref: SnapshotRef
    source_role: str
    study_config_path: str
    environment_config_path: str
    method_config_path: str
    environment_python_import_roots: Tuple[str, ...]
    method_python_import_roots: Tuple[str, ...]
    method_context_instruction_paths: Tuple[str, ...] = ()
    method_context_reference_paths: Tuple[str, ...] = ()
    trial_workspace_mappings: Tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_anchor, SourceAnchor):
            raise TypeError("source_anchor must be a SourceAnchor.")
        required_text(self.store_id, "retained package store_id", max_bytes=128)
        if not isinstance(self.snapshot_ref, SnapshotRef):
            raise TypeError("retained package snapshot_ref must be a SnapshotRef.")
        required_text(self.source_role, "retained package source_role", max_bytes=128)
        for field_name in (
            "study_config_path",
            "environment_config_path",
            "method_config_path",
        ):
            object.__setattr__(
                self,
                field_name,
                _portable_config_path(
                    getattr(self, field_name), f"retained package {field_name}"
                ),
            )
        object.__setattr__(
            self,
            "environment_python_import_roots",
            _ordered_package_paths(
                self.environment_python_import_roots,
                "retained package environment Python import roots",
            ),
        )
        object.__setattr__(
            self,
            "method_python_import_roots",
            _ordered_package_paths(
                self.method_python_import_roots,
                "retained package method Python import roots",
            ),
        )
        for field_name in (
            "method_context_instruction_paths",
            "method_context_reference_paths",
        ):
            value = getattr(self, field_name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise TypeError(f"retained package {field_name} must be a sequence.")
            object.__setattr__(
                self,
                field_name,
                tuple(
                    _portable_config_path(
                        item, f"retained package {field_name} entry"
                    )
                    for item in value
                ),
            )
        mappings = self.trial_workspace_mappings
        if isinstance(mappings, (str, bytes)) or not isinstance(
            mappings, Sequence
        ):
            raise TypeError(
                "retained package trial_workspace_mappings must be a sequence."
            )
        if len(mappings) > MAX_ATTEMPT_INPUT_LAYERS:
            raise ValueError(
                "retained package trial_workspace_mappings contains too many entries."
            )
        normalized_mappings: list[tuple[str, str]] = []
        for index, item in enumerate(mappings):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError(
                    "retained package trial workspace entries must be source/destination pairs."
                )
            source = _portable_root_or_path(
                item[0], f"retained package trial workspace source {index}"
            )
            destination = _portable_root_or_path(
                item[1], f"retained package trial workspace destination {index}"
            )
            normalized_mappings.append((source, destination))
        object.__setattr__(
            self, "trial_workspace_mappings", tuple(normalized_mappings)
        )
        expected_environment_root = _config_parent(self.environment_config_path)
        expected_method_root = _config_parent(self.method_config_path)
        if self.environment_python_import_roots[0] != expected_environment_root:
            raise ValueError(
                "retained package environment Python roots must begin with "
                "the environment config directory."
            )
        if self.method_python_import_roots[0] != expected_method_root:
            raise ValueError(
                "retained package method Python roots must begin with the "
                "method config directory."
            )

    def to_dict(self) -> JsonDict:
        return {
            "environment_config_path": self.environment_config_path,
            "environment_python_import_roots": list(
                self.environment_python_import_roots
            ),
            "method_config_path": self.method_config_path,
            "method_context_instruction_paths": list(
                self.method_context_instruction_paths
            ),
            "method_context_reference_paths": list(
                self.method_context_reference_paths
            ),
            "method_python_import_roots": list(self.method_python_import_roots),
            "snapshot_ref": str(self.snapshot_ref),
            "source_anchor": self.source_anchor.to_dict(),
            "source_role": self.source_role,
            "store_id": self.store_id,
            "study_config_path": self.study_config_path,
            "trial_workspace_mappings": [
                {"source": source, "destination": destination}
                for source, destination in self.trial_workspace_mappings
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetainedStudyPackage":
        _exact_keys(
            payload,
            {
                "environment_config_path",
                "environment_python_import_roots",
                "method_config_path",
                "method_context_instruction_paths",
                "method_context_reference_paths",
                "method_python_import_roots",
                "snapshot_ref",
                "source_anchor",
                "source_role",
                "store_id",
                "study_config_path",
                "trial_workspace_mappings",
            },
            "retained study package",
        )
        result = cls(
            source_anchor=SourceAnchor.from_dict(payload["source_anchor"]),
            store_id=payload["store_id"],
            snapshot_ref=SnapshotRef.parse(payload["snapshot_ref"]),
            source_role=payload["source_role"],
            study_config_path=payload["study_config_path"],
            environment_config_path=payload["environment_config_path"],
            method_config_path=payload["method_config_path"],
            method_context_instruction_paths=tuple(
                payload["method_context_instruction_paths"]
            ),
            method_context_reference_paths=tuple(
                payload["method_context_reference_paths"]
            ),
            environment_python_import_roots=tuple(
                payload["environment_python_import_roots"]
            ),
            method_python_import_roots=tuple(
                payload["method_python_import_roots"]
            ),
            trial_workspace_mappings=tuple(
                (item["source"], item["destination"])
                for item in payload["trial_workspace_mappings"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("retained study package is not canonical.")
        return result


@dataclass(frozen=True)
class LogicalPythonRuntimeSettings:
    """Typed provider-neutral Python import roots inside logical scopes."""

    import_roots: Tuple[ScopePath, ...]

    def __post_init__(self) -> None:
        if isinstance(self.import_roots, (str, bytes)) or not isinstance(
            self.import_roots, Sequence
        ):
            raise TypeError("logical Python import_roots must be a sequence.")
        roots = tuple(self.import_roots)
        if not roots or any(not isinstance(item, ScopePath) for item in roots):
            raise TypeError("logical Python import_roots must contain ScopePath values.")
        if len(set(roots)) != len(roots):
            raise ValueError("logical Python import_roots must not contain duplicates.")
        # Python path precedence is semantic.  Preserve the planner's exact
        # config-directory-first order instead of sorting it for convenience.
        object.__setattr__(self, "import_roots", roots)

    def to_dict(self) -> JsonDict:
        return {
            "import_roots": [item.to_dict() for item in self.import_roots],
            "schema": LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA,
        }


@dataclass(frozen=True)
class RetainedStudyCompilation:
    """The three mutually cross-anchored manifests produced by compilation."""

    run_definition: RunDefinitionManifest
    owner_derivation: OwnerDerivationManifest
    study_definition: StudyDefinitionManifest

    def __post_init__(self) -> None:
        if not isinstance(self.run_definition, RunDefinitionManifest):
            raise TypeError("run_definition must be a RunDefinitionManifest.")
        if not isinstance(self.owner_derivation, OwnerDerivationManifest):
            raise TypeError("owner_derivation must be an OwnerDerivationManifest.")
        if not isinstance(self.study_definition, StudyDefinitionManifest):
            raise TypeError("study_definition must be a StudyDefinitionManifest.")
        if (
            self.study_definition.run_definition != self.run_definition
            or self.study_definition.owner_id != self.owner_derivation.target_owner_id
            or self.study_definition.owner_derivation_manifest_digest
            != self.owner_derivation.digest
        ):
            raise ValueError("retained study compilation manifests do not cross-anchor.")


def _config_parent(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _manifest_paths(manifest: TreeManifest) -> tuple[set[str], set[str]]:
    files = {item.path for item in manifest.entries if item.kind == "file"}
    directories = {
        item.path for item in manifest.entries if item.kind == "directory"
    }
    directories.add(".")
    return files, directories


def _compile_attempt_input_layers(
    package: RetainedStudyPackage,
    package_manifest: TreeManifest,
) -> Tuple[ScopeLayer, ...]:
    """Compile trialWorkspace into layers after exact effective-tree collision checks."""

    manifest_entries = {item.path: item for item in package_manifest.entries}
    effective: dict[str, tuple[str, object | None, int | None, bool | None]] = {}
    spellings: dict[str, str] = {}

    def add(
        path: str,
        *,
        kind: str,
        blob_ref: object | None = None,
        size: int | None = None,
        executable: bool | None = None,
    ) -> None:
        try:
            path = validate_portable_path(path)
        except (TypeError, ValueError, RuntimeError) as error:
            _fail(
                "trial_workspace_collision",
                f"Trial workspace produces a non-portable path {path!r}.",
            )
        folded = "/".join(part.casefold() for part in path.split("/"))
        previous_spelling = spellings.get(folded)
        if previous_spelling is not None and previous_spelling != path:
            _fail(
                "trial_workspace_collision",
                "Trial workspace paths collide on a case-insensitive target: "
                f"{previous_spelling!r} and {path!r}.",
            )
        value = (kind, blob_ref, size, executable)
        previous = effective.get(path)
        if previous is not None and previous != value:
            _fail(
                "trial_workspace_collision",
                f"Trial workspace layers conflict at {path!r}.",
            )
        effective[path] = value
        spellings[folded] = path

    def add_parents(path: str) -> None:
        components = path.split("/")
        for index in range(1, len(components)):
            add("/".join(components[:index]), kind="directory")

    layers: list[ScopeLayer] = []
    for precedence, (source, destination) in enumerate(
        package.trial_workspace_mappings
    ):
        source_entry = None if source == "." else manifest_entries.get(source)
        if source != "." and source_entry is None:
            _fail(
                "trial_workspace_unretained",
                f"Trial workspace source {source!r} is absent from the retained package.",
            )
        source_kind = "directory" if source == "." else source_entry.kind
        if source_kind == "file":
            if destination == ".":
                _fail(
                    "trial_workspace_destination_invalid",
                    "A file trialWorkspace source requires an explicit destination file path.",
                )
            assert source_entry is not None
            add_parents(destination)
            add(
                destination,
                kind="file",
                blob_ref=source_entry.blob_ref,
                size=source_entry.size,
                executable=source_entry.executable,
            )
        elif source_kind == "directory":
            if destination != ".":
                add_parents(destination)
                add(destination, kind="directory")
            prefix = "" if source == "." else source + "/"
            for item in package_manifest.entries:
                if source != "." and not item.path.startswith(prefix):
                    continue
                relative = item.path if source == "." else item.path[len(prefix) :]
                target = (
                    relative
                    if destination == "."
                    else f"{destination}/{relative}"
                )
                add_parents(target)
                if item.kind == "directory":
                    add(target, kind="directory")
                else:
                    add(
                        target,
                        kind="file",
                        blob_ref=item.blob_ref,
                        size=item.size,
                        executable=item.executable,
                    )
        else:  # pragma: no cover - TreeManifest validates its union
            _fail(
                "trial_workspace_unretained",
                f"Trial workspace source {source!r} has an unsupported type.",
            )
        layers.append(
            ScopeLayer(
                scope=TRIAL_INPUT_SCOPE,
                snapshot_ref=package.snapshot_ref,
                source_subpath=source,
                destination_subpath=destination,
                precedence=precedence,
            )
        )
    file_sizes = [
        int(value[2])
        for value in effective.values()
        if value[0] == "file" and value[2] is not None
    ]
    if (
        len(effective) > TRIAL_FILESYSTEM_QUOTA.max_entries
        or any(size > TRIAL_FILESYSTEM_QUOTA.max_file_bytes for size in file_sizes)
        or sum(file_sizes) > TRIAL_FILESYSTEM_QUOTA.max_total_bytes
    ):
        _fail(
            "trial_workspace_quota_exceeded",
            "The effective trial workspace lower layers exceed the trial volume quota.",
        )
    return tuple(layers)


def _join_package_path(root: str, relative: str) -> str:
    if root == ".":
        return relative
    return f"{root}/{relative}"


def _common_parent(paths: Sequence[str]) -> str:
    if not paths:
        raise ValueError("method context paths must not be empty.")
    parents = [PurePosixPath(value).parent.parts for value in paths]
    common: list[str] = []
    for components in zip(*parents):
        if len(set(components)) != 1:
            break
        common.append(components[0])
    return "/".join(common) or "."


def _relative_to_package_subtree(path: str, root: str) -> str:
    selected = PurePosixPath(path)
    if root == ".":
        return selected.as_posix()
    try:
        return selected.relative_to(PurePosixPath(root)).as_posix()
    except ValueError as error:  # pragma: no cover - guarded by common parent
        raise ValueError("method context path escaped its package subtree.") from error


def _resolve_package_path(base: str, value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("interface_path_invalid", f"{label} must be a relative POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute():
        _fail("interface_host_path", f"{label} must not be an absolute host path.")
    components = [] if base == "." else base.split("/")
    for component in path.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not components:
                _fail("interface_path_escape", f"{label} escapes the retained package.")
            components.pop()
            continue
        components.append(component)
    return "/".join(components) or "."


def _callable_module(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _fail("python_callable_invalid", f"{label} must use module:object form.")
    module, separator, attribute = value.partition(":")
    if (
        not separator
        or not module
        or not attribute
        or any(not component.isidentifier() for component in module.split("."))
    ):
        _fail("python_callable_invalid", f"{label} must use module:object form.")
    return module


def _require_source_backed_callable(
    value: Any,
    *,
    import_roots: Sequence[str],
    files: set[str],
    label: str,
) -> None:
    module = _callable_module(value, label)
    module_path = "/".join(module.split("."))
    candidates = {
        _join_package_path(root, f"{module_path}.py")
        for root in import_roots
    }
    candidates.update(
        _join_package_path(root, f"{module_path}/__init__.py")
        for root in import_roots
    )
    if files.isdisjoint(candidates):
        _fail(
            "python_callable_unretained",
            f"{label} is not present under its declared retained Python roots.",
        )


def _validate_retained_package(
    study_spec: StudySpec,
    package: RetainedStudyPackage,
    package_manifest: TreeManifest,
) -> None:
    if not isinstance(package_manifest, TreeManifest):
        raise TypeError("package_manifest must be a TreeManifest.")
    if package_manifest.snapshot_ref != package.snapshot_ref:
        raise ValueError("retained package manifest differs from its snapshot ref.")
    files, directories = _manifest_paths(package_manifest)
    for path, label in (
        (package.study_config_path, "study config"),
        (package.environment_config_path, "environment config"),
        (package.method_config_path, "method config"),
    ):
        if path not in files:
            _fail(
                "authored_config_unretained",
                f"Retained {label} is absent from the exact package manifest.",
            )
    for roots, label in (
        (package.environment_python_import_roots, "environment"),
        (package.method_python_import_roots, "method"),
    ):
        missing = [path for path in roots if path not in directories]
        if missing:
            _fail(
                "python_root_unretained",
                f"Retained {label} Python roots are absent or not directories: {missing!r}.",
            )

    adapter = _mapping(study_spec.environment.get("adapter"), "environment.adapter")
    adapter_config = _mapping(adapter.get("config"), "environment.adapter.config")
    evaluate = _mapping(adapter_config.get("evaluate"), "environment evaluator")
    _require_source_backed_callable(
        evaluate.get("callable"),
        import_roots=package.environment_python_import_roots,
        files=files,
        label="environment evaluator callable",
    )
    implementation = _mapping(
        study_spec.method.get("implementation"), "method.implementation"
    )
    _require_source_backed_callable(
        implementation.get("callable"),
        import_roots=package.method_python_import_roots,
        files=files,
        label="method callable",
    )

    environment_parent = _config_parent(package.environment_config_path)
    interfaces = _sequence(
        adapter_config.get("interfaces", []), "environment interfaces"
    )
    for index, raw_profile in enumerate(interfaces):
        raw_profile = _mapping(raw_profile, f"environment interface {index}")
        raw_cwd = raw_profile.get("cwd", ".")
        if isinstance(raw_cwd, str):
            if raw_cwd.startswith(("/", "\\")) or (
                len(raw_cwd) >= 3
                and raw_cwd[1] == ":"
                and raw_cwd[2] in {"/", "\\"}
            ):
                _fail(
                    "interface_host_path",
                    f"Environment interface {index}.cwd contains an absolute host path.",
                )
            if ".." in PurePosixPath(raw_cwd).parts:
                _fail(
                    "interface_path_escape",
                    f"Environment interface {index}.cwd contains traversal.",
                )
        raw_command = raw_profile.get("command", ())
        if isinstance(raw_command, (list, tuple)):
            for token in raw_command:
                if isinstance(token, str) and (
                    token.startswith(("/", "\\"))
                    or (
                        len(token) >= 3
                        and token[1] == ":"
                        and token[2] in {"/", "\\"}
                    )
                ):
                    _fail(
                        "interface_host_path",
                        f"Environment interface {index}.command contains an absolute host path.",
                    )
                if isinstance(token, str) and ".." in PurePosixPath(token).parts:
                    _fail(
                        "interface_path_escape",
                        f"Environment interface {index}.command contains traversal.",
                    )
        try:
            profile = InterfaceLaunchProfile.from_dict(raw_profile)
        except RealmIntegrityError as error:
            _fail(
                "interface_profile_invalid",
                f"Environment interface {index} is invalid: {error}",
            )
        cwd = _resolve_package_path(
            environment_parent,
            profile.cwd,
            f"environment interface {index}.cwd",
        )
        if cwd not in directories:
            _fail(
                "interface_cwd_unretained",
                f"Environment interface {index}.cwd is absent from the retained package.",
            )
        if profile.runtime.setup is not None:
            for step_index, step in enumerate(profile.runtime.setup.steps):
                step_cwd = _resolve_package_path(
                    environment_parent,
                    step.get("cwd", "."),
                    f"environment interface {index}.runtime.setup.steps[{step_index}].cwd",
                )
                if step_cwd not in directories:
                    _fail(
                        "interface_setup_cwd_unretained",
                        f"Environment interface {index} setup step {step_index} cwd "
                        "is absent from the retained package.",
                    )
                if step.get("uses") == "python-venv":
                    for requirement in step.get("requirements", ()):
                        requirement_path = _resolve_package_path(
                            step_cwd,
                            requirement,
                            f"environment interface {index}.runtime.setup.steps"
                            f"[{step_index}].requirements",
                        )
                        if requirement_path not in files:
                            _fail(
                                "interface_setup_input_unretained",
                                f"Environment interface {index} setup requirement "
                                f"{requirement!r} is absent from the retained package.",
                            )
        interface_container = profile.runtime.container
        if interface_container is not None and interface_container.build is not None:
            build = interface_container.build
            build_context = _resolve_package_path(
                environment_parent,
                build.context,
                f"environment interface {index}.runtime.container.build.context",
            )
            if build_context not in directories:
                _fail(
                    "interface_build_context_unretained",
                    f"Environment interface {index} container build context is "
                    "absent from the retained package.",
                )
            dockerfile = _resolve_package_path(
                build_context,
                build.dockerfile,
                f"environment interface {index}.runtime.container.build.dockerfile",
            )
            if dockerfile not in files:
                _fail(
                    "interface_build_input_unretained",
                    f"Environment interface {index} container build Dockerfile is "
                    "absent from the retained package.",
                )


def _preflight_first_slice(
    study_spec: StudySpec,
    *,
    environment_setup_prepared: bool | None = None,
    method_setup_prepared: bool | None = None,
) -> None:
    if not isinstance(study_spec, StudySpec):
        raise TypeError("study_spec must be a StudySpec.")
    candidate = _mapping(study_spec.candidate, "candidate")
    candidate_format = candidate.get("format")
    if candidate_format not in {"parameters", "files"}:
        _fail(
            "candidate_format_unsupported",
            "The retained process-study slice supports parameter and file candidates.",
        )
    if candidate_format == "files":
        try:
            validate_file_candidate_contract(candidate)
        except (TypeError, ValueError, RecursionError) as error:
            _fail(
                "file_candidate_contract_invalid",
                f"The retained file-candidate contract is invalid: {error}",
            )

    method = _mapping(study_spec.method, "method")
    implementation = _mapping(method.get("implementation"), "method.implementation")
    if (
        implementation.get("type") != "python"
        or implementation.get("protocol") != _BATCH_PROTOCOL
    ):
        _fail(
            "method_mode_unsupported",
            "The retained process-study slice requires a Python optpilot.method.batch.v1 method.",
        )
    compatibility = _mapping(
        method.get("compatibility"), "method.compatibility"
    )
    formats = _sequence(
        compatibility.get("formats", ()), "method.compatibility.formats"
    )
    if candidate_format not in formats:
        _fail(
            "method_candidate_format_unsupported",
            "The retained method does not accept the environment candidate format.",
        )

    environment = _mapping(study_spec.environment, "environment")
    adapter = _mapping(environment.get("adapter"), "environment.adapter")
    adapter_config = _mapping(adapter.get("config"), "environment.adapter.config")
    evaluate = _mapping(adapter_config.get("evaluate"), "environment evaluator")
    if evaluate.get("type") != "python":
        _fail(
            "environment_mode_unsupported",
            "The retained process-study slice requires a configured Python evaluator.",
        )
    if evaluate.get("cwd", ".") != ".":
        _fail(
            "evaluator_cwd_unsupported",
            "The Python evaluator slice requires candidate-workspace cwd '.'.",
        )

    execution = _mapping(study_spec.execution, "execution")
    backend = _mapping(execution.get("backend"), "execution.backend")
    defaults = _mapping(execution.get("defaults"), "execution.defaults")
    sandbox = _mapping(defaults.get("sandboxSpec"), "execution defaults sandboxSpec")
    runtime_kinds = (
        _mapping(environment.get("runtime", {}), "environment.runtime").get("type", "process"),
        _mapping(method.get("runtime", {}), "method.runtime").get("type", "process"),
        "container" if backend.get("type") == "container" else "process",
        sandbox.get("runtimeType", "process"),
    )
    if any(value != "process" for value in runtime_kinds):
        _fail(
            "container_runtime_unsupported",
            "The retained process-study slice does not prepare container runtimes.",
        )

    environment_runtime = _mapping(
        environment.get("runtime", {}), "environment.runtime"
    )
    method_runtime = _mapping(method.get("runtime", {}), "method.runtime")
    backend_config = _mapping(
        backend.get("config", {}), "execution.backend.config"
    )
    environment_setup = environment_runtime.get("setup")
    backend_setup = backend_config.get("setup")
    if canonical_json_bytes(thaw_json(environment_setup)) != canonical_json_bytes(
        thaw_json(backend_setup)
    ):
        _fail(
            "environment_setup_mirror_mismatch",
            "The evaluator backend no longer mirrors the Environment dependency declaration.",
        )

    setup_policies = {
        "environment.runtime": environment_setup_prepared,
        "execution.backend.config": environment_setup_prepared,
        "method.runtime": method_setup_prepared,
    }
    for label, runtime in (
        ("environment.runtime", environment_runtime),
        ("method.runtime", method_runtime),
        ("execution.backend.config", backend_config),
    ):
        container = runtime.get("container", {})
        if container not in (None, {}) and not isinstance(container, Mapping):
            _fail("study_shape_invalid", f"{label}.container must be a mapping.")
        container = container if isinstance(container, Mapping) else {}
        if _present(runtime, "setup"):
            try:
                validate_locked_python_setup_declaration(runtime.get("setup"))
            except LockedPythonRuntimeError as error:
                _fail(error.code, f"{label}: {error}")
            if setup_policies[label] is False:
                _fail(
                    "runtime_setup_unprepared",
                    f"{label}.setup was not retained as an exact dependency layer.",
                )
        elif setup_policies[label] is True:
            _fail(
                "prepared_runtime_unexpected",
                f"A prepared layer was supplied for {label}, but it declares no setup.",
            )
        if _present(runtime, "build") or _present(container, "build"):
            _fail("runtime_build_unsupported", f"{label}.build is not prepared by this slice.")
        if _present(runtime, "envFromHost") and label != "method.runtime":
            _fail(
                "host_environment_unsupported",
                f"{label}.envFromHost is not a reproducible process-runtime input.",
            )
        if label == "method.runtime":
            try:
                normalize_method_environment_names(runtime.get("envFromHost"))
            except MethodLaunchEnvironmentError as error:
                _fail(error.code, str(error))
        if _present(runtime, "workdir"):
            _fail(
                "runtime_workdir_unsupported",
                f"{label}.workdir cannot be rebased from an expanded host path.",
            )

    # The host paths in the legacy expanded representation are ignored here.
    # Their exact, package-relative replacements are supplied by the sealed
    # package planner through ``RetainedStudyPackage`` and verified below.

    context = _mapping(candidate.get("context"), "candidate.context")
    context_workspace = _mapping(context.get("workspace", {}), "candidate workspace")
    environment_workspace = _mapping(
        adapter_config.get("workspace", {}), "environment workspace"
    )
    trial_declarations = context.get("trialWorkspace", ())
    if isinstance(trial_declarations, (str, bytes)) or not isinstance(
        trial_declarations, Sequence
    ):
        _fail(
            "study_shape_invalid",
            "candidate.context.trialWorkspace must be a sequence.",
        )
    if (
        context_workspace.get("copy", ()) != trial_declarations
        or environment_workspace.get("copy", ()) != trial_declarations
    ):
        _fail(
            "trial_workspace_mismatch",
            "Candidate and evaluator trial workspace declarations differ.",
        )
    unsupported_attempt_features: tuple[Any, ...] = ()
    for section in ("validation", "materialization"):
        definition = _mapping(candidate.get(section), f"candidate.{section}")
        config = _mapping(definition.get("config", {}), f"candidate.{section}.config")
        unsupported_attempt_features += (
            config.get("seedFiles"),
            config.get("readonlyFiles"),
        )
    if any(
        value not in (None, "", (), [], {})
        for value in unsupported_attempt_features
    ):
        _fail(
            "attempt_inputs_unsupported",
            "Legacy candidate validation/materialization path inputs are unsupported.",
        )


def preflight_retained_process_study(study_spec: StudySpec) -> None:
    """Check whether expanded study semantics fit the retained process slice.

    This is deliberately a pure, non-executing capability check.  Package
    authoring validation can therefore reuse the exact semantic gate enforced
    by :func:`compile_retained_process_study` without importing user modules,
    constructing callables, accessing a Realm, or preparing a runtime.
    Executable verification remains the responsibility of an explicit smoke
    run over the immutable package artifact.
    """

    _preflight_first_slice(study_spec)


def _path_free_study_spec(
    study_spec: StudySpec,
    *,
    package: RetainedStudyPackage,
    package_files: set[str],
    attempt_input_layers: Sequence[ScopeLayer],
    environment_setup_prepared: bool,
    method_setup_prepared: bool,
) -> tuple[StudySpec, JsonDict]:
    """Replace legacy expanded host import paths with retained runtime roots.

    The public compiler still expands ``pythonPath`` to checkout paths for the
    legacy runner.  Those values are neither identity nor authority here: the
    sealed package planner supplied their canonical logical replacements, and
    ``_validate_retained_package`` proved the callable under those roots.
    """

    raw = copy.deepcopy(study_spec.raw)
    adapter = raw["environment"]["adapter"]
    adapter["config"]["evaluate"]["pythonPath"] = []
    raw["method"]["implementation"].pop("pythonPath", None)

    # ``setup`` is an authoring-time recipe, not a Run-time requirement.  The
    # preflight above has already proved that every present recipe is the
    # supported locked form and that an exact prepared layer was supplied for
    # it.  Persist the resulting layer through the retained runtime manifests,
    # then remove the consumed recipe from all path-free contracts.  Keeping it
    # here would incorrectly ask a Run to execute setup again.
    if environment_setup_prepared:
        environment_runtime = raw["environment"]["runtime"]
        backend_runtime = raw["execution"]["backend"]["config"]
        environment_runtime.pop("setup", None)
        backend_runtime.pop("setup", None)
        # Authoring expansion adds this no-op default whenever ``runtime`` is
        # nonempty.  Network isolation is already retained by sandboxSpec; it
        # is not a second evaluator/backend runtime requirement.
        if environment_runtime.get("networkPolicy") == "disabled":
            environment_runtime.pop("networkPolicy")
        if backend_runtime.get("networkPolicy") == "disabled":
            backend_runtime.pop("networkPolicy")
    if method_setup_prepared:
        method_runtime = raw["method"]["runtime"]
        method_runtime.pop("setup", None)
        if method_runtime.get("networkPolicy") == "disabled":
            method_runtime.pop("networkPolicy")

    candidate_context = _mapping(
        raw["candidate"].get("context"), "candidate context"
    )
    adapter_context = _mapping(
        adapter["config"].get("context"), "environment adapter context"
    )
    projection_contract: JsonDict = {}
    candidate_method_context = _mapping(
        candidate_context.get("methodContext", {}), "candidate methodContext"
    )
    adapter_method_context = _mapping(
        adapter_context.get("methodContext", {}), "environment methodContext"
    )
    instructions = _sequence(
        candidate_method_context.get("instructions", ()),
        "candidate methodContext.instructions",
    )
    references = _sequence(
        candidate_method_context.get("references", ()),
        "candidate methodContext.references",
    )
    if (
        len(instructions) != len(package.method_context_instruction_paths)
        or len(references) != len(package.method_context_reference_paths)
    ):
        _fail(
            "method_context_plan_mismatch",
            "Retained method-context paths differ from the compiled environment.",
        )
    all_paths = (
        *package.method_context_instruction_paths,
        *package.method_context_reference_paths,
    )
    missing = [path for path in all_paths if path not in package_files]
    if missing:
        _fail(
            "method_context_unretained",
            f"Retained method-context files are absent from the package: {missing!r}.",
        )
    if canonical_json_bytes(candidate_method_context) != canonical_json_bytes(
        adapter_method_context
    ):
        _fail(
            "method_context_plan_mismatch",
            "Candidate and evaluator method-context declarations differ.",
        )
    if not all_paths:
        if candidate_method_context not in ({}, {"instructions": [], "references": []}):
            _fail(
                "method_context_plan_mismatch",
                "Method context is present without retained package inputs.",
            )
    else:
        root = _common_parent(all_paths)
        portable_instructions = [
            _relative_to_package_subtree(path, root)
            for path in package.method_context_instruction_paths
        ]
        portable_references: list[JsonDict] = []
        for index, raw_reference in enumerate(references):
            reference = dict(
                _mapping(raw_reference, f"candidate methodContext reference {index}")
            )
            reference["path"] = _relative_to_package_subtree(
                package.method_context_reference_paths[index], root
            )
            portable_references.append(reference)
        portable_method_context = {
            "instructions": portable_instructions,
            "references": portable_references,
        }
        candidate_context["methodContext"] = copy.deepcopy(portable_method_context)
        adapter_context["methodContext"] = copy.deepcopy(portable_method_context)
        projection_contract["method_context"] = {
            "logical_scope": METHOD_CONTEXT_SCOPE,
            "source": {"path": root, "scope": _PACKAGE_SCOPE},
        }

    adapter_config = _mapping(adapter.get("config"), "environment adapter config")
    candidate_workspace = _mapping(
        candidate_context.get("workspace", {}), "candidate workspace"
    )
    adapter_workspace = _mapping(
        adapter_context.get("workspace", {}), "environment context workspace"
    )
    environment_workspace = _mapping(
        adapter_config.get("workspace", {}), "environment workspace"
    )
    declarations = _sequence(
        candidate_context.get("trialWorkspace", ()),
        "candidate trialWorkspace",
    )
    mirrors = (
        candidate_workspace.get("copy", ()),
        adapter_context.get("trialWorkspace", ()),
        adapter_workspace.get("copy", ()),
        environment_workspace.get("copy", ()),
    )
    if any(
        canonical_json_bytes(value) != canonical_json_bytes(declarations)
        for value in mirrors
    ):
        _fail(
            "trial_workspace_plan_mismatch",
            "Candidate and evaluator trial workspace declarations differ.",
        )
    if len(declarations) != len(package.trial_workspace_mappings) or len(
        attempt_input_layers
    ) != len(package.trial_workspace_mappings):
        _fail(
            "trial_workspace_plan_mismatch",
            "Retained trial workspace mappings differ from the compiled environment.",
        )
    for index, raw_declaration in enumerate(declarations):
        declaration = _mapping(
            raw_declaration, f"candidate trialWorkspace[{index}]"
        )
        if set(declaration) != {"from", "to"}:
            _fail(
                "trial_workspace_plan_mismatch",
                f"candidate trialWorkspace[{index}] fields changed.",
            )
        if declaration.get("to") != package.trial_workspace_mappings[index][1]:
            _fail(
                "trial_workspace_plan_mismatch",
                f"candidate trialWorkspace[{index}] destination changed.",
            )

    # The runtime layer contract now owns these sources.  Keep the configured
    # evaluator and candidate context path-free instead of persisting a second,
    # legacy copy recipe with provider paths.
    candidate_context["trialWorkspace"] = []
    candidate_workspace["copy"] = []
    adapter_context["trialWorkspace"] = []
    adapter_workspace["copy"] = []
    environment_workspace["copy"] = []
    if attempt_input_layers:
        projection_contract["trial_workspace"] = {
            "collision_policy": "identical-effective-entries-only",
            "destination_scope": TRIAL_INPUT_SCOPE,
            "source_scope": _PACKAGE_SCOPE,
        }
    if projection_contract:
        projection_contract["schema"] = PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA
    return StudySpec(path=study_spec.path, raw=raw), projection_contract


def compile_retained_process_study(
    study_spec: StudySpec,
    *,
    package: RetainedStudyPackage,
    package_manifest: TreeManifest,
    provider: ProcessProviderIdentity,
    target_owner_id: str,
    environment_prepared_runtime: PreparedPythonRuntime | None = None,
    method_prepared_runtime: PreparedPythonRuntime | None = None,
) -> RetainedStudyCompilation:
    """Compile exact retained study semantics and no-copy owner provenance."""

    if not isinstance(package, RetainedStudyPackage):
        raise TypeError("package must be a RetainedStudyPackage.")
    if not isinstance(provider, ProcessProviderIdentity):
        raise TypeError("provider must be a ProcessProviderIdentity.")
    if environment_prepared_runtime is not None and (
        not isinstance(environment_prepared_runtime, PreparedPythonRuntime)
        or environment_prepared_runtime.component_kind != "environment"
    ):
        raise TypeError("environment_prepared_runtime is invalid.")
    if method_prepared_runtime is not None and (
        not isinstance(method_prepared_runtime, PreparedPythonRuntime)
        or method_prepared_runtime.component_kind != "method"
    ):
        raise TypeError("method_prepared_runtime is invalid.")
    required_text(target_owner_id, "study definition target owner_id")
    _preflight_first_slice(
        study_spec,
        environment_setup_prepared=environment_prepared_runtime is not None,
        method_setup_prepared=method_prepared_runtime is not None,
    )
    _validate_retained_package(study_spec, package, package_manifest)
    package_files, _package_directories = _manifest_paths(package_manifest)
    attempt_input_layers = _compile_attempt_input_layers(
        package, package_manifest
    )
    retained_study_spec, projection_contract = _path_free_study_spec(
        study_spec,
        package=package,
        package_files=package_files,
        attempt_input_layers=attempt_input_layers,
        environment_setup_prepared=environment_prepared_runtime is not None,
        method_setup_prepared=method_prepared_runtime is not None,
    )

    environment_layer = ScopeLayer(_PACKAGE_SCOPE, package.snapshot_ref)
    method_layer = ScopeLayer(_PACKAGE_SCOPE, package.snapshot_ref)
    environment_config = ScopePath(
        _PACKAGE_SCOPE, package.environment_config_path
    )
    method_config = ScopePath(_PACKAGE_SCOPE, package.method_config_path)

    adapter_config = _mapping(
        _mapping(study_spec.environment.get("adapter"), "environment.adapter").get("config"),
        "environment.adapter.config",
    )
    interface_profiles_list = []
    for index, item in enumerate(
        _sequence(adapter_config.get("interfaces", []), "environment interfaces")
    ):
        try:
            interface_profiles_list.append(
                InterfaceLaunchProfile.from_dict(
                    _mapping(item, f"environment interface {index}")
                )
            )
        except RealmIntegrityError as error:
            _fail(
                "interface_profile_invalid",
                f"Environment interface {index} is invalid: {error}",
            )
    interface_profiles = tuple(interface_profiles_list)
    environment_revision = EnvironmentRevisionManifest(
        environment_id=retained_study_spec.environment["environmentId"],
        compiler_id=RETAINED_PROCESS_STUDY_COMPILER_ID,
        compiler_version=RETAINED_PROCESS_STUDY_COMPILER_VERSION,
        authored_config=environment_config,
        source_layers=(environment_layer,),
        evaluator_contract=expected_retained_environment_contract(
            retained_study_spec
        ),
        candidate_contract=thaw_json(retained_study_spec.candidate),
        attempt_input_layers=attempt_input_layers,
        projection_contract=projection_contract,
        interface_profiles=interface_profiles,
    )
    environment_prepared_layers = (
        (
            ScopeLayer(
                environment_prepared_runtime.scope,
                environment_prepared_runtime.membership.content_ref,
                source_subpath="site-packages",
            ),
        )
        if environment_prepared_runtime is not None
        else ()
    )
    environment_import_roots = tuple(
        ScopePath(_PACKAGE_SCOPE, path)
        for path in package.environment_python_import_roots
    ) + tuple(
        ScopePath(environment_prepared_runtime.scope, path)
        for path in (
            environment_prepared_runtime.import_roots
            if environment_prepared_runtime is not None
            else ()
        )
    )
    environment_runtime = PreparedEnvironmentRuntimeManifest(
        environment_revision_digest=environment_revision.digest,
        runtime_kind="process",
        runtime_settings=LogicalPythonRuntimeSettings(
            environment_import_roots
        ).to_dict(),
        prepared_layers=environment_prepared_layers,
        workdir=ScopePath(
            _PACKAGE_SCOPE, _config_parent(package.environment_config_path)
        ),
        platform=provider.platform,
        portability="provider-scoped",
        builder_fingerprint=provider.builder_fingerprint,
    )

    implementation = _mapping(
        retained_study_spec.method.get("implementation"), "method.implementation"
    )
    method_revision = MethodRevisionManifest(
        method_id=retained_study_spec.method["id"],
        protocol=implementation["protocol"],
        compiler_id=RETAINED_PROCESS_STUDY_COMPILER_ID,
        compiler_version=RETAINED_PROCESS_STUDY_COMPILER_VERSION,
        authored_config=method_config,
        source_layers=(method_layer,),
        method_contract=expected_retained_method_contract(retained_study_spec),
    )
    method_prepared_layers = (
        (
            ScopeLayer(
                method_prepared_runtime.scope,
                method_prepared_runtime.membership.content_ref,
                source_subpath="site-packages",
            ),
        )
        if method_prepared_runtime is not None
        else ()
    )
    method_import_roots = tuple(
        ScopePath(_PACKAGE_SCOPE, path)
        for path in package.method_python_import_roots
    ) + tuple(
        ScopePath(method_prepared_runtime.scope, path)
        for path in (
            method_prepared_runtime.import_roots
            if method_prepared_runtime is not None
            else ()
        )
    )
    method_runtime = PreparedMethodRuntimeManifest(
        method_revision_digest=method_revision.digest,
        runtime_kind="process",
        runtime_settings=LogicalPythonRuntimeSettings(method_import_roots).to_dict(),
        prepared_layers=method_prepared_layers,
        workdir=ScopePath(_PACKAGE_SCOPE, _config_parent(package.method_config_path)),
        platform=provider.platform,
        portability="provider-scoped",
        builder_fingerprint=provider.builder_fingerprint,
    )

    try:
        run_definition = compile_study_run_definition(
            retained_study_spec,
            environment_revision=environment_revision,
            prepared_environment_runtime=environment_runtime,
            method_revision=method_revision,
            prepared_method_runtime=method_runtime,
        )
    except RetainedStudyCompileError:
        raise
    except StudyRealmCompileError as error:
        raise RetainedStudyCompileError(error.code, str(error)) from error

    expected_refs = {
        (RUN_ENVIRONMENT_SOURCE_ROLE, package.snapshot_ref),
        (RUN_METHOD_SOURCE_ROLE, package.snapshot_ref),
    }
    if attempt_input_layers:
        expected_refs.add((RUN_ATTEMPT_INPUT_ROLE, package.snapshot_ref))
    if environment_prepared_runtime is not None:
        expected_refs.add(
            (
                RUN_PREPARED_RUNTIME_ROLE,
                environment_prepared_runtime.membership.content_ref,
            )
        )
    if method_prepared_runtime is not None:
        expected_refs.add(
            (
                RUN_PREPARED_METHOD_RUNTIME_ROLE,
                method_prepared_runtime.membership.content_ref,
            )
        )
    if set(run_definition.required_content_refs) != expected_refs:
        _fail(
            "unexpected_content_closure",
            "The process-study slice produced content outside its exact retained source and dependency layers.",
        )
    prepared_by_role = {
        RUN_PREPARED_RUNTIME_ROLE: environment_prepared_runtime,
        RUN_PREPARED_METHOD_RUNTIME_ROLE: method_prepared_runtime,
    }
    sources = [package.source_anchor]
    sources.extend(
        prepared.source_anchor
        for prepared in (
            environment_prepared_runtime,
            method_prepared_runtime,
        )
        if prepared is not None
    )
    bindings: list[Binding] = []
    for role, content_ref in run_definition.required_content_refs:
        prepared = prepared_by_role.get(role)
        if prepared is None:
            if content_ref != package.snapshot_ref:
                _fail(
                    "unexpected_content_closure",
                    "A source role points outside the exact retained package.",
                )
            bindings.append(
                Binding(
                    source_owner_id=package.source_anchor.owner_id,
                    source_store_id=package.store_id,
                    content_ref=content_ref,
                    source_role=package.source_role,
                    target_role=role,
                )
            )
        else:
            if content_ref != prepared.membership.content_ref:
                _fail(
                    "unexpected_content_closure",
                    "A dependency role points outside its exact retained layer.",
                )
            bindings.append(
                Binding(
                    source_owner_id=prepared.source_anchor.owner_id,
                    source_store_id=prepared.membership.store_id,
                    content_ref=content_ref,
                    source_role=prepared.membership.role,
                    target_role=role,
                )
            )
    derivation = OwnerDerivationManifest(
        target_owner_id=target_owner_id,
        target_owner_kind=STUDY_DEFINITION_OWNER_KIND,
        sources=tuple(sources),
        bindings=tuple(bindings),
    )
    study_definition = StudyDefinitionManifest(
        owner_id=target_owner_id,
        authored_study_config=ScopePath(
            _PACKAGE_SCOPE, package.study_config_path
        ),
        owner_derivation_manifest_digest=derivation.digest,
        run_definition=run_definition,
    )
    return RetainedStudyCompilation(run_definition, derivation, study_definition)


__all__ = [
    "LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA",
    "METHOD_CONTEXT_SCOPE",
    "PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA",
    "RETAINED_PROCESS_STUDY_COMPILER_ID",
    "RETAINED_PROCESS_STUDY_COMPILER_VERSION",
    "LogicalPythonRuntimeSettings",
    "RetainedStudyCompilation",
    "RetainedStudyCompileError",
    "RetainedStudyPackage",
    "TRIAL_INPUT_SCOPE",
    "compile_retained_process_study",
    "package_projection_contract_features",
]
