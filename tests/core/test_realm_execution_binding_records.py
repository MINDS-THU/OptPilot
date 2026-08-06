from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.execution_binding_records import (
    ExecutionBindingLaunchReceipt,
    ExecutionBindingRecord,
    ExecutionLaunchIntentRecord,
    ExecutionProjectionHandle,
    ExecutionVolumeHandle,
    RunAttemptBindingReceipt,
    projection_private_coordinate_digest,
    run_attempt_binding_operation_id,
    run_attempt_projection_operation_id,
    run_attempt_resource_holder_id,
    run_attempt_volume_operation_id,
    run_attempt_volume_operational_ids,
)
from optpilot.runtime_binding import ExecutionBindingEvidence
from tests.core.test_realm_run_attempt_records import _attempt, _revision, _run
from tests.core.test_runtime_binding import (
    _binding_parts,
    _compile,
    _definition,
    _evaluation_spec,
)


def _binding_fixture():
    definition = _definition()
    evaluation_spec = _evaluation_spec(definition)
    spec = _compile(definition, evaluation_spec)
    provider, projection, volumes = _binding_parts(spec)
    evidence = ExecutionBindingEvidence.create(
        spec,
        provider=provider,
        projections=(projection,),
        writable_volumes=volumes,
    )
    projection_handles = (
        ExecutionProjectionHandle(
            logical_name=spec.projection_name,
            provider_kind="verified-copy.v1",
            realization_id="projection-realization-a",
            consumer_id="projection-consumer-a",
            consumer_lease_id="projection-consumer-lease-a",
            consumer_fencing_token=3,
        ),
    )
    volume_handles = tuple(
        ExecutionVolumeHandle(
            logical_name=volume.logical_name,
            provider_kind="local-directory.v1",
            volume_id=f"ephemeral-volume-{index}",
            usage_lease_id=f"ephemeral-volume-lease-{index}",
            usage_fencing_token=index + 1,
        )
        for index, volume in enumerate(volumes)
    )
    binding = ExecutionBindingRecord(
        run_id="run-a",
        attempt_id="attempt-a",
        binding_id="binding-a",
        portable_spec=spec,
        evidence=evidence,
        projections=projection_handles,
        writable_volumes=volume_handles,
        resource_ttl_seconds=300.0,
        created_run_revision=4,
        created_sequence=7,
        created_txn_id=40,
        created_at=5.0,
    )
    attempt = replace(
        _attempt(state="prepared", head=1, updated_at=4.0),
        evaluation_spec=evaluation_spec,
        prepared_runtime_digest=evaluation_spec.prepared_runtime_digest,
    )
    receipt = RunAttemptBindingReceipt(
        run=_run(revision=4, next_sequence=8),
        revision=_revision(
            revision=4,
            last_sequence=7,
            txn_id=40,
            kind="run.attempt.bind",
        ),
        attempt=attempt,
        binding=binding,
        launch_intent=ExecutionLaunchIntentRecord(
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            binding_id=binding.binding_id,
            launch_token=attempt.launch_token,
            provider_kind=binding.portable_spec.provider.kind,
            evidence_fingerprint=binding.evidence_fingerprint,
            launch_request_digest="d" * 64,
            created_by_principal_id="principal-a",
            created_txn_id=binding.created_txn_id,
            created_at=binding.created_at,
        ),
    )
    return receipt


