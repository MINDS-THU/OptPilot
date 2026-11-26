from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from optpilot.config import compile_authoring_config
from optpilot.locked_python_runtime_contract import (
    LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
    PreparedPythonRuntime,
)
from optpilot.realm._validation import thaw_json
from optpilot.realm.owner_derivation import OwnerDerivationManifest, SourceAnchor
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    InterfaceLaunchProfile,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.run_definition import (
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
)
from optpilot.realm.study_definition import StudyDefinitionManifest
from optpilot.retained_study_compiler import (
    LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA,
    METHOD_CONTEXT_SCOPE,
    PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA,
    TRIAL_INPUT_SCOPE,
    RetainedStudyCompileError,
    RetainedStudyPackage,
    compile_retained_process_study,
    package_projection_contract_features,
)
from optpilot.runtime_limits import TRIAL_FILESYSTEM_QUOTA
from optpilot.runtime_scopes import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    METHOD_PREPARED_PYTHON_SCOPE,
)
from optpilot.spec import StudySpec, study_spec_from_raw


_ROOT = Path(__file__).resolve().parents[2]
_STUDY = _ROOT / "tests/fixtures/catalog/studies/toy_random_search.yaml"


def _study() -> StudySpec:
    study = study_spec_from_raw(
        Path("/mutable/checkout/studies/toy.yaml"),
        compile_authoring_config(_STUDY),
    )
    study.environment["adapter"]["config"]["evaluate"]["callable"] = (
        "env_impl:evaluate"
    )
    study.method["implementation"]["callable"] = "method_impl:Method"
    return study


def _command_study(command: list[str] | None = None) -> StudySpec:
    study = _study()
    study.method["implementation"] = {
        "type": "command",
        "protocol": "optpilot.method.batch.v1",
        "command": list(command or ["python", "method_impl.py"]),
    }
    return study


def _capability_study(
    *,
    require: bool = True,
    callable_ref: str | None = "env_impl:replay_candidate",
) -> StudySpec:
    study = _study()
    capability: dict = {"id": "exact_seed_replay", "description": "replay"}
    if callable_ref:
        capability["callable"] = callable_ref
    study.candidate["context"]["capabilities"] = [dict(capability)]
    study.environment["adapter"]["config"]["context"]["capabilities"] = [
        dict(capability)
    ]
    if require:
        study.method["compatibility"]["requiredCapabilities"] = [
            "exact_seed_replay"
        ]
    return study


def _file_study() -> StudySpec:
    study = _study()
    context = study.candidate["context"]
    context["format"] = "files"
    context["candidate"] = {
        "format": "files",
        "materialize": {"root": "candidate"},
        "files": {
            "editable": [{"path": "solver.py"}],
            "required": ["solver.py"],
            "allow": ["solver.py", "lib/*"],
            "deny": ["*.secret"],
        },
    }
    study.environment["adapter"]["config"]["candidate"] = copy.deepcopy(
        context["candidate"]
    )
    study.environment["accessPolicy"] = "CodeAwareReadOnly"
    study.environment["mutationPolicy"] = "TrialWorkspaceOnly"
    context.pop("parameters", None)
    study.candidate.clear()
    study.candidate.update(
        {
            "format": "files",
            "context": context,
            "materialization": {
                "implementation": "builtin.workspace_bundle",
                "config": {
                    "candidateRoot": "candidate",
                    "entrypoint": "solver.py",
                },
            },
            "validation": {
                "implementation": "builtin.workspace_policy",
                "config": {
                    "requiredFiles": ["solver.py"],
                    "allow": ["solver.py", "lib/*"],
                    "deny": ["*.secret"],
                },
            },
        }
    )
    study.method["compatibility"]["formats"] = ["files"]
    study.method["compatibility"]["requiredContext"] = []
    return study


def _manifest() -> TreeManifest:
    entries = [
        TreeEntry.directory("environments"),
        TreeEntry.directory("methods"),
        TreeEntry.directory("studies"),
    ]
    for path, payload in (
        ("studies/toy.yaml", b"study"),
        ("environments/toy.yaml", b"environment"),
        ("environments/env_impl.py", b"def evaluate(): pass\n"),
        ("methods/random.yaml", b"method"),
        ("methods/method_impl.py", b"class Method: pass\n"),
    ):
        entries.append(
            TreeEntry.file(
                path,
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            )
        )
    return TreeManifest.build(entries)


def _study_with_method_context() -> tuple[StudySpec, TreeManifest, RetainedStudyPackage]:
    study = _study()
    host_root = "/provider/private/package/environments/context"
    method_context = {
        "instructions": [f"{host_root}/prompt.md"],
        "references": [
            {
                "name": "cases",
                "path": f"{host_root}/cases.yaml",
                "type": "dataset",
            }
        ],
    }
    study.candidate["context"]["methodContext"] = copy.deepcopy(method_context)
    study.environment["adapter"]["config"]["context"]["methodContext"] = (
        copy.deepcopy(method_context)
    )
    entries = list(_manifest().entries)
    entries.append(TreeEntry.directory("environments/context"))
    for path, payload in (
        ("environments/context/prompt.md", b"optimize\n"),
        ("environments/context/cases.yaml", b"cases: []\n"),
    ):
        entries.append(
            TreeEntry.file(
                path,
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            )
        )
    manifest = TreeManifest.build(entries)
    package = _package(
        snapshot_ref=manifest.snapshot_ref,
        method_context_instruction_paths=("environments/context/prompt.md",),
        method_context_reference_paths=("environments/context/cases.yaml",),
    )
    return study, manifest, package


