from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from optpilot.attempts import EvaluationSpec
from optpilot.realm._validation import thaw_json
from optpilot.realm.filesystem_quota import FilesystemQuota
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_closure import (
    InterfaceLaunchProfile,
    RunEvaluationClosure,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.run_records import NormalizedCandidateEnvelope
from optpilot.runtime_binding import (
    CANDIDATE_PROJECTION_PARTITION,
    CONTROL_SCOPE,
    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    ENVIRONMENT_PROJECTION,
    ENVIRONMENT_PROJECTION_PARTITION,
    ENVIRONMENT_SOURCE_SCOPE,
    TRIAL_SCOPE,
    CandidateRuntimeInput,
    ExecutionBindingEvidence,
    ExecutionProviderFacts,
    FileCandidateManifestEntry,
    FileCandidateMaterialization,
    LayeredVolumeScopeSource,
    PortableAttemptRuntimeSpec,
    PortableRuntimeScope,
    ProjectionBindingEvidence,
    ProjectionScopeSource,
    RuntimeBindingCompileError,
    VolumeScopeSource,
    WritableVolumeEvidence,
    compile_retained_process_attempt_runtime,
)
from optpilot.run_control_manifest import candidate_contract_digest
from optpilot.retained_study_compiler import compile_retained_process_study
from tests.core.test_retained_study_compiler import (
    _manifest,
    _package,
    _provider,
    _study,
    _study_with_trial_workspace,
)


def _definition(*, study=None, package=None, manifest=None):
    result = compile_retained_process_study(
        _study() if study is None else study,
        package=_package() if package is None else package,
        package_manifest=_manifest() if manifest is None else manifest,
        provider=_provider(),
        target_owner_id="runtime-binding-definition",
    )
    return result.run_definition


def _evaluation_spec(definition) -> EvaluationSpec:
    closure = definition.evaluation_closure
    contract = thaw_json(closure.environment_revision.candidate_contract)
    template = closure.evaluation_template
    return EvaluationSpec(
        environment_id=closure.environment_revision.environment_id,
        environment_revision_digest=closure.environment_revision.digest,
        prepared_runtime_digest=closure.prepared_runtime.digest,
        candidate_ref="",
        candidate={
            "candidate_id": "candidate-a",
            "format": contract["format"],
            "spec": {"x": 2.5, "y": 4, "mode": "balanced"},
            "lineage": {"parents": []},
            "generator": {"method_id": "method-a", "strategy": "external"},
            "validation": copy.deepcopy(contract.get("validation", {})),
            "materialization": copy.deepcopy(contract.get("materialization", {})),
        },
        objective=thaw_json(template.objective),
        resource_profile=thaw_json(template.resource_profile),
        sandbox_spec=thaw_json(template.sandbox_spec),
        seed=template.default_seed,
        repetition_index=0,
        metadata={},
    )


def _compile(
    definition=None,
    evaluation_spec=None,
    provider=None,
    *,
    candidate_input=None,
):
    definition = _definition() if definition is None else definition
    evaluation_spec = (
        _evaluation_spec(definition) if evaluation_spec is None else evaluation_spec
    )
    return compile_retained_process_attempt_runtime(
        owner_id="run-owner-a",
        run_definition=definition,
        evaluation_spec=evaluation_spec,
        provider=_provider() if provider is None else provider,
        candidate_input=candidate_input,
    )


def _replace_closure(definition, *, environment=None, runtime=None):
    previous = definition.evaluation_closure
    environment = previous.environment_revision if environment is None else environment
    runtime = previous.prepared_runtime if runtime is None else runtime
    if runtime.environment_revision_digest != environment.digest:
        runtime = replace(runtime, environment_revision_digest=environment.digest)
    template = replace(
        previous.evaluation_template,
        environment_revision_digest=environment.digest,
        runtime_revision_digest=runtime.digest,
    )
    return replace(
        definition,
        evaluation_closure=RunEvaluationClosure(environment, runtime, template),
        run_control_manifest=replace(
            definition.run_control_manifest,
            candidate_contract_digest=candidate_contract_digest(
                environment.candidate_contract
            ),
        ),
    )


def _definition_with_prepared_python(definition=None):
    definition = _definition() if definition is None else definition
    previous = definition.evaluation_closure.prepared_runtime
    settings = thaw_json(previous.runtime_settings)
    settings["import_roots"].append(
        ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, ".").to_dict()
    )
    prepared_snapshot = SnapshotRef.from_manifest_bytes(b"prepared-python-runtime")
    runtime = replace(
        previous,
        runtime_settings=settings,
        prepared_layers=(
            ScopeLayer(
                ENVIRONMENT_PREPARED_PYTHON_SCOPE,
                prepared_snapshot,
                source_subpath="site-packages",
            ),
        ),
    )
    return _replace_closure(definition, runtime=runtime)


