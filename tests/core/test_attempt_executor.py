from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from optpilot.attempts import (
    AttemptEnvelope,
    AttemptExecutor,
    AttemptFinalization,
    AttemptWorkspaceBinding,
    CapturedArtifact,
    EvaluationSpec,
    OutputDeclaration,
)
from optpilot.candidate_materialization import MaterializationRecord, ValidationReport


JsonDict = Dict[str, Any]


class _Validator:
    def __init__(self, calls: List[str], report: ValidationReport | None = None, error: Exception | None = None):
        self.calls = calls
        self.report = report or ValidationReport(
            accepted=True,
            metadata={"implementation": "test.validator"},
        )
        self.error = error

    def validate(self, candidate: JsonDict, context: JsonDict) -> ValidationReport:
        self.calls.append("validate")
        if self.error:
            raise self.error
        self.last_candidate = candidate
        self.last_context = context
        return self.report


class _Materializer:
    def __init__(self, calls: List[str], error: Exception | None = None):
        self.calls = calls
        self.error = error

    def materialize(self, candidate: JsonDict, workspace: Path, context: JsonDict) -> MaterializationRecord:
        self.calls.append("materialize")
        if self.error:
            raise self.error
        self.last_candidate = candidate
        self.last_workspace = workspace
        self.last_context = context
        return MaterializationRecord(
            runtime_spec={"x": candidate["spec"]["x"], "workspace": str(workspace)},
            output_files=[
                {
                    "declaration_id": "materializer:materialized",
                    "name": "materialized",
                    "path": "materialized.json",
                    "kind": "file",
                    "media_type": "application/json",
                    "metadata": {"producer": "materializer"},
                }
            ],
            metadata={"implementation": "test.materializer", "candidate_file_count": 0},
        )


class _Adapter:
    def __init__(self, calls: List[str], result: Any = None, error: Exception | None = None):
        self.calls = calls
        self.result = result if result is not None else {
            "status": "success",
            "metric_values": {"score": 7.5},
            "constraint_results": {"feasible": True},
            "output_files": [
                {
                    "declaration_id": "environment:result",
                    "name": "result",
                    "path": "declared/result.json",
                    "kind": "file",
                    "media_type": "application/json",
                    "metadata": {"producer": "environment"},
                }
            ],
            "event_summary": {"adapter": "test.environment"},
        }
        self.error = error

    def evaluate(self, runtime_spec: JsonDict, context: JsonDict) -> JsonDict:
        self.calls.append("evaluate")
        if self.error:
            raise self.error
        self.last_runtime_spec = runtime_spec
        self.last_context = context
        return self.result


class AttemptExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve() / "attempt"
        self.workspace.mkdir()
        self.spec = EvaluationSpec(
            environment_id="environment-a",
            environment_revision_digest="a" * 64,
            prepared_runtime_digest="b" * 64,
            candidate_ref="candidate:sha256:" + "a" * 64,
            candidate={
                "candidate_id": "candidate-a",
                "format": "parameters",
                "spec": {"x": 3},
                "lineage": {"parents": []},
                "generator": {"method_id": "method-a", "strategy": "test"},
                "validation": {"implementation": "test.validator", "config": {}},
                "materialization": {"implementation": "test.materializer", "config": {}},
            },
            objective={"primaryMetric": {"name": "score", "direction": "maximize"}},
            resource_profile={"cpu": 1, "memoryGiB": 2, "timeoutSeconds": 30},
            sandbox_spec={"runtimeType": "process", "networkPolicy": "disabled"},
            seed=11,
            repetition_index=2,
            metadata={"purpose": "semantic-test"},
        )
        self.binding = AttemptWorkspaceBinding(
            binding_id="binding-a",
            workspace=self.workspace,
            backend_identity={"implementation": "builtin.local_subprocess_backend"},
            backend_worker={"handle": "handle-a"},
            context={"projection_generation": 4},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _clock(start: float = 10.0, finish: float = 10.25):
        values = iter((start, finish))
        return lambda: next(values)

    def test_success_returns_immutable_neutral_execution_results(self) -> None:
        calls: List[str] = []
        validator = _Validator(calls)
        materializer = _Materializer(calls)
        adapter = _Adapter(calls)
        executor = AttemptExecutor(
            validator,
            materializer,
            adapter,
            clock=self._clock(),
        )

        envelope = executor.execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(calls, ["validate", "materialize", "evaluate"])
        self.assertEqual(envelope.outcome, "success")
        self.assertEqual(envelope.attempt_id, "attempt-a")
        self.assertEqual(envelope.evaluation_spec_digest, self.spec.digest)
        self.assertEqual(envelope.binding_id, "binding-a")
        self.assertRegex(envelope.digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(envelope.phase, "environment_evaluation")
        self.assertEqual(envelope.wall_clock_seconds, 0.25)
        self.assertEqual(dict(envelope.metric_values), {"score": 7.5})
        self.assertEqual(dict(envelope.constraint_results), {"feasible": True})
        self.assertEqual(
            [item.name for item in envelope.output_declarations],
            ["materialized", "result"],
        )
        self.assertEqual(envelope.event_summary["primary_metric"], "score")
        self.assertEqual(
            envelope.event_summary["materialization"]["implementation"],
            "test.materializer",
        )
        self.assertEqual(envelope.execution_metadata["seed"], 11)
        self.assertEqual(envelope.execution_metadata["repetition_index"], 2)
        self.assertEqual(envelope.execution_metadata["binding_id"], "binding-a")
        self.assertEqual(envelope.execution_metadata["candidate_ref"], self.spec.candidate_ref)
        self.assertEqual(
            envelope.execution_metadata["environment_revision_digest"],
            self.spec.environment_revision_digest,
        )
        self.assertEqual(
            envelope.execution_metadata["prepared_runtime_digest"],
            self.spec.prepared_runtime_digest,
        )
        self.assertEqual(materializer.last_workspace, self.workspace)
        self.assertEqual(adapter.last_runtime_spec["x"], 3)
        self.assertEqual(adapter.last_context["attempt_id"], "attempt-a")
        self.assertEqual(adapter.last_context["evaluation_spec_digest"], self.spec.digest)
        self.assertNotIn("trial_id", adapter.last_context)
        self.assertEqual(adapter.last_context["workspace"], str(self.workspace))
        self.assertEqual(adapter.last_context["projection_generation"], 4)

        with self.assertRaises(TypeError):
            envelope.metric_values["score"] = 1  # type: ignore[index]
        with self.assertRaises(TypeError):
            envelope.execution_metadata["new"] = True  # type: ignore[index]
        payload = envelope.to_dict()
        self.assertNotIn("candidate_record", payload)
        self.assertNotIn("trial_record", payload)
        self.assertNotIn("observation_record", payload)
        self.assertNotIn("canonical_trial_id", payload)
        payload["metric_values"]["score"] = -1
        self.assertEqual(envelope.metric_values["score"], 7.5)

    def test_rejected_candidate_stops_before_materialization(self) -> None:
        calls: List[str] = []
        report = ValidationReport(
            accepted=False,
            errors=["x is outside the environment schema"],
            metadata={"implementation": "test.validator"},
        )
        envelope = AttemptExecutor(
            _Validator(calls, report=report),
            _Materializer(calls),
            _Adapter(calls),
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(calls, ["validate"])
        self.assertEqual(envelope.outcome, "invalid")
        self.assertEqual(envelope.phase, "validation")
        self.assertEqual(envelope.error["type"], "ValidationError")
        self.assertTrue(envelope.materialization["metadata"]["skipped"])
        self.assertEqual(envelope.output_declarations, ())

    def test_validation_exception_becomes_failed_envelope(self) -> None:
        calls: List[str] = []
        envelope = AttemptExecutor(
            _Validator(calls, error=RuntimeError("validator exploded")),
            _Materializer(calls),
            _Adapter(calls),
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(calls, ["validate"])
        self.assertEqual(envelope.outcome, "failed")
        self.assertEqual(envelope.phase, "validation")
        self.assertEqual(envelope.error["type"], "RuntimeError")
        self.assertIn("validator exploded", envelope.error["message"])
        self.assertFalse(envelope.validation["accepted"])
        self.assertIn("exception", envelope.validation["metadata"])

    def test_materialization_exception_and_timeout_match_existing_statuses(self) -> None:
        for error, expected in (
            (RuntimeError("materializer exploded"), "failed"),
            (subprocess.TimeoutExpired(["materializer"], 3), "timeout"),
        ):
            with self.subTest(expected=expected):
                calls: List[str] = []
                envelope = AttemptExecutor(
                    _Validator(calls),
                    _Materializer(calls, error=error),
                    _Adapter(calls),
                    clock=self._clock(),
                ).execute(self.spec, self.binding, attempt_id="attempt-a")

                self.assertEqual(calls, ["validate", "materialize"])
                self.assertEqual(envelope.outcome, expected)
                self.assertEqual(envelope.phase, "materialization")
                self.assertTrue(envelope.materialization["metadata"]["failed"])
                self.assertEqual(envelope.error["phase"], "materialization")

    def test_environment_exception_and_malformed_result_become_failed_envelopes(self) -> None:
        scenarios = (
            (_Adapter([], error=RuntimeError("environment exploded")), "RuntimeError"),
            (_Adapter([], result=["not", "a", "mapping"]), "TypeError"),
        )
        for adapter, error_type in scenarios:
            with self.subTest(error_type=error_type):
                calls = adapter.calls
                envelope = AttemptExecutor(
                    _Validator(calls),
                    _Materializer(calls),
                    adapter,
                    clock=self._clock(),
                ).execute(self.spec, self.binding, attempt_id="attempt-a")

                self.assertEqual(calls, ["validate", "materialize", "evaluate"])
                self.assertEqual(envelope.outcome, "failed")
                self.assertEqual(envelope.phase, "environment_evaluation")
                self.assertEqual(envelope.error["type"], error_type)
                self.assertEqual(
                    [item.name for item in envelope.output_declarations],
                    ["materialized"],
                )

    def test_unsupported_environment_status_uses_existing_normalization(self) -> None:
        calls: List[str] = []
        adapter = _Adapter(
            calls,
            result={
                "status": "invented-status",
                "metric_values": {"score": 99},
                "constraint_results": {},
                "output_files": [
                    {
                        "declaration_id": "environment:raw",
                        "name": "raw",
                        "path": "declared/raw.log",
                        "kind": "file",
                        "media_type": "text/plain",
                        "metadata": {},
                    }
                ],
                "event_summary": {},
            },
        )
        envelope = AttemptExecutor(
            _Validator(calls),
            _Materializer(calls),
            adapter,
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(envelope.outcome, "failed")
        self.assertEqual(envelope.metric_values, {})
        self.assertEqual(envelope.error["code"], "unsupported_observation_status")
        self.assertEqual(
            [item.name for item in envelope.output_declarations],
            ["materialized", "raw"],
        )

    def test_executor_has_no_evidence_or_output_retention_side_effects(self) -> None:
        calls: List[str] = []
        declared_source = self.workspace / "declared-but-not-created.json"
        adapter = _Adapter(
            calls,
            result={
                "status": "success",
                "metric_values": {"score": 1},
                "constraint_results": {},
                "output_files": [
                    {
                        "declaration_id": "environment:declared",
                        "name": "declared",
                        "path": "declared-but-not-created.json",
                        "kind": "file",
                        "media_type": "application/json",
                        "metadata": {},
                    }
                ],
                "event_summary": {},
            },
        )

        envelope = AttemptExecutor(
            _Validator(calls),
            _Materializer(calls),
            adapter,
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(envelope.outcome, "success")
        self.assertFalse(declared_source.exists())
        self.assertFalse((self.workspace / "candidates.jsonl").exists())
        self.assertFalse((self.workspace / "trials.jsonl").exists())
        self.assertFalse((self.workspace / "observations.jsonl").exists())
        self.assertFalse((self.workspace / "evidence_files").exists())
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_binding_requires_an_existing_absolute_non_symlink_directory(self) -> None:
        with self.assertRaises(ValueError):
            AttemptWorkspaceBinding("relative", Path("relative-workspace"))
        with self.assertRaises(ValueError):
            AttemptWorkspaceBinding("missing", self.workspace / "missing")
        with self.assertRaises(ValueError):
            AttemptWorkspaceBinding(
                "reserved-context",
                self.workspace,
                context={"workspace": "/replacement"},
            )

    def test_spec_and_envelope_transport_round_trip_is_strict(self) -> None:
        spec_payload = self.spec.to_dict()
        restored_spec = EvaluationSpec.from_dict(spec_payload)
        self.assertEqual(restored_spec, self.spec)
        self.assertEqual(restored_spec.digest, self.spec.digest)
        with self.assertRaisesRegex(ValueError, "fields differ"):
            EvaluationSpec.from_dict({**spec_payload, "unexpected": True})
        with self.assertRaisesRegex(ValueError, "schema is unsupported"):
            EvaluationSpec.from_dict(
                {**spec_payload, "schema_version": "optpilot.evaluation-spec.v999"}
            )
        with self.assertRaisesRegex(ValueError, "environment_revision_digest"):
            EvaluationSpec.from_dict(
                {**spec_payload, "environment_revision_digest": "not-a-digest"}
            )
        with self.assertRaisesRegex(ValueError, "prepared_runtime_digest"):
            EvaluationSpec.from_dict(
                {**spec_payload, "prepared_runtime_digest": "A" * 64}
            )

        other_runtime = EvaluationSpec.from_dict(
            {**spec_payload, "prepared_runtime_digest": "c" * 64}
        )
        self.assertNotEqual(other_runtime.digest, self.spec.digest)

        calls: List[str] = []
        envelope = AttemptExecutor(
            _Validator(calls),
            _Materializer(calls),
            _Adapter(calls),
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")
        envelope_payload = envelope.to_dict()
        restored_envelope = AttemptEnvelope.from_dict(envelope_payload)
        self.assertEqual(restored_envelope, envelope)
        self.assertEqual(restored_envelope.digest, envelope.digest)
        with self.assertRaisesRegex(ValueError, "fields differ"):
            AttemptEnvelope.from_dict({**envelope_payload, "status": "failed"})
        with self.assertRaisesRegex(ValueError, "fields differ"):
            tampered = dict(envelope_payload)
            tampered.pop("phase")
            AttemptEnvelope.from_dict(tampered)

    def test_output_declarations_are_strict_portable_and_policy_free(self) -> None:
        declaration = OutputDeclaration(
            declaration_id="environment:plot",
            name="plot",
            path="reports/plot.png",
            kind="file",
            media_type="image/png",
            metadata={"producer_hint": "untrusted"},
        )
        self.assertEqual(OutputDeclaration.from_dict(declaration.to_dict()), declaration)
        self.assertNotIn("visibility", declaration.to_dict())
        self.assertNotIn("required", declaration.to_dict())
        with self.assertRaises(TypeError):
            declaration.metadata["new"] = True  # type: ignore[index]

        for path in (
            "/private/host-output.json",
            "../escape.json",
            "reports/../../escape.json",
            "C:\\host\\output.json",
            "./report.json",
            "reports//report.json",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "safe portable"):
                OutputDeclaration(
                    declaration_id="environment:unsafe",
                    name="unsafe",
                    path=path,
                )

        with self.assertRaisesRegex(ValueError, "fields differ"):
            OutputDeclaration.from_dict(
                {**declaration.to_dict(), "visibility": "method"}
            )

    def test_invalid_environment_declaration_is_normalized_as_attempt_failure(self) -> None:
        calls: List[str] = []
        adapter = _Adapter(
            calls,
            result={
                "status": "success",
                "metric_values": {"score": 1},
                "constraint_results": {},
                "output_files": [
                    {
                        "declaration_id": "environment:unsafe",
                        "name": "unsafe",
                        "path": "/host/result.json",
                        "kind": "file",
                        "media_type": "application/json",
                        "metadata": {},
                    }
                ],
                "event_summary": {},
            },
        )
        envelope = AttemptExecutor(
            _Validator(calls),
            _Materializer(calls),
            adapter,
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")

        self.assertEqual(envelope.outcome, "failed")
        self.assertEqual(envelope.phase, "environment_evaluation")
        self.assertEqual(envelope.error["type"], "ValueError")
        self.assertEqual(
            [item.declaration_id for item in envelope.output_declarations],
            ["materializer:materialized"],
        )

    def test_capture_and_finalization_are_strict_neutral_transports(self) -> None:
        calls: List[str] = []
        envelope = AttemptExecutor(
            _Validator(calls),
            _Materializer(calls),
            _Adapter(calls),
            clock=self._clock(),
        ).execute(self.spec, self.binding, attempt_id="attempt-a")
        declaration = envelope.output_declarations[1]
        content_ref = "blob:sha256:" + "b" * 64
        capture = CapturedArtifact(
            declaration=declaration,
            content_ref=content_ref,
            size_bytes=128,
            bindings=(
                {"store_id": "local-a", "content_ref": content_ref},
                {"store_id": "mirror-b", "content_ref": content_ref},
            ),
            visibility="method",
            metadata={"capture": "verified"},
        )
        finalization = AttemptFinalization(
            attempt_id=envelope.attempt_id,
            evaluation_spec_digest=envelope.evaluation_spec_digest,
            binding_id=envelope.binding_id,
            effective_outcome="success",
            effective_code=None,
            captured_artifacts=(capture,),
            envelope=envelope,
        )

        restored = AttemptFinalization.from_dict(finalization.to_dict())
        self.assertEqual(restored, finalization)
        self.assertEqual(restored.digest, finalization.digest)
        self.assertRegex(finalization.digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(capture.visibility, "method")
        self.assertEqual(capture.bindings[1]["store_id"], "mirror-b")
        with self.assertRaises(TypeError):
            capture.bindings[0]["store_id"] = "changed"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "binding content_ref must match"):
            CapturedArtifact(
                declaration=declaration,
                content_ref=content_ref,
                size_bytes=1,
                bindings=(
                    {
                        "store_id": "local-a",
                        "content_ref": "blob:sha256:" + "c" * 64,
                    },
                ),
                visibility="operator",
            )

    def test_platform_failure_finalization_is_explicit_and_exclusive(self) -> None:
        finalization = AttemptFinalization(
            attempt_id="attempt-platform-failure",
            evaluation_spec_digest=self.spec.digest,
            binding_id=self.binding.binding_id,
            effective_outcome="failed",
            effective_code="worker_lost",
            captured_artifacts=(),
            platform_error={
                "code": "worker_lost",
                "message": "The worker disappeared before an envelope was returned.",
                "details": {"retryable": True},
            },
        )
        self.assertEqual(
            AttemptFinalization.from_dict(finalization.to_dict()),
            finalization,
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            AttemptFinalization(
                attempt_id="attempt-no-result",
                evaluation_spec_digest=self.spec.digest,
                binding_id=self.binding.binding_id,
                effective_outcome="failed",
                effective_code="missing_result",
                captured_artifacts=(),
            )

    def test_candidate_compatibility_metadata_is_optional_but_must_be_structured(self) -> None:
        payload = self.spec.to_dict()
        payload["candidate"] = {
            "candidate_id": "candidate-minimal",
            "format": "parameters",
            "spec": {"x": 1},
        }
        minimal = EvaluationSpec.from_dict(payload)
        self.assertEqual(minimal.candidate_id, "candidate-minimal")
        payload["candidate"]["lineage"] = "not-a-mapping"
        with self.assertRaisesRegex(ValueError, "candidate.lineage"):
            EvaluationSpec.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
