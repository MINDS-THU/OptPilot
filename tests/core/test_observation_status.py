"""Focused tests for evaluator observation-status normalization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from optpilot.candidate_materialization import MaterializationRecord, ValidationReport
from optpilot.evidence import EvidenceView
from optpilot.execution import Evaluator, PUBLIC_OBSERVATION_STATUSES, _validate_environment_result
from optpilot.models import ResourceProfile, SandboxSpec, TrialSpec


class _RecordingEvidenceStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.candidates = []
        self.trials = []
        self.observations = []

    def create_trial_workspace(self, trial_id: str, *, attempt_index: int) -> Path:
        workspace = self.run_dir / "trial_workspaces" / trial_id / f"attempt-{attempt_index}"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def record_candidate(self, payload):
        self.candidates.append(payload)

    def record_trial(self, payload):
        self.trials.append(payload)

    def record_observation(self, payload):
        self.observations.append(payload)


class _EvidenceStore:
    def __init__(self, observations):
        self._observations = observations

    def read_observations(self):
        return self._observations

    def read_candidates(self):
        return []

    def read_method_calls(self):
        return []


class _AcceptingValidator:
    def validate(self, candidate, context):
        return ValidationReport(accepted=True)


class _PassthroughMaterializer:
    def materialize(self, candidate, workspace, context):
        return MaterializationRecord(runtime_spec=dict(candidate["spec"]), metadata={"workspace": str(workspace)})


class _StaticEnvironmentAdapter:
    def __init__(self, result):
        self.result = result

    def evaluate(self, candidate_runtime, context):
        return self.result


class ObservationStatusTests(unittest.TestCase):
    def test_public_status_vocabulary_matches_the_documented_six_outcomes(self) -> None:
        self.assertEqual(
            PUBLIC_OBSERVATION_STATUSES,
            {"success", "invalid", "failed", "timeout", "partial", "cancelled"},
        )

    def test_valid_evaluator_statuses_are_returned_unchanged(self) -> None:
        for status in sorted(PUBLIC_OBSERVATION_STATUSES):
            with self.subTest(status=status):
                result = {
                    "status": status,
                    "metric_values": {"score": 3.0},
                    "constraint_results": {"feasible": True},
                    "output_files": [{"name": "trace", "path": "trace.json"}],
                    "event_summary": {"source": "test"},
                }

                validated = _validate_environment_result(result)

                self.assertIs(validated, result)

    def test_unknown_status_is_failed_before_trial_and_observation_evidence(self) -> None:
        evaluator_result = {
            "status": "degraded",
            "metric_values": {"score": 999.0},
            "constraint_results": {"feasible": True},
            "output_files": [{"name": "debug_trace", "path": "debug.json"}],
            "event_summary": {
                "source": "custom_evaluator",
                "error": {"phase": "environment", "message": "original diagnostic"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = _RecordingEvidenceStore(Path(tmp_dir))
            evaluator = Evaluator(
                SimpleNamespace(
                    environment={"environmentId": "env-test", "environmentVersion": "1"},
                    evidence={},
                ),
                _StaticEnvironmentAdapter(evaluator_result),
                store,
                _PassthroughMaterializer(),
                _AcceptingValidator(),
            )
            trial_spec = TrialSpec(
                trial_id="trial-unknown-status",
                study_id="study-test",
                method_id="method-test",
                candidate={"candidate_id": "candidate-test", "format": "parameters", "spec": {"x": 1}},
                objective={"primaryMetric": {"name": "score", "direction": "maximize"}},
                resource_profile=ResourceProfile(),
                sandbox_spec=SandboxSpec(),
            )

            observations = evaluator.run_trial(trial_spec)

        self.assertEqual(len(observations), 1)
        observation = observations[0].to_dict()
        self.assertEqual(store.trials[0]["status"], "failed")
        self.assertEqual(store.observations[0]["status"], "failed")
        self.assertEqual(observation["status"], "failed")
        self.assertEqual(observation["metric_values"], {})
        self.assertEqual(observation["constraint_results"], {})
        self.assertEqual(observation["output_files"], evaluator_result["output_files"])

        diagnostic = observation["event_summary"]["error"]
        self.assertEqual(diagnostic["code"], "unsupported_observation_status")
        self.assertEqual(diagnostic["raw_status"], "degraded")
        self.assertEqual(diagnostic["phase"], "environment_evaluation")
        self.assertEqual(observation["event_summary"]["source"], "custom_evaluator")
        self.assertEqual(
            observation["event_summary"]["errors"][1],
            evaluator_result["event_summary"]["error"],
        )
        self.assertNotIn("raw_status", observation)
        self.assertNotIn("raw_status", store.trials[0])

    def test_method_evidence_view_hides_operator_only_raw_status_details(self) -> None:
        operator_observation = {
            "trial_id": "trial-raw-status",
            "candidate_id": "candidate-raw-status",
            "status": "failed",
            "metric_values": {},
            "output_files": [],
            "event_summary": {
                "errors": [
                    {
                        "code": "unsupported_observation_status",
                        "raw_status": "provider-private-state",
                        "message": "unsupported 'provider-private-state'",
                    }
                ]
            },
        }
        view = EvidenceView(
            _EvidenceStore([operator_observation]),
            SimpleNamespace(
                primary_metric_name="score",
                primary_metric_direction="maximize",
            ),
        )

        observations = view.observations()
        context = view.decision_context()

        self.assertIn("provider-private-state", str(operator_observation))
        self.assertNotIn("provider-private-state", str(observations))
        self.assertNotIn("provider-private-state", str(context))
        self.assertEqual(
            context["recent_failures"][0]["errors"][0]["code"],
            "unsupported_observation_status",
        )


if __name__ == "__main__":
    unittest.main()
