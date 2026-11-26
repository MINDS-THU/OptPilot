"""Contract tests for the ledger-first run-controller admission seam."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from typing import Any, Mapping, Sequence

from optpilot.realm.owners import OwnerCommitReceipt
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    LogicalTrialRecord,
    NormalizedCandidateEnvelope,
    RunAdmissionReceipt,
    RunCandidateRecord,
    RunNamespaceRecord,
    RunRevisionRecord,
)
from optpilot.run_controller import (
    MethodProtocolError,
    PreparedProposal,
    RunController,
    RunControllerStateError,
)


def _candidate(candidate_id: str, value: float = 1.0) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "format": "parameters",
        "spec": {"x": value},
    }


def _controller(**overrides: Any) -> RunController:
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
    }
    arguments.update(overrides)
    return RunController(**arguments)


def _controller_state(controller: RunController) -> tuple[Any, ...]:
    """All public proposal-owned state that preflight/apply may affect."""

    return (
        copy.deepcopy(controller.summary()),
        controller.controller_events,
        controller.rejections,
        controller.logical_trials,
        controller.accepted_candidate_ids,
    )


def _receipt_for(
    prepared: PreparedProposal,
    *,
    run_revision: int | None = None,
) -> RunAdmissionReceipt:
    """Build a small internally valid canonical-admission receipt fixture."""

    revision = (
        prepared.expected_run_revision + 1
        if run_revision is None
        else run_revision
    )
    candidate_count = len(prepared.candidates)
    trial_count = len(prepared.logical_trial_ids)
    last_sequence = candidate_count + trial_count
    accepted_txn_id = 2
    created_at = 10.0

    candidate_records = []
    candidate_keys: dict[str, str] = {}
    for sequence, candidate in enumerate(prepared.candidates, start=1):
        candidate_id = candidate["candidate_id"]
        candidate_key = f"candidate-key-{sequence}"
        candidate_keys[candidate_id] = candidate_key
        admission = CandidateAdmission(
            candidate_id=candidate_id,
            envelope=NormalizedCandidateEnvelope.build(
                candidate_format=candidate["format"],
                spec=candidate["spec"],
            ),
            lineage=candidate["lineage"],
            generator=candidate["generator"],
        )
        candidate_records.append(
            RunCandidateRecord(
                run_id="run-a",
                candidate_key=candidate_key,
                admission=admission,
                accepted_run_revision=revision,
                accepted_owner_revision=0,
                accepted_sequence=sequence,
                accepted_txn_id=accepted_txn_id,
                created_at=created_at,
            )
        )

    logical_records = []
    for offset, (logical_trial_id, candidate) in enumerate(
        zip(prepared.logical_trial_ids, prepared.candidates),
        start=1,
    ):
        candidate_id = candidate["candidate_id"]
        logical_records.append(
            LogicalTrialRecord(
                run_id="run-a",
                candidate_key=candidate_keys[candidate_id],
                admission=LogicalTrialAdmission(
                    logical_trial_id=logical_trial_id,
                    candidate_id=candidate_id,
                    submission_metadata={"admission_id": prepared.admission_id},
                ),
                budget_slot=offset,
                state="accepted",
                accepted_sequence=candidate_count + offset,
                accepted_txn_id=accepted_txn_id,
            )
        )

    return RunAdmissionReceipt(
        owner_commit=OwnerCommitReceipt(
            operation_id=f"run/admit/{prepared.admission_id}",
            change_id=f"change/{prepared.admission_id}",
            owner_id="run-owner-a",
            previous_revision=0,
            owner_revision=0,
            manifest_digest="0" * 64,
            additions=(),
            removals=(),
        ),
        run=RunNamespaceRecord(
            run_id="run-a",
            owner_id="run-owner-a",
            state="running",
            retention_state="active",
            current_revision=revision,
            next_sequence=last_sequence + 1,
            max_trials=5,
            accepted_logical_trials=trial_count,
            controller_lease_id="controller-lease-a",
            controller_holder_id="controller-a",
            controller_fencing_token=1,
            controller_generation=1,
            controller_txn_id=1,
            created_txn_id=1,
            created_at=1.0,
            updated_at=created_at,
        ),
        revision=RunRevisionRecord(
            run_id="run-a",
            revision=revision,
            owner_revision=0,
            last_sequence=last_sequence,
            next_sequence=last_sequence + 1,
            accepted_logical_trials=trial_count,
            controller_generation=1,
            writer_controller_lease_id="controller-lease-a",
            writer_controller_fencing_token=1,
            operation_kind="run.admit",
            txn_id=accepted_txn_id,
            created_at=created_at,
        ),
        candidates=tuple(candidate_records),
        logical_trials=tuple(logical_records),
    )


def _replace_candidate_spec(
    receipt: RunAdmissionReceipt,
    *,
    index: int,
    spec: Mapping[str, Any],
) -> RunAdmissionReceipt:
    records = list(receipt.candidates)
    record = records[index]
    admission = record.admission
    records[index] = replace(
        record,
        admission=replace(
            admission,
            envelope=NormalizedCandidateEnvelope.build(
                candidate_format=admission.envelope.candidate_format,
                spec=spec,
                content_refs=admission.envelope.content_refs,
            ),
        ),
    )
    return replace(receipt, candidates=tuple(records))


def _replace_trial_id(
    receipt: RunAdmissionReceipt,
    *,
    index: int,
    logical_trial_id: str,
) -> RunAdmissionReceipt:
    records = list(receipt.logical_trials)
    record = records[index]
    records[index] = replace(
        record,
        admission=replace(
            record.admission,
            logical_trial_id=logical_trial_id,
        ),
    )
    return replace(receipt, logical_trials=tuple(records))


class RunControllerAdmissionSeamTests(unittest.TestCase):
    def assert_preflight_unchanged(
        self,
        controller: RunController,
        candidates: Sequence[Mapping[str, Any]],
        *,
        admission_id: str = "admission-a",
        expected_run_revision: int = 0,
    ) -> PreparedProposal:
        before = _controller_state(controller)
        prepared = controller.preflight_proposal(
            candidates,
            admission_id=admission_id,
            expected_run_revision=expected_run_revision,
        )
        self.assertEqual(_controller_state(controller), before)
        return prepared

    def test_successful_preflight_is_pure_and_contains_normalized_candidates(self) -> None:
        controller = _controller()

        prepared = self.assert_preflight_unchanged(
            controller,
            [_candidate("candidate-a", 1), _candidate("candidate-b", 2)],
            admission_id="admission-success",
            expected_run_revision=7,
        )

        self.assertEqual(prepared.admission_id, "admission-success")
        self.assertEqual(prepared.expected_run_revision, 7)
        self.assertEqual(prepared.requested_width, 2)
        self.assertEqual(prepared.candidate_ids, ("candidate-a", "candidate-b"))
        self.assertEqual(tuple(item["spec"]["x"] for item in prepared.candidates), (1, 2))
        self.assertEqual(len(prepared.logical_trial_ids), 2)
        self.assertEqual(len(set(prepared.logical_trial_ids)), 2)
        self.assertEqual(len(prepared.digest), 64)

    def test_failed_preflight_is_pure_for_malformed_duplicate_and_overproduction(self) -> None:
        cases = (
            (
                _controller(),
                "not-a-candidate-sequence",
                "candidate_malformed",
            ),
            (
                _controller(),
                [_candidate("candidate-a"), {"format": "parameters", "spec": {"x": 2}}],
                "candidate_malformed",
            ),
            (
                _controller(),
                [_candidate("candidate-a"), _candidate("candidate-a", 2)],
                "duplicate_candidate_id",
            ),
            (
                _controller(max_trials=1),
                [_candidate("candidate-a"), _candidate("candidate-b")],
                "batch_overproduced",
            ),
        )
        for controller, candidates, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                before = _controller_state(controller)
                with self.assertRaises(MethodProtocolError) as captured:
                    controller.preflight_proposal(
                        candidates,
                        admission_id=f"admission-{expected_code}",
                        expected_run_revision=0,
                    )
                self.assertEqual(captured.exception.code, expected_code)
                self.assertEqual(_controller_state(controller), before)

    def test_preflight_trial_ids_and_digest_are_stable_across_retry_and_restart(self) -> None:
        candidates = [_candidate("candidate-a", 1), _candidate("candidate-b", 2)]
        first = self.assert_preflight_unchanged(
            _controller(logical_trial_id_factory=lambda: "legacy-first"),
            candidates,
            admission_id="admission-retry",
            expected_run_revision=3,
        )
        retry = self.assert_preflight_unchanged(
            _controller(logical_trial_id_factory=lambda: "legacy-second"),
            candidates,
            admission_id="admission-retry",
            expected_run_revision=3,
        )

        self.assertEqual(retry.logical_trial_ids, first.logical_trial_ids)
        self.assertEqual(retry.digest, first.digest)

    def test_apply_exact_receipt_mutates_once_and_replay_is_a_no_op(self) -> None:
        controller = _controller()
        prepared = self.assert_preflight_unchanged(
            controller,
            [_candidate("candidate-a", 1), _candidate("candidate-b", 2)],
            admission_id="admission-apply",
            expected_run_revision=0,
        )
        receipt = _receipt_for(prepared)

        accepted = controller.apply_admission(prepared, receipt)

        self.assertEqual(
            tuple(item.logical_trial_id for item in accepted),
            prepared.logical_trial_ids,
        )
        self.assertEqual(
            tuple(item.candidate for item in accepted),
            prepared.candidates,
        )
        self.assertEqual(controller.accepted_candidate_ids, prepared.candidate_ids)
        self.assertEqual(controller.accepted_logical_trials, 2)
        self.assertEqual(controller.remaining_trials, 3)
        self.assertEqual(len(controller.controller_events), 1)
        self.assertEqual(controller.controller_events[0].event, "proposal.accepted")

        after_first_apply = _controller_state(controller)
        replay = controller.apply_admission(prepared, receipt)
        self.assertEqual(replay, accepted)
        self.assertEqual(_controller_state(controller), after_first_apply)

    def test_apply_rejects_mismatched_receipt_without_mutation(self) -> None:
        candidates = [_candidate("candidate-a", 1), _candidate("candidate-b", 2)]

        def candidate_mismatch(prepared: PreparedProposal) -> RunAdmissionReceipt:
            return _replace_candidate_spec(_receipt_for(prepared), index=0, spec={"x": 99})

        def trial_mismatch(prepared: PreparedProposal) -> RunAdmissionReceipt:
            return _replace_trial_id(
                _receipt_for(prepared),
                index=0,
                logical_trial_id="wrong-trial",
            )

        def candidate_order_mismatch(prepared: PreparedProposal) -> RunAdmissionReceipt:
            receipt = _receipt_for(prepared)
            return replace(receipt, candidates=tuple(reversed(receipt.candidates)))

        def trial_order_mismatch(prepared: PreparedProposal) -> RunAdmissionReceipt:
            receipt = _receipt_for(prepared)
            return replace(receipt, logical_trials=tuple(reversed(receipt.logical_trials)))

        def revision_mismatch(prepared: PreparedProposal) -> RunAdmissionReceipt:
            return _receipt_for(
                prepared,
                run_revision=prepared.expected_run_revision + 2,
            )

        cases = {
            "candidate": candidate_mismatch,
            "trial": trial_mismatch,
            "candidate-order": candidate_order_mismatch,
            "trial-order": trial_order_mismatch,
            "revision": revision_mismatch,
        }
        for label, make_receipt in cases.items():
            with self.subTest(label=label):
                controller = _controller()
                prepared = self.assert_preflight_unchanged(
                    controller,
                    candidates,
                    admission_id=f"admission-mismatch-{label}",
                    expected_run_revision=0,
                )
                receipt = make_receipt(prepared)
                before = _controller_state(controller)

                with self.assertRaises(RunControllerStateError):
                    controller.apply_admission(prepared, receipt)

                self.assertEqual(_controller_state(controller), before)


if __name__ == "__main__":
    unittest.main()