def _file_definition_and_candidate_input():
    definition = _definition()
    previous = definition.evaluation_closure.environment_revision
    candidate_declaration = {
        "description": "A retained solver bundle.",
        "files": {
            "editable": [{"path": "solver.py"}],
            "required": ["solver.py"],
        },
        "format": "files",
    }
    context = thaw_json(previous.candidate_contract["context"])
    context.pop("parameters", None)
    context.update(
        {
            "candidate": copy.deepcopy(candidate_declaration),
            "description": "A retained solver bundle.",
            "files": copy.deepcopy(candidate_declaration["files"]),
            "format": "files",
        }
    )
    validation = {
        "implementation": "builtin.workspace_policy",
        "config": {
            "allow": ["solver.py", "lib/*"],
            "deny": ["*.secret"],
            "requiredFiles": ["solver.py"],
        },
    }
    materialization = {
        "implementation": "builtin.workspace_bundle",
        "config": {
            "candidateOptions": {"mode": "safe"},
            "candidateRoot": "candidate",
            "entrypoint": "solver.py",
        },
    }
    candidate_contract = {
        "context": context,
        "format": "files",
        "materialization": materialization,
        "validation": validation,
    }
    evaluator = thaw_json(previous.evaluator_contract)
    evaluator["access_policy"] = "CodeAwareReadOnly"
    evaluator["mutation_policy"] = "TrialWorkspaceOnly"
    evaluator["adapter"]["config"]["candidate"] = copy.deepcopy(
        candidate_declaration
    )
    evaluator["adapter"]["config"]["context"] = copy.deepcopy(context)
    environment = replace(
        previous,
        candidate_contract=candidate_contract,
        evaluator_contract=evaluator,
    )
    definition = _replace_closure(definition, environment=environment)

    solver = b"def solve():\n    return 7\n"
    helper = b"VALUE = 3\n"
    spec = {
        "directories": ["lib"],
        "entrypoint": "solver.py",
        "files": [
            {
                "executable": False,
                "path": "lib/helper.py",
                "sha256": BlobRef.from_bytes(helper).digest,
                "sizeBytes": len(helper),
            },
            {
                "executable": False,
                "path": "solver.py",
                "sha256": BlobRef.from_bytes(solver).digest,
                "sizeBytes": len(solver),
            },
        ],
        "options": {"candidateRoot": "candidate", "mode": "safe"},
        "schema": "optpilot.sealed-file-candidate-spec.v1",
    }
    snapshot_ref = SnapshotRef.from_manifest_bytes(b"retained-file-candidate")
    envelope = NormalizedCandidateEnvelope.build(
        candidate_format="files",
        spec=spec,
        content_refs=(snapshot_ref,),
    )
    closure = definition.evaluation_closure
    template = closure.evaluation_template
    evaluation_spec = EvaluationSpec(
        environment_id=environment.environment_id,
        environment_revision_digest=environment.digest,
        prepared_runtime_digest=closure.prepared_runtime.digest,
        candidate_ref=str(envelope.candidate_ref),
        candidate={
            "candidate_id": "candidate-files-a",
            "format": "files",
            "generator": {"method_id": "method-a", "strategy": "external"},
            "lineage": {"parents": []},
            "materialization": copy.deepcopy(materialization),
            "spec": spec,
            "validation": copy.deepcopy(validation),
        },
        objective=thaw_json(template.objective),
        resource_profile=thaw_json(template.resource_profile),
        sandbox_spec=thaw_json(template.sandbox_spec),
        seed=template.default_seed,
        repetition_index=0,
        metadata={},
    )
    return (
        definition,
        evaluation_spec,
        CandidateRuntimeInput.from_envelope(envelope),
        solver,
        helper,
    )


def _binding_parts(spec: PortableAttemptRuntimeSpec):
    projection = ProjectionBindingEvidence(
        logical_name=spec.projection_name,
        access_enforcement="advisory",
        spec_digest=spec.projection_spec.digest,
        plan_digest="c" * 64,
        source_snapshots=tuple(
            mapping.snapshot_ref for mapping in spec.projection_spec.mappings
        ),
    )
    volumes = tuple(
        WritableVolumeEvidence(
            logical_name=requirement.name,
            quota=requirement.quota,
            policy=requirement.policy,
            quota_enforcement="advisory",
        )
        for requirement in spec.writable_volumes
    )
    provider = ExecutionProviderFacts(
        kind="process",
        sandbox_enforcement="advisory",
        platform=spec.provider.platform,
        builder_fingerprint=spec.provider.builder_fingerprint,
    )
    return provider, projection, volumes


