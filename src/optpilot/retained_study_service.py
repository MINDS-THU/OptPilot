"""Realm-native preparation for one explicitly bounded local study package.

The live checkout is used only as a capture source.  Public configuration is
compiled from a read-only projection of the sealed snapshot, so the retained
semantics and retained bytes cannot straddle two source generations.  A
successful preparation ends at an immutable reusable study definition; run
creation is exposed separately as a thin ledger-authorized operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .locked_python_runtime import LockedPythonRuntimePreparer
from .locked_python_runtime_contract import (
    LOCKED_PYTHON_RUNTIME_OWNER_KIND,
    LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
    LockedPythonRuntimeError,
)
from .realm._validation import required_text, thaw_json
from .realm.content import AllowedTreeSource, TreeSealReceipt
from .realm.errors import RealmConflict, RealmNotFound
from .realm.ledger import RealmLedger
from .realm.local_study_package import (
    LocalStudyPackagePlan,
    plan_local_study_package,
)
from .realm.manifests import TreeManifest, validate_portable_path
from .realm.owner_derivation import SourceAnchor
from .realm.owners import OwnerMembership, OwnerPermission, OwnerState
from .realm.process_provider import ProcessProviderIdentity
from .realm.projection import ProjectionSpec, TreeMapping
from .realm.projection_service import RealmProjectionService
from .realm.refs import SnapshotRef, canonical_json_bytes, request_digest
from .realm.run_records import RunCreateReceipt
from .realm.run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    ScopePath,
)
from .realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
)
from .realm.service import RealmContentService
from .realm.selections import SelectionRef
from .realm.study_definition import StudyDefinitionReceipt
from .retained_study_compiler import (
    LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA,
    METHOD_CONTEXT_SCOPE,
    PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA,
    RETAINED_PROCESS_STUDY_COMPILER_ID,
    RETAINED_PROCESS_STUDY_COMPILER_VERSION,
    TRIAL_INPUT_SCOPE,
    RetainedStudyCompileError,
    RetainedStudyPackage,
    compile_retained_process_study,
    package_projection_contract_features,
)


RETAINED_STUDY_SOURCE_OWNER_KIND = "retained-study-source"
RETAINED_STUDY_SOURCE_ROLE = "study-package-source"
RETAINED_STUDY_PREPARATION_RECEIPT_FORMAT = (
    "optpilot.retained-study-preparation-receipt.v1"
)
# Keep this exact tuple aligned with Studio's
# ``CONFIGURED_PACKAGE_CAPTURE_EXCLUDED_DIRS``.  Whole-package Studio capture
# already omits these machine-local generated directories.  The retained CLI
# path must select the same authored bytes so a cache created by importing or
# testing a package cannot change its source identity.  This is intentionally a
# basename-only directory policy: ordinary files (including ``*.pyc``) and
# every other directory remain part of the captured package.
RETAINED_STUDY_SOURCE_EXCLUDED_DIRECTORY_NAMES = (
    ".git",
    ".mypy_cache",
    ".optpilot",
    ".optpilot-ui",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "runs",
)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _phase_operation_id(operation_id: str, phase: str) -> str:
    required_text(operation_id, "retained study operation_id", max_bytes=512)
    required_text(phase, "retained study operation phase", max_bytes=128)
    digest = request_digest(
        {
            "format": "optpilot.retained-study-operation-phase.v1",
            "operation_id": operation_id,
            "phase": phase,
        }
    )
    return f"retained-study/{phase}/{digest}"


def _runtime_mapping(component: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(component, Mapping):
        raise RetainedStudyCompileError(
            "study_shape_invalid", f"{label} must be an object."
        )
    runtime = component.get("runtime", {})
    if runtime in (None, {}):
        return {}
    if not isinstance(runtime, Mapping):
        raise RetainedStudyCompileError(
            "study_shape_invalid", f"{label}.runtime must be an object."
        )
    return runtime


def _relative_study_path(study_config_path: Path, package_root: Path) -> str:
    if not isinstance(study_config_path, Path):
        raise TypeError("study_config_path must be a Path.")
    if not isinstance(package_root, Path):
        raise TypeError("package_root must be a Path.")
    if ".." in package_root.parts or ".." in study_config_path.parts:
        raise ValueError("package_root and study_config_path must not contain traversal.")
    try:
        canonical_root = package_root.resolve(strict=True)
        canonical_study = study_config_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            "package_root and study_config_path must name existing paths."
        ) from error
    try:
        relative = canonical_study.relative_to(canonical_root).as_posix()
    except ValueError as error:
        raise ValueError(
            "study_config_path must be inside the explicit package_root."
        ) from error
    try:
        normalized = validate_portable_path(relative)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(
            "study_config_path must have a canonical portable package-relative path."
        ) from error
    if normalized != relative:
        raise ValueError(
            "study_config_path must have a canonical portable package-relative path."
        )
    return normalized


def _portable_study_relative_path(value: str) -> str:
    """Validate one path supplied without access to a live package checkout."""

    required_text(value, "study_config_relative_path", max_bytes=4096)
    try:
        normalized = validate_portable_path(value)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError(
            "study_config_relative_path must be a canonical portable package-relative path."
        ) from error
    if normalized != value:
        raise ValueError(
            "study_config_relative_path must be a canonical portable package-relative path."
        )
    return normalized


def _validate_manifest_study_entry(manifest: TreeManifest, relative_path: str) -> None:
    if not isinstance(manifest, TreeManifest):
        raise TypeError("sealed package manifest must be a TreeManifest.")
    if not any(
        entry.kind == "file" and entry.path == relative_path
        for entry in manifest.entries
    ):
        raise RealmConflict(
            "Study configuration path is absent from the retained package."
        )


def _validate_manifest_config_entries(
    manifest: TreeManifest, plan: LocalStudyPackagePlan
) -> None:
    if not isinstance(manifest, TreeManifest):
        raise TypeError("sealed package manifest must be a TreeManifest.")
    files = {entry.path for entry in manifest.entries if entry.kind == "file"}
    required = {
        plan.study_config_path,
        plan.environment_config_path,
        plan.method_config_path,
    }
    if not required.issubset(files):
        raise RealmConflict(
            "Compiled study configuration paths are absent from the sealed package."
        )


def _validate_projected_authoring_paths(plan: LocalStudyPackagePlan) -> None:
    """Cross-check compiler path anchors without retaining their host values."""

    extensions = plan.study_spec.raw.get("extensions")
    authoring = (
        extensions.get("authoringConfig")
        if isinstance(extensions, Mapping)
        else None
    )
    if not isinstance(authoring, Mapping):
        raise RealmConflict(
            "Compiled study omitted its exact authored configuration paths."
        )
    expected = {
        "studyConfigPath": plan.study_config_path,
        "environmentConfigPath": plan.environment_config_path,
        "methodConfigPath": plan.method_config_path,
    }
    for field, relative in expected.items():
        value = authoring.get(field)
        if not isinstance(value, str):
            raise RealmConflict(
                "Compiled study authored configuration paths are incomplete."
            )
        try:
            actual = Path(value).relative_to(plan.package_root).as_posix()
        except ValueError as error:
            raise RealmConflict(
                "Compiled study authored configuration path escaped its projection."
            ) from error
        if actual != relative:
            raise RealmConflict(
                "Compiled study authored configuration paths differ from its package plan."
            )


def _runtime_import_roots(
    runtime_settings: Mapping[str, Any],
    *,
    expected_scope: str,
    allowed_extra_scopes: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    if not isinstance(runtime_settings, Mapping):
        raise RealmConflict("Retained study runtime settings are invalid.")
    if runtime_settings.get("schema") != LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA:
        raise RealmConflict("Retained study runtime settings schema changed.")
    raw = runtime_settings.get("import_roots")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (tuple, list)):
        raise RealmConflict("Retained study runtime import roots are invalid.")
    try:
        roots = tuple(ScopePath.from_dict(item) for item in raw)
    except (TypeError, ValueError) as error:
        raise RealmConflict("Retained study runtime import roots are invalid.") from error
    scopes = {item.scope for item in roots}
    if (
        not roots
        or expected_scope not in scopes
        or not scopes <= ({expected_scope} | set(allowed_extra_scopes))
    ):
        raise RealmConflict("Retained study runtime import roots are invalid.")
    return tuple(
        item.relative_path for item in roots if item.scope == expected_scope
    )


def _retained_method_context_paths(
    environment,
    *,
    package_manifest: TreeManifest,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(package_manifest, TreeManifest):
        raise TypeError("package_manifest must be a TreeManifest.")
    manifest_entries = {item.path: item for item in package_manifest.entries}
    contract = environment.projection_contract
    try:
        features = package_projection_contract_features(contract)
    except (TypeError, ValueError) as error:
        raise RealmConflict(
            "Retained method-context projection contract changed."
        ) from error
    candidate_context = environment.candidate_contract.get("context")
    candidate_method_context = (
        candidate_context.get("methodContext")
        if isinstance(candidate_context, Mapping)
        else None
    )
    evaluator_adapter = environment.evaluator_contract.get("adapter")
    evaluator_config = (
        evaluator_adapter.get("config")
        if isinstance(evaluator_adapter, Mapping)
        else None
    )
    evaluator_context = (
        evaluator_config.get("context")
        if isinstance(evaluator_config, Mapping)
        else None
    )
    evaluator_method_context = (
        evaluator_context.get("methodContext")
        if isinstance(evaluator_context, Mapping)
        else None
    )
    if (
        not isinstance(candidate_method_context, Mapping)
        or not isinstance(evaluator_method_context, Mapping)
        or canonical_json_bytes(thaw_json(candidate_method_context))
        != canonical_json_bytes(thaw_json(evaluator_method_context))
    ):
        raise RealmConflict("Retained method-context declarations changed.")
    if "method_context" not in features:
        if any(
            candidate_method_context.get(name) not in (None, (), [])
            for name in ("instructions", "references")
        ):
            raise RealmConflict(
                "Retained method-context paths have no projection contract."
            )
        return (), ()
    if contract == {}:  # pragma: no cover - excluded by feature check
        raise RealmConflict("Retained method-context projection contract changed.")
    binding = contract.get("method_context")
    if not isinstance(binding, Mapping) or set(binding) != {
        "logical_scope",
        "source",
    }:
        raise RealmConflict("Retained method-context projection contract changed.")
    source = binding.get("source")
    if (
        binding.get("logical_scope") != METHOD_CONTEXT_SCOPE
        or not isinstance(source, Mapping)
        or set(source) != {"path", "scope"}
        or source.get("scope") != environment.authored_config.scope
    ):
        raise RealmConflict("Retained method-context projection contract changed.")
    source_path = source.get("path")
    if not isinstance(source_path, str):
        raise RealmConflict("Retained method-context projection contract changed.")
    if source_path != ".":
        try:
            if validate_portable_path(source_path) != source_path:
                raise ValueError("noncanonical")
        except (RuntimeError, TypeError, ValueError) as error:
            raise RealmConflict(
                "Retained method-context projection source changed."
            ) from error
        source_entry = manifest_entries.get(source_path)
        if source_entry is None or source_entry.kind != "directory":
            raise RealmConflict(
                "Retained method-context projection source is absent from the package tree."
            )

    method_context = candidate_method_context

    def package_path(value: Any) -> str:
        if not isinstance(value, str):
            raise RealmConflict("Retained method-context path changed.")
        try:
            if validate_portable_path(value) != value:
                raise ValueError("noncanonical")
        except (RuntimeError, TypeError, ValueError) as error:
            raise RealmConflict("Retained method-context path changed.") from error
        path = PurePosixPath(value) if source_path == "." else PurePosixPath(source_path) / value
        try:
            result = validate_portable_path(path.as_posix())
        except (RuntimeError, TypeError, ValueError) as error:
            raise RealmConflict("Retained method-context path changed.") from error
        entry = manifest_entries.get(result)
        if entry is None or entry.kind != "file":
            raise RealmConflict(
                "Retained method-context path is not a regular package-tree entry."
            )
        return result

    instructions = method_context.get("instructions", ())
    references = method_context.get("references", ())
    if (
        isinstance(instructions, (str, bytes))
        or not isinstance(instructions, (list, tuple))
        or isinstance(references, (str, bytes))
        or not isinstance(references, (list, tuple))
    ):
        raise RealmConflict("Retained method-context declaration changed.")
    if not instructions and not references:
        raise RealmConflict(
            "Retained method-context projection has no declared paths."
        )
    instruction_paths = tuple(package_path(value) for value in instructions)
    reference_paths = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise RealmConflict("Retained method-context declaration changed.")
        reference_paths.append(package_path(reference.get("path")))
    return instruction_paths, tuple(reference_paths)


def _retained_trial_workspace_mappings(
    environment,
    *,
    package_manifest: TreeManifest,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(package_manifest, TreeManifest):
        raise TypeError("package_manifest must be a TreeManifest.")
    manifest_entries = {item.path: item for item in package_manifest.entries}
    contract = environment.projection_contract
    layers = tuple(
        sorted(environment.attempt_input_layers, key=lambda item: item.precedence)
    )
    try:
        features = package_projection_contract_features(contract)
    except (TypeError, ValueError) as error:
        raise RealmConflict(
            "Retained trial workspace projection contract changed."
        ) from error
    candidate_context = environment.candidate_contract.get("context")
    evaluator_adapter = environment.evaluator_contract.get("adapter")
    evaluator_config = (
        evaluator_adapter.get("config")
        if isinstance(evaluator_adapter, Mapping)
        else None
    )
    evaluator_context = (
        evaluator_config.get("context")
        if isinstance(evaluator_config, Mapping)
        else None
    )
    declarations = (
        candidate_context.get("trialWorkspace")
        if isinstance(candidate_context, Mapping)
        else None,
        candidate_context.get("workspace", {}).get("copy")
        if isinstance(candidate_context, Mapping)
        and isinstance(candidate_context.get("workspace"), Mapping)
        else None,
        evaluator_context.get("trialWorkspace")
        if isinstance(evaluator_context, Mapping)
        else None,
        evaluator_context.get("workspace", {}).get("copy")
        if isinstance(evaluator_context, Mapping)
        and isinstance(evaluator_context.get("workspace"), Mapping)
        else None,
        evaluator_config.get("workspace", {}).get("copy")
        if isinstance(evaluator_config, Mapping)
        and isinstance(evaluator_config.get("workspace"), Mapping)
        else None,
    )
    if any(value not in ((), []) for value in declarations):
        raise RealmConflict(
            "Retained trial workspace legacy copy declarations changed."
        )
    if contract == {}:
        if layers:
            raise RealmConflict("Retained trial workspace layers lost their contract.")
        return ()
    binding = contract.get("trial_workspace")
    if "trial_workspace" not in features:
        if layers:
            raise RealmConflict("Retained trial workspace layers lost their contract.")
        return ()
    if not layers:
        raise RealmConflict(
            "Retained trial workspace projection has no input layers."
        )
    if not isinstance(binding, Mapping) or dict(binding) != {
        "collision_policy": "identical-effective-entries-only",
        "destination_scope": TRIAL_INPUT_SCOPE,
        "source_scope": environment.authored_config.scope,
    }:
        raise RealmConflict("Retained trial workspace projection contract changed.")
    if any(
        layer.scope != TRIAL_INPUT_SCOPE
        or layer.snapshot_ref
        not in {source.snapshot_ref for source in environment.source_layers}
        for layer in layers
    ) or tuple(layer.precedence for layer in layers) != tuple(range(len(layers))):
        raise RealmConflict("Retained trial workspace layers changed.")
    for layer in layers:
        source_entry = (
            None
            if layer.source_subpath == "."
            else manifest_entries.get(layer.source_subpath)
        )
        if layer.source_subpath != "." and source_entry is None:
            raise RealmConflict(
                "Retained trial workspace source is absent from the package tree."
            )
        if (
            source_entry is not None
            and source_entry.kind == "file"
            and layer.destination_subpath == "."
        ):
            raise RealmConflict(
                "Retained trial workspace file source has no destination file path."
            )
    return tuple(
        (layer.source_subpath, layer.destination_subpath) for layer in layers
    )


@dataclass(frozen=True)
class RetainedStudyPreparationReceipt:
    """Portable result of source retention and study-definition creation."""

    package: RetainedStudyPackage
    source_membership: OwnerMembership
    study_definition: StudyDefinitionReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.package, RetainedStudyPackage):
            raise TypeError("package must be a RetainedStudyPackage.")
        if not isinstance(self.source_membership, OwnerMembership):
            raise TypeError("source_membership must be an OwnerMembership.")
        if not isinstance(self.study_definition, StudyDefinitionReceipt):
            raise TypeError("study_definition must be a StudyDefinitionReceipt.")
        if self.source_membership != OwnerMembership(
            store_id=self.package.store_id,
            content_ref=self.package.snapshot_ref,
            role=self.package.source_role,
        ):
            raise ValueError("retained package and source membership differ.")
        if self.study_definition.manifest.required_content_refs != tuple(
            sorted(
                self.study_definition.manifest.required_content_refs,
                key=lambda item: (item[0], str(item[1])),
            )
        ):
            raise ValueError("study definition content refs are not canonical.")
        refs = set(self.study_definition.manifest.required_content_refs)
        package_roles = {
            RUN_ENVIRONMENT_SOURCE_ROLE,
            RUN_METHOD_SOURCE_ROLE,
            RUN_ATTEMPT_INPUT_ROLE,
        }
        allowed_roles = package_roles | {
            RUN_PREPARED_RUNTIME_ROLE,
            RUN_PREPARED_METHOD_RUNTIME_ROLE,
        }
        if (
            (RUN_ENVIRONMENT_SOURCE_ROLE, self.package.snapshot_ref) not in refs
            or (RUN_METHOD_SOURCE_ROLE, self.package.snapshot_ref) not in refs
            or any(role not in allowed_roles for role, _ in refs)
            or any(
                content_ref != self.package.snapshot_ref
                for role, content_ref in refs
                if role in package_roles
            )
        ):
            raise ValueError(
                "study definition does not use the exact retained source and dependency closure."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": RETAINED_STUDY_PREPARATION_RECEIPT_FORMAT,
            "package": self.package.to_dict(),
            "source_membership": self.source_membership.to_dict(),
            "study_definition": self.study_definition.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "RetainedStudyPreparationReceipt":
        _exact_keys(
            payload,
            {"format", "package", "source_membership", "study_definition"},
            "retained study preparation receipt",
        )
        if payload["format"] != RETAINED_STUDY_PREPARATION_RECEIPT_FORMAT:
            raise ValueError("retained study preparation receipt format is unsupported.")
        result = cls(
            package=RetainedStudyPackage.from_dict(payload["package"]),
            source_membership=OwnerMembership.from_dict(
                payload["source_membership"]
            ),
            study_definition=StudyDefinitionReceipt.from_dict(
                payload["study_definition"]
            ),
        )
        if result.to_dict() != dict(payload):
            raise ValueError("retained study preparation receipt is not canonical.")
        return result


class RetainedStudyService:
    """Compose capture, projection, compilation, retention, and guarded launch."""

    def __init__(
        self,
        ledger: RealmLedger,
        content_service: RealmContentService,
        projection_service: RealmProjectionService,
        provider: ProcessProviderIdentity,
        *,
        dependency_cache_root: Path | None = None,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(provider, ProcessProviderIdentity):
            raise TypeError("provider must be a ProcessProviderIdentity.")
        self._ledger = ledger
        self._content_service = content_service
        self._projection_service = projection_service
        self._provider = provider
        if dependency_cache_root is not None and not isinstance(
            dependency_cache_root, Path
        ):
            raise TypeError("dependency_cache_root must be a Path or None.")
        self._dependency_cache_root = dependency_cache_root

    def prepare_local_package(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store_id: str,
        package_root: Path,
        study_config_path: Path,
        source_owner_id: str,
        study_definition_owner_id: str,
        capture_ttl_seconds: float = 300,
        projection_ttl_seconds: float = 300,
        launch_inputs: Mapping[str, Any] | None = None,
    ) -> RetainedStudyPreparationReceipt:
        """Seal first, compile from those bytes, then retain one definition."""

        required_text(operation_id, "retained study operation_id", max_bytes=512)
        required_text(actor_principal_id, "actor_principal_id")
        required_text(store_id, "store_id", max_bytes=128)
        required_text(source_owner_id, "source_owner_id")
        required_text(study_definition_owner_id, "study_definition_owner_id")
        if source_owner_id == study_definition_owner_id:
            raise ValueError("source and study-definition owners must be independent.")
        if not isinstance(package_root, Path):
            raise TypeError("package_root must be a Path.")
        study_relative = _relative_study_path(study_config_path, package_root)
        replay = self._reuse_committed_definition(
            actor_principal_id=actor_principal_id,
            study_definition_owner_id=study_definition_owner_id,
            study_relative=study_relative,
            source_owner_id=source_owner_id,
            source_store_id=store_id,
        )
        if replay is not None:
            return replay

        membership, source_anchor, seal = self._capture_or_reuse_source(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            store_id=store_id,
            package_root=package_root,
            source_owner_id=source_owner_id,
            capture_ttl_seconds=capture_ttl_seconds,
        )
        return self._prepare_retained_source(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            study_relative=study_relative,
            source_owner_id=source_owner_id,
            study_definition_owner_id=study_definition_owner_id,
            membership=membership,
            source_anchor=source_anchor,
            seal=seal,
            capture_ttl_seconds=capture_ttl_seconds,
            projection_ttl_seconds=projection_ttl_seconds,
            launch_inputs=launch_inputs,
        )

    def prepare_selected_package(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        package_selection: SelectionRef,
        study_config_relative_path: str,
        source_owner_id: str,
        study_definition_owner_id: str,
        capture_ttl_seconds: float = 300,
        projection_ttl_seconds: float = 300,
    ) -> RetainedStudyPreparationReceipt:
        """Compile one exact retained package selection without re-capturing it.

        The selection is first adopted as an independent retained-study source
        owner by the ledger's atomic no-copy operation.  Compilation projects
        only that new owner, so later source advancement or retirement cannot
        change the package used by this preparation.
        """

        required_text(operation_id, "retained study operation_id", max_bytes=512)
        required_text(actor_principal_id, "actor_principal_id")
        if not isinstance(package_selection, SelectionRef):
            raise TypeError("package_selection must be a SelectionRef.")
        study_relative = _portable_study_relative_path(
            study_config_relative_path
        )
        required_text(source_owner_id, "source_owner_id")
        required_text(study_definition_owner_id, "study_definition_owner_id")
        if source_owner_id == study_definition_owner_id:
            raise ValueError("source and study-definition owners must be independent.")

        # A fresh local preparation must not create an owner for content whose
        # store is unavailable to this process.  Exact adoption replay skips
        # this mutable-source preflight so a completed adoption remains usable
        # after its original selection source retires.
        try:
            existing_selection = self._ledger.read_owner_selection_provenance(
                actor_principal_id=actor_principal_id,
                owner_id=source_owner_id,
            )
        except RealmNotFound:
            preflight = self._ledger.resolve_selection(
                actor_principal_id=actor_principal_id,
                selection=package_selection,
                permission=OwnerPermission.DERIVE,
            )
            if (
                not preflight.eligibility.eligible
                or preflight.root is None
                or preflight.root.store_id
                not in self._projection_service.available_store_ids
            ):
                raise RealmNotFound("Entity not found.")
        else:
            if existing_selection != package_selection:
                raise RealmConflict(
                    "Retained study source selection provenance changed."
                )

        adoption = self._ledger.adopt_selection_as_owner(
            operation_id=_phase_operation_id(
                operation_id, "adopt-source-selection"
            ),
            actor_principal_id=actor_principal_id,
            selection=package_selection,
            target_owner_id=source_owner_id,
            target_owner_kind=RETAINED_STUDY_SOURCE_OWNER_KIND,
            target_role=RETAINED_STUDY_SOURCE_ROLE,
        )
        if not adoption.eligibility.eligible or adoption.derivation is None:
            raise RealmNotFound("Entity not found.")
        derivation = adoption.derivation
        memberships = derivation.manifest.target_memberships
        if (
            derivation.owner.owner_id != source_owner_id
            or derivation.owner.owner_kind != RETAINED_STUDY_SOURCE_OWNER_KIND
            or derivation.owner.principal_id != actor_principal_id
            or derivation.owner.revision != 0
            or len(memberships) != 1
        ):
            raise RealmConflict("Retained study source adoption facts changed.")
        membership = memberships[0]
        if (
            membership.role != RETAINED_STUDY_SOURCE_ROLE
            or not isinstance(membership.content_ref, SnapshotRef)
            or membership.store_id
            not in self._projection_service.available_store_ids
        ):
            raise RealmConflict("Retained study source adoption facts changed.")
        source_anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
            revision=derivation.owner.revision,
        )

        replay = self._reuse_committed_definition(
            actor_principal_id=actor_principal_id,
            study_definition_owner_id=study_definition_owner_id,
            study_relative=study_relative,
            source_owner_id=source_owner_id,
            source_store_id=membership.store_id,
        )
        if replay is not None:
            return replay
        return self._prepare_retained_source(
            operation_id=operation_id,
            actor_principal_id=actor_principal_id,
            study_relative=study_relative,
            source_owner_id=source_owner_id,
            study_definition_owner_id=study_definition_owner_id,
            membership=membership,
            source_anchor=source_anchor,
            seal=None,
            capture_ttl_seconds=capture_ttl_seconds,
            projection_ttl_seconds=projection_ttl_seconds,
        )

    def _prepare_retained_source(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        study_relative: str,
        source_owner_id: str,
        study_definition_owner_id: str,
        membership: OwnerMembership,
        source_anchor: SourceAnchor,
        seal: TreeSealReceipt | None,
        capture_ttl_seconds: float,
        projection_ttl_seconds: float,
        launch_inputs: Mapping[str, Any] | None = None,
    ) -> RetainedStudyPreparationReceipt:
        """Verify, project once, compile, and retain one already-owned tree."""

        package_manifest = self._content_service.verify_owner_tree_manifest(
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
            expected_owner_revision=source_anchor.owner_revision,
            membership=membership,
        )
        if seal is not None and seal.manifest != package_manifest:
            raise RealmConflict("Captured and retained source manifests differ.")
        _validate_manifest_study_entry(package_manifest, study_relative)

        projection_operation = _phase_operation_id(operation_id, "project-source")
        projection = self._projection_service.project_read_only(
            operation_id=projection_operation,
            actor_principal_id=actor_principal_id,
            store_id=membership.store_id,
            spec=ProjectionSpec(
                owner_id=source_owner_id,
                mappings=(TreeMapping(membership.content_ref),),
            ),
            holder_id=_phase_operation_id(operation_id, "projection-holder"),
            ttl_seconds=projection_ttl_seconds,
            consumer_kind="retained-study-compilation",
            consumer_metadata={
                "source_owner_id": source_owner_id,
                "source_owner_revision": source_anchor.owner_revision,
            },
        )
        try:
            projected_root = projection.root_path
            plan = plan_local_study_package(
                projected_root / study_relative,
                projected_root,
                launch_inputs=launch_inputs,
            )
            _validate_projected_authoring_paths(plan)
            _validate_manifest_config_entries(package_manifest, plan)
            package = RetainedStudyPackage(
                source_anchor=source_anchor,
                store_id=membership.store_id,
                snapshot_ref=membership.content_ref,
                source_role=RETAINED_STUDY_SOURCE_ROLE,
                study_config_path=plan.study_config_path,
                environment_config_path=plan.environment_config_path,
                method_config_path=plan.method_config_path,
                environment_python_import_roots=(
                    plan.environment_python_import_roots
                ),
                method_python_import_roots=plan.method_python_import_roots,
                method_context_instruction_paths=(
                    plan.method_context_instruction_paths
                ),
                method_context_reference_paths=plan.method_context_reference_paths,
                trial_workspace_mappings=plan.trial_workspace_mappings,
            )
            environment_runtime = _runtime_mapping(
                plan.study_spec.environment, "environment"
            )
            method_runtime = _runtime_mapping(plan.study_spec.method, "method")
            needs_preparation = any(
                runtime.get("setup") not in (None, {})
                for runtime in (environment_runtime, method_runtime)
            )
            if needs_preparation and self._dependency_cache_root is None:
                raise RetainedStudyCompileError(
                    "runtime_preparer_unavailable",
                    "This Realm has no exact dependency preparer configured.",
                )
            preparer = (
                LockedPythonRuntimePreparer(
                    self._ledger,
                    self._content_service,
                    actor_principal_id=actor_principal_id,
                    store_id=membership.store_id,
                    provider=self._provider,
                    cache_root=self._dependency_cache_root,
                )
                if needs_preparation and self._dependency_cache_root is not None
                else None
            )
            try:
                environment_prepared_runtime = (
                    preparer.prepare(
                        operation_id=_phase_operation_id(
                            operation_id, "prepare-environment-runtime"
                        ),
                        package_root=projected_root,
                        package_snapshot=membership.content_ref,
                        component_kind="environment",
                        component_id=plan.study_spec.environment["environmentId"],
                        config_relative_path=plan.environment_config_path,
                        runtime=environment_runtime,
                        capture_ttl_seconds=capture_ttl_seconds,
                    )
                    if preparer is not None
                    else None
                )
                method_prepared_runtime = (
                    preparer.prepare(
                        operation_id=_phase_operation_id(
                            operation_id, "prepare-method-runtime"
                        ),
                        package_root=projected_root,
                        package_snapshot=membership.content_ref,
                        component_kind="method",
                        component_id=plan.study_spec.method["id"],
                        config_relative_path=plan.method_config_path,
                        runtime=method_runtime,
                        capture_ttl_seconds=capture_ttl_seconds,
                    )
                    if preparer is not None
                    else None
                )
            except LockedPythonRuntimeError as error:
                raise RetainedStudyCompileError(error.code, str(error)) from error
            compilation = compile_retained_process_study(
                plan.study_spec,
                package=package,
                package_manifest=package_manifest,
                provider=self._provider,
                target_owner_id=study_definition_owner_id,
                environment_prepared_runtime=environment_prepared_runtime,
                method_prepared_runtime=method_prepared_runtime,
            )
        finally:
            projection.close()

        definition = self._ledger.create_study_definition(
            operation_id=_phase_operation_id(operation_id, "create-definition"),
            actor_principal_id=actor_principal_id,
            derivation=compilation.owner_derivation,
            manifest=compilation.study_definition,
        )
        return RetainedStudyPreparationReceipt(
            package=package,
            source_membership=membership,
            study_definition=definition,
        )

    def _reuse_committed_definition(
        self,
        *,
        actor_principal_id: str,
        study_definition_owner_id: str,
        study_relative: str,
        source_owner_id: str,
        source_store_id: str,
    ) -> RetainedStudyPreparationReceipt | None:
        """Replay from independent immutable definition records only."""

        try:
            owner = self._ledger.read_owner(
                actor_principal_id=actor_principal_id,
                owner_id=study_definition_owner_id,
                permission=OwnerPermission.DERIVE,
            )
        except RealmNotFound:
            return None
        if owner.revision != 0 or owner.state is not OwnerState.ACTIVE:
            raise RealmConflict("Retained study definition owner facts changed.")
        manifest = self._ledger.read_study_definition(
            actor_principal_id=actor_principal_id,
            owner_id=study_definition_owner_id,
        )
        derivation = self._ledger.read_owner_derivation(
            actor_principal_id=actor_principal_id,
            owner_id=study_definition_owner_id,
        )
        source_anchors = {
            item.owner_id: item for item in derivation.sources
        }
        source_anchor = source_anchors.get(source_owner_id)
        if source_anchor is None:
            raise RealmConflict("Retained study definition source anchor changed.")
        if derivation.target_owner_id != study_definition_owner_id:
            raise RealmConflict("Retained study definition derivation target changed.")
        bindings_by_target = {
            (item.target_role, item.content_ref): item
            for item in derivation.bindings
        }
        if set(bindings_by_target) != set(manifest.required_content_refs):
            raise RealmConflict("Retained study definition source bindings changed.")
        expected_definition_memberships = tuple(
            sorted(
                derivation.target_memberships,
                key=lambda item: (
                    item.store_id,
                    str(item.content_ref),
                    item.role,
                ),
            )
        )
        actual_definition_memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=study_definition_owner_id,
            permission=OwnerPermission.DERIVE,
        )
        if actual_definition_memberships != expected_definition_memberships:
            raise RealmConflict(
                "Retained study definition content memberships changed."
            )

        package_roles = {
            RUN_ENVIRONMENT_SOURCE_ROLE,
            RUN_METHOD_SOURCE_ROLE,
            RUN_ATTEMPT_INPUT_ROLE,
        }
        prepared_roles = {
            RUN_PREPARED_RUNTIME_ROLE,
            RUN_PREPARED_METHOD_RUNTIME_ROLE,
        }
        package_bindings = [
            item for item in derivation.bindings if item.target_role in package_roles
        ]
        if (
            not package_bindings
            or any(
                item.source_owner_id != source_owner_id
                or item.source_store_id != source_store_id
                or item.source_role != RETAINED_STUDY_SOURCE_ROLE
                or not isinstance(item.content_ref, SnapshotRef)
                for item in package_bindings
            )
        ):
            raise RealmConflict("Retained study definition source bindings changed.")
        package_placements = {
            (item.source_store_id, item.content_ref, item.source_role)
            for item in package_bindings
        }
        if len(package_placements) != 1:
            raise RealmConflict("Retained study definition source bindings changed.")
        binding_store_id, snapshot_ref, source_role = next(iter(package_placements))
        membership = OwnerMembership(binding_store_id, snapshot_ref, source_role)

        # Re-authorize every immutable source anchor, including dependency
        # owners, rather than trusting only the derived target memberships.
        for source in derivation.sources:
            source_owner = self._ledger.read_owner(
                actor_principal_id=actor_principal_id,
                owner_id=source.owner_id,
                permission=OwnerPermission.DERIVE,
            )
            current_anchor = self._ledger.read_owner_source_anchor(
                actor_principal_id=actor_principal_id,
                owner_id=source.owner_id,
                revision=source.owner_revision,
            )
            if current_anchor != source:
                raise RealmConflict("Retained study definition source anchor changed.")
            source_bindings = [
                item
                for item in derivation.bindings
                if item.source_owner_id == source.owner_id
            ]
            # The derived Study owner now retains these exact bytes.  Source
            # owners may legitimately advance or retire later, so replay is
            # checked against the historical source anchor and immutable
            # derivation record rather than their current membership set.
            if not source_bindings:
                raise RealmConflict("Retained study definition source bindings changed.")
            if source.owner_id == source_owner_id:
                if source_owner.owner_kind != RETAINED_STUDY_SOURCE_OWNER_KIND:
                    raise RealmConflict("Retained study definition source owner changed.")
                continue
            if (
                source_owner.owner_kind != LOCKED_PYTHON_RUNTIME_OWNER_KIND
                or any(
                    item.source_role != LOCKED_PYTHON_RUNTIME_SOURCE_ROLE
                    or item.target_role not in prepared_roles
                    for item in source_bindings
                )
            ):
                raise RealmConflict("Retained study dependency source changed.")

        if any(
            item.target_role not in package_roles | prepared_roles
            for item in derivation.bindings
        ):
            raise RealmConflict("Retained study definition source bindings changed.")
        try:
            environment_membership = next(
                item
                for item in expected_definition_memberships
                if item.role == RUN_ENVIRONMENT_SOURCE_ROLE
            )
        except StopIteration as error:  # pragma: no cover - run manifest validates it
            raise RealmConflict(
                "Retained study definition environment membership changed."
            ) from error
        package_manifest = self._content_service.verify_owner_tree_manifest(
            actor_principal_id=actor_principal_id,
            owner_id=study_definition_owner_id,
            expected_owner_revision=manifest.owner_revision,
            membership=environment_membership,
        )
        if package_manifest.snapshot_ref != snapshot_ref:
            raise RealmConflict(
                "Retained study definition package snapshot changed."
            )

        run_definition = manifest.run_definition
        environment = run_definition.evaluation_closure.environment_revision
        environment_runtime = run_definition.evaluation_closure.prepared_runtime
        method = run_definition.method_revision
        method_runtime = run_definition.prepared_method_runtime
        (
            method_context_instruction_paths,
            method_context_reference_paths,
        ) = _retained_method_context_paths(
            environment,
            package_manifest=package_manifest,
        )
        trial_workspace_mappings = _retained_trial_workspace_mappings(
            environment,
            package_manifest=package_manifest,
        )
        if (
            manifest.authored_study_config.relative_path != study_relative
            or manifest.authored_study_config.scope
            != RETAINED_STUDY_SOURCE_ROLE
            or environment.authored_config.scope
            != manifest.authored_study_config.scope
            or method.authored_config.scope
            != manifest.authored_study_config.scope
            or environment.compiler_id != RETAINED_PROCESS_STUDY_COMPILER_ID
            or environment.compiler_version
            != RETAINED_PROCESS_STUDY_COMPILER_VERSION
            or method.compiler_id != RETAINED_PROCESS_STUDY_COMPILER_ID
            or method.compiler_version != RETAINED_PROCESS_STUDY_COMPILER_VERSION
            or environment_runtime.builder_fingerprint
            != self._provider.builder_fingerprint
            or environment_runtime.platform != self._provider.platform
            or method_runtime.builder_fingerprint != self._provider.builder_fingerprint
            or method_runtime.platform != self._provider.platform
        ):
            raise RealmConflict("Retained study definition compilation facts changed.")
        package = RetainedStudyPackage(
            source_anchor=source_anchor,
            store_id=binding_store_id,
            snapshot_ref=snapshot_ref,
            source_role=source_role,
            study_config_path=manifest.authored_study_config.relative_path,
            environment_config_path=environment.authored_config.relative_path,
            method_config_path=method.authored_config.relative_path,
            environment_python_import_roots=_runtime_import_roots(
                environment_runtime.runtime_settings,
                expected_scope=environment.authored_config.scope,
                allowed_extra_scopes=frozenset(
                    layer.scope for layer in environment_runtime.prepared_layers
                ),
            ),
            method_python_import_roots=_runtime_import_roots(
                method_runtime.runtime_settings,
                expected_scope=method.authored_config.scope,
                allowed_extra_scopes=frozenset(
                    layer.scope for layer in method_runtime.prepared_layers
                ),
            ),
            method_context_instruction_paths=method_context_instruction_paths,
            method_context_reference_paths=method_context_reference_paths,
            trial_workspace_mappings=trial_workspace_mappings,
        )
        return RetainedStudyPreparationReceipt(
            package=package,
            source_membership=membership,
            study_definition=StudyDefinitionReceipt(owner=owner, manifest=manifest),
        )

    def launch_definition_run(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        controller_holder_id: str,
        controller_ttl_seconds: float,
        preparation: RetainedStudyPreparationReceipt,
        run_id: str | None = None,
        owner_id: str | None = None,
    ) -> RunCreateReceipt:
        """Create only the guarded run namespace; execution remains separate."""

        if not isinstance(preparation, RetainedStudyPreparationReceipt):
            raise TypeError(
                "preparation must be a RetainedStudyPreparationReceipt."
            )
        definition = preparation.study_definition
        return self._ledger.create_run_from_study_definition(
            operation_id=_phase_operation_id(operation_id, "create-run"),
            actor_principal_id=actor_principal_id,
            controller_holder_id=controller_holder_id,
            controller_ttl_seconds=controller_ttl_seconds,
            study_definition_owner_id=definition.owner.owner_id,
            expected_study_definition_owner_revision=definition.owner.revision,
            expected_run_definition_digest=definition.manifest.run_definition_digest,
            run_id=run_id,
            owner_id=owner_id,
        )

    def _capture_or_reuse_source(
        self,
        *,
        operation_id: str,
        actor_principal_id: str,
        store_id: str,
        package_root: Path,
        source_owner_id: str,
        capture_ttl_seconds: float,
    ) -> tuple[OwnerMembership, SourceAnchor, TreeSealReceipt | None]:
        self._ledger.create_owner(
            operation_id=_phase_operation_id(operation_id, "create-source-owner"),
            owner_id=source_owner_id,
            owner_kind=RETAINED_STUDY_SOURCE_OWNER_KIND,
            principal_id=actor_principal_id,
        )
        owner = self._ledger.read_owner(
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
            permission=OwnerPermission.DERIVE,
        )
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
        )
        if (
            owner.owner_kind != RETAINED_STUDY_SOURCE_OWNER_KIND
            or owner.principal_id != actor_principal_id
            or owner.state is not OwnerState.ACTIVE
        ):
            raise RealmConflict("Retained study source owner facts changed.")

        if owner.revision == 1:
            if len(memberships) != 1:
                raise RealmConflict("Retained study source memberships changed.")
            membership = memberships[0]
            if (
                membership.store_id != store_id
                or membership.role != RETAINED_STUDY_SOURCE_ROLE
                or not isinstance(membership.content_ref, SnapshotRef)
            ):
                raise RealmConflict("Retained study source memberships changed.")
            anchor = self._ledger.read_owner_source_anchor(
                actor_principal_id=actor_principal_id,
                owner_id=source_owner_id,
                revision=1,
            )
            return membership, anchor, None

        if owner.revision != 0 or memberships:
            raise RealmConflict("Retained study source owner revision changed.")
        change = self._ledger.begin_owner_change(
            operation_id=_phase_operation_id(operation_id, "begin-source-capture"),
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=capture_ttl_seconds,
        )
        capture = self._content_service.capture(
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            store_id=store_id,
        )
        seal = capture.seal_tree(
            source=AllowedTreeSource(
                package_root,
                excluded_directory_names=RETAINED_STUDY_SOURCE_EXCLUDED_DIRECTORY_NAMES,
            ),
            operation_id=_phase_operation_id(operation_id, "seal-source-capture"),
        )
        membership = OwnerMembership(
            store_id=store_id,
            content_ref=seal.snapshot_ref,
            role=RETAINED_STUDY_SOURCE_ROLE,
        )
        self._ledger.hold_owner_content(
            operation_id=_phase_operation_id(operation_id, "hold-source-capture"),
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            memberships=(membership,),
        )
        commit = self._ledger.commit_owner_change(
            operation_id=_phase_operation_id(operation_id, "commit-source-capture"),
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        if commit.owner_revision != 1 or commit.additions != (membership,):
            raise RealmConflict("Retained study source commit facts differ.")
        anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=actor_principal_id,
            owner_id=source_owner_id,
            revision=1,
        )
        if anchor.owner_manifest_digest != commit.manifest_digest:
            raise RealmConflict("Retained study source manifest digest changed.")
        return membership, anchor, seal


__all__ = [
    "RETAINED_STUDY_PREPARATION_RECEIPT_FORMAT",
    "RETAINED_STUDY_SOURCE_EXCLUDED_DIRECTORY_NAMES",
    "RETAINED_STUDY_SOURCE_OWNER_KIND",
    "RETAINED_STUDY_SOURCE_ROLE",
    "RetainedStudyPreparationReceipt",
    "RetainedStudyService",
]
