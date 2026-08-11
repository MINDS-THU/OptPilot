"""Focused invariants for the pure WP1A batch run controller."""

from __future__ import annotations

import itertools
import unittest

from optpilot.run_controller import MethodProtocolError, RunController


def _candidate(candidate_id: str, value: float = 1.0):
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
    }


def _observation(candidate_id: str, status: str = "success", score: float | None = 1.0):
    metrics = {} if score is None else {"score": score}
    return {
        "candidate_id": candidate_id,
        "status": status,
        "metric_values": metrics,
    }


def _controller(**overrides):
    ids = itertools.count(1)
    arguments = {
        "method_id": "method-a",
        "candidate_contract": {
            "format": "parameters",
            "validation": {},
            "materialization": {},
        },
        "objective_metric": "score",
        "objective_direction": "maximize",
        "proposal_width": 2,
        "max_trials": 5,
        "logical_trial_id_factory": lambda: f"logical-{next(ids)}",
    }
    arguments.update(overrides)
    return RunController(**arguments)


class RunControllerTests(unittest.TestCase):
    def test_two_when_only_one_requested_rejects_without_acceptance_or_budget(self):
        controller = _controller(proposal_width=2, max_trials=1)
        self.assertEqual(controller.next_proposal_width, 1)

        with self.assertRaises(MethodProtocolError) as captured:
            controller.accept_proposal([_candidate("candidate-a"), _candidate("candidate-b")])

        self.assertEqual(captured.exception.code, "batch_overproduced")
        self.assertEqual(controller.accepted_logical_trials, 0)
        self.assertEqual(controller.remaining_trials, 1)
        self.assertEqual(controller.accepted_candidate_ids, ())
        self.assertEqual(controller.run_status, "failed")
        self.assertEqual(controller.stop_code, "protocol_error")
        self.assertEqual(controller.rejections[0].details["requested_width"], 1)

    def test_malformed_and_duplicate_items_reject_the_whole_proposal_atomically(self):
        cases = (
            (
                [_candidate("candidate-a"), {"format": "parameters", "spec": {"x": 2}}],
                "candidate_malformed",
            ),
            (
                [_candidate("candidate-a"), _candidate("candidate-a", 2)],
                "duplicate_candidate_id",
            ),
        )
        for proposal, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                controller = _controller()
                with self.assertRaises(MethodProtocolError) as captured:
                    controller.accept_proposal(proposal)
                self.assertEqual(captured.exception.code, expected_code)
                self.assertEqual(controller.accepted_logical_trials, 0)
                self.assertEqual(controller.remaining_trials, 5)
                self.assertEqual(controller.logical_trials, ())
                self.assertEqual(controller.summary()["candidate_count"], 0)

    def test_candidate_ids_are_unique_across_all_proposals_in_the_run(self):
        controller = _controller(max_trials=3)
        first = controller.accept_proposal([_candidate("candidate-a")])
        controller.record_completion(first[0].logical_trial_id, _observation("candidate-a"))

        with self.assertRaises(MethodProtocolError) as captured:
            controller.accept_proposal([_candidate("candidate-b"), _candidate("candidate-a")])

        self.assertEqual(captured.exception.code, "duplicate_candidate_id")
        self.assertEqual(controller.accepted_logical_trials, 1)
        self.assertEqual(controller.accepted_candidate_ids, ("candidate-a",))
        self.assertEqual(controller.remaining_trials, 2)
        self.assertEqual(controller.run_status, "failed")
        self.assertEqual(controller.stop_code, "protocol_error")

    def test_acceptance_preserves_the_current_normalized_public_candidate_shape(self):
        controller = _controller()
        accepted = controller.accept_proposal([{"id": "candidate-a", "spec": {"x": 2}}])[0]

        self.assertEqual(
            set(accepted.candidate),
            {
                "id",
                "candidate_id",
                "format",
                "spec",
                "lineage",
                "generator",
                "validation",
                "materialization",
            },
        )
        self.assertEqual(accepted.candidate["candidate_id"], "candidate-a")
        self.assertEqual(accepted.candidate["format"], "parameters")
        self.assertEqual(accepted.candidate["lineage"], {"parents": []})
        self.assertEqual(accepted.candidate["generator"]["method_id"], "method-a")

    def test_max_trials_three_requests_two_then_one_and_drains(self):
        controller = _controller(proposal_width=2, max_trials=3)

        self.assertEqual(controller.next_proposal_width, 2)
        first = controller.accept_proposal([_candidate("candidate-a", 1), _candidate("candidate-b", 2)])
        self.assertEqual(controller.next_proposal_width, 0)
        for accepted in first:
            controller.record_completion(
                accepted.logical_trial_id,
                _observation(accepted.candidate["candidate_id"], score=accepted.candidate["spec"]["x"]),
            )

        self.assertEqual(controller.next_proposal_width, 1)
        final = controller.accept_proposal([_candidate("candidate-c", 3)])
        self.assertEqual(controller.run_status, "running")
        self.assertEqual(controller.stop_code, "max_trials")
        self.assertTrue(controller.submissions_closed)
        controller.record_completion(final[0].logical_trial_id, _observation("candidate-c", score=3))

        summary = controller.summary()
        self.assertEqual(summary["accepted_logical_trials"], 3)
        self.assertEqual(summary["terminal_logical_trials"], 3)
        self.assertEqual(summary["remaining_trials"], 0)
        self.assertEqual(summary["run_status"], "succeeded")
        self.assertEqual(summary["stop_code"], "max_trials")
        self.assertEqual(summary["best_candidate_id"], "candidate-c")

    def test_retries_are_completion_metadata_and_never_consume_slots(self):
        controller = _controller(max_trials=2)
        accepted = controller.accept_proposal([_candidate("candidate-a")])[0]
        completed = controller.record_completion(
            accepted.logical_trial_id,
            _observation("candidate-a", score=4),
            attempt_count=3,
            observation_count=5,
            completion_metadata={"backend_attempts": ["failed", "timeout", "success"]},
        )

        self.assertEqual(completed.attempt_count, 3)
        self.assertEqual(completed.completion_metadata["backend_attempts"][-1], "success")
        summary = controller.summary()
        self.assertEqual(summary["accepted_logical_trials"], 1)
        self.assertEqual(summary["terminal_logical_trials"], 1)
        self.assertEqual(summary["remaining_trials"], 1)
        self.assertEqual(summary["attempt_count"], 3)
        self.assertEqual(summary["observation_count"], 5)
        self.assertEqual(summary["retry_count"], 2)
        self.assertEqual(summary["final_logical_failures"], 0)

    def test_max_failures_counts_final_logical_failures_not_attempts(self):
        controller = _controller(max_trials=4, max_failures=2)
        accepted = controller.accept_proposal([_candidate("candidate-a"), _candidate("candidate-b")])

        controller.record_completion(
            accepted[0].logical_trial_id,
            _observation("candidate-a", status="failed", score=None),
            attempt_count=3,
        )
        self.assertEqual(controller.summary()["final_logical_failures"], 1)
        self.assertEqual(controller.run_status, "running")
        controller.record_completion(
            accepted[1].logical_trial_id,
            _observation("candidate-b", status="timeout", score=None),
            attempt_count=2,
        )

        summary = controller.summary()
        self.assertEqual(summary["accepted_logical_trials"], 2)
        self.assertEqual(summary["attempt_count"], 5)
        self.assertEqual(summary["retry_count"], 3)
        self.assertEqual(summary["final_logical_failures"], 2)
        self.assertEqual(summary["run_status"], "failed")
        self.assertEqual(summary["stop_code"], "max_failures")

    def test_controller_transitions_are_structured_for_projection(self):
        controller = _controller(max_trials=1)
        accepted = controller.accept_proposal([_candidate("candidate-a")])[0]
        controller.record_completion(
            accepted.logical_trial_id,
            _observation("candidate-a", score=2),
            attempt_count=2,
            observation_count=3,
        )

        events = [event.to_dict() for event in controller.controller_events]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "proposal.accepted",
                "submissions.closed",
                "logical_trial.terminal",
                "run.terminal",
            ],
        )
        self.assertEqual([event["controller_sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual(events[0]["logical_trials"][0]["candidate_id"], "candidate-a")
        self.assertEqual(events[2]["attempt_count"], 2)
        self.assertEqual(events[2]["observation_count"], 3)
        self.assertEqual(events[3]["run_status"], "succeeded")
        self.assertEqual(events[3]["stop_code"], "max_trials")
        self.assertEqual(
            [event.controller_sequence for event in controller.controller_events_since(2)],
            [3, 4],
        )

        rejected = _controller(max_trials=1)
        with self.assertRaises(MethodProtocolError):
            rejected.accept_proposal([_candidate("candidate-a"), _candidate("candidate-b")])
        rejection_events = [event.to_dict() for event in rejected.controller_events]
        self.assertEqual(
            [event["event"] for event in rejection_events],
            ["proposal.rejected", "submissions.closed", "run.terminal"],
        )
        self.assertEqual(rejection_events[0]["code"], "batch_overproduced")
        self.assertEqual(rejection_events[-1]["accepted_logical_trials"], 0)

    def test_terminal_reasons_are_explicit_and_no_success_has_one_reason(self):
        successful = _controller()
        accepted = successful.accept_proposal([_candidate("candidate-a")])[0]
        successful.record_completion(accepted.logical_trial_id, _observation("candidate-a"))
        successful.finish_method()
        self.assertEqual((successful.run_status, successful.stop_code), ("succeeded", "method_completed"))

        empty = _controller()
        self.assertEqual(empty.accept_proposal([]), ())
        self.assertEqual((empty.run_status, empty.stop_code), ("failed", "no_successful_observation"))

        wall_clock = _controller()
        accepted = wall_clock.accept_proposal([_candidate("candidate-a")])[0]
        wall_clock.record_completion(accepted.logical_trial_id, _observation("candidate-a"))
        wall_clock.stop_for_wall_clock()
        self.assertEqual((wall_clock.run_status, wall_clock.stop_code), ("succeeded", "wall_clock_budget"))

        cancelled = _controller()
        cancelled.cancel()
        self.assertEqual((cancelled.run_status, cancelled.stop_code), ("cancelled", "user_cancelled"))

        fatal = _controller()
        fatal.fail("method_failed")
        self.assertEqual((fatal.run_status, fatal.stop_code), ("failed", "method_failed"))

    def test_convergence_tracks_best_state_and_uses_converged_stop_code(self):
        controller = _controller(max_trials=10, proposal_width=1, patience_trials=1, min_delta=0.1)
        first = controller.accept_proposal([_candidate("candidate-a")])[0]
        controller.record_completion(first.logical_trial_id, _observation("candidate-a", score=5.0))
        second = controller.accept_proposal([_candidate("candidate-b")])[0]
        controller.record_completion(second.logical_trial_id, _observation("candidate-b", score=5.05))

        summary = controller.summary()
        self.assertEqual(summary["run_status"], "succeeded")
        self.assertEqual(summary["stop_code"], "converged")
        self.assertEqual(summary["best_metric"], 5.0)
        self.assertEqual(summary["best_candidate_id"], "candidate-a")
        self.assertEqual(summary["no_improvement_count"], 1)

    def test_convergence_is_decided_at_the_synchronous_batch_barrier(self):
        controller = _controller(max_trials=10, proposal_width=2, patience_trials=1)
        seed = controller.accept_proposal([_candidate("candidate-seed")])[0]
        controller.record_completion(seed.logical_trial_id, _observation("candidate-seed", score=5.0))

        batch = controller.accept_proposal([_candidate("candidate-low"), _candidate("candidate-high")])
        controller.record_completion(batch[0].logical_trial_id, _observation("candidate-low", score=4.0))
        self.assertEqual(controller.stop_code, None)
        controller.record_completion(batch[1].logical_trial_id, _observation("candidate-high", score=6.0))

        self.assertEqual(controller.run_status, "running")
        self.assertEqual(controller.stop_code, None)
        self.assertEqual(controller.summary()["best_candidate_id"], "candidate-high")
        self.assertEqual(controller.next_proposal_width, 2)

    def test_unknown_observation_status_becomes_one_final_logical_failure(self):
        controller = _controller(max_trials=2)
        accepted = controller.accept_proposal([_candidate("candidate-a")])[0]
        completed = controller.record_completion(
            accepted.logical_trial_id,
            _observation("candidate-a", status="mystery", score=None),
            attempt_count=4,
        )

        self.assertEqual(completed.outcome, "failed")
        self.assertEqual(completed.code, "unsupported_observation_status")
        self.assertEqual(completed.completion_metadata["raw_status"], "mystery")
        self.assertEqual(controller.summary()["final_logical_failures"], 1)

    def test_pre_dispatch_failure_has_zero_attempts_without_negative_retry_count(self):
        controller = _controller(max_trials=1)
        accepted = controller.accept_proposal([_candidate("candidate-a")])[0]
        controller.fail("evaluator_failed")
        controller.record_completion(
            accepted.logical_trial_id,
            _observation("candidate-a", status="failed", score=None),
            attempt_count=0,
            observation_count=1,
        )

        summary = controller.summary()
        self.assertEqual(summary["attempt_count"], 0)
        self.assertEqual(summary["observation_count"], 1)
        self.assertEqual(summary["retry_count"], 0)
        self.assertEqual(summary["run_status"], "failed")
        self.assertEqual(summary["stop_code"], "evaluator_failed")


if __name__ == "__main__":
    unittest.main()