class RuntimeBindingCompilerTest(unittest.TestCase):
    def assert_code(self, code: str, callback) -> RuntimeBindingCompileError:
        with self.assertRaises(RuntimeBindingCompileError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_compiles_retained_process_attempt_to_fixed_logical_scopes(self) -> None:
        definition = _definition()
        evaluation_spec = _evaluation_spec(definition)

        result = _compile(definition, evaluation_spec)

        self.assertEqual(result.run_definition_digest, definition.digest)
        self.assertEqual(result.evaluation_spec_digest, evaluation_spec.digest)
        self.assertEqual(
            result.environment_revision_digest,
            definition.evaluation_closure.environment_revision.digest,
        )
        self.assertEqual(
            result.prepared_runtime_digest,
            definition.evaluation_closure.prepared_runtime.digest,
        )
        self.assertEqual(result.provider.kind, "process")
        self.assertEqual(result.provider.portability, "provider-scoped")
        self.assertEqual(result.provider.platform, _provider().platform)
        self.assertEqual(
            result.provider.builder_fingerprint, _provider().builder_fingerprint
        )
        self.assertEqual(
            [
                (item.name, item.logical_path, item.access, item.source_kind)
                for item in result.scopes
            ],
            [
                (CONTROL_SCOPE, "/optpilot/control", "read-write", "volume"),
                (
                    ENVIRONMENT_SOURCE_SCOPE,
                    "/optpilot/environment_source",
                    "read",
                    "projection",
                ),
                (TRIAL_SCOPE, "/optpilot/trial", "read-write", "volume"),
            ],
        )
        self.assertEqual(
            result.entrypoint.to_dict()["module"],
            "env_impl",
        )
        self.assertEqual(result.entrypoint.to_dict()["attribute"], "evaluate")
        self.assertEqual(result.workdir, ScopePath(TRIAL_SCOPE, "."))
        self.assertEqual(result.evaluator_settings, {"target_x": 4.2, "target_y": 7})
        self.assertEqual(result.declared_metric_names, ("throughput", "cycle_time"))
        self.assertEqual(result.resources.to_dict()["memory_gib"], 1)
        self.assertEqual(result.requested_network_policy, "disabled")
        self.assertEqual(result.cleanup_policy, "always")
        self.assertEqual(result.read_only_scope_enforcement, "advisory")
        self.assertEqual(
            {item.name: item.quota_enforcement for item in result.writable_volumes},
            {CONTROL_SCOPE: "advisory", TRIAL_SCOPE: "advisory"},
        )
        self.assertTrue(
            all(item.quota.max_total_bytes > 0 for item in result.writable_volumes)
        )
        # Resource timeout (120s) is stricter than evaluator timeout (600s).
        self.assertEqual(result.timeout_seconds, 120.0)

        mappings = result.projection_spec.mappings
        self.assertEqual(len(mappings), 1)
        self.assertEqual(result.projection_name, ENVIRONMENT_PROJECTION)
        self.assertEqual(mappings[0].destination, "environment")
        self.assertEqual(mappings[0].source_subpath, ".")
        self.assertEqual(
            mappings[0].snapshot_ref,
            definition.evaluation_closure.environment_revision.source_layers[
                0
            ].snapshot_ref,
        )

    def test_trial_workspace_compiles_to_one_layered_trial_volume_alias(self) -> None:
        study, manifest, package = _study_with_trial_workspace()
        definition = _definition(
            study=study,
            package=package,
            manifest=manifest,
        )

        result = _compile(definition)

        trial_scope = next(item for item in result.scopes if item.name == TRIAL_SCOPE)
        self.assertIsInstance(trial_scope.source, LayeredVolumeScopeSource)
        self.assertEqual(trial_scope.source.volume_name, TRIAL_SCOPE)
        self.assertEqual(
            tuple(
                (layer.source_subpath, layer.destination_subpath, layer.precedence)
                for layer in trial_scope.source.lower_layers
            ),
            (
                ("environments/seeds/base.json", "inputs/base.json", 0),
                ("environments/fixtures", "fixtures", 1),
            ),
        )
        self.assertEqual(len(result.projection_spec.mappings), 1)
        mapping = result.projection_spec.mappings[0]
        self.assertEqual(mapping.snapshot_ref, package.snapshot_ref)
        self.assertEqual(mapping.source_subpath, ".")
        self.assertEqual(mapping.destination, "environment")
        self.assertEqual(PortableAttemptRuntimeSpec.from_dict(result.to_dict()), result)
        encoded = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("/provider/private", encoded)

    def test_explicit_process_sandbox_accepts_its_disabled_network_contract(self) -> None:
        study = _study()
        study.environment["runtime"] = {"networkPolicy": "disabled"}

        result = _compile(_definition(study=study))

        self.assertEqual(result.requested_network_policy, "disabled")

    def test_file_candidate_compiles_to_final_projected_replace_layer(self) -> None:
        (
            definition,
            evaluation_spec,
            candidate_input,
            _solver,
            _helper,
        ) = _file_definition_and_candidate_input()

        result = _compile(
            definition,
            evaluation_spec,
            candidate_input=candidate_input,
        )

        self.assertEqual(
            tuple(item.destination for item in result.projection_spec.mappings),
            (
                ENVIRONMENT_PROJECTION_PARTITION,
                CANDIDATE_PROJECTION_PARTITION,
            ),
        )
        candidate_mapping = result.projection_spec.mappings[-1]
        self.assertEqual(candidate_mapping.snapshot_ref, candidate_input.snapshot_ref)
        trial_scope = next(item for item in result.scopes if item.name == TRIAL_SCOPE)
        self.assertIsInstance(trial_scope.source, LayeredVolumeScopeSource)
        candidate_layer = trial_scope.source.lower_layers[-1]
        self.assertEqual(candidate_layer.collision_policy, "replace")
        self.assertEqual(
            candidate_layer.projection_subpath, CANDIDATE_PROJECTION_PARTITION
        )
        self.assertEqual(candidate_layer.destination_subpath, "candidate")
        self.assertEqual(candidate_layer.snapshot_ref, candidate_input.snapshot_ref)
        self.assertIsNotNone(result.file_materialization)
        self.assertEqual(result.file_materialization.root, ScopePath(TRIAL_SCOPE, "candidate"))
        self.assertEqual(result.file_materialization.entrypoint, "solver.py")
        self.assertEqual(
            result.file_materialization.required_files, ("solver.py",)
        )
        self.assertEqual(PortableAttemptRuntimeSpec.from_dict(result.to_dict()), result)
        descriptor = json.dumps(
            result.file_materialization.to_dict(), sort_keys=True
        )
        self.assertNotIn("tree:sha256:", descriptor)
        self.assertNotIn("candidate:sha256:", descriptor)
        self.assertNotIn("contentRef", descriptor)
        self.assertNotIn("snapshotRef", descriptor)
        self.assertNotIn("storeId", descriptor)
        self.assertNotIn("/private/", descriptor)

    def test_file_candidate_requires_exact_one_tree_and_matching_identity(self) -> None:
        definition, evaluation_spec, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )
        self.assert_code(
            "candidate_content_unavailable",
            lambda: _compile(definition, evaluation_spec),
        )

        spec = thaw_json(evaluation_spec.candidate["spec"])
        with self.assertRaisesRegex(ValueError, "exactly one tree snapshot"):
            CandidateRuntimeInput.from_envelope(
                NormalizedCandidateEnvelope.build(
                    candidate_format="files",
                    spec=spec,
                    content_refs=(
                        candidate_input.snapshot_ref,
                        SnapshotRef.from_manifest_bytes(b"other-tree"),
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "exactly one tree snapshot"):
            CandidateRuntimeInput.from_envelope(
                NormalizedCandidateEnvelope.build(
                    candidate_format="files",
                    spec=spec,
                    content_refs=(BlobRef.from_bytes(b"not-a-tree"),),
                )
            )

        other_envelope = NormalizedCandidateEnvelope.build(
            candidate_format="files",
            spec=spec,
            content_refs=(SnapshotRef.from_manifest_bytes(b"different-tree"),),
        )
        self.assert_code(
            "candidate_content_unavailable",
            lambda: _compile(
                definition,
                evaluation_spec,
                candidate_input=CandidateRuntimeInput.from_envelope(other_envelope),
            ),
        )

    def test_file_candidate_rejects_legacy_materialization_and_validation_fields(self) -> None:
        definition, evaluation_spec, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )

        cases = (
            ("materialization", "seedFiles", [], "candidate_materialization_unsupported"),
            (
                "validation",
                "allowAbsoluteContentRefs",
                False,
                "candidate_validation_unsupported",
            ),
        )
        for field_name, config_name, config_value, code in cases:
            with self.subTest(field_name=field_name, config_name=config_name):
                previous_environment = (
                    definition.evaluation_closure.environment_revision
                )
                candidate_contract = thaw_json(
                    previous_environment.candidate_contract
                )
                candidate_contract[field_name]["config"][config_name] = config_value
                environment = replace(
                    previous_environment,
                    candidate_contract=candidate_contract,
                )
                changed_definition = _replace_closure(
                    definition,
                    environment=environment,
                )
                candidate = thaw_json(evaluation_spec.candidate)
                candidate[field_name]["config"][config_name] = config_value
                changed_evaluation = replace(
                    evaluation_spec,
                    environment_revision_digest=environment.digest,
                    prepared_runtime_digest=(
                        changed_definition.evaluation_closure.prepared_runtime.digest
                    ),
                    candidate=candidate,
                )
                self.assert_code(
                    code,
                    lambda: _compile(
                        changed_definition,
                        changed_evaluation,
                        candidate_input=candidate_input,
                    ),
                )

    def test_file_materialization_dto_rejects_manifest_and_policy_tampering(self) -> None:
        definition, evaluation_spec, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )
        descriptor = _compile(
            definition,
            evaluation_spec,
            candidate_input=candidate_input,
        ).file_materialization
        self.assertIsNotNone(descriptor)
        base = descriptor.to_dict()

        def changed(callback):
            payload = copy.deepcopy(base)
            callback(payload)
            return lambda: FileCandidateMaterialization.from_dict(payload)

        cases = (
            (
                "duplicate-directory",
                changed(lambda value: value["directories"].append("lib")),
                "directories must be unique",
            ),
            (
                "duplicate-pattern",
                changed(
                    lambda value: value["allow_patterns"].append("solver.py")
                ),
                "patterns must be unique",
            ),
            (
                "file-directory-conflict",
                changed(
                    lambda value: value["files"].append(
                        {
                            "executable": False,
                            "path": "lib",
                            "sha256": "1" * 64,
                            "sizeBytes": 1,
                        }
                    )
                ),
                "both files and directories",
            ),
            (
                "missing-parent",
                changed(lambda value: value.__setitem__("directories", [])),
                "explicit parent directory",
            ),
            (
                "missing-entrypoint",
                changed(
                    lambda value: value.__setitem__(
                        "entrypoint", "missing.py"
                    )
                ),
                "entrypoint is absent",
            ),
            (
                "missing-required",
                changed(
                    lambda value: value.__setitem__(
                        "required_files", ["missing.py"]
                    )
                ),
                "required candidate files are absent",
            ),
            (
                "outside-allow",
                changed(
                    lambda value: value.__setitem__(
                        "allow_patterns", ["solver.py"]
                    )
                ),
                "outside the allow policy",
            ),
            (
                "denied",
                changed(
                    lambda value: value.__setitem__(
                        "deny_patterns", ["solver.py"]
                    )
                ),
                "denied by policy",
            ),
            (
                "reserved-option",
                changed(
                    lambda value: value["options"].__setitem__(
                        "nested", {"snapshotRef": "tree:sha256:" + "1" * 64}
                    )
                ),
                "reserved ref or path field",
            ),
        )
        for name, callback, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    callback()

        reordered = copy.deepcopy(base)
        reordered["files"].reverse()
        with self.assertRaisesRegex(ValueError, "not canonical"):
            FileCandidateMaterialization.from_dict(reordered)

    def test_layered_trial_volume_rejects_partial_projection_and_schema_tampering(self) -> None:
        study, manifest, package = _study_with_trial_workspace()
        definition = _definition(
            study=study,
            package=package,
            manifest=manifest,
        )
        result = _compile(definition)

        partial = result.to_dict()
        partial["projection_spec"]["mappings"][0]["source_subpath"] = (
            "environments"
        )
        with self.assertRaisesRegex(ValueError, "complete immutable trees"):
            PortableAttemptRuntimeSpec.from_dict(partial)

        extra = result.to_dict()
        trial = next(item for item in extra["scopes"] if item["name"] == TRIAL_SCOPE)
        trial["source"]["provider_path"] = "/private/trial"
        with self.assertRaisesRegex(ValueError, "fields differ"):
            PortableAttemptRuntimeSpec.from_dict(extra)

    def test_runtime_compiler_rejects_trial_layer_snapshot_and_contract_mismatch(self) -> None:
        study, manifest, package = _study_with_trial_workspace()
        definition = _definition(
            study=study,
            package=package,
            manifest=manifest,
        )
        closure = definition.evaluation_closure
        environment = closure.environment_revision
        layers = tuple(
            sorted(environment.attempt_input_layers, key=lambda item: item.precedence)
        )
        different_snapshot = SnapshotRef.from_manifest_bytes(b"different")
        mismatched_layer = replace(layers[0], snapshot_ref=different_snapshot)
        cases = (
            (
                "snapshot",
                replace(
                    environment,
                    attempt_input_layers=(mismatched_layer, *layers[1:]),
                ),
                "attempt_input_layers_unsupported",
            ),
            (
                "missing-contract",
                replace(environment, projection_contract={}),
                "attempt_input_contract_unsupported",
            ),
            (
                "contract-without-layers",
                replace(environment, attempt_input_layers=()),
                "attempt_input_contract_unsupported",
            ),
        )
        for name, changed_environment, code in cases:
            with self.subTest(name=name):
                changed_definition = _replace_closure(
                    definition,
                    environment=changed_environment,
                )
                self.assert_code(
                    code,
                    lambda changed_definition=changed_definition: _compile(
                        changed_definition
                    ),
                )
    def test_typed_environment_profile_does_not_change_evaluator_binding(self) -> None:
        study = _study()
        profile = {
            "id": "default",
            "label": "Inspect",
            "description": "Inspect the selected candidate.",
            "command": ["python", "-m", "viewer"],
            "cwd": ".",
            "env": {"VIEW_MODE": "inspect"},
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
                "readyPath": "/ready",
                "readyTimeoutSeconds": 10,
            },
            "accepts": {"selectionKinds": ["candidate"], "mediaTypes": []},
        }
        study.environment["adapter"]["config"]["interfaces"] = [profile]

        definition = _definition(study=study)
        result = _compile(definition)

        retained = definition.evaluation_closure.environment_revision.interface_profiles
        self.assertEqual(len(retained), 1)
        self.assertIsInstance(retained[0], InterfaceLaunchProfile)
        self.assertEqual(retained[0].to_dict(), profile)
        self.assertEqual(result.entrypoint.to_dict()["module"], "env_impl")

    def test_import_root_precedence_survives_rebasing_and_no_host_path_is_retained(self) -> None:
        study = _study()
        study.environment["adapter"]["config"]["evaluate"]["pythonPath"] = [
            "/private/mutable-checkout/environment"
        ]
        package = _package(
            environment_python_import_roots=("environments", "."),
            method_python_import_roots=("methods", "."),
        )
        definition = _definition(study=study, package=package)

        result = _compile(definition)

        self.assertEqual(
            result.python_import_roots,
            (
                ScopePath(ENVIRONMENT_SOURCE_SCOPE, "environments"),
                ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),
            ),
        )
        self.assertEqual(len(result.projection_spec.mappings), 1)
        encoded = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("/private/mutable-checkout", encoded)
        self.assertNotIn("/mutable/checkout", encoded)
        self.assertNotIn("run-owner-a/", encoded)

    def test_compiles_one_sealed_prepared_python_layer_as_final_import_root(self) -> None:
        definition = _definition_with_prepared_python()

        result = _compile(definition)

        prepared_layer = (
            definition.evaluation_closure.prepared_runtime.prepared_layers[0]
        )
        self.assertEqual(
            tuple(
                (item.destination, item.source_subpath, item.snapshot_ref)
                for item in result.projection_spec.mappings
            ),
            (
                (
                    ENVIRONMENT_PROJECTION_PARTITION,
                    ".",
                    definition.evaluation_closure.environment_revision.source_layers[
                        0
                    ].snapshot_ref,
                ),
                (
                    ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                    "site-packages",
                    prepared_layer.snapshot_ref,
                ),
            ),
        )
        prepared_scope = next(
            item
            for item in result.scopes
            if item.name == ENVIRONMENT_PREPARED_PYTHON_SCOPE
        )
        self.assertEqual(prepared_scope.logical_path, "/optpilot/environment_prepared_python")
        self.assertEqual(prepared_scope.access, "read")
        self.assertEqual(
            prepared_scope.source.relative_path,
            ENVIRONMENT_PREPARED_PYTHON_PARTITION,
        )
        self.assertEqual(
            result.python_import_roots[-1],
            ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "."),
        )
        self.assertEqual(PortableAttemptRuntimeSpec.from_dict(result.to_dict()), result)
        with self.assertRaisesRegex(ValueError, "environment source scope"):
            replace(
                result,
                entrypoint=replace(
                    result.entrypoint,
                    scope=ENVIRONMENT_PREPARED_PYTHON_SCOPE,
                ),
            )

    def test_prepared_python_layer_coexists_with_file_candidate_projection(self) -> None:
        definition, evaluation, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )
        definition = _definition_with_prepared_python(definition)
        evaluation = replace(
            evaluation,
            prepared_runtime_digest=(
                definition.evaluation_closure.prepared_runtime.digest
            ),
        )

        result = _compile(
            definition,
            evaluation,
            candidate_input=candidate_input,
        )

        self.assertEqual(
            tuple(item.destination for item in result.projection_spec.mappings),
            (
                ENVIRONMENT_PROJECTION_PARTITION,
                ENVIRONMENT_PREPARED_PYTHON_PARTITION,
                CANDIDATE_PROJECTION_PARTITION,
            ),
        )

    def test_rejects_noncanonical_prepared_python_layer_and_import_order(self) -> None:
        definition = _definition_with_prepared_python()
        closure = definition.evaluation_closure
        prepared = closure.prepared_runtime.prepared_layers[0]
        bad_layer = replace(prepared, source_subpath=".")
        self.assert_code(
            "prepared_layers_unsupported",
            lambda: _compile(
                _replace_closure(
                    definition,
                    runtime=replace(
                        closure.prepared_runtime,
                        prepared_layers=(bad_layer,),
                    ),
                )
            ),
        )

        settings = thaw_json(closure.prepared_runtime.runtime_settings)
        settings["import_roots"] = list(reversed(settings["import_roots"]))
        self.assert_code(
            "runtime_settings_unsupported",
            lambda: _compile(
                _replace_closure(
                    definition,
                    runtime=replace(
                        closure.prepared_runtime,
                        runtime_settings=settings,
                    ),
                )
            ),
        )

    def test_portable_spec_round_trip_is_strict_and_canonical(self) -> None:
        result = _compile()
        payload = result.to_dict()

        restored = PortableAttemptRuntimeSpec.from_dict(payload)

        self.assertEqual(restored, result)
        self.assertEqual(restored.digest, result.digest)
        self.assertEqual(
            replace(result, scopes=tuple(reversed(result.scopes))).digest,
            result.digest,
        )
        with self.assertRaisesRegex(ValueError, "fields differ"):
            PortableAttemptRuntimeSpec.from_dict({**payload, "host_path": "/tmp/x"})
        with self.assertRaisesRegex(ValueError, "/optpilot"):
            PortableRuntimeScope(
                "source",
                "/tmp/source",
                "read",
                ProjectionScopeSource(ENVIRONMENT_PROJECTION),
            )
        noncanonical = copy.deepcopy(payload)
        noncanonical["timeout_seconds"] = 120
        with self.assertRaisesRegex(ValueError, "not canonical"):
            PortableAttemptRuntimeSpec.from_dict(noncanonical)

        overlapping = PortableRuntimeScope(
            "nested-source",
            "/optpilot/environment_source/nested",
            "read",
            ProjectionScopeSource(ENVIRONMENT_PROJECTION),
        )
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            replace(result, scopes=(*result.scopes, overlapping))

        trial_scope = next(item for item in result.scopes if item.name == TRIAL_SCOPE)
        wrong_volume_source = replace(
            trial_scope,
            source=VolumeScopeSource(CONTROL_SCOPE),
        )
        with self.assertRaisesRegex(ValueError, "volume scopes differ"):
            replace(
                result,
                scopes=tuple(
                    wrong_volume_source if item.name == TRIAL_SCOPE else item
                    for item in result.scopes
                ),
            )

        owner_definition = _definition()
        with self.assertRaisesRegex(RuntimeBindingCompileError, "path-free"):
            compile_retained_process_attempt_runtime(
                owner_id="/private/tmp/leak",
                run_definition=owner_definition,
                evaluation_spec=_evaluation_spec(owner_definition),
                provider=_provider(),
            )

    def test_provider_and_evaluation_mismatches_fail_with_stable_codes(self) -> None:
        definition = _definition()
        evaluation_spec = _evaluation_spec(definition)
        mismatched_provider = ProcessProviderIdentity(
            builder_fingerprint="e" * 64,
            platform=_provider().platform,
        )
        self.assert_code(
            "provider_mismatch",
            lambda: _compile(definition, evaluation_spec, mismatched_provider),
        )

        wrong_revision = replace(
            evaluation_spec,
            environment_revision_digest="f" * 64,
        )
        self.assert_code(
            "evaluation_anchor_mismatch",
            lambda: _compile(definition, wrong_revision),
        )

        objective = thaw_json(evaluation_spec.objective)
        objective["primaryMetric"]["direction"] = "minimize"
        wrong_policy = replace(evaluation_spec, objective=objective)
        self.assert_code(
            "evaluation_policy_mismatch",
            lambda: _compile(definition, wrong_policy),
        )

        for field_name in ("validation", "materialization"):
            candidate = thaw_json(evaluation_spec.candidate)
            candidate[field_name] = {"type": "substituted"}
            wrong_contract = replace(evaluation_spec, candidate=candidate)
            self.assert_code(
                "candidate_contract_mismatch",
                lambda: _compile(definition, wrong_contract),
            )

    def test_spoofed_compiler_backend_and_unrepresented_fields_are_rejected(self) -> None:
        definition = _definition()
        closure = definition.evaluation_closure

        wrong_compiler = replace(
            closure.environment_revision,
            compiler_id="untrusted.compiler",
        )
        self.assert_code(
            "compiler_unsupported",
            lambda: _compile(
                _replace_closure(definition, environment=wrong_compiler)
            ),
        )

        execution = thaw_json(definition.execution_policy)
        execution["backend"]["implementation"] = "untrusted.backend"
        self.assert_code(
            "backend_unsupported",
            lambda: _compile(replace(definition, execution_policy=execution)),
        )

        evaluator_contract = thaw_json(
            closure.environment_revision.evaluator_contract
        )
        evaluator_contract["adapter"]["config"]["evaluate"]["hostPath"] = (
            "/private/tmp/leak"
        )
        extra_evaluator_field = replace(
            closure.environment_revision,
            evaluator_contract=evaluator_contract,
        )
        self.assert_code(
            "runtime_shape_unsupported",
            lambda: _compile(
                _replace_closure(
                    definition,
                    environment=extra_evaluator_field,
                )
            ),
        )

        sandbox = thaw_json(closure.evaluation_template.sandbox_spec)
        sandbox["mounts"] = []
        template = replace(closure.evaluation_template, sandbox_spec=sandbox)
        execution = thaw_json(definition.execution_policy)
        execution["defaults"]["sandboxSpec"] = sandbox
        extra_sandbox_field = replace(
            definition,
            evaluation_closure=RunEvaluationClosure(
                closure.environment_revision,
                closure.prepared_runtime,
                template,
            ),
            execution_policy=execution,
        )
        self.assert_code(
            "runtime_shape_unsupported",
            lambda: _compile(extra_sandbox_field),
        )

    def test_first_slice_rejects_env_layers_inputs_and_portability_extensions(self) -> None:
        definition = _definition()
        closure = definition.evaluation_closure
        source = closure.environment_revision.source_layers[0]

        prepared_runtime = replace(
            closure.prepared_runtime,
            prepared_layers=(
                ScopeLayer(
                    source.scope,
                    source.snapshot_ref,
                    destination_subpath="prepared",
                ),
            ),
        )
        with_prepared = _replace_closure(definition, runtime=prepared_runtime)
        self.assert_code(
            "prepared_layers_unsupported",
            lambda: _compile(with_prepared),
        )

        environment_with_input = replace(
            closure.environment_revision,
            attempt_input_layers=(
                ScopeLayer("trial-input", source.snapshot_ref),
            ),
        )
        with_input = _replace_closure(
            definition, environment=environment_with_input
        )
        self.assert_code(
            "attempt_input_contract_unsupported",
            lambda: _compile(with_input),
        )

        environment_with_two_sources = replace(
            closure.environment_revision,
            source_layers=(
                source,
                ScopeLayer("second-source", source.snapshot_ref),
            ),
        )
        with_two_sources = _replace_closure(
            definition, environment=environment_with_two_sources
        )
        self.assert_code(
            "source_layers_unsupported",
            lambda: _compile(with_two_sources),
        )

        portable_runtime = replace(
            closure.prepared_runtime,
            portability="portable",
            platform=None,
            builder_fingerprint=None,
        )
        portable = _replace_closure(definition, runtime=portable_runtime)
        self.assert_code(
            "runtime_portability_unsupported",
            lambda: _compile(portable),
        )

        study = _study()
        study.environment["adapter"]["config"]["evaluate"]["env"] = {
            "MODE": "inspect"
        }
        with_env = _definition(study=study)
        self.assert_code(
            "evaluator_environment_unsupported",
            lambda: _compile(with_env),
        )


