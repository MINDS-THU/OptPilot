"""Focused tests for the pure retained-method read projection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot.attempts import (
    AttemptEnvelope,
    AttemptFinalization,
    CapturedArtifact,
    OutputDeclaration,
)
from optpilot.method_exchange_projection import (
    MethodProjectionError,
    build_method_observation_exchange_input,
    build_method_proposal_exchange_input,
    build_method_study_state,
    observation_worker_payload,
    project_method_observations,
    proposal_worker_payload,
)
from optpilot.realm.content import AllowedFileSource, LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.method_exchange_records import MethodTerminalTransitionRef
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot.realm.run_attempt_records import RUN_ARTIFACT_ROLE
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class MethodExchangeProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="method-projection/principal",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="method-projection/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, bindings, source_owner_id, source_owner_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="method-projection",
                candidate_contract={
                    "format": "parameters",
                    "context": {
                        "domain_value": "/this/is/domain/data",
                        "methodContext": {"instructions": ["optimize score"]},
                    },
                },
            )
        )
        control = prepare_test_run_control_manifest(closure, max_trials=3)
        definition, definition_bindings = prepare_test_run_definition(
            closure, control, bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="method-projection/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=60,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.counter = 0
        self.run_revision = 0
        self.owner_revision = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"method-projection/{self.counter}/{label}"

    @property
    def controller(self):
        return self.created.controller_lease

    def controller_arguments(self) -> dict[str, object]:
        return {
            "controller_lease_id": self.controller.lease_id,
            "controller_holder_id": self.controller.holder_id,
            "controller_fencing_token": self.controller.fencing_token,
        }

    def snapshot(self):
        return self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )

    @staticmethod
    def plan() -> RunAdmissionPlan:
        return RunAdmissionPlan(
            (
                CandidateAdmission(
                    "candidate-a",
                    NormalizedCandidateEnvelope.build(
                        candidate_format="parameters", spec={"x": 1}
                    ),
                    generator={"method_id": "test-method"},
                ),
            ),
            (LogicalTrialAdmission("trial-a", "candidate-a", seed=7),),
        )

    def prepare_and_admit_round(self):
        exchange_input = build_method_proposal_exchange_input(
            self.snapshot(), requested_width=1
        )
        prepared = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-proposal"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=self.run_revision,
            expected_controller_generation=self.created.run.controller_generation,
            exchange_input=exchange_input,
            **self.controller_arguments(),
        )
        self.run_revision = prepared.prepared_run_revision
        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-admission"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=60,
        )
        plan = self.plan()
        completion = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("complete-proposal"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="admitted",
            response_digest=request_digest(
                {
                    "exchange_id": prepared.exchange_id,
                    "ok": True,
                    "result": {"candidates": [{"x": 1}]},
                }
            ),
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            change_id=change.change_id,
            plan=plan,
            **self.controller_arguments(),
        )
        self.run_revision = completion.admission.revision.revision
        self.owner_revision = completion.admission.owner_commit.owner_revision
        return prepared, completion

    def test_proposal_state_is_compact_path_free_semantics_and_replay_uses_checkpoint(
        self,
    ) -> None:
        snapshot = self.snapshot()
        state = build_method_study_state(snapshot)
        self.assertEqual(
            set(state),
            {
                "accepted_trials",
                "completed_trials",
                "failure_count",
                "attempt_count",
                "observation_count",
                "best_metric",
                "best_trial_id",
                "best_candidate_id",
                "candidate_context",
            },
        )
        self.assertEqual(
            state["candidate_context"]["domain_value"],
            "/this/is/domain/data",
        )
        self.assertNotIn("runtime_context", state)
        self.assertNotIn("run_dir", repr(state))

        prepared_input = build_method_proposal_exchange_input(
            snapshot, requested_width=1
        )
        preparation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            exchange_input=prepared_input,
            **self.controller_arguments(),
        )
        self.assertEqual(
            proposal_worker_payload(preparation),
            {
                "n_candidates": 1,
                "study_state": prepared_input.to_dict()["study_state"],
                "evidence": prepared_input.to_dict()["evidence"],
            },
        )

    def test_cancelled_before_dispatch_projects_one_terminal_public_observation(
        self,
    ) -> None:
        _prepared, _completion = self.prepare_and_admit_round()
        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id=self.op("cancel"),
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            expected_run_revision=self.run_revision,
            code="user_cancelled",
            **self.controller_arguments(),
        )
        self.run_revision = cancelled.revision.revision

        exchange_input = build_method_observation_exchange_input(
            self.snapshot(), round_index=1
        )
        self.assertEqual(
            exchange_input.terminal_transitions,
            (MethodTerminalTransitionRef(cancelled.transition),),
        )
        projected = project_method_observations(self.snapshot(), exchange_input)
        self.assertEqual(
            projected[0].to_dict(),
            {
                "logical_trial_id": "trial-a",
                "candidate_id": "candidate-a",
                "status": "cancelled",
                "metric_values": {},
                "constraint_results": {},
                "resource_usage": {},
                "artifacts": [],
                "error": {
                    "phase": "run",
                    "code": "user_cancelled",
                    "message": "The logical trial was cancelled before evaluation.",
                },
            },
        )

        observation_preparation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=self.run_revision,
            expected_controller_generation=1,
            exchange_input=exchange_input,
            **self.controller_arguments(),
        )
        self.assertEqual(
            observation_worker_payload(self.snapshot(), observation_preparation),
            {"observations": [projected[0].to_dict()]},
        )

    def test_environment_projection_filters_operator_data_and_realized_paths(
        self,
    ) -> None:
        self.prepare_and_admit_round()
        prepared = self.ledger.prepare_run_attempt(
            operation_id=self.op("prepare-attempt"),
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            attempt_id="attempt-a1",
            expected_run_revision=self.run_revision,
            **self.controller_arguments(),
        )
        self.run_revision = prepared.revision.revision

        declarations = []
        captures = []
        memberships = []
        for index, (name, visibility) in enumerate(
            (("public-result", "method"), ("private-log", "operator")), start=1
        ):
            source = self.root / name
            source.mkdir()
            filename = f"{name}.json"
            (source / filename).write_text(
                f'{{"artifact":{index}}}\n', encoding="utf-8"
            )
            capture = self.store.capture(
                change_id=prepared.attempt.capture_change_id,
                authority=self.ledger.content_capture_handle(
                    actor_principal_id="operator",
                    change_id=prepared.attempt.capture_change_id,
                    store_id=self.store.store_id,
                ),
            )
            sealed = capture.seal_blob(
                source=AllowedFileSource(source, filename)
            )
            membership = OwnerMembership(
                self.store.store_id, sealed.blob_ref, RUN_ARTIFACT_ROLE
            )
            declaration = OutputDeclaration(
                declaration_id=f"environment:{name}",
                name=name,
                path=filename,
                media_type="application/json",
                metadata={"private_path": str(source / filename)},
            )
            captured = CapturedArtifact(
                declaration=declaration,
                content_ref=str(sealed.blob_ref),
                size_bytes=sealed.publication.logical_bytes,
                bindings=(
                    {
                        "store_id": self.store.store_id,
                        "content_ref": str(sealed.blob_ref),
                    },
                ),
                visibility=visibility,
                metadata={"capture_path": str(source)},
            )
            declarations.append(declaration)
            captures.append(captured)
            memberships.append(membership)
        self.ledger.hold_owner_content(
            operation_id=self.op("hold-artifacts"),
            actor_principal_id="operator",
            change_id=prepared.attempt.capture_change_id,
            memberships=tuple(memberships),
        )
        envelope = AttemptEnvelope(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            outcome="failed",
            phase="environment_evaluation",
            wall_clock_seconds=0.25,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {}, "metadata": {}},
            metric_values={"score": 3.5},
            constraint_results={"feasible": False},
            output_declarations=tuple(declarations),
            event_summary={"traceback": str(self.root / "traceback.txt")},
            execution_metadata={"worker_path": str(self.root)},
            error={
                "phase": "environment_evaluation",
                "type": "RuntimeError",
                "code": "evaluation_failed",
                "message": f"failed in {self.root / 'checkout' / 'evaluate.py'}",
                "details": {
                    "path": str(self.root / "secret"),
                    "reason": "bad input",
                },
            },
        )
        finalization = AttemptFinalization(
            attempt_id=prepared.attempt.attempt_id,
            evaluation_spec_digest=prepared.attempt.evaluation_spec_digest,
            binding_id=prepared.attempt.binding_id,
            effective_outcome="failed",
            effective_code="evaluation_failed",
            captured_artifacts=tuple(captures),
            envelope=envelope,
        )
        adopted = self.ledger.adopt_run_attempt(
            operation_id=self.op("adopt"),
            actor_principal_id="operator",
            run_id="run-a",
            attempt_id=prepared.attempt.attempt_id,
            change_id=prepared.attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            **self.controller_arguments(),
        )
        self.run_revision = adopted.revision.revision
        self.owner_revision = adopted.owner_commit.owner_revision

        exchange_input = build_method_observation_exchange_input(
            self.snapshot(), round_index=1
        )
        projected = project_method_observations(self.snapshot(), exchange_input)[0]
        payload = projected.to_dict()
        self.assertEqual(payload["metric_values"], {"score": 3.5})
        self.assertEqual(payload["constraint_results"], {"feasible": False})
        self.assertEqual(payload["resource_usage"], {"wall_clock_seconds": 0.25})
        self.assertEqual(len(payload["artifacts"]), 1)
        self.assertEqual(payload["artifacts"][0]["name"], "public-result")
        self.assertNotIn("path", repr(payload["artifacts"]))
        self.assertNotIn("capture", repr(payload["artifacts"]))
        self.assertEqual(payload["error"]["details"], {"reason": "bad input"})
        self.assertIn("[redacted-path]", payload["error"]["message"])
        self.assertNotIn(str(self.root), repr(payload))
        self.assertNotIn("event_summary", payload)
        self.assertNotIn("execution_metadata", payload)

    def test_observation_cannot_be_prepared_before_the_round_drains(self) -> None:
        self.prepare_and_admit_round()
        with self.assertRaisesRegex(MethodProjectionError, "nonterminal"):
            build_method_observation_exchange_input(
                self.snapshot(), round_index=1
            )


if __name__ == "__main__":
    unittest.main()
