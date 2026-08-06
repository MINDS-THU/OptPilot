"""End-to-end recovery checks for the Realm-backed parameter authority."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.attempts import AttemptEnvelope, AttemptFinalization
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.run_closure import RunEvaluationClosure
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.run_authority import RetainedRunAuthority
from optpilot.run_control_manifest import RetryPolicy
from optpilot.run_controller import RunControllerStateError
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


def _normalizer(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result.setdefault("format", "parameters")
    result.setdefault("spec", {})
    result.setdefault("lineage", {"parents": []})
    result.setdefault(
        "generator", {"method_id": "test-method", "strategy": "external"}
    )
    result.setdefault("validation", {})
    result.setdefault("materialization", {})
    return result


class RetainedRunAuthorityHydrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="hydration/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="hydration/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="hydration",
            )
        )
        self.closure = closure
        self.closure_bindings = bindings
        self.source_owner_id = source_owner_id
        self.source_revision = source_revision
        self.manifest = replace(
            prepare_test_run_control_manifest(closure, max_trials=4),
            retry_policy=RetryPolicy(
                max_attempts=2,
                retryable_outcomes=("failed",),
            ),
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, self.manifest, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="hydration/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        change = self.ledger.begin_owner_change(
            operation_id="hydration/admission/change",
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id="hydration/admission/commit",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=0,
            expected_owner_revision=0,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission(
                        candidate_id="candidate-a",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 1}
                        ),
                        lineage={"parents": []},
                        generator={
                            "method_id": "test-method",
                            "strategy": "external",
                        },
                    ),
                ),
                logical_trials=(
                    LogicalTrialAdmission(
                        logical_trial_id="trial-a", candidate_id="candidate-a"
                    ),
                ),
            ),
            **self.controller_arguments,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    @property
    def controller_arguments(self) -> dict[str, object]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def hydrate(self) -> RetainedRunAuthority:
        return RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-a",
            candidate_normalizer=_normalizer,
            normalizer_version="test-normalizer.v1",
            logical_trial_id_factory=lambda: "trial-next",
        )

    def prepare(self, *, attempt_id: str, expected_run_revision: int):
        return self.ledger.prepare_run_attempt(
            operation_id=f"hydration/{attempt_id}/prepare",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            attempt_id=attempt_id,
            expected_run_revision=expected_run_revision,
            attempt_ttl_seconds=60,
            **self.controller_arguments,
        )

    def adopt(
        self,
        prepared,
        *,
        expected_run_revision: int,
        outcome: str,
        score: float | None = None,
    ):
        envelope = AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome=outcome,
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 1}, "metadata": {}},
            metric_values={} if score is None else {"score": score},
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
            effective_code=None if outcome == "success" else "evaluation_failed",
            captured_artifacts=(),
            envelope=envelope,
        )
        return self.ledger.adopt_run_attempt(
            operation_id=f"hydration/{prepared.attempt.attempt_id}/adopt",
            actor_principal_id="operator",
            run_id="run-a",
            attempt_id=prepared.attempt.attempt_id,
            change_id=prepared.attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=expected_run_revision,
            expected_owner_revision=0,
            **self.controller_arguments,
        )

    def test_hydrates_successful_observation_and_resumes_at_canonical_head(self) -> None:
        prepared = self.prepare(attempt_id="attempt-1", expected_run_revision=1)
        self.adopt(
            prepared,
            expected_run_revision=2,
            outcome="success",
            score=3.5,
        )

        first = self.hydrate()
        second = self.hydrate()

        self.assertEqual(first.run_revision, 3)
        self.assertEqual(first.controller.summary(), second.controller.summary())
        self.assertEqual(first.controller.controller_events, ())
        trial = first.controller.logical_trials[0]
        self.assertEqual(trial.state, "terminal")
        self.assertEqual(trial.outcome, "success")
        self.assertEqual(trial.attempt_count, 1)
        self.assertEqual(trial.observation_count, 1)
        self.assertEqual(trial.metric_values, {"score": 3.5})
        summary = first.controller.summary()
        self.assertEqual(summary["best_candidate_id"], "candidate-a")
        self.assertEqual(summary["best_metric"], 3.5)
        self.assertEqual(summary["attempt_count"], 1)
        prepared_next = first.preflight(
            [{"candidate_id": "candidate-b", "spec": {"x": 2}}],
            admission_id="batch-next",
        )
        replayed_preflight = second.preflight(
            [{"candidate_id": "candidate-b", "spec": {"x": 2}}],
            admission_id="batch-next",
        )
        self.assertEqual(prepared_next.expected_run_revision, 3)
        self.assertEqual(prepared_next, replayed_preflight)

    def test_hydrates_retry_wait_and_then_administrative_cancellation(self) -> None:
        prepared = self.prepare(attempt_id="attempt-1", expected_run_revision=1)
        self.adopt(
            prepared,
            expected_run_revision=2,
            outcome="failed",
        )

        retrying = self.hydrate()
        retrying_trial = retrying.controller.logical_trials[0]
        self.assertEqual(retrying.run_revision, 3)
        self.assertEqual(retrying_trial.state, "retrying")
        self.assertEqual(retrying_trial.attempt_count, 1)
        self.assertEqual(retrying_trial.observation_count, 1)
        self.assertEqual(retrying.controller.next_proposal_width, 0)
        # Unresolved work is visible on the trial but is not double-counted in
        # terminal summary aggregates before its eventual resolution.
        self.assertEqual(retrying.controller.summary()["attempt_count"], 0)

        self.ledger.cancel_run_logical_trial(
            operation_id="hydration/retry/cancel",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            expected_run_revision=3,
            code="admin_cancelled",
            **self.controller_arguments,
        )
        cancelled = self.hydrate()
        cancelled_trial = cancelled.controller.logical_trials[0]
        self.assertEqual(cancelled.run_revision, 4)
        self.assertEqual(cancelled_trial.state, "terminal")
        self.assertEqual(cancelled_trial.outcome, "cancelled")
        self.assertEqual(cancelled_trial.code, "admin_cancelled")
        self.assertEqual(cancelled_trial.attempt_count, 1)
        self.assertEqual(cancelled_trial.observation_count, 1)
        self.assertIsNone(
            cancelled_trial.completion_metadata["terminal_attempt_id"]
        )
        self.assertEqual(cancelled.controller.summary()["attempt_count"], 1)
        self.assertEqual(cancelled.controller.next_proposal_width, 1)

    def test_hydrates_draining_and_terminal_control_without_reopening(self) -> None:
        self.ledger.cancel_run_logical_trial(
            operation_id="hydration/cancel",
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            expected_run_revision=1,
            code="admin_cancelled",
            **self.controller_arguments,
        )
        self.ledger.close_run_submissions(
            operation_id="hydration/close",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=2,
            stop_code="admin_cancelled",
            **self.controller_arguments,
        )

        draining = self.hydrate()
        self.assertEqual(draining.run_revision, 3)
        self.assertEqual(draining.controller.run_status, "running")
        self.assertEqual(draining.controller.stop_code, "admin_cancelled")
        self.assertTrue(draining.controller.submissions_closed)
        with self.assertRaisesRegex(RunControllerStateError, "Submissions are closed"):
            draining.preflight(
                [{"candidate_id": "candidate-b", "spec": {"x": 2}}],
                admission_id="must-not-reopen",
            )

        self.ledger.finish_run(
            operation_id="hydration/finish",
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=3,
            terminal_state="cancelled",
            code="admin_cancelled",
            **self.controller_arguments,
        )
        terminal = self.hydrate()
        self.assertEqual(terminal.run_revision, 4)
        self.assertEqual(terminal.controller.run_status, "cancelled")
        self.assertEqual(terminal.controller.stop_code, "admin_cancelled")
        self.assertEqual(terminal.controller.controller_events, ())
        with self.assertRaisesRegex(RunControllerStateError, "already terminal"):
            terminal.preflight(
                [{"candidate_id": "candidate-b", "spec": {"x": 2}}],
                admission_id="terminal-must-not-reopen",
            )

    def test_hydration_requires_exact_normalizer_version_and_immutable_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "version does not match"):
            RetainedRunAuthority.hydrate(
                ledger=self.ledger,
                actor_principal_id="operator",
                run_id="run-a",
                candidate_normalizer=_normalizer,
                normalizer_version="wrong-normalizer.v1",
            )

        def changing_normalizer(candidate: dict[str, object]) -> dict[str, object]:
            result = _normalizer(candidate)
            result["spec"] = {"x": 999}
            return result

        with self.assertRaisesRegex(ValueError, "changes immutable admitted facts"):
            RetainedRunAuthority.hydrate(
                ledger=self.ledger,
                actor_principal_id="operator",
                run_id="run-a",
                candidate_normalizer=changing_normalizer,
                normalizer_version="test-normalizer.v1",
            )

        def extra_field_normalizer(candidate: dict[str, object]) -> dict[str, object]:
            result = _normalizer(candidate)
            result["runtime_override"] = {"unsafe": True}
            return result

        with self.assertRaisesRegex(ValueError, "canonical field set"):
            RetainedRunAuthority.hydrate(
                ledger=self.ledger,
                actor_principal_id="operator",
                run_id="run-a",
                candidate_normalizer=extra_field_normalizer,
                normalizer_version="test-normalizer.v1",
            )

    def test_hydration_keeps_the_environment_candidate_contract_immutable(self) -> None:
        authority = self.hydrate()

        with self.assertRaises(TypeError):
            authority.candidate_contract["format"] = "files"  # type: ignore[index]
        with self.assertRaisesRegex(AttributeError, "immutable"):
            authority.candidate_contract = {"format": "files"}
        self.assertEqual(authority.candidate_contract["format"], "parameters")

        authority.refresh_controller()
        with self.assertRaises(TypeError):
            authority.candidate_contract["validation"] = {  # type: ignore[index]
                "mutable": True
            }

    def test_authority_hydrates_a_file_candidate_environment(self) -> None:
        environment = replace(
            self.closure.environment_revision,
            candidate_contract={"format": "files"},
        )
        runtime = replace(
            self.closure.prepared_runtime,
            environment_revision_digest=environment.digest,
        )
        template = replace(
            self.closure.evaluation_template,
            environment_revision_digest=environment.digest,
            runtime_revision_digest=runtime.digest,
        )
        file_closure = RunEvaluationClosure(environment, runtime, template)
        file_manifest = prepare_test_run_control_manifest(
            file_closure, max_trials=1
        )
        run_definition, definition_bindings = prepare_test_run_definition(
            file_closure, file_manifest, self.closure_bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id="hydration/files/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-files",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_revision,
            run_id="run-files",
            owner_id="run-owner-files",
        )

        authority = RetainedRunAuthority.from_create_receipt(
            ledger=self.ledger,
            actor_principal_id="operator",
            receipt=created,
            candidate_normalizer=_normalizer,
            normalizer_version="test-normalizer.v1",
        )
        self.assertEqual(authority.candidate_contract["format"], "files")

    def test_budget_exhausting_admission_is_canonically_draining_on_hydration(self) -> None:
        budget_manifest = replace(self.manifest, max_trials=1)
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, budget_manifest, self.closure_bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id="hydration/budget/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-budget",
            controller_ttl_seconds=60,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_revision,
            run_id="run-budget",
            owner_id="run-owner-budget",
        )
        change = self.ledger.begin_owner_change(
            operation_id="hydration/budget/admission/change",
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.commit_run_candidate_admissions(
            operation_id="hydration/budget/admission/commit",
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                candidates=(
                    CandidateAdmission(
                        candidate_id="candidate-budget",
                        envelope=NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 10}
                        ),
                        lineage={"parents": []},
                        generator={"method_id": "test-method"},
                    ),
                ),
                logical_trials=(
                    LogicalTrialAdmission(
                        logical_trial_id="trial-budget",
                        candidate_id="candidate-budget",
                    ),
                ),
            ),
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
        )
        self.assertEqual(admitted.revision.revision, 1)
        control = self.ledger.read_run_control(
            actor_principal_id="operator", run_id="run-budget"
        )
        self.assertEqual(control.current_submission.state, "draining")
        self.assertEqual(control.current_submission.stop_code, "max_trials")
        self.assertEqual(control.current_submission.run_revision, 1)

        authority = RetainedRunAuthority.hydrate(
            ledger=self.ledger,
            actor_principal_id="operator",
            run_id="run-budget",
            candidate_normalizer=_normalizer,
            normalizer_version="test-normalizer.v1",
        )
        self.assertEqual(authority.run_revision, 1)
        self.assertEqual(authority.controller.stop_code, "max_trials")
        self.assertTrue(authority.controller.submissions_closed)
        self.assertEqual(authority.controller.next_proposal_width, 0)


if __name__ == "__main__":
    unittest.main()
