from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import (
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.method_exchange_records import (
    METHOD_OBSERVATION_ACK_RESULT_DIGEST,
    MethodObservationExchangeInput,
    MethodObservationPayload,
    MethodProposalExchangeInput,
    MethodTerminalTransitionRef,
    method_exchange_id,
    method_exchange_sequence,
    method_worker_response_digest,
)
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import BlobRef, SnapshotRef, canonical_json_bytes
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_snapshot import RunLedgerSnapshot
from tests.realm_run_support import (
    TEST_LEASE_TTL_SECONDS,
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmMethodExchangeLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database_path)
        self.ledger.register_principal(
            operation_id="method-exchange/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="method-exchange/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        closure, closure_bindings, source_owner_id, source_owner_revision = (
            prepare_test_run_closure(
                ledger=self.ledger,
                store=self.store,
                root=self.root,
                actor_principal_id="operator",
                prefix="method-exchange",
            )
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=3)
        self.closure = closure
        self.closure_bindings = closure_bindings
        self.source_owner_id = source_owner_id
        self.source_owner_revision = source_owner_revision
        self.manifest = manifest
        definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, closure_bindings
        )
        self.created = self.ledger.create_run_namespace(
            operation_id="method-exchange/run/create",
            actor_principal_id="operator",
            controller_holder_id="controller-a",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_owner_revision,
            run_id="run-a",
            owner_id="run-owner-a",
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"method-exchange/{self.counter}/{label}"

    @property
    def controller(self):
        return self.created.controller_lease

    def proposal_input(self, *, marker: str = "/domain/value"):
        return MethodProposalExchangeInput(
            requested_width=1,
            study_state={
                "accepted_trials": 0,
                "domain_value": marker,
                "windows_like_value": r"C:\domain\value",
            },
            evidence={"best": None, "label": "file:///opaque-domain-value"},
        )

    def prepare_proposal(
        self,
        *,
        operation_id: str,
        round_index: int = 1,
        expected_run_revision: int = 0,
        exchange_input=None,
        controller=None,
        generation: int | None = None,
    ):
        selected = self.controller if controller is None else controller
        return self.ledger.prepare_run_method_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=round_index,
            expected_run_revision=expected_run_revision,
            expected_controller_generation=(
                self.created.run.controller_generation
                if generation is None
                else generation
            ),
            controller_lease_id=selected.lease_id,
            controller_holder_id=selected.holder_id,
            controller_fencing_token=selected.fencing_token,
            exchange_input=(
                self.proposal_input() if exchange_input is None else exchange_input
            ),
        )

    @staticmethod
    def plan(*, suffix: str = "a", value: int = 1) -> RunAdmissionPlan:
        candidate_id = f"candidate-{suffix}"
        return RunAdmissionPlan(
            (
                CandidateAdmission(
                    candidate_id,
                    NormalizedCandidateEnvelope.build(
                        candidate_format="parameters", spec={"x": value}
                    ),
                    generator={"method_id": "test-method"},
                ),
            ),
            (
                LogicalTrialAdmission(
                    logical_trial_id=f"trial-{suffix}",
                    candidate_id=candidate_id,
                    seed=7,
                ),
            ),
        )

    @staticmethod
    def two_candidate_plan() -> RunAdmissionPlan:
        return RunAdmissionPlan(
            tuple(
                CandidateAdmission(
                    f"candidate-{suffix}",
                    NormalizedCandidateEnvelope.build(
                        candidate_format="parameters", spec={"x": value}
                    ),
                    generator={"method_id": "test-method"},
                )
                for suffix, value in (("a", 1), ("b", 2))
            ),
            tuple(
                LogicalTrialAdmission(
                    logical_trial_id=f"trial-{suffix}",
                    candidate_id=f"candidate-{suffix}",
                    seed=value,
                )
                for suffix, value in (("a", 1), ("b", 2))
            ),
        )

    def begin_change(self, *, expected_owner_revision: int = 0):
        return self.ledger.begin_owner_change(
            operation_id=self.op("begin-owner-change"),
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=expected_owner_revision,
            ttl_seconds=60,
        )

    def complete_admission(self, prepared, *, operation_id: str):
        plan = self.plan()
        change = self.begin_change()
        return self.ledger.complete_run_method_proposal_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="admitted",
            response_digest=method_worker_response_digest(
                {"candidates": [plan.to_dict()]}
            ),
            expected_run_revision=prepared.prepared_run_revision,
            expected_owner_revision=0,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )

    @staticmethod
    def file_plan(*, suffix: str, content_refs) -> RunAdmissionPlan:
        candidate_id = f"file-candidate-{suffix}"
        return RunAdmissionPlan(
            (
                CandidateAdmission(
                    candidate_id,
                    NormalizedCandidateEnvelope.build(
                        candidate_format="files",
                        spec={"entrypoint": "run.py"},
                        content_refs=tuple(content_refs),
                    ),
                    generator={"method_id": "file-method"},
                ),
            ),
            (
                LogicalTrialAdmission(
                    logical_trial_id=f"file-trial-{suffix}",
                    candidate_id=candidate_id,
                ),
            ),
        )

    def capture_file_candidate(
        self, change, *, suffix: str, contents: str | None = None
    ):
        source = self.root / f"file-candidate-source-{suffix}"
        source.mkdir()
        (source / "run.py").write_text(
            f"print({suffix!r})\n" if contents is None else contents,
            encoding="utf-8",
        )
        capture = self.store.capture(
            change_id=change.change_id,
            authority=self.ledger.content_capture_handle(
                actor_principal_id="operator",
                change_id=change.change_id,
                store_id=self.store.store_id,
            ),
        )
        sealed = capture.seal_tree(
            source=AllowedTreeSource(source),
            operation_id=self.op(f"seal-file-candidate-{suffix}"),
        )
        binding = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            RUN_CANDIDATE_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id=self.op(f"hold-file-candidate-{suffix}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(binding,),
        )
        return self.file_plan(
            suffix=suffix, content_refs=(sealed.snapshot_ref,)
        ), binding, sealed

    def complete_file_proposal(
        self,
        prepared,
        *,
        operation_id: str,
        change,
        plan: RunAdmissionPlan,
        bindings,
        expected_owner_revision: int = 0,
        expected_run_revision: int | None = None,
        round_index: int = 1,
        controller=None,
        rebase: bool = True,
    ):
        selected = self.controller if controller is None else controller
        return self.ledger.complete_run_method_proposal_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=round_index,
            prepared_input_digest=prepared.input_digest,
            outcome="admitted",
            response_digest=method_worker_response_digest(
                {"candidates": [item.to_dict() for item in plan.candidates]}
            ),
            expected_run_revision=(
                prepared.prepared_run_revision
                if expected_run_revision is None
                else expected_run_revision
            ),
            expected_owner_revision=expected_owner_revision,
            controller_lease_id=selected.lease_id,
            controller_holder_id=selected.holder_id,
            controller_fencing_token=selected.fencing_token,
            change_id=change.change_id,
            plan=plan,
            content_bindings=tuple(bindings),
            rebase_file_candidate_owner_change=rebase,
        )

    @staticmethod
    def observation_input(
        *references: MethodTerminalTransitionRef,
    ) -> MethodObservationExchangeInput:
        observations = []
        for reference in references:
            transition = reference.transition
            candidate_id = transition.logical_trial_id.replace(
                "trial", "candidate", 1
            )
            observations.append(
                MethodObservationPayload(
                    logical_trial_id=transition.logical_trial_id,
                    candidate_id=candidate_id,
                    status=transition.outcome,
                    metric_values={"score": 1.25},
                    constraint_results={"feasible": True},
                    resource_usage={"wall_clock_seconds": 0.5},
                    artifacts=(
                        {
                            "name": "preview",
                            "content_ref": "sha256:" + "a" * 64,
                        },
                    ),
                    error=(
                        None
                        if transition.outcome == "success"
                        else {
                            "phase": "run",
                            "code": transition.code or transition.outcome,
                            "message": "The trial ended before evaluation.",
                        }
                    ),
                )
            )
        return MethodObservationExchangeInput(tuple(references), tuple(observations))

    def cancel_trial(self, *, expected_run_revision: int = 2):
        return self.ledger.cancel_run_logical_trial(
            operation_id=self.op("cancel-trial"),
            actor_principal_id="operator",
            run_id="run-a",
            logical_trial_id="trial-a",
            expected_run_revision=expected_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            code="user_cancelled",
        )

    def test_admission_observation_ack_and_next_round_form_one_durable_stream(self) -> None:
        operation_id = self.op("prepare-proposal")
        prepared = self.prepare_proposal(operation_id=operation_id)
        replay = self.prepare_proposal(operation_id=operation_id)
        self.assertEqual(replay, prepared)
        self.assertEqual(prepared.prepared_run_revision, 1)
        prepared_head = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(prepared_head.revision.revision, 1)
        self.assertEqual(prepared_head.revision.last_sequence, 1)
        prepared_timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=1,
            expected_head_sequence=1,
        )
        self.assertEqual(
            [event.event for event in prepared_timeline.items],
            ["method_exchange_prepared"],
        )
        self.assertEqual(
            prepared_timeline.items[0].method_exchange_id,
            prepared.exchange_id,
        )
        self.assertEqual(
            prepared.exchange_id,
            method_exchange_id(run_id="run-a", round_index=1, kind="proposal"),
        )
        self.assertEqual(prepared.exchange_input.study_state["domain_value"], "/domain/value")

        completion_operation = self.op("complete-admission")
        completed = self.complete_admission(
            prepared, operation_id=completion_operation
        )
        self.assertEqual(completed.completion.outcome, "admitted")
        self.assertEqual(
            completed.completion.completed_txn_id,
            completed.admission.revision.txn_id,
        )
        self.assertEqual(completed.completion.logical_trial_ids, ("trial-a",))
        self.assertEqual(completed.completion.result_digest, self.plan().digest)

        cancelled = self.cancel_trial()
        observation_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=cancelled.run.controller_generation,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=observation_input,
        )
        self.assertEqual(
            observation.exchange_input.worker_request,
            {
                "observations": [
                    observation_input.observations[0].to_dict()
                ]
            },
        )
        self.assertEqual(
            observation.exchange_input.observations[0].metric_values["score"],
            1.25,
        )
        changed_payload = observation_input.to_dict()
        changed_payload["observations"][0]["metric_values"]["score"] = 9.0
        changed_input = MethodObservationExchangeInput.from_dict(changed_payload)
        self.assertNotEqual(changed_input.digest, observation.input_digest)
        ack_operation = self.op("ack-observation")
        ack_response_digest = method_worker_response_digest({"ok": True})
        ack = self.ledger.complete_run_method_observation_exchange(
            operation_id=ack_operation,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            outcome="acknowledged",
            response_digest=ack_response_digest,
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(
            self.ledger.complete_run_method_observation_exchange(
                operation_id=ack_operation,
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=observation.input_digest,
                outcome="acknowledged",
                response_digest=ack_response_digest,
                expected_run_revision=observation.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            ),
            ack,
        )
        ack_head = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(
            ack_head.revision.revision,
            ack.completion.committed_run_revision,
        )
        self.assertEqual(ack.completion.logical_trial_ids, ("trial-a",))
        self.assertEqual(
            ack.completion.result_digest,
            METHOD_OBSERVATION_ACK_RESULT_DIGEST,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.complete_run_method_observation_exchange(
                operation_id=ack_operation,
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=observation.input_digest,
                outcome="acknowledged",
                response_digest="0" * 64,
                expected_run_revision=observation.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )

        second = self.prepare_proposal(
            operation_id=self.op("prepare-second-proposal"),
            round_index=2,
            expected_run_revision=ack.completion.committed_run_revision,
        )
        self.assertEqual(second.round_index, 2)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(
            RunLedgerSnapshot.from_dict(snapshot.to_dict()), snapshot
        )
        self.assertEqual(len(snapshot.method_exchange_preparations), 3)
        self.assertEqual(len(snapshot.method_exchange_completions), 2)
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        method_events = [
            (
                event.event,
                event.method_round_index,
                event.method_exchange_kind,
            )
            for event in timeline.items
            if event.method_exchange_id is not None
        ]
        self.assertEqual(
            method_events,
            [
                ("method_exchange_prepared", 1, "proposal"),
                ("method_exchange_completed", 1, "proposal"),
                ("method_exchange_prepared", 1, "observation"),
                ("method_exchange_completed", 1, "observation"),
                ("method_exchange_prepared", 2, "proposal"),
            ],
        )

    def test_empty_proposal_closes_atomically_and_changed_replay_is_rejected(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-empty"))
        operation_id = self.op("complete-empty")
        response_digest = method_worker_response_digest({"candidates": []})
        result = self.ledger.complete_run_method_proposal_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="empty",
            response_digest=response_digest,
            expected_run_revision=prepared.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(result.completion.outcome, "empty")
        self.assertEqual(result.control.record.stop_code, "method_completed")
        self.assertEqual(
            result.completion.completed_txn_id, result.control.revision.txn_id
        )
        self.assertEqual(result.completion.committed_run_revision, 2)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.candidates, ())
        with self.assertRaises(RealmConflict):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=operation_id,
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="empty",
                response_digest="0" * 64,
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )

    def test_protocol_failure_is_typed_bounded_and_has_no_admission_gap(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-failure"))
        result = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("complete-protocol-failure"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="protocol_error",
            response_digest=method_worker_response_digest(
                {"kind": "oversized_proposal"}
            ),
            error_code="proposal_oversized",
            expected_run_revision=prepared.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(result.completion.error_code, "proposal_oversized")
        self.assertEqual(result.control.record.stop_code, "protocol_error")
        self.assertEqual(result.completion.logical_trial_ids, ())
        with self.assertRaisesRegex(ValueError, "lowercase token"):
            replace(result.completion, error_code="Not_A_Canonical_Code")
        with self.assertRaises(ValueError):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("invalid-error"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="method_failed",
                response_digest="0" * 64,
                error_code="/private/tmp/traceback",
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )

    def _prepared_observation(self, label: str):
        """Admit a candidate, cancel its trial, and prepare the observe call."""

        prepared = self.prepare_proposal(operation_id=self.op(f"propose-{label}"))
        self.complete_admission(prepared, operation_id=self.op(f"admit-{label}"))
        cancelled = self.cancel_trial()
        return self.ledger.prepare_run_method_exchange(
            operation_id=self.op(f"prepare-{label}"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=cancelled.run.controller_generation,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=self.observation_input(
                MethodTerminalTransitionRef(cancelled.transition)
            ),
        )

    def _failed_observation(self, *, error_json, operation: str):
        observation = self._prepared_observation(operation)
        return self.ledger.complete_run_method_observation_exchange(
            operation_id=self.op(operation),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            outcome="method_failed",
            response_digest=method_worker_response_digest(
                {"ok": False, "error": {"code": "worker_crashed"}}
            ),
            error_code="observe_worker_failed",
            error_json=error_json,
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )

    def test_a_failed_exchange_records_why_the_method_failed(self) -> None:
        # Without this the only account of a broken method is the word
        # "method_failed", and a Run whose trials all succeeded has no failed
        # trial to open instead.
        completed = self._failed_observation(
            error_json={
                "type": "RuntimeError",
                "message": "FileNotFoundError: [Errno 2] no such directory: '<path>'",
                "truncated": False,
            },
            operation="complete-observe-with-cause",
        )
        recorded = completed.completion.error_json

        self.assertEqual(recorded["type"], "RuntimeError")
        self.assertIn("FileNotFoundError", recorded["message"])
        self.assertFalse(recorded["truncated"])

        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        durable = snapshot.method_exchange_completions[-1]
        self.assertEqual(durable.error_json, recorded)

    def test_a_failed_exchange_without_a_recovered_cause_records_none(self) -> None:
        # The worker's diagnostic volume is erased seconds after it dies, so
        # the cause is sometimes genuinely unrecoverable. That must read as
        # "nothing retained", never as an empty explanation.
        completed = self._failed_observation(
            error_json=None, operation="complete-observe-without-cause"
        )
        self.assertIsNone(completed.completion.error_json)

    def test_an_acknowledged_exchange_cannot_carry_a_cause(self) -> None:
        observation = self._prepared_observation("ack-cause")
        with self.assertRaises(ValueError):
            self.ledger.complete_run_method_observation_exchange(
                operation_id=self.op("ack-with-cause"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=observation.input_digest,
                outcome="acknowledged",
                response_digest=method_worker_response_digest({"ok": True}),
                error_json={"type": "RuntimeError", "message": "x", "truncated": False},
                expected_run_revision=observation.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )

    def test_oversized_admission_is_rejected_before_owner_change_and_can_close_typed(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-oversized"))
        change = self.begin_change()
        plan = self.two_candidate_plan()
        response = {"candidates": [item.to_dict() for item in plan.candidates]}
        response_digest = method_worker_response_digest(response)
        with self.assertRaisesRegex(RealmConflict, "exceeds its requested width"):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("reject-oversized"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="admitted",
                response_digest=response_digest,
                expected_run_revision=prepared.prepared_run_revision,
                expected_owner_revision=0,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                change_id=change.change_id,
                plan=plan,
            )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.run.current_revision, 1)
        self.assertEqual(snapshot.candidates, ())
        self.assertEqual(snapshot.logical_trials, ())
        closed = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("complete-oversized-protocol-error"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="protocol_error",
            response_digest=response_digest,
            error_code="proposal_oversized",
            expected_run_revision=prepared.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(closed.completion.outcome, "protocol_error")
        self.assertEqual(closed.control.record.stop_code, "protocol_error")

    def test_file_proposal_owner_change_rebases_additively_and_replays(self) -> None:
        proposal_change = self.begin_change()
        proposal_plan, proposal_binding, _ = self.capture_file_candidate(
            proposal_change, suffix="proposal"
        )
        unrelated_change = self.begin_change()
        unrelated_plan, unrelated_binding, _ = self.capture_file_candidate(
            unrelated_change, suffix="unrelated"
        )
        unrelated = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admit-unrelated-file-candidate"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            change_id=unrelated_change.change_id,
            plan=unrelated_plan,
            content_bindings=(unrelated_binding,),
        )
        self.assertEqual(unrelated.owner_commit.owner_revision, 1)
        with self.assertRaises(RealmConflict):
            self.ledger.commit_run_candidate_admissions(
                operation_id=self.op("generic-admission-stays-strict"),
                actor_principal_id="operator",
                run_id="run-a",
                expected_run_revision=unrelated.revision.revision,
                expected_owner_revision=0,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                change_id=proposal_change.change_id,
                plan=proposal_plan,
                content_bindings=(proposal_binding,),
            )

        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-proposal-after-owner-advance"),
            expected_run_revision=unrelated.revision.revision,
        )
        operation_id = self.op("complete-rebased-file-proposal")
        admitted = self.complete_file_proposal(
            prepared,
            operation_id=operation_id,
            change=proposal_change,
            plan=proposal_plan,
            bindings=(proposal_binding,),
        )
        replay = self.complete_file_proposal(
            prepared,
            operation_id=operation_id,
            change=proposal_change,
            plan=proposal_plan,
            bindings=(proposal_binding,),
        )
        self.assertEqual(replay, admitted)
        assert admitted.admission is not None
        self.assertEqual(admitted.admission.owner_commit.previous_revision, 1)
        self.assertEqual(admitted.admission.owner_commit.owner_revision, 2)
        self.assertEqual(admitted.admission.run.accepted_logical_trials, 2)
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=self.created.run.owner_id
        )
        self.assertIn(unrelated_binding, memberships)
        self.assertIn(proposal_binding, memberships)
        with self.assertRaises(RealmConflict):
            self.complete_file_proposal(
                prepared,
                operation_id=operation_id,
                change=proposal_change,
                plan=proposal_plan,
                bindings=(proposal_binding,),
                rebase=False,
            )

    def test_admitted_proposal_recovery_returns_exact_historical_receipt(
        self,
    ) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-recoverable-proposal")
        )
        plan = self.plan()
        response_digest = method_worker_response_digest(
            {"candidates": [plan.to_dict()]}
        )
        operation_id = (
            f"run/run-a/method-proposal/{prepared.exchange_id}/complete"
        )
        self.assertIsNone(
            self.ledger.read_run_method_proposal_completion_receipt(
                actor_principal_id="operator",
                run_id="run-a",
                exchange_id=prepared.exchange_id,
                expected_prepared_input_digest=prepared.input_digest,
                expected_response_digest=response_digest,
            )
        )

        admitted = self.complete_admission(
            prepared, operation_id=operation_id
        )
        recovered = self.ledger.read_run_method_proposal_completion_receipt(
            actor_principal_id="operator",
            run_id="run-a",
            exchange_id=prepared.exchange_id,
            expected_prepared_input_digest=prepared.input_digest,
            expected_response_digest=response_digest,
        )
        self.assertEqual(recovered, admitted)

        # Recovery is historical: later run revisions must not rewrite the
        # admission receipt or force reconstruction from the current head.
        self.cancel_trial(expected_run_revision=2)
        self.assertEqual(
            self.ledger.read_run_method_proposal_completion_receipt(
                actor_principal_id="operator",
                run_id="run-a",
                exchange_id=prepared.exchange_id,
                expected_prepared_input_digest=prepared.input_digest,
                expected_response_digest=response_digest,
            ),
            admitted,
        )

    def test_admitted_proposal_recovery_rejects_wrong_anchors_and_actor(
        self,
    ) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-anchor-checked-proposal")
        )
        plan = self.plan()
        response_digest = method_worker_response_digest(
            {"candidates": [plan.to_dict()]}
        )
        self.complete_admission(
            prepared,
            operation_id=(
                f"run/run-a/method-proposal/{prepared.exchange_id}/complete"
            ),
        )
        for label, prepared_digest, result_digest in (
            ("prepared", "a" * 64, response_digest),
            ("response", prepared.input_digest, "b" * 64),
        ):
            with self.subTest(label=label), self.assertRaises(RealmConflict):
                self.ledger.read_run_method_proposal_completion_receipt(
                    actor_principal_id="operator",
                    run_id="run-a",
                    exchange_id=prepared.exchange_id,
                    expected_prepared_input_digest=prepared_digest,
                    expected_response_digest=result_digest,
                )

        self.ledger.register_principal(
            operation_id=self.op("register-recovery-outsider"),
            principal_id="outsider",
            kind="human",
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_run_method_proposal_completion_receipt(
                actor_principal_id="outsider",
                run_id="run-a",
                exchange_id=prepared.exchange_id,
                expected_prepared_input_digest=prepared.input_digest,
                expected_response_digest=response_digest,
            )

    def test_admitted_proposal_recovery_rejects_tampered_receipt(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-tamper-checked-proposal")
        )
        plan = self.plan()
        response_digest = method_worker_response_digest(
            {"candidates": [plan.to_dict()]}
        )
        operation_id = (
            f"run/run-a/method-proposal/{prepared.exchange_id}/complete"
        )
        self.complete_admission(prepared, operation_id=operation_id)
        connection = sqlite3.connect(self.database_path)
        try:
            stored = connection.execute(
                "SELECT receipt_json FROM ledger_transactions "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            assert stored is not None
            tampered = json.loads(stored[0])
            tampered["candidates"][0]["admission"]["generator"] = {
                "tampered": True
            }
            connection.execute(
                "UPDATE ledger_transactions SET receipt_json = ? "
                "WHERE operation_id = ?",
                (
                    canonical_json_bytes(tampered).decode("utf-8"),
                    operation_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmIntegrityError):
            self.ledger.read_run_method_proposal_completion_receipt(
                actor_principal_id="operator",
                run_id="run-a",
                exchange_id=prepared.exchange_id,
                expected_prepared_input_digest=prepared.input_digest,
                expected_response_digest=response_digest,
            )

    def test_admitted_proposal_recovery_rejects_reused_operation_kind(
        self,
    ) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-kind-checked-proposal")
        )
        plan = self.plan()
        response_digest = method_worker_response_digest(
            {"candidates": [plan.to_dict()]}
        )
        operation_id = (
            f"run/run-a/method-proposal/{prepared.exchange_id}/complete"
        )
        self.complete_admission(prepared, operation_id=operation_id)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE ledger_transactions SET operation_kind = 'owner.create' "
                "WHERE operation_id = ?",
                (operation_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmConflict):
            self.ledger.read_run_method_proposal_completion_receipt(
                actor_principal_id="operator",
                run_id="run-a",
                exchange_id=prepared.exchange_id,
                expected_prepared_input_digest=prepared.input_digest,
                expected_response_digest=response_digest,
            )

    def test_file_proposal_rebase_deduplicates_shared_cas_snapshot(self) -> None:
        wide_manifest = replace(self.manifest, proposal_width=2)
        wide_definition, wide_bindings = prepare_test_run_definition(
            self.closure, wide_manifest, self.closure_bindings
        )
        wide = self.ledger.create_run_namespace(
            operation_id=self.op("create-shared-cas-run"),
            actor_principal_id="operator",
            controller_holder_id="controller-wide",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=wide_definition,
            definition_bindings=wide_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id="run-shared-cas",
            owner_id="run-owner-shared-cas",
        )
        prepared = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-shared-cas-proposal"),
            actor_principal_id="operator",
            run_id=wide.run.run_id,
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=wide.controller_lease.lease_id,
            controller_holder_id=wide.controller_lease.holder_id,
            controller_fencing_token=wide.controller_lease.fencing_token,
            exchange_input=MethodProposalExchangeInput(2, {}, {}),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-shared-cas-change"),
            actor_principal_id="operator",
            owner_id=wide.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        base_plan, binding, _ = self.capture_file_candidate(
            change,
            suffix="shared-cas",
            contents="print('one immutable tree')\n",
        )
        shared_envelope = base_plan.candidates[0].envelope
        plan = RunAdmissionPlan(
            (
                CandidateAdmission(
                    "file-candidate-shared-cas-a",
                    shared_envelope,
                    generator={"method_id": "file-method"},
                ),
                CandidateAdmission(
                    "file-candidate-shared-cas-b",
                    shared_envelope,
                    generator={"method_id": "file-method"},
                ),
            ),
            (
                LogicalTrialAdmission(
                    "file-trial-shared-cas-a",
                    "file-candidate-shared-cas-a",
                ),
                LogicalTrialAdmission(
                    "file-trial-shared-cas-b",
                    "file-candidate-shared-cas-b",
                ),
            ),
        )
        response_digest = method_worker_response_digest(
            {"candidates": [item.to_dict() for item in plan.candidates]}
        )
        admitted = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("complete-shared-cas-proposal"),
            actor_principal_id="operator",
            run_id=wide.run.run_id,
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="admitted",
            response_digest=response_digest,
            expected_run_revision=prepared.prepared_run_revision,
            expected_owner_revision=0,
            controller_lease_id=wide.controller_lease.lease_id,
            controller_holder_id=wide.controller_lease.holder_id,
            controller_fencing_token=wide.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=plan,
            content_bindings=(binding,),
            rebase_file_candidate_owner_change=True,
        )
        assert admitted.admission is not None
        self.assertEqual(len(admitted.admission.candidates), 2)
        self.assertEqual(
            {
                item.candidate_ref
                for item in admitted.admission.candidates
            },
            {shared_envelope.candidate_ref},
        )
        self.assertEqual(admitted.admission.owner_commit.additions, (binding,))
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=wide.run.owner_id
        )
        self.assertEqual(memberships.count(binding), 1)

    def test_file_proposal_rebase_does_not_bump_owner_for_existing_membership(
        self,
    ) -> None:
        shared_contents = "print('shared immutable candidate')\n"
        proposal_change = self.begin_change()
        proposal_plan, proposal_binding, _ = self.capture_file_candidate(
            proposal_change,
            suffix="proposal-shared",
            contents=shared_contents,
        )
        unrelated_change = self.begin_change()
        unrelated_plan, unrelated_binding, _ = self.capture_file_candidate(
            unrelated_change,
            suffix="unrelated-shared",
            contents=shared_contents,
        )
        self.assertEqual(unrelated_binding, proposal_binding)
        unrelated = self.ledger.commit_run_candidate_admissions(
            operation_id=self.op("admit-shared-file-membership"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            change_id=unrelated_change.change_id,
            plan=unrelated_plan,
            content_bindings=(unrelated_binding,),
        )
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-proposal-with-shared-membership"),
            expected_run_revision=unrelated.revision.revision,
        )
        admitted = self.complete_file_proposal(
            prepared,
            operation_id=self.op("complete-proposal-with-shared-membership"),
            change=proposal_change,
            plan=proposal_plan,
            bindings=(proposal_binding,),
        )
        assert admitted.admission is not None
        self.assertEqual(admitted.admission.owner_commit.previous_revision, 1)
        self.assertEqual(admitted.admission.owner_commit.owner_revision, 1)
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=self.created.run.owner_id
        )
        self.assertEqual(memberships.count(proposal_binding), 1)

    def test_file_proposal_rebase_rejects_non_file_and_non_exact_shapes(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-invalid-file-rebase-shapes")
        )
        change = self.begin_change()
        tree_a = SnapshotRef("a" * 64)
        tree_b = SnapshotRef("b" * 64)
        blob = BlobRef("c" * 64)
        binding_a = OwnerMembership(
            self.store.store_id, tree_a, RUN_CANDIDATE_ROLE
        )
        binding_b = OwnerMembership(
            self.store.store_id, tree_b, RUN_CANDIDATE_ROLE
        )
        file_a = self.file_plan(suffix="shape-a", content_refs=(tree_a,))
        parameter = self.plan(suffix="shape-parameter")
        opaque = RunAdmissionPlan(
            (
                CandidateAdmission(
                    "opaque-shape",
                    NormalizedCandidateEnvelope.build(
                        candidate_format="opaque", spec={"value": 1}
                    ),
                ),
            ),
            (LogicalTrialAdmission("opaque-shape-trial", "opaque-shape"),),
        )
        mixed = RunAdmissionPlan(
            (parameter.candidates[0], file_a.candidates[0]),
            (parameter.logical_trials[0], file_a.logical_trials[0]),
        )
        cases = (
            ("parameter", parameter, ()),
            ("opaque", opaque, ()),
            ("mixed", mixed, (binding_a,)),
            (
                "blob",
                self.file_plan(suffix="shape-blob", content_refs=(blob,)),
                (
                    OwnerMembership(
                        self.store.store_id, blob, RUN_CANDIDATE_ROLE
                    ),
                ),
            ),
            (
                "multiple-refs",
                self.file_plan(
                    suffix="shape-multiple", content_refs=(tree_a, tree_b)
                ),
                (binding_a, binding_b),
            ),
            ("missing-binding", file_a, ()),
            ("extra-binding", file_a, (binding_a, binding_b)),
            (
                "wrong-role",
                file_a,
                (OwnerMembership(self.store.store_id, tree_a, "other-role"),),
            ),
            (
                "same-ref-two-stores",
                file_a,
                (
                    binding_a,
                    OwnerMembership("local-b", tree_a, RUN_CANDIDATE_ROLE),
                ),
            ),
        )
        for label, plan, bindings in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.complete_file_proposal(
                    prepared,
                    operation_id=self.op(f"reject-{label}"),
                    change=change,
                    plan=plan,
                    bindings=bindings,
                )
        with self.assertRaises(ValueError):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("reject-duplicate-binding"),
                change=change,
                plan=file_a,
                bindings=(binding_a, binding_a),
            )
        with self.assertRaises(TypeError):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("reject-nonboolean-rebase"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="empty",
                response_digest=method_worker_response_digest({"candidates": []}),
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                rebase_file_candidate_owner_change=1,
            )
        with self.assertRaises(ValueError):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("reject-rebase-on-closed-proposal"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="empty",
                response_digest=method_worker_response_digest({"candidates": []}),
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                rebase_file_candidate_owner_change=True,
            )

    def test_file_proposal_rebase_requires_exact_pending_exchange_and_base(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-exchange-guards")
        )
        change = self.begin_change()
        plan, binding, _ = self.capture_file_candidate(
            change, suffix="exchange-guards"
        )
        with self.assertRaises(RealmConflict):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("wrong-proposal-round"),
                change=change,
                plan=plan,
                bindings=(binding,),
                round_index=2,
            )
        with self.assertRaises(RealmConflict):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("wrong-owner-change-base"),
                change=change,
                plan=plan,
                bindings=(binding,),
                expected_owner_revision=1,
            )
        admitted = self.complete_file_proposal(
            prepared,
            operation_id=self.op("complete-file-exchange-once"),
            change=change,
            plan=plan,
            bindings=(binding,),
        )
        assert admitted.admission is not None
        with self.assertRaises(RealmConflict):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("complete-file-exchange-twice"),
                change=change,
                plan=plan,
                bindings=(binding,),
                expected_run_revision=admitted.admission.revision.revision,
            )

    def test_file_proposal_rebase_rejects_expired_change(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-expired-file-change")
        )
        change = self.begin_change()
        plan, binding, _ = self.capture_file_candidate(
            change, suffix="expired-change"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE owner_transactions SET expires_at = 0 WHERE change_id = ?",
                (change.change_id,),
            )
            connection.execute(
                "UPDATE leases SET expires_at = 0 WHERE lease_id = ?",
                (change.retention_lease_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmExpired):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("reject-expired-file-change"),
                change=change,
                plan=plan,
                bindings=(binding,),
            )

    def test_file_proposal_rebase_fences_stale_controller_and_allows_takeover(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-before-controller-takeover")
        )
        change = self.begin_change()
        plan, binding, _ = self.capture_file_candidate(
            change, suffix="controller-takeover"
        )
        replacement = self.ledger.replace_run_controller(
            operation_id=self.op("replace-file-proposal-controller"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_controller_generation=1,
            expected_controller_lease_id=self.controller.lease_id,
            expected_controller_holder_id=self.controller.holder_id,
            expected_controller_fencing_token=self.controller.fencing_token,
            new_controller_holder_id="controller-b",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        with self.assertRaises(RealmConflict):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("stale-file-proposal-controller"),
                change=change,
                plan=plan,
                bindings=(binding,),
                expected_run_revision=replacement.run.current_revision,
                controller=self.controller,
            )
        admitted = self.complete_file_proposal(
            prepared,
            operation_id=self.op("replacement-file-proposal-controller"),
            change=change,
            plan=plan,
            bindings=(binding,),
            expected_run_revision=replacement.run.current_revision,
            controller=replacement.controller_lease,
        )
        self.assertEqual(admitted.completion.controller_generation, 2)

    def test_file_proposal_rebase_rejects_changed_planned_holds(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-with-extra-hold")
        )
        change = self.begin_change()
        plan, binding, _ = self.capture_file_candidate(
            change, suffix="intended-hold"
        )
        self.capture_file_candidate(change, suffix="unexpected-hold")
        with self.assertRaisesRegex(RealmConflict, "exact provisional holds"):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("reject-extra-file-hold"),
                change=change,
                plan=plan,
                bindings=(binding,),
            )

    def test_file_proposal_rebase_rejects_changed_transitive_hold_closure(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-with-broken-closure")
        )
        change = self.begin_change()
        plan, binding, sealed = self.capture_file_candidate(
            change, suffix="broken-closure"
        )
        child_ref = next(
            item.blob_ref for item in sealed.manifest.entries if item.blob_ref is not None
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "DELETE FROM lease_content WHERE lease_id = ? AND content_ref = ?",
                (change.retention_lease_id, str(child_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RealmConflict, "transitive content closure"):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("reject-broken-file-hold-closure"),
                change=change,
                plan=plan,
                bindings=(binding,),
            )

    def test_file_proposal_rebase_rejects_non_live_binding(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-file-with-non-live-binding")
        )
        change = self.begin_change()
        plan, binding, _ = self.capture_file_candidate(
            change, suffix="non-live-binding"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE content_objects SET lifecycle_state = 'corrupt' "
                "WHERE store_id = ? AND content_ref = ?",
                (binding.store_id, str(binding.content_ref)),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RealmConflict, "unavailable or unverified"):
            self.complete_file_proposal(
                prepared,
                operation_id=self.op("reject-non-live-file-binding"),
                change=change,
                plan=plan,
                bindings=(binding,),
            )

    def test_sql_guard_rejects_oversized_admission_if_python_check_is_bypassed(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-sql-width"))
        change = self.begin_change()
        plan = self.two_candidate_plan()
        response_digest = method_worker_response_digest(
            {"candidates": [item.to_dict() for item in plan.candidates]}
        )
        bypass_input = MethodProposalExchangeInput(2, {}, {})
        with mock.patch.object(
            self.ledger,
            "_load_method_proposal_preparation_in_txn",
            return_value=(mock.sentinel.prepared_row, bypass_input),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger.complete_run_method_proposal_exchange(
                    operation_id=self.op("sql-reject-oversized"),
                    actor_principal_id="operator",
                    run_id="run-a",
                    round_index=1,
                    prepared_input_digest=prepared.input_digest,
                    outcome="admitted",
                    response_digest=response_digest,
                    expected_run_revision=prepared.prepared_run_revision,
                    expected_owner_revision=0,
                    controller_lease_id=self.controller.lease_id,
                    controller_holder_id=self.controller.holder_id,
                    controller_fencing_token=self.controller.fencing_token,
                    change_id=change.change_id,
                    plan=plan,
                )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertEqual(snapshot.run.current_revision, 1)
        self.assertEqual(snapshot.candidates, ())
        self.assertEqual(snapshot.logical_trials, ())

    def test_observe_failure_closes_atomically_replays_and_never_forges_ack(self) -> None:
        proposal = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(proposal, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        exchange_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=cancelled.run.controller_generation,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=exchange_input,
        )
        operation_id = self.op("complete-observe-failure")
        worker_response = {
            "ok": False,
            "error": {"code": "worker_crashed"},
        }
        response_digest = method_worker_response_digest(worker_response)
        completed = self.ledger.complete_run_method_observation_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            outcome="method_failed",
            response_digest=response_digest,
            error_code="observe_worker_failed",
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(completed.completion.outcome, "method_failed")
        self.assertEqual(completed.control.record.stop_code, "method_failed")
        self.assertEqual(
            completed.completion.completed_txn_id,
            completed.control.revision.txn_id,
        )
        self.assertEqual(
            completed.completion.committed_run_revision,
            completed.control.revision.revision,
        )

        self.ledger.close()
        self.ledger = RealmLedger(self.database_path)
        replay = self.ledger.complete_run_method_observation_exchange(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            outcome="method_failed",
            response_digest=response_digest,
            error_code="observe_worker_failed",
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(replay, completed)
        with self.assertRaises(RealmConflict):
            self.ledger.complete_run_method_observation_exchange(
                operation_id=operation_id,
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=observation.input_digest,
                outcome="method_failed",
                response_digest=method_worker_response_digest(
                    {"ok": False, "error": {"code": "different"}}
                ),
                error_code="observe_worker_failed",
                expected_run_revision=observation.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )
        with self.assertRaises(RealmConflict):
            self.prepare_proposal(
                operation_id=self.op("proposal-after-observe-failure"),
                round_index=2,
                expected_run_revision=completed.control.revision.revision,
            )
        finished = self.ledger.finish_run(
            operation_id=self.op("finish-after-observe-failure"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=completed.control.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "failed")

    def test_final_max_trials_observe_failure_preserves_drain_and_dominates_finish(self) -> None:
        final_manifest = replace(self.manifest, max_trials=1)
        definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            final_manifest,
            self.closure_bindings,
        )
        created = self.ledger.create_run_namespace(
            operation_id=self.op("create-final-run"),
            actor_principal_id="operator",
            controller_holder_id="final-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id="run-final",
            owner_id="run-final-owner",
        )
        controller = created.controller_lease
        proposal = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-final-proposal"),
            actor_principal_id="operator",
            run_id="run-final",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            exchange_input=MethodProposalExchangeInput(1, {}, {}),
        )
        plan = self.plan(suffix="final-a")
        change = self.ledger.begin_owner_change(
            operation_id=self.op("begin-final-owner-change"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("admit-final"),
            actor_principal_id="operator",
            run_id="run-final",
            round_index=1,
            prepared_input_digest=proposal.input_digest,
            outcome="admitted",
            response_digest=method_worker_response_digest(
                {"candidates": [item.to_dict() for item in plan.candidates]}
            ),
            expected_run_revision=proposal.prepared_run_revision,
            expected_owner_revision=0,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )
        after_admission = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-final"
        )
        self.assertEqual(after_admission.control.current_submission.state, "draining")
        self.assertEqual(after_admission.control.current_submission.stop_code, "max_trials")
        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id=self.op("cancel-final-trial"),
            actor_principal_id="operator",
            run_id="run-final",
            logical_trial_id="trial-final-a",
            expected_run_revision=admitted.admission.revision.revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            code="user_cancelled",
        )
        exchange_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-final-observation"),
            actor_principal_id="operator",
            run_id="run-final",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=1,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            exchange_input=exchange_input,
        )
        completed = self.ledger.complete_run_method_observation_exchange(
            operation_id=self.op("fail-final-observation"),
            actor_principal_id="operator",
            run_id="run-final",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            outcome="method_failed",
            response_digest=method_worker_response_digest(
                {"ok": False, "error": {"code": "worker_crashed"}}
            ),
            error_code="observe_worker_failed",
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
        )
        self.assertIsNone(completed.control)
        self.assertEqual(
            completed.completion.committed_run_revision,
            observation.prepared_run_revision + 1,
        )
        before_finish = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-final"
        )
        self.assertEqual(before_finish.control.current_submission.stop_code, "max_trials")
        finished = self.ledger.finish_run(
            operation_id=self.op("finish-final-observe-failure"),
            actor_principal_id="operator",
            run_id="run-final",
            expected_run_revision=completed.completion.committed_run_revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "failed")
        self.assertEqual(finished.finalization.code, "observe_worker_failed")

    def test_pending_proposal_survives_controller_replacement_but_old_fence_cannot_complete(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-before-handoff"))
        replacement = self.ledger.replace_run_controller(
            operation_id=self.op("replace-controller"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_controller_generation=1,
            expected_controller_lease_id=self.controller.lease_id,
            expected_controller_holder_id=self.controller.holder_id,
            expected_controller_fencing_token=self.controller.fencing_token,
            new_controller_holder_id="controller-b",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("stale-completion"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="empty",
                response_digest=method_worker_response_digest({"candidates": []}),
                expected_run_revision=1,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )
        completed = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("replacement-completion"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="empty",
            response_digest=method_worker_response_digest({"candidates": []}),
            expected_run_revision=replacement.run.current_revision,
            controller_lease_id=replacement.controller_lease.lease_id,
            controller_holder_id=replacement.controller_lease.holder_id,
            controller_fencing_token=replacement.controller_lease.fencing_token,
        )
        self.assertEqual(prepared.controller_generation, 1)
        self.assertEqual(completed.completion.controller_generation, 2)
        self.assertEqual(completed.completion.committed_run_revision, 3)

    def test_schema_guard_fences_old_term_observation_ack_after_takeover(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(prepared, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        observation_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=1,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=observation_input,
        )
        replacement = self.ledger.replace_run_controller(
            operation_id=self.op("replace-before-ack"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_controller_generation=1,
            expected_controller_lease_id=self.controller.lease_id,
            expected_controller_holder_id=self.controller.holder_id,
            expected_controller_fencing_token=self.controller.fencing_token,
            new_controller_holder_id="controller-b",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'run.method.observation.ack', ?, '{}', ?)",
                (self.op("stale-direct-ack"), "0" * 64, now),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO run_method_exchange_completions("
                    "exchange_id, run_id, round_index, kind, prepared_input_digest, "
                    "outcome, response_digest, result_digest, error_code, "
                    "logical_trial_ids_json, committed_run_revision, "
                    "controller_generation, controller_lease_id, "
                    "controller_fencing_token, completed_by_principal_id, "
                    "completed_txn_id, created_at) VALUES (?, 'run-a', 1, "
                    "'observation', ?, 'acknowledged', ?, ?, NULL, '[\"trial-a\"]', "
                    "?, 1, ?, ?, 'operator', ?, ?)",
                    (
                        observation.exchange_id,
                        observation.input_digest,
                        method_worker_response_digest({"ok": True}),
                        METHOD_OBSERVATION_ACK_RESULT_DIGEST,
                        replacement.run.current_revision,
                        self.controller.lease_id,
                        self.controller.fencing_token,
                        txn_id,
                        now,
                    ),
                )
        finally:
            connection.rollback()
            connection.close()
        ack = self.ledger.acknowledge_run_method_observation_exchange(
            operation_id=self.op("replacement-ack"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            prepared_input_digest=observation.input_digest,
            response_digest=method_worker_response_digest({"ok": True}),
            expected_run_revision=replacement.run.current_revision,
            controller_lease_id=replacement.controller_lease.lease_id,
            controller_holder_id=replacement.controller_lease.holder_id,
            controller_fencing_token=replacement.controller_lease.fencing_token,
        )
        self.assertEqual(ack.completion.controller_generation, 2)

    def test_schema_guard_rejects_null_completion_logical_trial_id(self) -> None:
        proposal = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(proposal, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=1,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=self.observation_input(
                MethodTerminalTransitionRef(cancelled.transition)
            ),
        )
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'run.method.observation.ack', ?, '{}', ?)",
                (self.op("null-direct-ack"), "0" * 64, now),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO run_method_exchange_completions("
                    "exchange_id, run_id, round_index, kind, prepared_input_digest, "
                    "outcome, response_digest, result_digest, error_code, "
                    "logical_trial_ids_json, committed_run_revision, "
                    "controller_generation, controller_lease_id, "
                    "controller_fencing_token, completed_by_principal_id, "
                    "completed_txn_id, created_at) VALUES (?, 'run-a', 1, "
                    "'observation', ?, 'acknowledged', ?, ?, NULL, '[null]', "
                    "?, 1, ?, ?, 'operator', ?, ?)",
                    (
                        observation.exchange_id,
                        observation.input_digest,
                        method_worker_response_digest({"ok": True}),
                        METHOD_OBSERVATION_ACK_RESULT_DIGEST,
                        cancelled.revision.revision,
                        self.controller.lease_id,
                        self.controller.fencing_token,
                        txn_id,
                        now,
                    ),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_round_skips_overlap_and_payload_tampering_fail_closed(self) -> None:
        self.assertEqual(
            MethodProposalExchangeInput(MAX_BATCH_EXCHANGE_ITEMS, {}, {}).requested_width,
            4096,
        )
        with self.assertRaisesRegex(ValueError, "width is too large"):
            MethodProposalExchangeInput(MAX_BATCH_EXCHANGE_ITEMS + 1, {}, {})
        self.assertEqual(method_exchange_sequence(round_index=1, kind="proposal"), 1)
        self.assertEqual(
            method_exchange_sequence(round_index=1, kind="observation"), 2
        )
        self.assertEqual(
            method_worker_response_digest({"ok": True, "value": "/domain/value"}),
            method_worker_response_digest({"value": "/domain/value", "ok": True}),
        )
        self.assertNotEqual(
            method_worker_response_digest({"ok": False}),
            method_worker_response_digest(
                {"ok": False, "error": {"code": "worker_failed"}}
            ),
        )
        operation_id = self.op("prepare")
        self.prepare_proposal(operation_id=operation_id)
        operational_payload = self.proposal_input().to_dict()
        operational_payload["cwd"] = "/private/tmp/method"
        with self.assertRaises(RealmIntegrityError):
            MethodProposalExchangeInput.from_dict(operational_payload)
        with self.assertRaises(RealmConflict):
            self.prepare_proposal(
                operation_id=operation_id,
                exchange_input=self.proposal_input(marker="changed"),
            )
        with self.assertRaises(RealmConflict):
            self.prepare_proposal(
                operation_id=self.op("same-coordinate"),
            )
        with self.assertRaises(RealmConflict):
            self.prepare_proposal(
                operation_id=self.op("skipped-round"),
                round_index=2,
            )

    def test_schema_guard_rejects_forged_terminal_transition_evidence(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(prepared, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        canonical_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        ).to_dict()
        transition = canonical_input["terminal_transitions"][0]
        mutations = [
            ("run_id", "run_id", "other-run"),
            ("logical_trial_id", "logical_trial_id", "other-trial"),
            (
                "transition_index",
                "transition_index",
                transition["transition_index"] + 1,
            ),
            ("from_state", "from_state", "retrying"),
            ("to_state", "to_state", "running"),
            ("outcome", "outcome", "failed"),
            ("code", "code", "forged_code"),
            ("attempt_id", "attempt_id", "forged-attempt"),
            ("sequence", "sequence", transition["sequence"] + 1),
            ("run_revision", "run_revision", transition["run_revision"] + 1),
            ("txn_id", "txn_id", transition["txn_id"] + 1),
            ("created_at", "created_at", transition["created_at"] + 1.0),
            ("unexpected_field", "unexpected_field", "/private/tmp/operational"),
        ]
        mutations.extend(
            (f"null_{field}", field, None)
            for field in (
                "run_id",
                "logical_trial_id",
                "transition_index",
                "to_state",
                "outcome",
                "sequence",
                "run_revision",
                "txn_id",
                "created_at",
            )
        )
        for label, field, forged_value in mutations:
            with self.subTest(field=label):
                input_value = json.loads(json.dumps(canonical_input))
                input_value["terminal_transitions"][0][field] = forged_value
                input_json = canonical_json_bytes(input_value).decode("utf-8")
                connection = sqlite3.connect(self.database_path)
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    txn_id = connection.execute(
                        "INSERT INTO ledger_transactions("
                        "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                        ") VALUES (?, 'run.method.exchange.prepare', ?, '{}', ?)",
                        (
                            self.op(f"forged-direct-write-{label}"),
                            "0" * 64,
                            time.time(),
                        ),
                    ).lastrowid
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO run_method_exchange_preparations("
                            "exchange_id, run_id, round_index, kind, input_digest, input_json, "
                            "prepared_run_revision, controller_generation, controller_lease_id, "
                            "controller_fencing_token, prepared_by_principal_id, prepared_txn_id, "
                            "created_at) VALUES (?, 'run-a', 1, 'observation', ?, ?, ?, 1, ?, ?, "
                            "'operator', ?, ?)",
                            (
                                method_exchange_id(
                                    run_id="run-a",
                                    round_index=1,
                                    kind="observation",
                                ),
                                "1" * 64,
                                input_json,
                                cancelled.revision.revision,
                                self.controller.lease_id,
                                self.controller.fencing_token,
                                txn_id,
                                time.time(),
                            ),
                        )
                finally:
                    connection.rollback()
                    connection.close()

    def test_schema_guard_rejects_reordered_full_round_observations(self) -> None:
        wide_manifest = replace(self.manifest, proposal_width=2)
        definition, definition_bindings = prepare_test_run_definition(
            self.closure,
            wide_manifest,
            self.closure_bindings,
        )
        wide = self.ledger.create_run_namespace(
            operation_id=self.op("create-wide-run"),
            actor_principal_id="operator",
            controller_holder_id="wide-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id="run-wide",
            owner_id="run-wide-owner",
        )
        proposal_input = MethodProposalExchangeInput(2, {}, {})
        proposal = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-wide-proposal"),
            actor_principal_id="operator",
            run_id="run-wide",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=wide.controller_lease.lease_id,
            controller_holder_id=wide.controller_lease.holder_id,
            controller_fencing_token=wide.controller_lease.fencing_token,
            exchange_input=proposal_input,
        )
        candidates = tuple(
            CandidateAdmission(
                f"wide-candidate-{suffix}",
                NormalizedCandidateEnvelope.build(
                    candidate_format="parameters", spec={"x": value}
                ),
            )
            for suffix, value in (("a", 1), ("b", 2))
        )
        plan = RunAdmissionPlan(
            candidates,
            tuple(
                LogicalTrialAdmission(
                    f"wide-trial-{suffix}",
                    f"wide-candidate-{suffix}",
                    seed=value,
                )
                for suffix, value in (("a", 1), ("b", 2))
            ),
        )
        change = self.ledger.begin_owner_change(
            operation_id=self.op("wide-owner-change"),
            actor_principal_id="operator",
            owner_id=wide.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("wide-admit"),
            actor_principal_id="operator",
            run_id="run-wide",
            round_index=1,
            prepared_input_digest=proposal.input_digest,
            outcome="admitted",
            response_digest=method_worker_response_digest({"candidate_count": 2}),
            expected_run_revision=proposal.prepared_run_revision,
            expected_owner_revision=0,
            controller_lease_id=wide.controller_lease.lease_id,
            controller_holder_id=wide.controller_lease.holder_id,
            controller_fencing_token=wide.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )
        transitions = []
        revision = admitted.admission.revision.revision
        for suffix in ("a", "b"):
            cancelled = self.ledger.cancel_run_logical_trial(
                operation_id=self.op(f"cancel-wide-{suffix}"),
                actor_principal_id="operator",
                run_id="run-wide",
                logical_trial_id=f"wide-trial-{suffix}",
                expected_run_revision=revision,
                controller_lease_id=wide.controller_lease.lease_id,
                controller_holder_id=wide.controller_lease.holder_id,
                controller_fencing_token=wide.controller_lease.fencing_token,
                code="user_cancelled",
            )
            revision = cancelled.revision.revision
            transitions.append(MethodTerminalTransitionRef(cancelled.transition))
        reversed_input = self.observation_input(
            *tuple(reversed(transitions))
        ).to_dict()
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'run.method.exchange.prepare', ?, '{}', ?)",
                (self.op("reordered-direct-write"), "0" * 64, now),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO run_method_exchange_preparations("
                    "exchange_id, run_id, round_index, kind, input_digest, input_json, "
                    "prepared_run_revision, controller_generation, controller_lease_id, "
                    "controller_fencing_token, prepared_by_principal_id, prepared_txn_id, "
                    "created_at) VALUES (?, 'run-wide', 1, 'observation', ?, ?, ?, 1, ?, ?, "
                    "'operator', ?, ?)",
                    (
                        method_exchange_id(
                            run_id="run-wide", round_index=1, kind="observation"
                        ),
                        "1" * 64,
                        canonical_json_bytes(reversed_input).decode("utf-8"),
                        revision,
                        wide.controller_lease.lease_id,
                        wide.controller_lease.fencing_token,
                        txn_id,
                        now,
                    ),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_hard_stop_abandons_pending_proposal_and_interlocks_generic_admission(
        self,
    ) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-proposal-to-abandon")
        )
        change = self.begin_change()
        with self.assertRaisesRegex(RealmConflict, "pending method proposal"):
            self.ledger.commit_run_candidate_admissions(
                operation_id=self.op("generic-admission-during-proposal"),
                actor_principal_id="operator",
                run_id="run-a",
                expected_run_revision=prepared.prepared_run_revision,
                expected_owner_revision=0,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                change_id=change.change_id,
                plan=self.plan(),
            )
        with self.assertRaisesRegex(RealmConflict, "cannot abandon"):
            self.ledger.close_run_submissions(
                operation_id=self.op("soft-close-during-proposal"),
                actor_principal_id="operator",
                run_id="run-a",
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                stop_code="method_completed",
            )

        close_operation = self.op("cancel-during-proposal")
        closed = self.ledger.close_run_submissions(
            operation_id=close_operation,
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=prepared.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            stop_code="user_cancelled",
        )
        self.assertEqual(
            self.ledger.close_run_submissions(
                operation_id=close_operation,
                actor_principal_id="operator",
                run_id="run-a",
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                stop_code="user_cancelled",
            ),
            closed,
        )
        with self.assertRaises(RealmConflict):
            self.ledger.complete_run_method_proposal_exchange(
                operation_id=self.op("late-proposal-response"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=prepared.input_digest,
                outcome="empty",
                response_digest=method_worker_response_digest({"candidates": []}),
                expected_run_revision=prepared.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )
        head = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=head.revision.revision,
            expected_head_sequence=head.revision.last_sequence,
        )
        self.assertEqual(
            [event.event for event in timeline.items],
            [
                "method_exchange_prepared",
                "method_exchange_abandoned",
                "run_submissions_closed",
            ],
        )
        finished = self.ledger.finish_run(
            operation_id=self.op("finish-abandoned-proposal"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=closed.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "cancelled")

    def test_soft_max_trials_escalation_abandons_pending_observation(self) -> None:
        manifest = replace(self.manifest, max_trials=1)
        definition, bindings = prepare_test_run_definition(
            self.closure, manifest, self.closure_bindings
        )
        created = self.ledger.create_run_namespace(
            operation_id=self.op("escalation-run-create"),
            actor_principal_id="operator",
            controller_holder_id="escalation-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            run_definition=definition,
            definition_bindings=bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id="run-escalation",
            owner_id="run-escalation-owner",
        )
        controller = created.controller_lease
        prepared = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("escalation-proposal-prepare"),
            actor_principal_id="operator",
            run_id="run-escalation",
            round_index=1,
            expected_run_revision=0,
            expected_controller_generation=1,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            exchange_input=self.proposal_input(),
        )
        plan = self.plan()
        change = self.ledger.begin_owner_change(
            operation_id=self.op("escalation-owner-change"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = self.ledger.complete_run_method_proposal_exchange(
            operation_id=self.op("escalation-proposal-complete"),
            actor_principal_id="operator",
            run_id="run-escalation",
            round_index=1,
            prepared_input_digest=prepared.input_digest,
            outcome="admitted",
            response_digest=method_worker_response_digest(
                {"candidates": [plan.to_dict()]}
            ),
            expected_run_revision=prepared.prepared_run_revision,
            expected_owner_revision=0,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            change_id=change.change_id,
            plan=plan,
        )
        after_admission = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-escalation"
        )
        self.assertEqual(
            after_admission.control.current_submission.stop_code,
            "max_trials",
        )
        cancelled = self.ledger.cancel_run_logical_trial(
            operation_id=self.op("escalation-trial-cancel"),
            actor_principal_id="operator",
            run_id="run-escalation",
            logical_trial_id="trial-a",
            expected_run_revision=admitted.admission.revision.revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            code="not_selected",
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("escalation-observation-prepare"),
            actor_principal_id="operator",
            run_id="run-escalation",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=1,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            exchange_input=self.observation_input(
                MethodTerminalTransitionRef(cancelled.transition)
            ),
        )
        escalated = self.ledger.escalate_run_stop(
            operation_id=self.op("escalation-hard-stop"),
            actor_principal_id="operator",
            run_id="run-escalation",
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
            stop_code="protocol_error",
        )

        head = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-escalation"
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-escalation",
            expected_run_revision=head.revision.revision,
            expected_head_sequence=head.revision.last_sequence,
        )
        self.assertEqual(
            [item.event for item in timeline.items[-2:]],
            ["method_exchange_abandoned", "run_stop_escalated"],
        )
        finished = self.ledger.finish_run(
            operation_id=self.op("escalation-finish"),
            actor_principal_id="operator",
            run_id="run-escalation",
            expected_run_revision=escalated.revision.revision,
            controller_lease_id=controller.lease_id,
            controller_holder_id=controller.holder_id,
            controller_fencing_token=controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "failed")
        self.assertEqual(finished.finalization.code, "protocol_error")

    def test_schema_rejects_abandonment_event_outside_hard_stop_control(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-before-forged-abandonment")
        )
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            txn_id = connection.execute(
                "INSERT INTO ledger_transactions("
                "operation_id, operation_kind, request_digest, receipt_json, committed_at"
                ") VALUES (?, 'run.admit', ?, '{}', ?)",
                (self.op("forged-abandonment-admission"), "0" * 64, now),
            ).lastrowid
            payload = canonical_json_bytes(
                {
                    "exchange_id": prepared.exchange_id,
                    "round_index": prepared.round_index,
                    "kind": prepared.kind,
                    "input_digest": prepared.input_digest,
                    "stop_code": "user_cancelled",
                }
            ).decode("utf-8")
            connection.execute(
                "INSERT INTO run_events("
                "run_id, sequence, event_id, schema_version, producer, event, "
                "phase, state, outcome, code, terminal, candidate_id, "
                "logical_trial_id, session_handle, payload_json, run_revision, "
                "txn_id, created_at, attempt_id, attempt"
                ") VALUES ('run-a', 2, ?, '1', 'method', "
                "'method_exchange_abandoned', 'method', 'abandoned', 'abandoned', "
                "'user_cancelled', 0, NULL, NULL, NULL, ?, 2, ?, ?, NULL, NULL)",
                (self.op("forged-abandonment-event"), payload, txn_id, now),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "run control must exactly abandon its pending method exchange",
            ):
                connection.execute(
                    "INSERT INTO run_revisions("
                    "run_id, revision, owner_revision, last_sequence, next_sequence, "
                    "accepted_logical_trials, controller_generation, "
                    "writer_controller_lease_id, writer_controller_fencing_token, "
                    "operation_kind, txn_id, created_at"
                    ") VALUES ('run-a', 2, 0, 2, 3, 0, 1, ?, ?, 'run.admit', ?, ?)",
                    (
                        self.controller.lease_id,
                        self.controller.fencing_token,
                        txn_id,
                        now,
                    ),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_completed_exchange_wins_before_hard_close_without_abandonment(self) -> None:
        prepared = self.prepare_proposal(
            operation_id=self.op("prepare-before-completion-wins")
        )
        completed = self.complete_admission(
            prepared,
            operation_id=self.op("admitted-response-before-hard-close"),
        )
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("hard-close-after-completion"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=completed.admission.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            stop_code="user_cancelled",
        )
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=snapshot.revision.revision,
            expected_head_sequence=snapshot.revision.last_sequence,
        )
        self.assertEqual(closed.revision.revision, 3)
        self.assertEqual(len(snapshot.method_exchange_completions), 1)
        self.assertFalse(
            any(
                event.event == "method_exchange_abandoned"
                for event in timeline.items
            )
        )

    def test_hard_stop_after_admission_waives_unprepared_observation(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(prepared, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        with self.assertRaisesRegex(RealmConflict, "cannot skip"):
            self.ledger.close_run_submissions(
                operation_id=self.op("dishonest-method-complete"),
                actor_principal_id="operator",
                run_id="run-a",
                expected_run_revision=cancelled.revision.revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
                stop_code="method_completed",
            )
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("admin-stop-before-observe"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=cancelled.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            stop_code="admin_cancelled",
        )
        finished = self.ledger.finish_run(
            operation_id=self.op("finish-without-observe"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=closed.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "cancelled")
        self.assertEqual(finished.finalization.code, "admin_cancelled")
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertFalse(
            any(
                item.kind == "observation"
                for item in snapshot.method_exchange_preparations
            )
        )

    def test_hard_stop_abandons_pending_observation_and_finish_skips_callback(self) -> None:
        prepared = self.prepare_proposal(operation_id=self.op("prepare-admission"))
        self.complete_admission(prepared, operation_id=self.op("admit"))
        cancelled = self.cancel_trial()
        observation_input = self.observation_input(
            MethodTerminalTransitionRef(cancelled.transition)
        )
        observation = self.ledger.prepare_run_method_exchange(
            operation_id=self.op("prepare-observation"),
            actor_principal_id="operator",
            run_id="run-a",
            round_index=1,
            expected_run_revision=cancelled.revision.revision,
            expected_controller_generation=cancelled.run.controller_generation,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            exchange_input=observation_input,
        )
        closed = self.ledger.close_run_submissions(
            operation_id=self.op("cancel-during-observe"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=observation.prepared_run_revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
            stop_code="user_cancelled",
        )
        with self.assertRaises(RealmConflict):
            self.ledger.acknowledge_run_method_observation_exchange(
                operation_id=self.op("late-ack"),
                actor_principal_id="operator",
                run_id="run-a",
                round_index=1,
                prepared_input_digest=observation.input_digest,
                response_digest=method_worker_response_digest({"ok": True}),
                expected_run_revision=observation.prepared_run_revision,
                controller_lease_id=self.controller.lease_id,
                controller_holder_id=self.controller.holder_id,
                controller_fencing_token=self.controller.fencing_token,
            )
        head = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        timeline = self.ledger.read_run_timeline_page(
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=head.revision.revision,
            expected_head_sequence=head.revision.last_sequence,
        )
        abandoned = [
            event
            for event in timeline.items
            if event.event == "method_exchange_abandoned"
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].method_exchange_id, observation.exchange_id)
        self.assertEqual(abandoned[0].method_round_index, 1)
        self.assertEqual(abandoned[0].method_exchange_kind, "observation")
        finished = self.ledger.finish_run(
            operation_id=self.op("finish-after-abandon"),
            actor_principal_id="operator",
            run_id="run-a",
            expected_run_revision=closed.revision.revision,
            controller_lease_id=self.controller.lease_id,
            controller_holder_id=self.controller.holder_id,
            controller_fencing_token=self.controller.fencing_token,
        )
        self.assertEqual(finished.run.state, "cancelled")
        self.assertEqual(finished.finalization.code, "user_cancelled")
        terminal_snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id="run-a"
        )
        self.assertFalse(
            any(
                item.kind == "observation"
                for item in terminal_snapshot.method_exchange_completions
            )
        )


if __name__ == "__main__":
    unittest.main()