def _study_with_trial_workspace(
    mappings: tuple[tuple[str, str], ...] | None = None,
    *,
    entries: tuple[TreeEntry, ...] = (),
) -> tuple[StudySpec, TreeManifest, RetainedStudyPackage]:
    selected = mappings or (
        ("environments/seeds/base.json", "inputs/base.json"),
        ("environments/fixtures", "fixtures"),
    )
    study = _study()
    host_root = "/provider/private/package"
    declarations = [
        {"from": f"{host_root}/{source}", "to": destination}
        for source, destination in selected
    ]
    candidate_context = study.candidate["context"]
    adapter_config = study.environment["adapter"]["config"]
    adapter_context = adapter_config["context"]
    candidate_context["trialWorkspace"] = copy.deepcopy(declarations)
    candidate_context["workspace"]["copy"] = copy.deepcopy(declarations)
    adapter_context["trialWorkspace"] = copy.deepcopy(declarations)
    adapter_context["workspace"]["copy"] = copy.deepcopy(declarations)
    adapter_config["workspace"]["copy"] = copy.deepcopy(declarations)

    base_entries = list(_manifest().entries)
    if not entries:
        payload = b'{"seed": 7}\n'
        entries = (
            TreeEntry.directory("environments/seeds"),
            TreeEntry.file(
                "environments/seeds/base.json",
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            ),
            TreeEntry.directory("environments/fixtures"),
            TreeEntry.file(
                "environments/fixtures/case.txt",
                blob_ref=BlobRef.from_bytes(b"case"),
                size=4,
                executable=False,
            ),
        )
    manifest = TreeManifest.build((*base_entries, *entries))
    return (
        study,
        manifest,
        _package(
            snapshot_ref=manifest.snapshot_ref,
            trial_workspace_mappings=selected,
        ),
    )


_LOCKED_SETUP = {
    "cache": "prepared",
    "timeoutSeconds": 300,
    "steps": [
        {"uses": "python-venv", "cwd": ".", "requirements": ["requirements.lock"]}
    ],
}


def _prepared_runtime(component_kind: str) -> PreparedPythonRuntime:
    """One already-retained dependency layer, as preparation would return it."""

    digest, cache_key, owner_digest = (
        ("e" * 64, "c" * 64, "d" * 64)
        if component_kind == "environment"
        else ("b" * 64, "f" * 64, "9" * 64)
    )
    return PreparedPythonRuntime(
        component_kind=component_kind,
        cache_key=cache_key,
        source_anchor=SourceAnchor(f"{component_kind}-dependency-owner", 3, owner_digest),
        membership=OwnerMembership(
            store_id="local-store",
            content_ref=SnapshotRef(digest),
            role=LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
        ),
        scope=(
            ENVIRONMENT_PREPARED_PYTHON_SCOPE
            if component_kind == "environment"
            else METHOD_PREPARED_PYTHON_SCOPE
        ),
    )


def _dependency_study(
    *,
    requires_capability: bool,
    method_setup: bool = False,
) -> StudySpec:
    """A study whose Environment locks dependencies the Method may need."""

    study = _study()
    if requires_capability:
        capability = {
            "id": "exact_seed_replay",
            "description": "Replay one exact evaluation seed.",
        }
        study.candidate["context"]["capabilities"] = [copy.deepcopy(capability)]
        study.environment["adapter"]["config"]["context"]["capabilities"] = [
            copy.deepcopy(capability)
        ]
        study.method["compatibility"]["requiredCapabilities"] = ["exact_seed_replay"]
    study.environment["runtime"]["setup"] = copy.deepcopy(_LOCKED_SETUP)
    study.execution["backend"]["config"]["setup"] = copy.deepcopy(_LOCKED_SETUP)
    if method_setup:
        study.method["runtime"]["setup"] = copy.deepcopy(_LOCKED_SETUP)
    return study


def _package(**changes) -> RetainedStudyPackage:
    values = {
        "source_anchor": SourceAnchor("package-owner", 7, "a" * 64),
        "store_id": "local-store",
        "snapshot_ref": _manifest().snapshot_ref,
        "source_role": "package-source",
        "study_config_path": "studies/toy.yaml",
        "environment_config_path": "environments/toy.yaml",
        "method_config_path": "methods/random.yaml",
        "environment_python_import_roots": ("environments",),
        "method_python_import_roots": ("methods",),
    }
    values.update(changes)
    return RetainedStudyPackage(**values)


def _provider() -> ProcessProviderIdentity:
    return ProcessProviderIdentity(
        builder_fingerprint="b" * 64,
        platform="darwin-arm64-python3.13",
    )


def _copy_study(study: StudySpec) -> StudySpec:
    return StudySpec(path=study.path, raw=copy.deepcopy(study.raw))


