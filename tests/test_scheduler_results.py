"""Focused checks for WP1A logical-trial scheduler results."""

from __future__ import annotations

import unittest

from optpilot.models import Observation, ResourceProfile, SandboxSpec, TrialSpec
from optpilot.scheduler import LocalTrialScheduler


class _EvidenceStore:
    def __init__(self) -> None:
        self.events = []

    def record_scheduler_event(self, event) -> None:
        self.events.append(event)


class _RetryBackend:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, trial_spec: TrialSpec) -> str:
        attempt = int(trial_spec.metadata.get("attempt_index", 1))
        handle = f"handle-{trial_spec.trial_id}-{attempt}"
        self.submitted.append((handle, trial_spec))
        return handle

    def collect(self, handle: str):
        attempt = int(handle.rsplit("-", 1)[1])
        return [_observation("failed" if attempt == 1 else "success", attempt)]

    def status(self, handle: str):
        return {"state": "completed", "worker": {"handle": handle}}


class _PartialSubmitFailureBackend:
    def __init__(self) -> None:
        self.submit_count = 0
        self.cancelled = []
        self.drained = []

    def submit(self, trial_spec: TrialSpec) -> str:
        self.submit_count += 1
        if self.submit_count == 2:
            raise RuntimeError("dispatch unavailable")
        return f"handle-{trial_spec.trial_id}"

    def cancel(self, handle: str) -> None:
        self.cancelled.append(handle)

    def collect(self, handle: str):
        self.drained.append(handle)
        return []

    def status(self, handle: str):
        return {"state": "cancelled", "worker": {"handle": handle}}


def _observation(status: str, attempt: int) -> Observation:
    return Observation(
        trial_id="trial-a",
        study_id="study-a",
        candidate_id="candidate-a",
        environment_id="environment-a",
        status=status,
        metric_values={"score": 1.0} if status == "success" else {},
        constraint_results={},
        resource_usage={},
        output_files=[],
        event_summary={},
        provenance={"attempt_index": attempt},
    )


def _trial_spec(trial_id: str, candidate_id: str) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        study_id="study-a",
        method_id="method-a",
        candidate={"candidate_id": candidate_id, "format": "parameters", "spec": {}},
        objective={},
        resource_profile=ResourceProfile(),
        sandbox_spec=SandboxSpec(),
    )


class LogicalTrialSchedulerResultTests(unittest.TestCase):
    def test_retries_return_one_logical_result_with_every_attempt_observation(self) -> None:
        evidence = _EvidenceStore()
        backend = _RetryBackend()
        scheduler = LocalTrialScheduler(
            {
                "implementation": "builtin.local_scheduler",
                "config": {
                    "retryPolicy": {
                        "maxAttempts": 2,
                        "retryStatuses": ["failed"],
                    }
                },
            },
            backend,
            evidence,
        )
        spec = _trial_spec("trial-a", "candidate-a")

        results = scheduler.run_batch([spec])

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.logical_trial_id, "trial-a")
        self.assertEqual(result.candidate_id, "candidate-a")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(result.observation_count, 2)
        self.assertEqual(
            [attempt.observations[0].status for attempt in result.attempts],
            ["failed", "success"],
        )
        self.assertEqual([item.status for item in result.final_observations], ["success"])
        self.assertEqual(len(backend.submitted), 2)

        collected = evidence.events[-1]
        self.assertEqual(collected["event"], "batch_collected")
        self.assertEqual(collected["trial_count"], 1)
        self.assertEqual(collected["attempt_count"], 2)
        self.assertEqual(collected["observation_count"], 2)
        self.assertEqual(collected["final_observation_count"], 1)
        self.assertEqual(collected["handles"][0]["attempt_count"], 2)

    def test_partial_submission_failure_cancels_drains_and_preserves_zero_attempt_slots(self) -> None:
        evidence = _EvidenceStore()
        backend = _PartialSubmitFailureBackend()
        scheduler = LocalTrialScheduler(
            {"implementation": "builtin.local_scheduler", "config": {}},
            backend,
            evidence,
        )

        results = scheduler.run_batch(
            [
                _trial_spec("trial-a", "candidate-a"),
                _trial_spec("trial-b", "candidate-b"),
                _trial_spec("trial-c", "candidate-c"),
            ]
        )

        self.assertEqual([result.logical_trial_id for result in results], ["trial-a", "trial-b", "trial-c"])
        self.assertEqual([result.attempt_count for result in results], [1, 0, 0])
        self.assertTrue(all(result.error for result in results))
        self.assertEqual(backend.cancelled, ["handle-trial-a"])
        self.assertEqual(backend.drained, ["handle-trial-a"])
        self.assertEqual(evidence.events[0]["event"], "batch_submission_failed")
        self.assertEqual(evidence.events[-1]["event"], "batch_collected")
        self.assertTrue(evidence.events[-1]["aborted"])
        self.assertEqual(evidence.events[-1]["attempt_count"], 1)
        self.assertEqual(evidence.events[-1]["observation_count"], 0)


if __name__ == "__main__":
    unittest.main()