class ExecutionBindingEvidenceTest(unittest.TestCase):
    def test_file_projection_evidence_uses_canonical_two_source_set(self) -> None:
        definition, evaluation, candidate_input, *_ = (
            _file_definition_and_candidate_input()
        )
        spec = _compile(
            definition,
            evaluation,
            candidate_input=candidate_input,
        )
        provider, projection, volumes = _binding_parts(spec)

        evidence = ExecutionBindingEvidence.create(
            spec,
            provider=provider,
            projections=(projection,),
            writable_volumes=volumes,
        )

        self.assertEqual(
            evidence.projections[0].source_snapshots,
            tuple(
                sorted(
                    {
                        mapping.snapshot_ref
                        for mapping in spec.projection_spec.mappings
                    },
                    key=str,
                )
            ),
        )

    def test_evidence_cross_anchors_round_trips_and_stays_path_free(self) -> None:
        spec = _compile()
        provider, projection, volumes = _binding_parts(spec)

        evidence = ExecutionBindingEvidence.create(
            spec,
            provider=provider,
            projections=(projection,),
            writable_volumes=volumes,
        )
        restored = ExecutionBindingEvidence.from_dict(evidence.to_dict())

        self.assertEqual(restored, evidence)
        self.assertEqual(restored.fingerprint, evidence.fingerprint)
        self.assertEqual(restored.portable_spec_digest, spec.digest)
        self.assertEqual(restored.logical_map, spec.scopes)
        encoded = json.dumps(restored.to_dict(), sort_keys=True)
        self.assertNotIn("/tmp/", encoded)
        self.assertNotIn("/private/", encoded)
        self.assertNotIn("secret_value", encoded)
        self.assertNotIn("binding_id", encoded)
        self.assertNotIn("realization_id", encoded)
        self.assertNotIn("volume_id", encoded)
        self.assertTrue(
            all("provider_kind" not in item for item in restored.to_dict()["writable_volumes"])
        )
        self.assertTrue(
            all("provider_kind" not in item for item in restored.to_dict()["projections"])
        )
        reordered = ExecutionBindingEvidence.create(
            spec,
            provider=provider,
            projections=(projection,),
            writable_volumes=tuple(reversed(volumes)),
        )
        self.assertEqual(reordered.fingerprint, evidence.fingerprint)

    def test_create_rejects_provider_projection_volume_and_secret_substitution(self) -> None:
        spec = _compile()
        provider, projection, volumes = _binding_parts(spec)

        wrong_provider = replace(provider, builder_fingerprint="e" * 64)
        with self.assertRaisesRegex(ValueError, "provider differs"):
            ExecutionBindingEvidence.create(
                spec,
                provider=wrong_provider,
                projections=(projection,),
                writable_volumes=volumes,
            )

        wrong_projection = replace(projection, spec_digest="e" * 64)
        with self.assertRaisesRegex(ValueError, "projection evidence differs"):
            ExecutionBindingEvidence.create(
                spec,
                provider=provider,
                projections=(wrong_projection,),
                writable_volumes=volumes,
            )

        with self.assertRaisesRegex(ValueError, "volume evidence differs"):
            ExecutionBindingEvidence.create(
                spec,
                provider=provider,
                projections=(projection,),
                writable_volumes=volumes[:-1],
            )

        wrong_quota = replace(
            volumes[0],
            quota=FilesystemQuota(
                max_entries=volumes[0].quota.max_entries,
                max_file_bytes=volumes[0].quota.max_file_bytes,
                max_total_bytes=volumes[0].quota.max_total_bytes + 1,
            ),
        )
        with self.assertRaisesRegex(ValueError, "volume evidence differs"):
            ExecutionBindingEvidence.create(
                spec,
                provider=provider,
                projections=(projection,),
                writable_volumes=(wrong_quota, *volumes[1:]),
            )

        strict_requirements = tuple(
            replace(item, quota_enforcement="enforced")
            if item.name == volumes[0].logical_name
            else item
            for item in spec.writable_volumes
        )
        strict_spec = replace(spec, writable_volumes=strict_requirements)
        with self.assertRaisesRegex(ValueError, "weaker than required"):
            ExecutionBindingEvidence.create(
                strict_spec,
                provider=provider,
                projections=(projection,),
                writable_volumes=volumes,
            )

        enforcing_spec = replace(spec, read_only_scope_enforcement="enforced")
        with self.assertRaisesRegex(ValueError, "weaker than required"):
            ExecutionBindingEvidence.create(
                enforcing_spec,
                provider=provider,
                projections=(projection,),
                writable_volumes=volumes,
            )

    def test_nested_evidence_records_are_strict(self) -> None:
        spec = _compile()
        provider, projection, volumes = _binding_parts(spec)
        evidence = ExecutionBindingEvidence.create(
            spec,
            provider=provider,
            projections=(projection,),
            writable_volumes=volumes,
        )
        payload = evidence.to_dict()
        payload["writable_volumes"][0]["host_path"] = "/tmp/trial"

        with self.assertRaisesRegex(ValueError, "fields differ"):
            ExecutionBindingEvidence.from_dict(payload)

        unrelated = SnapshotRef.from_manifest_bytes(b"unrelated")
        wrong_source = replace(projection, source_snapshots=(unrelated,))
        with self.assertRaisesRegex(ValueError, "projection evidence differs"):
            ExecutionBindingEvidence.create(
                spec,
                provider=provider,
                projections=(wrong_source,),
                writable_volumes=volumes,
            )


if __name__ == "__main__":
    unittest.main()