class RetainedStudyPackageTest(unittest.TestCase):
    def test_record_is_strict_canonical_and_round_trips(self) -> None:
        package = _package()
        self.assertEqual(RetainedStudyPackage.from_dict(package.to_dict()), package)

        extra = {**package.to_dict(), "host_path": "/tmp/source"}
        with self.assertRaisesRegex(ValueError, "fields differ"):
            RetainedStudyPackage.from_dict(extra)

        for path in ("../study.yaml", "/study.yaml", "a\\study.yaml", "a/./study.yaml"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "portable relative path"):
                    _package(study_config_path=path)

    def test_trial_workspace_mappings_are_strict_and_bounded(self) -> None:
        package = _package(
            trial_workspace_mappings=(("environments/seed.json", "seed.json"),)
        )
        self.assertEqual(RetainedStudyPackage.from_dict(package.to_dict()), package)
        with self.assertRaisesRegex(ValueError, "too many"):
            _package(
                trial_workspace_mappings=tuple(
                    ("environments/seed.json", f"seed/{index}.json")
                    for index in range(129)
                )
            )

    def test_projection_contract_rejects_schema_without_a_feature(self) -> None:
        with self.assertRaisesRegex(ValueError, "no features"):
            package_projection_contract_features(
                {"schema": PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA}
            )


