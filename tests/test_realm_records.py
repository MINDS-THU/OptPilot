from __future__ import annotations

import math
import unittest

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.leases import LeaseRecord, LeaseState
from optpilot.realm.ledger import ContentRecord, PrincipalRecord, StoreRecord
from optpilot.realm.owners import (
    OwnerChange,
    OwnerChangeState,
    OwnerCommitReceipt,
    OwnerMembership,
    OwnerRecord,
    OwnerState,
)
from optpilot.realm.refs import BlobRef, CandidateRef


class RealmValueRecordTests(unittest.TestCase):
    def test_principal_store_and_content_records_validate_persisted_facts(self) -> None:
        principal = PrincipalRecord.from_dict(
            {"principal_id": "principal-a", "kind": "human", "created_at": 1}
        )
        self.assertEqual(principal.created_at, 1.0)
        with self.assertRaises(RealmIntegrityError):
            PrincipalRecord.from_dict(
                {"principal_id": " ", "kind": "human", "created_at": 1}
            )

        store = StoreRecord.from_dict(
            {
                "store_id": "store-a",
                "backend_kind": "local-cas",
                "root_marker": "marker-a",
                "state": "active",
                "created_at": 1,
            }
        )
        self.assertEqual(store.state, "active")
        invalid_store = store.to_dict()
        invalid_store["state"] = "invented"
        with self.assertRaises(RealmIntegrityError):
            StoreRecord.from_dict(invalid_store)

        metadata = {"nested": {"values": [1, 2]}}
        content = ContentRecord.from_dict(
            {
                "store_id": "store-a",
                "content_ref": str(BlobRef.from_bytes(b"payload")),
                "kind": "blob",
                "logical_bytes": 7,
                "physical_bytes": 42,
                "lifecycle_state": "live",
                "trust_state": "verified_local",
                "metadata": metadata,
                "created_at": 1,
                "verified_at": 2,
            }
        )
        metadata["nested"]["values"].append(3)
        self.assertEqual(content.to_dict()["metadata"], {"nested": {"values": [1, 2]}})
        with self.assertRaises(TypeError):
            content.metadata["new"] = True  # type: ignore[index]

        invalid_content = content.to_dict()
        invalid_content["lifecycle_state"] = "invented"
        with self.assertRaises(RealmIntegrityError):
            ContentRecord.from_dict(invalid_content)
        invalid_content = content.to_dict()
        invalid_content["kind"] = "tree"
        with self.assertRaises(RealmIntegrityError):
            ContentRecord.from_dict(invalid_content)
        candidate_content = content.to_dict()
        candidate_content["content_ref"] = str(
            CandidateRef.build(
                candidate_format="parameters", spec={"x": 1}, content_refs=[]
            )
        )
        candidate_content["kind"] = "candidate"
        with self.assertRaises(RealmIntegrityError):
            ContentRecord.from_dict(candidate_content)

    def test_owner_records_reject_invalid_ids_revisions_and_times(self) -> None:
        with self.assertRaises(ValueError):
            OwnerRecord("", "workspace", "principal-a", 0, OwnerState.ACTIVE, 1.0, 1.0)
        with self.assertRaises(ValueError):
            OwnerRecord("owner-a", "workspace", "principal-a", True, OwnerState.ACTIVE, 1.0, 1.0)
        with self.assertRaises(ValueError):
            OwnerRecord("owner-a", "workspace", "principal-a", 0, OwnerState.ACTIVE, math.nan, 1.0)
        with self.assertRaises(ValueError):
            OwnerRecord("owner-a", "workspace", "principal-a", 0, OwnerState.ACTIVE, 2.0, 1.0)

        with self.assertRaises(RealmIntegrityError):
            OwnerRecord.from_dict(
                {
                    "owner_id": "owner-a",
                    "owner_kind": "workspace",
                    "principal_id": "principal-a",
                    "revision": -1,
                    "state": "active",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
            )

    def test_owner_change_and_receipt_validate_authority_fields(self) -> None:
        change = OwnerChange(
            change_id="change-a",
            owner_id="owner-a",
            base_owner_revision=0,
            retention_lease_id="lease-a",
            expires_at=2,
            state=OwnerChangeState.ACTIVE,
        )
        self.assertEqual(change.expires_at, 2.0)
        membership = OwnerMembership("store-a", BlobRef.from_bytes(b"payload"), "candidate")
        with self.assertRaises(ValueError):
            OwnerMembership(
                "store-a",
                CandidateRef.build(
                    candidate_format="parameters", spec={"x": 1}, content_refs=[]
                ),  # type: ignore[arg-type]
                "candidate-envelope",
            )
        receipt = OwnerCommitReceipt(
            operation_id="commit-a",
            change_id="change-a",
            owner_id="owner-a",
            previous_revision=0,
            owner_revision=1,
            manifest_digest="a" * 64,
            additions=(membership,),
            removals=(),
        )
        self.assertEqual(OwnerCommitReceipt.from_dict(receipt.to_dict()), receipt)

        with self.assertRaises(ValueError):
            OwnerCommitReceipt(
                operation_id="commit-a",
                change_id="change-a",
                owner_id="owner-a",
                previous_revision=0,
                owner_revision=2,
                manifest_digest="not-a-digest",
                additions=(),
                removals=(),
            )

    def test_lease_records_are_validated_and_metadata_is_deeply_immutable(self) -> None:
        source_metadata = {"nested": {"values": [1, 2]}}
        lease = LeaseRecord(
            lease_id="lease-a",
            owner_id="owner-a",
            parent_lease_id=None,
            lease_kind="inspection",
            audience="studio",
            holder_id="holder-a",
            scope_key="owner:owner-a",
            fencing_token=1,
            heartbeat_revision=0,
            state=LeaseState.ACTIVE,
            expires_at=3,
            created_at=1,
            updated_at=1,
            metadata=source_metadata,
        )
        source_metadata["nested"]["values"].append(3)
        self.assertEqual(lease.to_dict()["metadata"], {"nested": {"values": [1, 2]}})
        with self.assertRaises(TypeError):
            lease.metadata["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            lease.metadata["nested"]["new"] = True  # type: ignore[index]
        self.assertEqual(LeaseRecord.from_dict(lease.to_dict()), lease)

        invalid = lease.to_dict()
        invalid["fencing_token"] = False
        with self.assertRaises(RealmIntegrityError):
            LeaseRecord.from_dict(invalid)
        invalid = lease.to_dict()
        invalid["expires_at"] = float("inf")
        with self.assertRaises(RealmIntegrityError):
            LeaseRecord.from_dict(invalid)


if __name__ == "__main__":
    unittest.main()
