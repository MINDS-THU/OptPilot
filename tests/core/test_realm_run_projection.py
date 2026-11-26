"""Focused checks for the pure canonical-run summary projection."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_projection import (
    RUN_SUMMARY_PROJECTION_SCHEMA,
    RunSummaryProjection,
)
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.run_control_manifest import RetryPolicy
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="projection/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="projection/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            closure_bindings,
            source_owner_id,
            source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="operator",
            prefix="run-projection",
        )
        self.manifest = replace(
            prepare_test_run_control_manifest(self.closure, max_trials=4),
            retry_policy=RetryPolicy(
                max_attempts=2,
                retryable_outcomes=("failed",),
            ),
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, self.manifest, closure_bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="projection/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.operation_index = 0
        self.run_revision = 0
        self.owner_revision = 0
        self._admit_four_trials()

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.operation_index += 1
        return f"projection/{self.operation_index}/{label}"

    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit_four_trials(self) -> None:
        candidates = []
        trials = []
        for index, suffix in enumerate(("a", "b", "c", "d"), start=1):
            candidates.append(
                CandidateAdmission(
                    f"candidate-{suffix}",
                    NormalizedCandidateEnvelope.build(
                        candidate_format="parameters",
                        spec={"x": index},
                    ),
                    lineage={"parents": []},
                    generator={"method_id": "test-method"},
                )
            )
            trials.append(
                LogicalTrialAdmission(
                    f"trial-{suffix}",
                    f"candidate-{suffix}",
                )
            )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("admission-begin"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        receipt = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admission-commit"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=RunAdmissionPlan(tuple(candidates), tuple(trials)),
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision

    def _prepare(self, trial: str, attempt: str):
        receipt = self.ledger.prepare_run_attempt(
            operation_id=self.op(f"prepare-{attempt}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id=trial,
            attempt_id=attempt,
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        return receipt

    def _confirm(self, prepared) -> None:
        # Projection tests exercise terminal run views.  Provider-backed launch
        # confirmation is covered by test_realm_execution_binding_ledger.
        self.assertEqual(prepared.attempt.state, "prepared")

    def _adopt(
        self,
        prepared,
        *,
        outcome: str,
        metric: float | None,
        code: str | None = None,
        platform_error: bool = False,
    ):
        if platform_error:
            finalization = AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome=outcome,
                effective_code=code,
                captured_artifacts=(),
                platform_error={
                    "code": code,
                    "message": "The attempt ended before environment evaluation.",
                    "details": {"source": "test"},
                },
            )
        else:
            envelope = AttemptEnvelope(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                outcome=outcome,
                phase="environment_evaluation",
                wall_clock_seconds=0.1,
                validation={"accepted": True, "errors": []},
                materialization={"runtime_spec": {}, "metadata": {}},
                metric_values={} if metric is None else {"score": metric},
                constraint_results={},
                output_declarations=(),
                event_summary={},
                execution_metadata={"worker": "test"},
                error=(
                    {}
                    if outcome == "success"
                    else {
                        "phase": "environment_evaluation",
                        "type": "RuntimeError",
                        "message": "evaluation failed",
                    }
                ),
            )
            finalization = AttemptFinalization(
                attempt_id=prepared.attempt.attempt_id,
                evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
                binding_id=prepared.attempt.binding_id,
                effective_outcome=outcome,
                effective_code=code,
                captured_artifacts=(),
                envelope=envelope,
            )
        receipt = self.ledger.adopt_run_attempt(
            operation_id=self.op(f"adopt-{prepared.attempt.attempt_id}"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            attempt_id=prepared.attempt.attempt_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=prepared.attempt.capture_change_id,
            finalization=finalization,
            **self.controller_arguments(),
        )
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.owner_commit.owner_revision
        return receipt

    def _complete_environment_attempt(
        self,
        trial: str,
        attempt: str,
        *,
        outcome: str,
        metric: float | None,
        code: str | None = None,
    ):
        prepared = self._prepare(trial, attempt)
        self._confirm(prepared)
        return self._adopt(
            prepared,
            outcome=outcome,
            metric=metric,
            code=code,
        )

    def snapshot(self):
        return self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.created.run.run_id
        )

    def test_live_projection_counts_canonical_facts_and_is_immutable(self) -> None:
        snapshot = self.snapshot()
        projection = RunSummaryProjection.from_snapshot(snapshot)

        self.assertEqual(projection.run_status, "running")
        self.assertEqual(projection.submission_state, "draining")
        self.assertEqual(projection.stop_code, "max_trials")
        self.assertEqual(projection.candidate_count, 4)
        self.assertEqual(projection.accepted_logical_trials, 4)
        self.assertEqual(projection.active_logical_trials, 4)
        self.assertEqual(
            dict(projection.logical_trials_by_state),
            {
                "accepted": 4,
                "queued": 0,
                "running": 0,
                "retrying": 0,
                "terminal": 0,
            },
        )
        self.assertEqual(projection.attempt_count, 0)
        self.assertEqual(projection.retry_count, 0)
        self.assertEqual(projection.observation_count, 0)
        self.assertEqual(projection.successful_objective_observations, 0)
        self.assertEqual(projection.no_improvement_count, 0)
        self.assertEqual(projection.remaining_trials, 0)
        self.assertEqual(projection.cursor.revision, snapshot.revision.revision)
        self.assertEqual(projection.cursor.sequence, snapshot.revision.last_sequence)
        self.assertEqual(projection.cursor.next_sequence, snapshot.revision.next_sequence)
        self.assertIsNone(projection.best_metric)
        with self.assertRaises(TypeError):
            projection.logical_trials_by_state["accepted"] = 0  # type: ignore[index]

        payload = projection.to_dict()
        self.assertEqual(payload["schema"], RUN_SUMMARY_PROJECTION_SCHEMA)
        payload["counts"]["logical_trials"]["by_state"]["accepted"] = 0
        self.assertEqual(projection.logical_trials_by_state["accepted"], 4)

    def test_terminal_projection_uses_terminal_order_and_truthful_attempt_counts(self) -> None:
        # Completion order deliberately differs from admission order.
        self._complete_environment_attempt(
            "trial-b", "attempt-b1", outcome="success", metric=9.0
        )
        best = self._complete_environment_attempt(
            "trial-a", "attempt-a1", outcome="success", metric=10.0
        )
        self._complete_environment_attempt(
            "trial-c",
            "attempt-c1",
            outcome="failed",
            metric=999.0,
            code="evaluation_failed",
        )
        self._complete_environment_attempt(
            "trial-c", "attempt-c2", outcome="success", metric=10.0
        )

        # A pre-evaluation terminal attempt is counted without inventing an
        # environment observation.
        invalid = self._prepare("trial-d", "attempt-d1")
        self._adopt(
            invalid,
            outcome="invalid",
            metric=None,
            code="candidate_invalid",
            platform_error=True,
        )

        finished = self.ledger.finish_run(
            operation_id=self.op("finish"),
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = finished.revision.revision

        snapshot = self.snapshot()
        projection = RunSummaryProjection.from_snapshot(snapshot)

        self.assertEqual(projection.run_status, "succeeded")
        self.assertEqual(projection.submission_state, "terminal")
        self.assertEqual(projection.stop_code, "max_trials")
        self.assertEqual(projection.accepted_logical_trials, 4)
        self.assertEqual(projection.terminal_logical_trials, 4)
        self.assertEqual(projection.active_logical_trials, 0)
        self.assertEqual(projection.successful_logical_trials, 3)
        self.assertEqual(projection.final_logical_failures, 1)
        self.assertEqual(projection.logical_trials_by_state["terminal"], 4)
        self.assertEqual(projection.attempt_count, 5)
        self.assertEqual(projection.retry_count, 1)
        self.assertEqual(
            dict(projection.attempts_by_state),
            {"prepared": 0, "running": 0, "terminal": 5},
        )
        self.assertEqual(projection.observation_count, 4)
        self.assertEqual(projection.successful_objective_observations, 3)
        # The failed physical retry is not another logical result. Only the
        # final tied success and the invalid logical completion increment this.
        self.assertEqual(projection.no_improvement_count, 2)
        self.assertEqual(projection.observations_by_outcome["success"], 3)
        self.assertEqual(projection.observations_by_outcome["failed"], 1)
        self.assertEqual(projection.observations_by_outcome["invalid"], 0)

        # The failed retry's larger raw metric is not a final successful result;
        # candidate A wins before candidate C, so the later exact tie is stable.
        self.assertEqual(projection.best_metric, 10.0)
        self.assertEqual(projection.best_candidate_id, "candidate-a")
        self.assertEqual(projection.best_logical_trial_id, "trial-a")
        self.assertEqual(projection.best_attempt_id, "attempt-a1")
        self.assertEqual(
            projection.best_observation_id,
            best.observation.observation_id,
        )
        self.assertEqual(projection.cursor.revision, finished.revision.revision)
        self.assertEqual(projection.cursor.sequence, finished.revision.last_sequence)
        self.assertEqual(
            projection.to_dict()["best"],
            {
                "metric": 10.0,
                "candidate_id": "candidate-a",
                "logical_trial_id": "trial-a",
                "attempt_id": "attempt-a1",
                "observation_id": best.observation.observation_id,
            },
        )
        self.assertEqual(
            projection.to_dict()["counts"]["attempts"]["retries"], 1
        )
        self.assertEqual(
            projection.to_dict()["counts"]["logical_trials"]["no_improvement"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
