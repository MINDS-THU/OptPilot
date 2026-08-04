from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.config import (
    _normalize_candidate,
    _public_candidate_context,
    compile_authoring_config,
)
from optpilot.method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.refs import SnapshotRef
from optpilot.realm.run_closure import (
    RUN_ATTEMPT_INPUT_ROLE,
    RUN_ENVIRONMENT_SOURCE_ROLE,
    RUN_PREPARED_RUNTIME_ROLE,
    EnvironmentRevisionManifest,
    PreparedEnvironmentRuntimeManifest,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.run_definition import (
    RUN_DEFINITION_MANIFEST_SCHEMA,
    RUN_METHOD_SOURCE_ROLE,
    RUN_PREPARED_METHOD_RUNTIME_ROLE,
    MethodRevisionManifest,
    PreparedMethodRuntimeManifest,
    RunDefinitionManifest,
)
from optpilot.spec import StudySpec, study_spec_from_raw
from optpilot.study_realm_compiler import (
    CANDIDATE_NORMALIZER_VERSION,
    STUDY_REALM_COMPILER_VERSION,
    StudyRealmCompileError,
    compile_study_run_definition,
    expected_retained_environment_contract,
    expected_retained_method_contract,
)


_ROOT = Path(__file__).resolve().parents[1]
_STUDY = _ROOT / "tests/fixtures/catalog/studies/toy_random_search.yaml"
_CLI_STUDY = _ROOT / "tests/fixtures/catalog/studies/toy_cli_random_search.yaml"


def _tree(label: str) -> SnapshotRef:
    return SnapshotRef.from_manifest_bytes(label.encode("utf-8"))


def _study(path: Path = _STUDY) -> StudySpec:
    raw = compile_authoring_config(path)
    # Deliberately use a path that must never enter the compiled definition.
    return study_spec_from_raw(Path("/mutable/authoring/study.yaml"), raw)


def _environment_inputs(
    study: StudySpec,
    *,
    environment_id: str | None = None,
    candidate_contract: dict | None = None,
    evaluator_contract: dict | None = None,
) -> tuple[EnvironmentRevisionManifest, PreparedEnvironmentRuntimeManifest]:
    environment = EnvironmentRevisionManifest(
        environment_id=environment_id or study.environment["environmentId"],
        compiler_id="test.environment-compiler",
        compiler_version="1",
        authored_config=ScopePath("environment-source", "environment.yaml"),
        source_layers=(
            ScopeLayer("environment-source", _tree("retained-environment-source")),
        ),
        attempt_input_layers=(
            ScopeLayer("attempt-input", _tree("retained-attempt-input")),
        ),
        evaluator_contract=(
            evaluator_contract
            if evaluator_contract is not None
            else expected_retained_environment_contract(study)
        ),
        candidate_contract=(
            candidate_contract
            if candidate_contract is not None
            else copy.deepcopy(study.candidate)
        ),
    )
    runtime = PreparedEnvironmentRuntimeManifest(
        environment_revision_digest=environment.digest,
        runtime_kind="process",
        runtime_settings={"interpreter": "managed"},
        prepared_layers=(
            ScopeLayer("environment-runtime", _tree("prepared-environment-runtime")),
        ),
        workdir=ScopePath("environment-source", "."),
        portability="portable",
    )
    return environment, runtime


def _method_inputs(
    study: StudySpec,
    *,
    source_label: str = "retained-method-source",
    runtime_label: str = "prepared-method-runtime",
    runtime_settings: dict | None = None,
) -> tuple[MethodRevisionManifest, PreparedMethodRuntimeManifest]:
    method = MethodRevisionManifest(
        method_id=study.method["id"],
        protocol=study.method["implementation"]["protocol"],
        compiler_id="test.method-compiler",
        compiler_version="1",
        authored_config=ScopePath("method-source", "method.yaml"),
        source_layers=(ScopeLayer("method-source", _tree(source_label)),),
        method_contract=expected_retained_method_contract(study),
    )
    runtime = PreparedMethodRuntimeManifest(
        method_revision_digest=method.digest,
        runtime_kind=study.method["runtime"].get("type", "process"),
        runtime_settings=(
            runtime_settings
            if runtime_settings is not None
            else {"interpreter": "managed"}
        ),
        prepared_layers=(ScopeLayer("method-runtime", _tree(runtime_label)),),
        workdir=ScopePath("method-source", "."),
        portability="portable",
    )
    return method, runtime


def _compile(study: StudySpec) -> RunDefinitionManifest:
    environment, environment_runtime = _environment_inputs(study)
    method, method_runtime = _method_inputs(study)
    return compile_study_run_definition(
        study,
        environment_revision=environment,
        prepared_environment_runtime=environment_runtime,
        method_revision=method,
        prepared_method_runtime=method_runtime,
    )


class StudyRealmCompilerTest(unittest.TestCase):
    def assert_compile_code(self, code: str, callback) -> StudyRealmCompileError:
        with self.assertRaises(StudyRealmCompileError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_omitted_candidate_description_has_one_canonical_representation(self) -> None:
        candidate = _normalize_candidate(
            {
                "format": "parameters",
                "parameters": {
                    "schema": {
                        "x": {"valueType": "float", "min": 0.0, "max": 1.0}
                    }
                },
            }
        )

        self.assertEqual(candidate["description"], "")
        self.assertEqual(_public_candidate_context(candidate), candidate)

    def test_compiles_complete_path_free_definition_control_and_capacity(self) -> None:
        study = _study()
        first = _compile(study)
        second = _compile(study)

        self.assertEqual(first.to_dict()["schema"], RUN_DEFINITION_MANIFEST_SCHEMA)
        self.assertEqual(first.compiler_version, STUDY_REALM_COMPILER_VERSION)
        self.assertEqual(
            first.run_control_manifest.normalizer_version,
            CANDIDATE_NORMALIZER_VERSION,
        )
        self.assertEqual(first.run_control_manifest.proposal_width, 4)
        self.assertEqual(first.evaluator_capacity, 4)
        self.assertEqual(first.run_control_manifest.max_trials, 12)
        self.assertIsNone(first.run_control_manifest.max_failures)
        self.assertEqual(first.run_control_manifest.convergence.patience_trials, 12)
        self.assertEqual(first.run_control_manifest.convergence.min_delta, 0.0)
        self.assertEqual(first.run_control_manifest.retry_policy.max_attempts, 1)
        self.assertEqual(
            first.run_control_manifest.retry_policy.retryable_outcomes,
            ("failed", "timeout"),
        )
        self.assertEqual(
            first.evaluation_closure.evaluation_template.to_dict()["objective"],
            study.objective,
        )
        self.assertEqual(first.evaluation_closure.evaluation_template.default_seed, 7)
        self.assertEqual(first.digest, second.digest)

        encoded = json.dumps(first.to_dict(), sort_keys=True)
        self.assertNotIn(str(study.path), encoded)
        self.assertNotIn(study.method["configBaseDir"], encoded)
        self.assertNotIn("studyConfigPath", encoded)
        self.assertEqual(
            MethodRevisionManifest.from_bytes(first.method_revision.to_bytes()),
            first.method_revision,
        )
        self.assertEqual(
            PreparedMethodRuntimeManifest.from_bytes(
                first.prepared_method_runtime.to_bytes()
            ),
            first.prepared_method_runtime,
        )
        self.assertEqual(RunDefinitionManifest.from_dict(first.to_dict()), first)
        self.assertEqual(RunDefinitionManifest.from_bytes(first.to_bytes()), first)

    def test_method_exchange_timeout_is_retained_with_method_runtime(self) -> None:
        study = _study()
        self.assertEqual(study.method["runtime"]["exchangeTimeoutSeconds"], 10)
        self.assertEqual(
            expected_retained_method_contract(study)["runtime_requirements"][
                "exchangeTimeoutSeconds"
            ],
            10,
        )

        study.method["runtime"]["exchangeTimeoutSeconds"] = 71
        definition = _compile(study)
        self.assertEqual(
            definition.method_revision.method_contract["runtime_requirements"][
                "exchangeTimeoutSeconds"
            ],
            71,
        )

    def test_proposal_width_is_explicit_and_not_evaluator_capacity(self) -> None:
        study = _study()
        study.method["config"]["batchSize"] = 2
        study.method["settings"]["batchSize"] = 2
        study.execution["parallelism"]["candidateParallelism"] = 7

        definition = _compile(study)

        self.assertEqual(definition.run_control_manifest.proposal_width, 2)
        self.assertEqual(definition.evaluator_capacity, 7)

        del study.method["config"]["batchSize"]
        del study.method["settings"]["batchSize"]
        self.assert_compile_code("proposal_width_missing", lambda: _compile(study))

    def test_proposal_width_cannot_exceed_durable_batch_limit(self) -> None:
        study = _study()
        study.method["config"]["batchSize"] = MAX_BATCH_EXCHANGE_ITEMS
        study.method["settings"]["batchSize"] = MAX_BATCH_EXCHANGE_ITEMS
        self.assertEqual(
            _compile(study).run_control_manifest.proposal_width,
            MAX_BATCH_EXCHANGE_ITEMS,
        )

        study.method["config"]["batchSize"] = MAX_BATCH_EXCHANGE_ITEMS + 1
        study.method["settings"]["batchSize"] = MAX_BATCH_EXCHANGE_ITEMS + 1
        self.assert_compile_code(
            "proposal_width_too_large", lambda: _compile(study)
        )

    def test_rejects_environment_candidate_and_all_objective_mismatches(self) -> None:
        study = _study()
        environment, environment_runtime = _environment_inputs(
            study, environment_id="other"
        )
        method, method_runtime = _method_inputs(study)
        self.assert_compile_code(
            "environment_revision_mismatch",
            lambda: compile_study_run_definition(
                study,
                environment_revision=environment,
                prepared_environment_runtime=environment_runtime,
                method_revision=method,
                prepared_method_runtime=method_runtime,
            ),
        )

        changed_candidate = copy.deepcopy(study.candidate)
        changed_candidate["validation"]["config"]["enforceBounds"] = False
        environment, environment_runtime = _environment_inputs(
            study, candidate_contract=changed_candidate
        )
        method, method_runtime = _method_inputs(study)
        self.assert_compile_code(
            "candidate_contract_mismatch",
            lambda: compile_study_run_definition(
                study,
                environment_revision=environment,
                prepared_environment_runtime=environment_runtime,
                method_revision=method,
                prepared_method_runtime=method_runtime,
            ),
        )

        study = _study()
        study.objective["primaryMetric"]["name"] = "not-declared"
        self.assert_compile_code(
            "objective_environment_incompatible", lambda: _compile(study)
        )

        study = _study()
        study.objective["secondaryMetrics"] = ["cycle_time"]
        self.assertEqual(
            _compile(study).evaluation_closure.evaluation_template.objective[
                "secondaryMetrics"
            ],
            ("cycle_time",),
        )
        study.objective["secondaryMetrics"] = ["not-declared"]
        self.assert_compile_code(
            "objective_environment_incompatible", lambda: _compile(study)
        )

    def test_rechecks_method_environment_compatibility(self) -> None:
        study = _study()
        study.method["compatibility"]["formats"] = ["files"]
        self.assert_compile_code(
            "method_environment_incompatible", lambda: _compile(study)
        )

    def test_method_semantics_source_and_prepared_runtime_change_identity(self) -> None:
        study = _study()
        environment, environment_runtime = _environment_inputs(study)
        method, method_runtime = _method_inputs(study)
        original = compile_study_run_definition(
            study,
            environment_revision=environment,
            prepared_environment_runtime=environment_runtime,
            method_revision=method,
            prepared_method_runtime=method_runtime,
        )

        changed_source, changed_source_runtime = _method_inputs(
            study, source_label="different-method-source"
        )
        source_changed = compile_study_run_definition(
            study,
            environment_revision=environment,
            prepared_environment_runtime=environment_runtime,
            method_revision=changed_source,
            prepared_method_runtime=changed_source_runtime,
        )
        self.assertNotEqual(original.method_revision.digest, changed_source.digest)
        self.assertNotEqual(original.digest, source_changed.digest)

        changed_runtime = PreparedMethodRuntimeManifest(
            method_revision_digest=method.digest,
            runtime_kind="process",
            runtime_settings={"interpreter": "managed", "lock": "different"},
            prepared_layers=(
                ScopeLayer("method-runtime", _tree("different-prepared-runtime")),
            ),
            workdir=ScopePath("method-source", "."),
        )
        runtime_changed = compile_study_run_definition(
            study,
            environment_revision=environment,
            prepared_environment_runtime=environment_runtime,
            method_revision=method,
            prepared_method_runtime=changed_runtime,
        )
        self.assertNotEqual(method_runtime.digest, changed_runtime.digest)
        self.assertNotEqual(original.digest, runtime_changed.digest)

        study.method["config"]["selectionStrategy"] = "uncertainty"
        study.method["settings"]["selectionStrategy"] = "uncertainty"
        self.assert_compile_code(
            "method_revision_mismatch",
            lambda: compile_study_run_definition(
                study,
                environment_revision=environment,
                prepared_environment_runtime=environment_runtime,
                method_revision=method,
                prepared_method_runtime=method_runtime,
            ),
        )
        changed_method, changed_method_runtime = _method_inputs(study)
        semantic_changed = compile_study_run_definition(
            study,
            environment_revision=environment,
            prepared_environment_runtime=environment_runtime,
            method_revision=changed_method,
            prepared_method_runtime=changed_method_runtime,
        )
        self.assertNotEqual(original.method_revision.digest, changed_method.digest)
        self.assertNotEqual(original.digest, semantic_changed.digest)

    def test_rejects_stale_or_wrong_kind_prepared_method_runtime(self) -> None:
        study = _study()
        environment, environment_runtime = _environment_inputs(study)
        method, _ = _method_inputs(study)
        stale = PreparedMethodRuntimeManifest(
            method_revision_digest="0" * 64,
            runtime_kind="process",
            runtime_settings={},
        )
        self.assert_compile_code(
            "method_runtime_mismatch",
            lambda: compile_study_run_definition(
                study,
                environment_revision=environment,
                prepared_environment_runtime=environment_runtime,
                method_revision=method,
                prepared_method_runtime=stale,
            ),
        )

        wrong_kind = PreparedMethodRuntimeManifest(
            method_revision_digest=method.digest,
            runtime_kind="container",
            runtime_settings={},
            oci_image_digest="sha256:" + "1" * 64,
        )
        self.assert_compile_code(
            "method_runtime_mismatch",
            lambda: compile_study_run_definition(
                study,
                environment_revision=environment,
                prepared_environment_runtime=environment_runtime,
                method_revision=method,
                prepared_method_runtime=wrong_kind,
            ),
        )

    def test_all_retained_policies_and_metadata_change_definition_identity(self) -> None:
        study = _study()
        original = _compile(study)
        self.assertEqual(set(original.evidence_policy), {"capture", "retention"})

        study.execution["parallelism"]["candidateParallelism"] = 3
        execution_changed = _compile(study)
        self.assertNotEqual(original.digest, execution_changed.digest)

        study.evidence["capture"]["methodCalls"] = False
        evidence_changed = _compile(study)
        self.assertNotEqual(execution_changed.digest, evidence_changed.digest)
        self.assertFalse(evidence_changed.evidence_policy["capture"]["methodCalls"])

        study.reproducibility["recordModelInvocations"] = True
        reproducibility_changed = _compile(study)
        self.assertNotEqual(evidence_changed.digest, reproducibility_changed.digest)

        study.metadata["description"] = "Changed retained metadata"
        metadata_changed = _compile(study)
        self.assertNotEqual(reproducibility_changed.digest, metadata_changed.digest)

        study.evidence["outputDir"] = "/mutable/run-output"
        self.assert_compile_code("legacy_host_path", lambda: _compile(study))

        study = _study()
        study.method["compatibility"]["requiredContext"] = [
            "candidate.parameters.missing"
        ]
        self.assert_compile_code(
            "method_environment_incompatible", lambda: _compile(study)
        )

        study = _study()
        study.method["compatibility"]["requiredCapabilities"] = ["gpu-sim"]
        self.assert_compile_code(
            "method_environment_incompatible", lambda: _compile(study)
        )

    def test_required_content_refs_cover_environment_and_method_closures(self) -> None:
        definition = _compile(_study())
        refs = definition.content_refs_by_role

        self.assertEqual(set(refs), {
            RUN_ENVIRONMENT_SOURCE_ROLE,
            RUN_ATTEMPT_INPUT_ROLE,
            RUN_PREPARED_RUNTIME_ROLE,
            RUN_METHOD_SOURCE_ROLE,
            RUN_PREPARED_METHOD_RUNTIME_ROLE,
        })
        self.assertEqual(
            {str(ref) for ref in refs[RUN_METHOD_SOURCE_ROLE]},
            {str(_tree("retained-method-source"))},
        )
        self.assertEqual(
            {str(ref) for ref in refs[RUN_PREPARED_METHOD_RUNTIME_ROLE]},
            {str(_tree("prepared-method-runtime"))},
        )
        self.assertEqual(
            definition.to_dict()["required_content_refs"],
            [
                {"content_ref": str(ref), "role": role}
                for role, ref in definition.required_content_refs
            ],
        )

    def test_retry_and_stopping_are_mapped_exactly_or_rejected(self) -> None:
        study = _study()
        study.execution["scheduler"]["config"]["retryPolicy"] = {
            "maxAttempts": 3,
            "retryStatuses": ["partial", "timeout", "failed"],
        }
        study.execution["defaults"]["retryPolicy"] = {"maxRetries": 2}
        study.stopping["maxFailures"] = 5
        study.stopping["convergenceRule"]["config"] = {
            "patienceTrials": 3,
            "minDelta": 0.25,
        }

        definition = _compile(study)

        self.assertEqual(definition.run_control_manifest.max_failures, 5)
        self.assertEqual(
            definition.run_control_manifest.convergence.patience_trials, 3
        )
        self.assertEqual(definition.run_control_manifest.convergence.min_delta, 0.25)
        self.assertEqual(definition.run_control_manifest.retry_policy.max_attempts, 3)
        self.assertEqual(
            definition.run_control_manifest.retry_policy.retryable_outcomes,
            ("failed", "partial", "timeout"),
        )

        study.execution["defaults"]["retryPolicy"]["maxRetries"] = 1
        self.assert_compile_code("retry_policy_mismatch", lambda: _compile(study))

        study = _study()
        study.stopping["maxWallClockSeconds"] = 60
        self.assert_compile_code(
            "stopping_policy_unsupported", lambda: _compile(study)
        )

        study = _study()
        study.stopping["convergenceRule"]["implementation"] = "custom.stop"
        self.assert_compile_code(
            "stopping_policy_unsupported", lambda: _compile(study)
        )

    def test_rejects_legacy_adapter_path_and_protocol_assumptions(self) -> None:
        cli_study = _study(_CLI_STUDY)
        self.assert_compile_code(
            "legacy_environment_adapter",
            lambda: expected_retained_environment_contract(cli_study),
        )

        study = _study()
        study.environment["adapter"]["config"]["evaluate"]["pythonPath"] = [
            "/mutable/checkout"
        ]
        self.assert_compile_code(
            "legacy_host_path",
            lambda: expected_retained_environment_contract(study),
        )

        study = _study()
        study.environment["runtime"]["setup"] = {
            "steps": [{"uses": "uv", "requirements": ["requirements.txt"]}]
        }
        self.assert_compile_code(
            "legacy_host_path",
            lambda: expected_retained_environment_contract(study),
        )

        study = _study()
        study.method["implementation"]["protocol"] = "optpilot.method.session.v1"
        self.assert_compile_code(
            "method_protocol_unsupported", lambda: _compile(study)
        )

    def test_definition_round_trip_rejects_unknown_tampered_and_large_records(self) -> None:
        definition = _compile(_study())
        payload = definition.to_dict()
        payload["unexpected"] = True
        with self.assertRaisesRegex(RealmIntegrityError, "fields differ"):
            RunDefinitionManifest.from_dict(payload)

        payload = definition.to_dict()
        payload["method_revision_digest"] = "0" * 64
        with self.assertRaisesRegex(RealmIntegrityError, "method revision digest"):
            RunDefinitionManifest.from_dict(payload)

        payload = definition.to_dict()
        payload["prepared_method_runtime"]["method_revision_digest"] = "0" * 64
        payload["prepared_method_runtime_digest"] = PreparedMethodRuntimeManifest.from_dict(
            payload["prepared_method_runtime"]
        ).digest
        with self.assertRaisesRegex(
            RealmIntegrityError, "targets a different method revision"
        ):
            RunDefinitionManifest.from_dict(payload)

        payload = definition.to_dict()
        payload["required_content_refs"].pop()
        with self.assertRaisesRegex(RealmIntegrityError, "not canonical"):
            RunDefinitionManifest.from_dict(payload)

        with self.assertRaisesRegex(RealmIntegrityError, "canonical JSON"):
            RunDefinitionManifest.from_bytes(
                json.dumps(definition.to_dict(), indent=2).encode("utf-8")
            )

        with self.assertRaisesRegex(ValueError, "maximum encoded size"):
            replace(definition, metadata={"description": "x" * (1024 * 1024)})

    def test_definition_revalidates_policy_cross_anchors(self) -> None:
        definition = _compile(_study())

        payload = definition.to_dict()
        payload["execution_policy"]["parallelism"]["candidateParallelism"] = 99
        with self.assertRaisesRegex(
            RealmIntegrityError, "execution policy and evaluator capacity differ"
        ):
            RunDefinitionManifest.from_dict(payload)

        payload = definition.to_dict()
        payload["execution_policy"]["defaults"]["sandboxSpec"]["runtimeType"] = (
            "container"
        )
        with self.assertRaisesRegex(
            RealmIntegrityError, "evaluation sandbox spec differ"
        ):
            RunDefinitionManifest.from_dict(payload)

        payload = definition.to_dict()
        payload["run_control_manifest"]["objective"]["metric"] = "other"
        with self.assertRaises(RealmIntegrityError):
            RunDefinitionManifest.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