class ExecutionBindingRecordTest(unittest.TestCase):
    def test_resource_coordinates_are_bounded_stable_and_substitution_sensitive(self) -> None:
        arguments = {
            "run_id": "run/" + "r" * 500,
            "attempt_id": "attempt/" + "a" * 500,
            "binding_id": "binding-1",
            "logical_name": "trial",
        }
        projection = run_attempt_projection_operation_id(**arguments)
        volume = run_attempt_volume_operation_id(**arguments)
        binding_operation = run_attempt_binding_operation_id(
            run_id=arguments["run_id"],
            attempt_id=arguments["attempt_id"],
            binding_id=arguments["binding_id"],
        )
        holder = run_attempt_resource_holder_id(
            run_id=arguments["run_id"],
            attempt_id=arguments["attempt_id"],
            binding_id=arguments["binding_id"],
        )

        self.assertLess(len(projection), 128)
        self.assertLess(len(volume), 128)
        self.assertLess(len(binding_operation), 128)
        self.assertLess(len(holder), 128)
        self.assertEqual(
            run_attempt_volume_operational_ids(volume),
            run_attempt_volume_operational_ids(volume),
        )
        self.assertNotEqual(
            volume,
            run_attempt_volume_operation_id(
                **{**arguments, "binding_id": "binding-2"}
            ),
        )
        self.assertNotEqual(
            projection_private_coordinate_digest(
                realm_id="realm-a", operation_id=projection
            ),
            projection_private_coordinate_digest(
                realm_id="realm-b", operation_id=projection
            ),
        )

    def test_binding_and_receipt_round_trip_canonically(self) -> None:
        receipt = _binding_fixture()

        restored = RunAttemptBindingReceipt.from_dict(receipt.to_dict())

        self.assertEqual(restored, receipt)
        self.assertEqual(
            restored.binding.portable_spec_digest,
            receipt.binding.portable_spec.digest,
        )
        self.assertEqual(
            restored.binding.evidence_fingerprint,
            receipt.binding.evidence.fingerprint,
        )

    def test_immutable_binding_launch_receipt_survives_terminal_attempt(self) -> None:
        committed = _binding_fixture()
        terminal_attempt = replace(
            committed.attempt,
            state="terminal",
            outcome="failed",
            code="attempt_authority_lost",
            head_transition_index=2,
            updated_at=7.0,
        )
        receipt = ExecutionBindingLaunchReceipt(
            attempt=terminal_attempt,
            binding=committed.binding,
            launch_intent=committed.launch_intent,
        )

        self.assertEqual(
            ExecutionBindingLaunchReceipt.from_dict(receipt.to_dict()), receipt
        )
        tampered = copy.deepcopy(receipt.to_dict())
        tampered["launch_intent"]["launch_token"] = "substituted-launch"
        with self.assertRaisesRegex(RealmIntegrityError, "malformed"):
            ExecutionBindingLaunchReceipt.from_dict(tampered)

    def test_portable_record_excludes_all_operational_handles(self) -> None:
        binding = _binding_fixture().binding

        encoded = json.dumps(binding.portable_record(), sort_keys=True)

        for forbidden in (
            binding.run_id,
            binding.attempt_id,
            binding.binding_id,
            binding.projections[0].realization_id,
            binding.projections[0].consumer_id,
            binding.projections[0].consumer_lease_id,
            binding.writable_volumes[0].volume_id,
            binding.writable_volumes[0].usage_lease_id,
        ):
            self.assertNotIn(forbidden, encoded)

    def test_binding_rejects_noncanonical_numeric_and_sequence_shapes(self) -> None:
        payload = _binding_fixture().binding.to_dict()
        payload["created_at"] = 5
        with self.assertRaisesRegex(ValueError, "not canonical"):
            ExecutionBindingRecord.from_dict(payload)

        payload = _binding_fixture().binding.to_dict()
        payload["projections"] = tuple(payload["projections"])
        with self.assertRaises((TypeError, ValueError)):
            ExecutionBindingRecord.from_dict(payload)

    def test_binding_rejects_reused_cleanup_authority(self) -> None:
        binding = _binding_fixture().binding
        first, second = binding.writable_volumes
        duplicate_volume_lease = replace(
            second,
            usage_lease_id=first.usage_lease_id,
        )
        with self.assertRaisesRegex(ValueError, "usage leases must be unique"):
            replace(
                binding,
                writable_volumes=(first, duplicate_volume_lease),
            )

        cross_kind_lease = replace(
            second,
            usage_lease_id=binding.projections[0].consumer_lease_id,
        )
        with self.assertRaisesRegex(ValueError, "authority lease ids must be globally unique"):
            replace(
                binding,
                writable_volumes=(first, cross_kind_lease),
            )

    def test_receipt_anchors_the_exact_prepared_evaluation(self) -> None:
        receipt = _binding_fixture()
        unrelated_attempt = _attempt(state="prepared", head=1, updated_at=4.0)

        with self.assertRaisesRegex(ValueError, "prepared attempt"):
            replace(receipt, attempt=unrelated_attempt)

    def test_bind_receipt_cannot_contain_a_later_attempt_state(self) -> None:
        receipt = _binding_fixture()
        running_attempt = replace(
            receipt.attempt,
            state="running",
            head_transition_index=2,
            updated_at=6.0,
        )

        with self.assertRaisesRegex(ValueError, "identities differ"):
            replace(receipt, attempt=running_attempt)

    def test_bind_receipt_must_follow_attempt_preparation(self) -> None:
        receipt = _binding_fixture()
        impossible_attempt = replace(
            receipt.attempt,
            prepared_run_revision=receipt.binding.created_run_revision,
        )

        with self.assertRaisesRegex(ValueError, "identities differ"):
            replace(receipt, attempt=impossible_attempt)

    def test_receipt_wire_form_is_canonical_across_nested_records(self) -> None:
        payload = copy.deepcopy(_binding_fixture().to_dict())
        payload["run"]["created_at"] = 1

        with self.assertRaises(RealmIntegrityError):
            RunAttemptBindingReceipt.from_dict(payload)

    def test_receipt_rejects_wrong_revision_operation_and_sequence(self) -> None:
        receipt = _binding_fixture()

        with self.assertRaisesRegex(ValueError, "identities differ"):
            replace(
                receipt,
                revision=replace(receipt.revision, operation_kind="run.attempt.confirm"),
            )
        with self.assertRaisesRegex(ValueError, "identities differ"):
            replace(
                receipt,
                binding=replace(receipt.binding, created_sequence=6),
            )


if __name__ == "__main__":
    unittest.main()
