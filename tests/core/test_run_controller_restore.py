"""Pure reconstruction checks for the disposable run-controller cache."""

from __future__ import annotations

import copy
import unittest

from optpilot.run_controller import (
    LogicalTrialRestoreState,
    RunController,
    RunControllerRestoreState,
    RunControllerStateError,
)


def _candidate(candidate_id: str, value: float = 1.0) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
        "lineage": {"parents": []},
        "generator": {"method_id": "method-a"},
        "validation": {},
        "materialization": {},
    }


def _normalizer(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result.setdefault("candidate_id", result.get("id"))
    result.setdefault("format", "parameters")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault("generator", {"method_id": "method-a"})
    result.setdefault("validation", {})
    result.setdefault("materialization", {})
    return result


def _controller(**overrides: object) -> RunController:
    arguments: dict[str, object] = {
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
        "patience_trials": 3,
        "min_delta": 0.1,
        "candidate_normalizer": _normalizer,
    }
    arguments.update(overrides)
    return RunController(**arguments)  # type: ignore[arg-type]


def _terminal(
    logical_trial_id: str,
    candidate_id: str,
    *,
    sequence: int,
    outcome: str = "success",
    score: float | None = 1.0,
    attempts: int = 1,
    observations: int = 1,
) -> LogicalTrialRestoreState:
    return LogicalTrialRestoreState(
        logical_trial_id=logical_trial_id,
        candidate=_candidate(candidate_id),
        state="terminal",
        outcome=outcome,
        code=None if outcome == "success" else f"trial_{outcome}",
        terminal_sequence=sequence,
        attempt_count=attempts,
        observation_count=observations,
        metric_values={} if score is None else {"score": score},
        completion_metadata={"source": "canonical-ledger"},
    )


class RunControllerRestoreTests(unittest.TestCase):
    def test_restores_terminal_summary_in_canonical_completion_order(self) -> None:
        controller = _controller(max_trials=3)
        state = RunControllerRestoreState(
            run_status="succeeded",
            submission_state="terminal",
            submission_stop_code="max_trials",
            terminal_code=None,
            # Admission order deliberately differs from completion order.
            logical_trials=(
                _terminal(
                    "trial-a",
                    "candidate-a",
                    sequence=30,
                    score=10.0,
                    attempts=2,
                    observations=2,
                ),
                _terminal("trial-b", "candidate-b", sequence=10, score=9.0),
                _terminal(
                    "trial-c",
                    "candidate-c",
                    sequence=20,
                    outcome="failed",
                    score=None,
                    attempts=3,
                    observations=3,
                ),
            ),
        )

        restored = controller.restore_canonical_state(state)

        self.assertIs(restored, controller)
        self.assertEqual(controller.run_status, "succeeded")
        self.assertEqual(controller.stop_code, "max_trials")
        self.assertEqual(controller.accepted_candidate_ids, (
            "candidate-a",
            "candidate-b",
            "candidate-c",
        ))
        self.assertEqual(controller.controller_events, ())
        summary = controller.summary()
        self.assertEqual(summary["accepted_logical_trials"], 3)
        self.assertEqual(summary["terminal_logical_trials"], 3)
        self.assertEqual(summary["successful_logical_trials"], 2)
        self.assertEqual(summary["successful_objective_observations"], 2)
        self.assertEqual(summary["final_logical_failures"], 1)
        self.assertEqual(summary["attempt_count"], 6)
        self.assertEqual(summary["observation_count"], 6)
        self.assertEqual(summary["retry_count"], 3)
        self.assertEqual(summary["best_metric"], 10.0)
        self.assertEqual(summary["best_candidate_id"], "candidate-a")
        self.assertEqual(summary["best_logical_trial_id"], "trial-a")
        self.assertEqual(summary["no_improvement_count"], 0)

    def test_active_attempt_counts_are_incorporated_once_on_completion(self) -> None:
        controller = _controller(max_trials=2)
        active = LogicalTrialRestoreState(
            logical_trial_id="trial-a",
            candidate=_candidate("candidate-a"),
            state="retrying",
            attempt_count=1,
            observation_count=1,
            metric_values={},
        )
        controller.restore_canonical_state(
            RunControllerRestoreState(
                run_status="running",
                submission_state="accepting",
                submission_stop_code=None,
                terminal_code=None,
                logical_trials=(active,),
            )
        )

        self.assertEqual(controller.next_proposal_width, 0)
        self.assertEqual(controller.logical_trials[0].state, "retrying")
        self.assertEqual(controller.logical_trials[0].attempt_count, 1)
        self.assertEqual(controller.summary()["attempt_count"], 0)
        controller.record_completion(
            "trial-a",
            {
                "candidate_id": "candidate-a",
                "status": "success",
                "metric_values": {"score": 4.0},
            },
            attempt_count=2,
            observation_count=2,
        )

        self.assertEqual(controller.summary()["attempt_count"], 2)
        self.assertEqual(controller.summary()["observation_count"], 2)
        self.assertEqual(controller.summary()["retry_count"], 1)
        self.assertEqual(controller.next_proposal_width, 1)

    def test_terminal_code_is_distinct_from_submission_stop_reason(self) -> None:
        controller = _controller(max_trials=1)
        controller.restore_canonical_state(
            RunControllerRestoreState(
                run_status="failed",
                submission_state="terminal",
                submission_stop_code="max_trials",
                terminal_code="no_successful_observation",
                logical_trials=(
                    _terminal(
                        "trial-a",
                        "candidate-a",
                        sequence=10,
                        outcome="failed",
                        score=None,
                    ),
                ),
            )
        )

        self.assertEqual(controller.stop_code, "no_successful_observation")
        self.assertTrue(controller.submissions_closed)

    def test_repeated_trials_may_share_one_identical_candidate(self) -> None:
        controller = _controller(max_trials=2)
        candidate = _candidate("candidate-a")
        controller.restore_canonical_state(
            RunControllerRestoreState(
                run_status="running",
                submission_state="accepting",
                submission_stop_code=None,
                terminal_code=None,
                logical_trials=(
                    LogicalTrialRestoreState(
                        logical_trial_id="trial-seed-1",
                        candidate=candidate,
                        state="accepted",
                    ),
                    LogicalTrialRestoreState(
                        logical_trial_id="trial-seed-2",
                        candidate=candidate,
                        state="accepted",
                    ),
                ),
            )
        )

        self.assertEqual(controller.accepted_logical_trials, 2)
        self.assertEqual(controller.accepted_candidate_ids, (
            "candidate-a",
            "candidate-a",
        ))
        self.assertEqual(controller.summary()["candidate_count"], 1)

    def test_restore_rejects_inconsistent_facts_without_partial_mutation(self) -> None:
        controller = _controller()
        before = controller.summary()
        duplicate_id = RunControllerRestoreState(
            run_status="running",
            submission_state="accepting",
            submission_stop_code=None,
            terminal_code=None,
            logical_trials=(
                LogicalTrialRestoreState(
                    logical_trial_id="trial-a",
                    candidate=_candidate("candidate-a"),
                    state="accepted",
                ),
                LogicalTrialRestoreState(
                    logical_trial_id="trial-a",
                    candidate=_candidate("candidate-b"),
                    state="accepted",
                ),
            ),
        )

        with self.assertRaisesRegex(
            RunControllerStateError, "Duplicate canonical logical trial"
        ):
            controller.restore_canonical_state(duplicate_id)
        self.assertEqual(controller.summary(), before)
        self.assertEqual(controller.logical_trials, ())
        self.assertEqual(controller.controller_events, ())

        malformed_candidate = _candidate("candidate-a")
        del malformed_candidate["generator"]
        with self.assertRaisesRegex(
            RunControllerStateError, "resolved normalizer output"
        ):
            controller.restore_canonical_state(
                RunControllerRestoreState(
                    run_status="running",
                    submission_state="accepting",
                    submission_stop_code=None,
                    terminal_code=None,
                    logical_trials=(
                        LogicalTrialRestoreState(
                            logical_trial_id="trial-a",
                            candidate=malformed_candidate,
                            state="accepted",
                        ),
                    ),
                )
            )
        self.assertEqual(controller.summary(), before)

    def test_restore_is_fresh_controller_only(self) -> None:
        controller = _controller()
        state = RunControllerRestoreState(
            run_status="running",
            submission_state="accepting",
            submission_stop_code=None,
            terminal_code=None,
            logical_trials=(
                LogicalTrialRestoreState(
                    logical_trial_id="trial-a",
                    candidate=_candidate("candidate-a"),
                    state="accepted",
                ),
            ),
        )
        controller.restore_canonical_state(state)

        with self.assertRaisesRegex(RunControllerStateError, "fresh controller"):
            controller.restore_canonical_state(state)


class RunControllerRestoreValueTests(unittest.TestCase):
    def test_projection_rejects_impossible_control_and_trial_states(self) -> None:
        with self.assertRaisesRegex(ValueError, "Accepting submissions"):
            RunControllerRestoreState(
                run_status="running",
                submission_state="accepting",
                submission_stop_code="max_trials",
                terminal_code=None,
                logical_trials=(),
            )
        with self.assertRaisesRegex(ValueError, "terminal run"):
            RunControllerRestoreState(
                run_status="failed",
                submission_state="terminal",
                submission_stop_code="max_trials",
                terminal_code="no_successful_observation",
                logical_trials=(
                    LogicalTrialRestoreState(
                        logical_trial_id="trial-a",
                        candidate=_candidate("candidate-a"),
                        state="running",
                        attempt_count=1,
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            LogicalTrialRestoreState(
                logical_trial_id="trial-a",
                candidate=_candidate("candidate-a"),
                state="accepted",
                attempt_count=0,
                observation_count=1,
            )


if __name__ == "__main__":
    unittest.main()