class RetainedStudyCompilerTest(unittest.TestCase):
    def assert_code(self, code: str, callback) -> RetainedStudyCompileError:
        with self.assertRaises(RetainedStudyCompileError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_compiles_one_snapshot_into_exact_semantics_and_no_copy_derivation(self) -> None:
        study = _study()
        package = _package()
        result = compile_retained_process_study(
            study,
            package=package,
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="study-definition-1",
        )

        definition = result.run_definition
        environment = definition.evaluation_closure.environment_revision
        environment_runtime = definition.evaluation_closure.prepared_runtime
        method = definition.method_revision
        method_runtime = definition.prepared_method_runtime

        self.assertEqual(
            environment.authored_config,
            ScopePath("study-package-source", "environments/toy.yaml"),
        )
        self.assertEqual(
            method.authored_config,
            ScopePath("study-package-source", "methods/random.yaml"),
        )
        self.assertEqual(environment.source_layers[0].snapshot_ref, package.snapshot_ref)
        self.assertEqual(method.source_layers[0].snapshot_ref, package.snapshot_ref)
        self.assertEqual(environment.source_layers[0].source_subpath, ".")
        self.assertEqual(method.source_layers[0].source_subpath, ".")
        self.assertEqual(
            environment_runtime.workdir,
            ScopePath("study-package-source", "environments"),
        )
        self.assertEqual(
            method_runtime.workdir,
            ScopePath("study-package-source", "methods"),
        )

        for runtime, scope, parent in (
            (environment_runtime, "study-package-source", "environments"),
            (method_runtime, "study-package-source", "methods"),
        ):
            self.assertEqual(runtime.runtime_kind, "process")
            self.assertEqual(runtime.portability, "provider-scoped")
            self.assertEqual(runtime.builder_fingerprint, "b" * 64)
            self.assertEqual(runtime.platform, "darwin-arm64-python3.13")
            self.assertEqual(runtime.prepared_layers, ())
            self.assertEqual(
                runtime.runtime_settings["schema"],
                LOGICAL_PYTHON_RUNTIME_SETTINGS_SCHEMA,
            )
            self.assertEqual(
                runtime.runtime_settings["import_roots"],
                ({"path": parent, "scope": scope},),
            )

        self.assertEqual(
            definition.required_content_refs,
            (
                (RUN_ENVIRONMENT_SOURCE_ROLE, package.snapshot_ref),
                (RUN_METHOD_SOURCE_ROLE, package.snapshot_ref),
            ),
        )
        derivation = result.owner_derivation
        self.assertEqual(derivation.sources, (package.source_anchor,))
        self.assertEqual(len(derivation.bindings), 2)
        self.assertEqual(
            {binding.target_role for binding in derivation.bindings},
            {RUN_ENVIRONMENT_SOURCE_ROLE, RUN_METHOD_SOURCE_ROLE},
        )
        for binding in derivation.bindings:
            self.assertEqual(binding.source_store_id, package.store_id)
            self.assertEqual(binding.content_ref, package.snapshot_ref)
            self.assertEqual(binding.source_role, package.source_role)

        retained = result.study_definition
        self.assertEqual(retained.run_definition, definition)
        self.assertEqual(retained.owner_derivation_manifest_digest, derivation.digest)
        self.assertEqual(
            retained.authored_study_config,
            ScopePath("study-package-source", "studies/toy.yaml"),
        )
        self.assertEqual(
            OwnerDerivationManifest.from_dict(derivation.to_dict()), derivation
        )
        self.assertEqual(StudyDefinitionManifest.from_dict(retained.to_dict()), retained)

        encoded = json.dumps(retained.to_dict(), sort_keys=True)
        self.assertNotIn(str(study.path), encoded)
        self.assertNotIn(study.method["configBaseDir"], encoded)

    def test_method_host_environment_is_a_name_only_launch_requirement(self) -> None:
        study = _study()
        study.method["runtime"]["envFromHost"] = [
            "OPENROUTER_API_KEY",
            "OPTPILOT_LLM_MODEL",
        ]

        result = compile_retained_process_study(
            study,
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="study-definition-method-environment",
        )

        requirements = result.run_definition.method_revision.method_contract[
            "runtime_requirements"
        ]
        self.assertEqual(
            requirements["envFromHost"],
            ("OPENROUTER_API_KEY", "OPTPILOT_LLM_MODEL"),
        )
        encoded = json.dumps(result.study_definition.to_dict(), sort_keys=True)
        self.assertIn("OPENROUTER_API_KEY", encoded)
        self.assertNotIn("private-api-key-value", encoded)

    def test_method_host_environment_names_are_strict_and_worker_safe(self) -> None:
        for names, code in (
            (["OPENROUTER_API_KEY", "OPENROUTER_API_KEY"], "method_environment_declaration_invalid"),
            (["NOT-A-NAME"], "method_environment_declaration_invalid"),
            (["PYTHONHASHSEED"], "method_environment_name_reserved"),
        ):
            study = _study()
            study.method["runtime"]["envFromHost"] = names
            with self.subTest(names=names):
                self.assert_code(
                    code,
                    lambda study=study: compile_retained_process_study(
                        study,
                        package=_package(),
                        package_manifest=_manifest(),
                        provider=_provider(),
                        target_owner_id=f"method-environment-{code}",
                    ),
                )

    def test_method_context_is_path_free_and_aliases_the_existing_package_snapshot(self) -> None:
        study, manifest, package = _study_with_method_context()

        result = compile_retained_process_study(
            study,
            package=package,
            package_manifest=manifest,
            provider=_provider(),
            target_owner_id="study-definition-context",
        )

        environment = result.run_definition.evaluation_closure.environment_revision
        self.assertEqual(
            environment.projection_contract,
            {
                "method_context": {
                    "logical_scope": METHOD_CONTEXT_SCOPE,
                    "source": {
                        "path": "environments/context",
                        "scope": "study-package-source",
                    },
                },
                "schema": PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA,
            },
        )
        method_context = environment.candidate_contract["context"]["methodContext"]
        self.assertEqual(method_context["instructions"], ("prompt.md",))
        self.assertEqual(method_context["references"][0]["path"], "cases.yaml")
        self.assertEqual(
            result.run_definition.required_content_refs,
            (
                (RUN_ENVIRONMENT_SOURCE_ROLE, package.snapshot_ref),
                (RUN_METHOD_SOURCE_ROLE, package.snapshot_ref),
            ),
        )
        encoded = json.dumps(result.study_definition.to_dict(), sort_keys=True)
        self.assertNotIn("/provider/private", encoded)

    def test_file_candidate_contract_compiles_without_legacy_path_authority(self) -> None:
        result = compile_retained_process_study(
            _file_study(),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="file-candidate-study",
        )

        contract = (
            result.run_definition.evaluation_closure.environment_revision.candidate_contract
        )
        self.assertEqual(contract["format"], "files")
        self.assertEqual(
            contract["materialization"],
            {
                "implementation": "builtin.workspace_bundle",
                "config": {
                    "candidateRoot": "candidate",
                    "entrypoint": "solver.py",
                },
            },
        )
        self.assertEqual(
            set(contract["validation"]["config"]),
            {"allow", "deny", "requiredFiles"},
        )
        encoded = json.dumps(result.study_definition.to_dict(), sort_keys=True)
        for legacy in (
            "allowAbsoluteContentRefs",
            "readonlyFiles",
            "requireExistingRefs",
            "requireHashes",
            "seedFiles",
        ):
            self.assertNotIn(legacy, encoded)

    def test_trial_workspace_compiles_to_portable_layers_on_the_package_snapshot(self) -> None:
        study, manifest, package = _study_with_trial_workspace()

        result = compile_retained_process_study(
            study,
            package=package,
            package_manifest=manifest,
            provider=_provider(),
            target_owner_id="study-definition-trial-inputs",
        )

        environment = result.run_definition.evaluation_closure.environment_revision
        self.assertEqual(
            tuple(
                sorted(
                    environment.attempt_input_layers,
                    key=lambda item: item.precedence,
                )
            ),
            (
                ScopeLayer(
                    TRIAL_INPUT_SCOPE,
                    package.snapshot_ref,
                    source_subpath="environments/seeds/base.json",
                    destination_subpath="inputs/base.json",
                    precedence=0,
                ),
                ScopeLayer(
                    TRIAL_INPUT_SCOPE,
                    package.snapshot_ref,
                    source_subpath="environments/fixtures",
                    destination_subpath="fixtures",
                    precedence=1,
                ),
            ),
        )
        self.assertEqual(
            environment.projection_contract,
            {
                "schema": PACKAGE_INPUT_PROJECTION_CONTRACT_SCHEMA,
                "trial_workspace": {
                    "collision_policy": "identical-effective-entries-only",
                    "destination_scope": TRIAL_INPUT_SCOPE,
                    "source_scope": "study-package-source",
                },
            },
        )
        candidate_context = environment.candidate_contract["context"]
        evaluator_config = environment.evaluator_contract["adapter"]["config"]
        self.assertEqual(candidate_context["trialWorkspace"], ())
        self.assertEqual(candidate_context["workspace"]["copy"], ())
        self.assertEqual(evaluator_config["context"]["trialWorkspace"], ())
        self.assertEqual(evaluator_config["context"]["workspace"]["copy"], ())
        self.assertEqual(evaluator_config["workspace"]["copy"], ())
        self.assertEqual(
            result.run_definition.required_content_refs,
            (
                (RUN_ATTEMPT_INPUT_ROLE, package.snapshot_ref),
                (RUN_ENVIRONMENT_SOURCE_ROLE, package.snapshot_ref),
                (RUN_METHOD_SOURCE_ROLE, package.snapshot_ref),
            ),
        )
        self.assertEqual(
            {binding.target_role for binding in result.owner_derivation.bindings},
            {
                RUN_ATTEMPT_INPUT_ROLE,
                RUN_ENVIRONMENT_SOURCE_ROLE,
                RUN_METHOD_SOURCE_ROLE,
            },
        )
        encoded = json.dumps(result.study_definition.to_dict(), sort_keys=True)
        self.assertNotIn("/provider/private", encoded)

    def test_trial_workspace_identical_overlaps_are_allowed(self) -> None:
        payload = b"same"
        entries = (
            TreeEntry.file(
                "environments/a.txt",
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            ),
            TreeEntry.file(
                "environments/b.txt",
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            ),
        )
        study, manifest, package = _study_with_trial_workspace(
            (
                ("environments/a.txt", "shared/value.txt"),
                ("environments/b.txt", "shared/value.txt"),
            ),
            entries=entries,
        )

        result = compile_retained_process_study(
            study,
            package=package,
            package_manifest=manifest,
            provider=_provider(),
            target_owner_id="study-definition-identical-inputs",
        )

        self.assertEqual(
            len(
                result.run_definition.evaluation_closure.environment_revision.attempt_input_layers
            ),
            2,
        )

    def test_trial_workspace_effective_collisions_fail_closed(self) -> None:
        payloads = {
            "environments/a.txt": b"a",
            "environments/b.txt": b"b",
        }
        entries = tuple(
            TreeEntry.file(
                path,
                blob_ref=BlobRef.from_bytes(payload),
                size=len(payload),
                executable=False,
            )
            for path, payload in payloads.items()
        )
        cases = (
            (
                "different-content",
                (("environments/a.txt", "same.txt"), ("environments/b.txt", "same.txt")),
            ),
            (
                "casefold",
                (("environments/a.txt", "Data.txt"), ("environments/b.txt", "data.txt")),
            ),
            (
                "file-ancestor",
                (("environments/a.txt", "node"), ("environments/b.txt", "node/child")),
            ),
        )
        for name, mappings in cases:
            with self.subTest(name=name):
                study, manifest, package = _study_with_trial_workspace(
                    mappings,
                    entries=entries,
                )
                self.assert_code(
                    "trial_workspace_collision",
                    lambda: compile_retained_process_study(
                        study,
                        package=package,
                        package_manifest=manifest,
                        provider=_provider(),
                        target_owner_id=f"study-definition-collision-{name}",
                    ),
                )

    def test_trial_workspace_missing_source_and_quota_fail_closed(self) -> None:
        study, manifest, package = _study_with_trial_workspace(
            (("environments/missing.txt", "input.txt"),),
            entries=(
                TreeEntry.file(
                    "environments/other.txt",
                    blob_ref=BlobRef.from_bytes(b"other"),
                    size=5,
                    executable=False,
                ),
            ),
        )
        self.assert_code(
            "trial_workspace_unretained",
            lambda: compile_retained_process_study(
                study,
                package=package,
                package_manifest=manifest,
                provider=_provider(),
                target_owner_id="study-definition-missing-input",
            ),
        )

        oversized = TreeEntry.file(
            "environments/huge.bin",
            blob_ref=BlobRef.from_bytes(b"placeholder"),
            size=TRIAL_FILESYSTEM_QUOTA.max_file_bytes + 1,
            executable=False,
        )
        study, manifest, package = _study_with_trial_workspace(
            (("environments/huge.bin", "huge.bin"),),
            entries=(oversized,),
        )
        self.assert_code(
            "trial_workspace_quota_exceeded",
            lambda: compile_retained_process_study(
                study,
                package=package,
                package_manifest=manifest,
                provider=_provider(),
                target_owner_id="study-definition-oversized-input",
            ),
        )

    def test_adapter_only_method_context_host_path_is_rejected(self) -> None:
        study = _study()
        study.environment["adapter"]["config"]["context"]["methodContext"] = {
            "instructions": ["/provider/private/secret.md"],
            "references": [],
        }

        self.assert_code(
            "method_context_plan_mismatch",
            lambda: compile_retained_process_study(
                study,
                package=_package(),
                package_manifest=_manifest(),
                provider=_provider(),
                target_owner_id="study-definition-adapter-only-context",
            ),
        )

    def test_retains_interface_profiles_exactly(self) -> None:
        study = _study()
        profile = {
            "id": "default",
            "label": "Inspect",
            "description": "",
            "command": ["python", "-m", "viewer"],
            "cwd": ".",
            "env": {"MODE": "inspect"},
            "runtime": {
                "sandbox": "container",
                "setup": {
                    "steps": [
                        {
                            "uses": "command",
                            "cwd": ".",
                            "command": ["python", "-m", "pip", "--version"],
                        }
                    ],
                    "timeoutSeconds": 30,
                },
                "container": {
                    "engine": "docker",
                    "image": "toy/viewer:1",
                    "platform": "linux/amd64",
                },
            },
            "grants": {
                "envFromHost": [],
                "network": "enabled",
                "secretsFromHost": ["VIEW_TOKEN"],
            },
            "resources": {"cpu": 1, "memoryMiB": 512, "gpus": 0},
            "timeoutSeconds": 60,
            "presentation": {
                "kind": "web",
                "port": 8000,
                "extraPorts": [],
                "readyPath": "/",
                "readyTimeoutSeconds": 10,
            },
            "accepts": {"selectionKinds": ["candidate"], "mediaTypes": []},
        }
        study.environment["adapter"]["config"]["interfaces"] = [profile]

        result = compile_retained_process_study(
            study,
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="study-definition-interface",
        )

        self.assertEqual(
            result.run_definition.evaluation_closure.environment_revision.to_dict()[
                "interface_profiles"
            ],
            [profile],
        )
        self.assertIsInstance(
            result.run_definition.evaluation_closure.environment_revision.interface_profiles[0],
            InterfaceLaunchProfile,
        )

    def test_requires_exact_manifest_paths_roots_and_source_backed_callables(self) -> None:
        study = _study()
        cases = []

        missing_config = TreeManifest.build(
            tuple(
                entry
                for entry in _manifest().entries
                if entry.path != "studies/toy.yaml"
            )
        )
        cases.append(
            (
                "authored_config_unretained",
                _package(snapshot_ref=missing_config.snapshot_ref),
                missing_config,
                study,
            )
        )

        package = _package(
            environment_python_import_roots=("environments", "missing")
        )
        cases.append(("python_root_unretained", package, _manifest(), study))

        missing_callable = _copy_study(study)
        missing_callable.method["implementation"]["callable"] = "absent:Method"
        cases.append(
            ("python_callable_unretained", _package(), _manifest(), missing_callable)
        )

        for code, package, manifest, case_study in cases:
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda package=package, manifest=manifest, case_study=case_study: (
                        compile_retained_process_study(
                            case_study,
                            package=package,
                            package_manifest=manifest,
                            provider=_provider(),
                            target_owner_id=f"manifest-{code}",
                        )
                    ),
                )

        other_manifest = TreeManifest.build(
            (
                *_manifest().entries,
                TreeEntry.file(
                    "other.txt",
                    blob_ref=BlobRef.from_bytes(b"other"),
                    size=5,
                    executable=False,
                ),
            )
        )
        with self.assertRaisesRegex(ValueError, "snapshot ref"):
            compile_retained_process_study(
                study,
                package=_package(snapshot_ref=other_manifest.snapshot_ref),
                package_manifest=_manifest(),
                provider=_provider(),
                target_owner_id="wrong-manifest",
            )

    def test_preserves_import_precedence_and_drops_expanded_host_paths(self) -> None:
        study = _study()
        study.environment["adapter"]["config"]["evaluate"]["pythonPath"] = [
            "/private/projection/environment"
        ]
        study.method["implementation"]["pythonPath"] = [
            "/private/projection/method"
        ]
        package = _package(
            environment_python_import_roots=("environments", "."),
            method_python_import_roots=("methods", "."),
        )
        result = compile_retained_process_study(
            study,
            package=package,
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="ordered-imports",
        )

        environment_runtime = result.run_definition.evaluation_closure.prepared_runtime
        method_runtime = result.run_definition.prepared_method_runtime
        expected_environment = (
            {"path": "environments", "scope": "study-package-source"},
            {"path": ".", "scope": "study-package-source"},
        )
        expected_method = (
            {"path": "methods", "scope": "study-package-source"},
            {"path": ".", "scope": "study-package-source"},
        )
        self.assertEqual(
            environment_runtime.runtime_settings["import_roots"],
            expected_environment,
        )
        self.assertEqual(
            method_runtime.runtime_settings["import_roots"], expected_method
        )
        encoded = json.dumps(result.study_definition.to_dict(), sort_keys=True)
        self.assertNotIn("/private/projection", encoded)

    def test_capability_method_receives_the_environment_prepared_layer(self) -> None:
        environment_prepared = _prepared_runtime("environment")
        result = compile_retained_process_study(
            _dependency_study(requires_capability=True),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="capability-dependency",
            environment_prepared_runtime=environment_prepared,
        )

        method_runtime = result.run_definition.prepared_method_runtime
        # Without this layer the Environment callable the Method invokes would
        # resolve its third-party imports from whatever the host happens to
        # have installed, so the Study would only run on the build machine.
        self.assertEqual(
            method_runtime.runtime_settings["import_roots"],
            (
                {"path": "methods", "scope": "study-package-source"},
                {"path": ".", "scope": ENVIRONMENT_PREPARED_PYTHON_SCOPE},
            ),
        )
        self.assertEqual(len(method_runtime.prepared_layers), 1)
        layer = method_runtime.prepared_layers[0]
        self.assertEqual(layer.scope, ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        self.assertEqual(
            layer.snapshot_ref, environment_prepared.membership.content_ref
        )
        self.assertEqual(layer.source_subpath, "site-packages")
        self.assertEqual(layer.destination_subpath, ".")
        self.assertEqual(layer.precedence, 0)

        # The shared layer must reach the Run as retained content under the
        # Method role, not only as an Environment-role closure member.
        refs = result.run_definition.content_refs_by_role
        self.assertEqual(
            refs[RUN_PREPARED_METHOD_RUNTIME_ROLE],
            (environment_prepared.membership.content_ref,),
        )
        self.assertEqual(
            refs[RUN_PREPARED_RUNTIME_ROLE],
            (environment_prepared.membership.content_ref,),
        )
        dependency_bindings = {
            binding.target_role: binding
            for binding in result.owner_derivation.bindings
            if binding.source_owner_id == environment_prepared.source_anchor.owner_id
        }
        self.assertEqual(
            set(dependency_bindings),
            {RUN_PREPARED_RUNTIME_ROLE, RUN_PREPARED_METHOD_RUNTIME_ROLE},
        )
        for binding in dependency_bindings.values():
            self.assertEqual(
                binding.content_ref, environment_prepared.membership.content_ref
            )
            self.assertEqual(binding.source_role, LOCKED_PYTHON_RUNTIME_SOURCE_ROLE)
        self.assertEqual(
            [item.owner_id for item in result.owner_derivation.sources].count(
                environment_prepared.source_anchor.owner_id
            ),
            1,
        )

    def test_method_without_a_capability_keeps_the_environment_layer_out(self) -> None:
        result = compile_retained_process_study(
            _dependency_study(requires_capability=False),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="no-capability-dependency",
            environment_prepared_runtime=_prepared_runtime("environment"),
        )

        method_runtime = result.run_definition.prepared_method_runtime
        self.assertEqual(
            method_runtime.runtime_settings["import_roots"],
            ({"path": "methods", "scope": "study-package-source"},),
        )
        self.assertEqual(method_runtime.prepared_layers, ())
        self.assertNotIn(
            RUN_PREPARED_METHOD_RUNTIME_ROLE,
            result.run_definition.content_refs_by_role,
        )

    def test_capability_layer_never_outranks_the_methods_own_dependencies(self) -> None:
        environment_prepared = _prepared_runtime("environment")
        method_prepared = _prepared_runtime("method")
        result = compile_retained_process_study(
            _dependency_study(requires_capability=True, method_setup=True),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="capability-and-method-dependency",
            environment_prepared_runtime=environment_prepared,
            method_prepared_runtime=method_prepared,
        )

        method_runtime = result.run_definition.prepared_method_runtime
        self.assertEqual(
            method_runtime.runtime_settings["import_roots"],
            (
                {"path": "methods", "scope": "study-package-source"},
                {"path": ".", "scope": METHOD_PREPARED_PYTHON_SCOPE},
                {"path": ".", "scope": ENVIRONMENT_PREPARED_PYTHON_SCOPE},
            ),
        )
        self.assertEqual(
            {layer.scope for layer in method_runtime.prepared_layers},
            {METHOD_PREPARED_PYTHON_SCOPE, ENVIRONMENT_PREPARED_PYTHON_SCOPE},
        )
        self.assertEqual(
            set(
                result.run_definition.content_refs_by_role[
                    RUN_PREPARED_METHOD_RUNTIME_ROLE
                ]
            ),
            {
                method_prepared.membership.content_ref,
                environment_prepared.membership.content_ref,
            },
        )

    def test_interface_paths_and_profile_shape_fail_closed(self) -> None:
        base = _study()
        profile = {
            "id": "default",
            "label": "Inspect",
            "description": "",
            "command": ["python", "-m", "viewer"],
            "cwd": ".",
            "env": {},
            "runtime": {},
            "grants": {
                "envFromHost": [],
                "network": "disabled",
                "secretsFromHost": [],
            },
            "resources": {"cpu": 1, "memoryMiB": 512, "gpus": 0},
            "timeoutSeconds": 60,
            "presentation": {
                "kind": "web",
                "port": 8000,
                "extraPorts": [],
                "readyPath": "/",
                "readyTimeoutSeconds": 10,
            },
            "accepts": {"selectionKinds": ["candidate"], "mediaTypes": []},
        }
        cases = (
            ("interface_host_path", {**profile, "cwd": "/tmp/app"}),
            (
                "interface_host_path",
                {**profile, "command": ["/usr/bin/python", "/tmp/app.py"]},
            ),
            ("interface_path_escape", {**profile, "cwd": "../../../outside"}),
            ("interface_cwd_unretained", {**profile, "cwd": "missing"}),
            (
                "interface_profile_invalid",
                {
                    **profile,
                    "grants": {
                        "envFromHost": [],
                        "network": "ambient",
                        "secretsFromHost": [],
                    },
                },
            ),
            (
                "interface_build_context_unretained",
                {
                    **profile,
                    "runtime": {
                        "sandbox": "container",
                        "container": {
                            "engine": "docker",
                            "build": {
                                "args": {},
                                "context": "missing",
                                "dockerfile": "Containerfile",
                                "tag": "toy/viewer:local",
                            },
                        },
                    },
                },
            ),
            (
                "interface_build_input_unretained",
                {
                    **profile,
                    "runtime": {
                        "sandbox": "container",
                        "container": {
                            "engine": "docker",
                            "build": {
                                "args": {},
                                "context": ".",
                                "dockerfile": "Containerfile",
                                "tag": "toy/viewer:local",
                            },
                        },
                    },
                },
            ),
        )
        for code, case_profile in cases:
            study = _copy_study(base)
            study.environment["adapter"]["config"]["interfaces"] = [case_profile]
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda study=study: compile_retained_process_study(
                        study,
                        package=_package(),
                        package_manifest=_manifest(),
                        provider=_provider(),
                        target_owner_id=f"interface-{code}",
                    ),
                )

    def test_required_capability_callable_extends_method_import_roots(self) -> None:
        def compiled_import_roots(study: StudySpec, owner: str):
            result = compile_retained_process_study(
                study,
                package=_package(),
                package_manifest=_manifest(),
                provider=_provider(),
                target_owner_id=owner,
            )
            return result.run_definition.prepared_method_runtime.runtime_settings[
                "import_roots"
            ]

        method_only = ({"path": "methods", "scope": "study-package-source"},)
        with_environment = (
            {"path": "methods", "scope": "study-package-source"},
            {"path": "environments", "scope": "study-package-source"},
        )

        self.assertEqual(
            compiled_import_roots(_capability_study(), "capability-required"),
            with_environment,
        )
        self.assertEqual(
            compiled_import_roots(
                _capability_study(require=False), "capability-unrequired"
            ),
            method_only,
        )
        self.assertEqual(
            compiled_import_roots(
                _capability_study(callable_ref=None), "capability-no-callable"
            ),
            method_only,
        )

    def test_capability_callable_must_be_retained_under_environment_roots(self) -> None:
        study = _capability_study(callable_ref="missing_replay:replay_candidate")
        self.assert_code(
            "python_callable_unretained",
            lambda: compile_retained_process_study(
                study,
                package=_package(),
                package_manifest=_manifest(),
                provider=_provider(),
                target_owner_id="capability-unretained",
            ),
        )

    def test_policy_validation_declaration_is_retained_in_the_candidate_contract(
        self,
    ) -> None:
        study = _study()
        policy = {
            "entrypoint": {
                "file": "solver.py",
                "callable": "create_solver",
                "maxArguments": 0,
            },
            "forbiddenImports": ["os", "sys"],
        }
        study.candidate["context"]["policyValidation"] = copy.deepcopy(policy)
        study.environment["adapter"]["config"]["context"]["policyValidation"] = (
            copy.deepcopy(policy)
        )
        result = compile_retained_process_study(
            study,
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="policy-validation-retained",
        )
        contract = result.run_definition.evaluation_closure.environment_revision
        self.assertEqual(
            thaw_json(contract.candidate_contract["context"]["policyValidation"]),
            policy,
        )

    def test_command_batch_method_compiles_into_the_retained_slice(self) -> None:
        result = compile_retained_process_study(
            _command_study(),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="command-study-definition",
        )
        contract = result.run_definition.method_revision.method_contract
        self.assertEqual(
            thaw_json(contract["implementation"]),
            {
                "type": "command",
                "protocol": "optpilot.method.batch.v1",
                "command": ["python", "method_impl.py"],
            },
        )
        other = compile_retained_process_study(
            _command_study(["python", "method_impl.py", "--iterations", "3"]),
            package=_package(),
            package_manifest=_manifest(),
            provider=_provider(),
            target_owner_id="command-study-definition",
        )
        self.assertNotEqual(
            result.run_definition.digest, other.run_definition.digest
        )

    def test_first_slice_failures_have_stable_typed_codes(self) -> None:
        base = _study()

        cases = []
        study = _copy_study(base)
        study.candidate["format"] = "opaque"
        cases.append(("candidate_format_unsupported", study))

        study = _file_study()
        study.candidate["materialization"]["config"]["seedFiles"] = []
        cases.append(("file_candidate_contract_invalid", study))

        study = _copy_study(base)
        study.method["implementation"] = {
            "type": "command",
            "protocol": "optpilot.method.batch.v1",
            "command": ["bash", "method.sh"],
        }
        cases.append(("method_command_unsupported", study))

        study = _copy_study(base)
        study.method["implementation"] = {
            "type": "command",
            "protocol": "optpilot.method.batch.v1",
            "command": ["python", "missing_method.py"],
        }
        cases.append(("method_command_unretained", study))

        study = _copy_study(base)
        study.method["implementation"]["protocol"] = "optpilot.method.session.v1"
        cases.append(("method_mode_unsupported", study))

        study = _copy_study(base)
        study.method["runtime"]["type"] = "container"
        cases.append(("container_runtime_unsupported", study))

        study = _copy_study(base)
        study.environment["runtime"]["setup"] = {"steps": []}
        study.execution["backend"]["config"]["setup"] = {"steps": []}
        cases.append(("dependency_setup_unsupported", study))

        study = _copy_study(base)
        study.method["runtime"]["build"] = {"context": "/host/source"}
        cases.append(("runtime_build_unsupported", study))

        study = _copy_study(base)
        study.environment["adapter"]["config"]["evaluate"]["cwd"] = "runner"
        cases.append(("evaluator_cwd_unsupported", study))

        study = _copy_study(base)
        study.environment["runtime"]["envFromHost"] = ["TOKEN"]
        cases.append(("host_environment_unsupported", study))

        study = _copy_study(base)
        study.candidate["context"]["trialWorkspace"] = [
            {"from": "/host/data", "to": "data"}
        ]
        cases.append(("trial_workspace_mismatch", study))

        for code, study in cases:
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    lambda study=study: compile_retained_process_study(
                        study,
                        package=_package(),
                        package_manifest=_manifest(),
                        provider=_provider(),
                        target_owner_id=f"target-{code}",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
