from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmExpired
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.projection import (
    ProjectionSpec,
    TreeMapping,
    VerifiedCopyProjectionProvider,
)
from optpilot.realm.projection_records import ProjectionRealizationState
from optpilot.realm.refs import SnapshotRef, request_digest


class RealmProjectionLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="principal", principal_id="operator", kind="human"
        )
        self.store = LocalContentStore(self.root / "store", store_id="local")
        self.ledger.register_store(
            operation_id="store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="owner",
            owner_id="workspace",
            owner_kind="workspace",
            principal_id="operator",
        )
        source = self.root / "source"
        source.mkdir()
        (source / "payload.txt").write_text("projection", encoding="utf-8")
        change = self.ledger.begin_owner_change(
            operation_id="change",
            actor_principal_id="operator",
            owner_id="workspace",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        capture = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        published = self.store.capture(
            change_id=change.change_id, authority=capture
        ).seal_tree(source=AllowedTreeSource(source))
        self.snapshot_ref = published.snapshot_ref
        membership = OwnerMembership(
            self.store.store_id, self.snapshot_ref, "workspace-base"
        )
        self.ledger.hold_owner_content(
            operation_id="hold",
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.ledger.commit_owner_change(
            operation_id="commit",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        self.projection_root = self.root / "projections"
        self.projection_root.mkdir()
        identity = self.projection_root.stat()
        self.projection_root_id = "projection-root"
        self.root_nonce = "1" * 64
        self.provider_kind = VerifiedCopyProjectionProvider.PROVIDER_KIND
        marker = self.ledger.projection_root_marker_digest(
            projection_root_id=self.projection_root_id,
            backend_kind=self.provider_kind,
            claim_nonce=self.root_nonce,
        )
        self.root_record = self.ledger.register_projection_root(
            operation_id="projection-root",
            actor_principal_id="operator",
            projection_root_id=self.projection_root_id,
            backend_kind=self.provider_kind,
            canonical_path=str(self.projection_root),
            marker_digest=marker,
            claim_nonce=self.root_nonce,
            device_id=identity.st_dev,
            inode=identity.st_ino,
        )
        self.spec = ProjectionSpec(
            owner_id="workspace", mappings=(TreeMapping(self.snapshot_ref),)
        ).to_dict()
        self.resolution = {
            "format": "optpilot.projection-availability.v1",
            "store_id": self.store.store_id,
            "backend_kind": self.store.BACKEND_KIND,
            "root_marker": self.store.root_marker,
            "snapshot_roots": [str(self.snapshot_ref)],
        }
        self.semantic_digest = request_digest(
            {
                "format": "optpilot.projection-request.v1",
                "spec_digest": request_digest(self.spec),
                "availability_resolution_digest": request_digest(self.resolution),
                "provider_kind": self.provider_kind,
            }
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"projection-ledger/{self.counter}/{label}"

    def create(self, *, ttl: float = 60, suffix: str = "a"):
        return self.ledger.create_projection_realization(
            operation_id=self.op(f"create-{suffix}"),
            actor_principal_id="operator",
            projection_root_id=self.projection_root_id,
            owner_id="workspace",
            store_id=self.store.store_id,
            spec=self.spec,
            availability_resolution=self.resolution,
            request_digest_value=self.semantic_digest,
            provider_kind=self.provider_kind,
            claim_nonce=suffix[0] * 64,
            relative_name=f"projection-{suffix}",
            snapshot_roots=(self.snapshot_ref,),
            owner_holder_id=f"owner-{suffix}",
            owner_ttl_seconds=ttl,
        )

    def create_with_raw_inputs(
        self,
        *,
        label: str,
        spec,
        resolution,
        snapshot_roots,
        owner_id: str = "workspace",
    ):
        semantic_digest = request_digest(
            {
                "format": "optpilot.projection-request.v1",
                "spec_digest": request_digest(spec),
                "availability_resolution_digest": request_digest(resolution),
                "provider_kind": self.provider_kind,
            }
        )
        operation_id = self.op(label)
        identity_digest = request_digest({"operation_id": operation_id})
        return self.ledger.create_projection_realization(
            operation_id=operation_id,
            actor_principal_id="operator",
            projection_root_id=self.projection_root_id,
            owner_id=owner_id,
            store_id=self.store.store_id,
            spec=spec,
            availability_resolution=resolution,
            request_digest_value=semantic_digest,
            provider_kind=self.provider_kind,
            claim_nonce=identity_digest,
            relative_name=f"invalid-{self.counter}",
            snapshot_roots=snapshot_roots,
            owner_holder_id=f"invalid-owner-{self.counter}",
            owner_ttl_seconds=60,
        )

    def make_ready(self, *, ttl: float = 60, suffix: str = "a"):
        created = self.create(ttl=ttl, suffix=suffix)
        claimed = self.ledger.claim_projection_materialization(
            operation_id=self.op(f"claim-{suffix}"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
            builder_holder_id=f"builder-{suffix}",
            builder_ttl_seconds=ttl,
        )
        wrapper = self.projection_root / f"projection-{suffix}"
        wrapper.mkdir()
        exposed = wrapper / "tree"
        exposed.mkdir()
        wrapper_stat = wrapper.stat()
        exposed_stat = exposed.stat()
        claimed = self.ledger.record_projection_namespace_claim(
            operation_id=self.op(f"namespace-{suffix}"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            builder_holder_id=claimed.builder_lease.holder_id,
            builder_fencing_token=claimed.builder_lease.fencing_token,
            wrapper_device_id=wrapper_stat.st_dev,
            wrapper_inode=wrapper_stat.st_ino,
        )
        return self.ledger.publish_projection_ready(
            operation_id=self.op(f"ready-{suffix}"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            builder_holder_id=claimed.builder_lease.holder_id,
            builder_fencing_token=claimed.builder_lease.fencing_token,
            wrapper_device_id=wrapper_stat.st_dev,
            wrapper_inode=wrapper_stat.st_ino,
            exposed_tree_device_id=exposed_stat.st_dev,
            exposed_tree_inode=exposed_stat.st_ino,
            plan_digest="c" * 64,
            copied_logical_bytes=10,
            copied_file_count=1,
        )

    def test_realization_creation_rejects_noncanonical_spec_inputs(self) -> None:
        malformed_specs = {
            "format": {**self.spec, "format": "optpilot.projection-spec.v2"},
            "extra-field": {**self.spec, "unexpected": True},
            "quota-shape": {
                **self.spec,
                "quota": {
                    "max_entries": self.spec["quota"]["max_entries"],
                    "max_total_bytes": self.spec["quota"]["max_total_bytes"],
                },
            },
            "quota-type": {
                **self.spec,
                "quota": {**self.spec["quota"], "max_entries": 100_000.0},
            },
            "mapping-shape": {
                **self.spec,
                "mappings": [
                    {
                        "snapshot_ref": str(self.snapshot_ref),
                        "destination": ".",
                    }
                ],
            },
            "mapping-ref": {
                **self.spec,
                "mappings": [
                    {
                        **self.spec["mappings"][0],
                        "snapshot_ref": "not-a-snapshot-ref",
                    }
                ],
            },
        }
        for label, spec in malformed_specs.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError, "canonical"
            ):
                self.create_with_raw_inputs(
                    label=f"invalid-spec-{label}",
                    spec=spec,
                    resolution=self.resolution,
                    snapshot_roots=(self.snapshot_ref,),
                )

        wrong_owner_spec = {**self.spec, "owner_id": "another-owner"}
        with self.assertRaisesRegex(ValueError, "owner does not match"):
            self.create_with_raw_inputs(
                label="mismatched-spec-owner",
                spec=wrong_owner_spec,
                resolution=self.resolution,
                snapshot_roots=(self.snapshot_ref,),
            )

    def test_realization_creation_rejects_mismatched_roots_and_availability(self) -> None:
        another_root = SnapshotRef("a" * 64)
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.create_with_raw_inputs(
                label="mismatched-snapshot-roots",
                spec=self.spec,
                resolution=self.resolution,
                snapshot_roots=(another_root,),
            )

        malformed_resolutions = {
            "format": {
                **self.resolution,
                "format": "optpilot.projection-availability.v2",
            },
            "store": {**self.resolution, "store_id": "another-store"},
            "backend": {**self.resolution, "backend_kind": "another-backend"},
            "marker": {**self.resolution, "root_marker": "another-marker"},
            "roots": {
                **self.resolution,
                "snapshot_roots": [str(another_root)],
            },
            "extra-field": {**self.resolution, "unexpected": True},
        }
        for label, resolution in malformed_resolutions.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError, "registered store binding"
            ):
                self.create_with_raw_inputs(
                    label=f"invalid-availability-{label}",
                    spec=self.spec,
                    resolution=resolution,
                    snapshot_roots=(self.snapshot_ref,),
                )

    def test_full_lifecycle_keeps_consumer_authority_narrow(self) -> None:
        created = self.create()
        claimed = self.ledger.claim_projection_materialization(
            operation_id=self.op("claim"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
            builder_holder_id="builder",
            builder_ttl_seconds=60,
        )
        wrapper = self.projection_root / "projection-a"
        wrapper.mkdir()
        exposed = wrapper / "tree"
        exposed.mkdir()
        wrapper_stat = wrapper.stat()
        exposed_stat = exposed.stat()
        claimed = self.ledger.record_projection_namespace_claim(
            operation_id=self.op("namespace"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            builder_holder_id=claimed.builder_lease.holder_id,
            builder_fencing_token=claimed.builder_lease.fencing_token,
            wrapper_device_id=wrapper_stat.st_dev,
            wrapper_inode=wrapper_stat.st_ino,
        )
        ready = self.ledger.publish_projection_ready(
            operation_id=self.op("ready"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            builder_holder_id=claimed.builder_lease.holder_id,
            builder_fencing_token=claimed.builder_lease.fencing_token,
            wrapper_device_id=wrapper_stat.st_dev,
            wrapper_inode=wrapper_stat.st_ino,
            exposed_tree_device_id=exposed_stat.st_dev,
            exposed_tree_inode=exposed_stat.st_ino,
            plan_digest="c" * 64,
            copied_logical_bytes=10,
            copied_file_count=1,
        )
        consumer = self.ledger.acquire_projection_consumer(
            operation_id=self.op("consumer"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            consumer_holder_id="viewer",
            consumer_ttl_seconds=60,
            consumer_kind="inspection",
            metadata={"surface": "gui"},
        )
        self.assertNotIn("owner_lease", consumer.to_dict())
        self.ledger.close_projection_realization(
            operation_id=self.op("close"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=ready.owner_lease.holder_id,
            owner_fencing_token=ready.owner_lease.fencing_token,
        )
        renewed = self.ledger.heartbeat_projection_consumer(
            operation_id=self.op("heartbeat"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            consumer_id=consumer.consumer.consumer_id,
            consumer_holder_id=consumer.consumer_lease.holder_id,
            consumer_fencing_token=consumer.consumer_lease.fencing_token,
            ttl_seconds=60,
        )
        self.assertNotIn("owner_lease", renewed.to_dict())
        self.ledger.release_projection_consumer(
            operation_id=self.op("release"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            consumer_id=consumer.consumer.consumer_id,
            consumer_holder_id=consumer.consumer_lease.holder_id,
            consumer_fencing_token=consumer.consumer_lease.fencing_token,
        )
        cleanup = self.ledger.claim_projection_cleanup(
            operation_id=self.op("cleanup"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=ready.owner_lease.holder_id,
            owner_fencing_token=ready.owner_lease.fencing_token,
            builder_holder_id="cleaner",
            builder_ttl_seconds=60,
            cleanup_token="d" * 64,
        )
        completed = self.ledger.complete_projection_cleanup(
            operation_id=self.op("complete"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=cleanup.owner_lease.holder_id,
            owner_fencing_token=cleanup.owner_lease.fencing_token,
            builder_holder_id=cleanup.builder_lease.holder_id,
            builder_fencing_token=cleanup.builder_lease.fencing_token,
            cleanup_token="d" * 64,
        )
        self.assertEqual(completed.realization.state, ProjectionRealizationState.CLEANED)

    def test_expired_owner_partial_realization_can_be_recovered_and_cleaned(self) -> None:
        created = self.create(ttl=0.01)
        time.sleep(0.02)
        closing = self.ledger.close_projection_realization(
            operation_id=self.op("recover-close"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=None,
            owner_fencing_token=None,
        )
        self.assertEqual(closing.state, ProjectionRealizationState.CLOSING)
        cleanup = self.ledger.claim_projection_cleanup(
            operation_id=self.op("recover-claim"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id="reconciler",
            owner_fencing_token=None,
            builder_holder_id="cleaner",
            builder_ttl_seconds=60,
            cleanup_token="e" * 64,
        )
        self.assertEqual(cleanup.realization.owner_generation, 2)
        completed = self.ledger.complete_projection_cleanup(
            operation_id=self.op("recover-complete"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=cleanup.owner_lease.holder_id,
            owner_fencing_token=cleanup.owner_lease.fencing_token,
            builder_holder_id=cleanup.builder_lease.holder_id,
            builder_fencing_token=cleanup.builder_lease.fencing_token,
            cleanup_token="e" * 64,
        )
        self.assertEqual(completed.realization.state, ProjectionRealizationState.CLEANED)

    def test_consumer_authority_survives_cleanup_owner_recovery(self) -> None:
        ready = self.make_ready(ttl=60)
        consumer = self.ledger.acquire_projection_consumer(
            operation_id=self.op("historical-consumer"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_holder_id="historical-viewer",
            consumer_ttl_seconds=60,
            consumer_kind="inspection",
            metadata={"surface": "gui"},
        )
        with sqlite3.connect(self.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
                (time.time() - 1, ready.owner_lease.lease_id),
            )
        closing = self.ledger.close_projection_realization(
            operation_id=self.op("historical-close"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            owner_holder_id=None,
            owner_fencing_token=None,
        )
        cleanup = self.ledger.claim_projection_cleanup(
            operation_id=self.op("historical-cleanup"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            owner_holder_id="historical-reconciler",
            owner_fencing_token=None,
            builder_holder_id="historical-cleaner",
            builder_ttl_seconds=60,
            cleanup_token="f" * 64,
        )
        self.assertEqual(cleanup.realization.owner_generation, 2)
        cleaned = self.ledger.complete_projection_cleanup(
            operation_id=self.op("historical-complete"),
            actor_principal_id="operator",
            realization_id=closing.realization_id,
            owner_holder_id=cleanup.owner_lease.holder_id,
            owner_fencing_token=cleanup.owner_lease.fencing_token,
            builder_holder_id=cleanup.builder_lease.holder_id,
            builder_fencing_token=cleanup.builder_lease.fencing_token,
            cleanup_token="f" * 64,
        )

        authority = self.ledger.read_projection_consumer_authority(
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_id=consumer.consumer.consumer_id,
        )

        self.assertEqual(
            authority.realization.state, ProjectionRealizationState.CLEANED
        )
        self.assertEqual(authority.consumer, consumer.consumer)
        self.assertEqual(
            authority.consumer_lease.lease_id,
            consumer.consumer_lease.lease_id,
        )
        self.assertEqual(authority.consumer_lease.state.value, "revoked")
        self.assertNotEqual(
            authority.consumer_lease.parent_lease_id,
            cleaned.realization.owner_lease_id,
        )

    def test_expired_cleaning_term_can_be_reclaimed_and_completed(self) -> None:
        created = self.create(ttl=60)
        closing = self.ledger.close_projection_realization(
            operation_id=self.op("close-before-cleaning"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
        )
        claimed = self.ledger.claim_projection_cleanup(
            operation_id=self.op("short-cleanup-claim"),
            actor_principal_id="operator",
            realization_id=closing.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
            builder_holder_id="first-cleaner",
            builder_ttl_seconds=0.01,
            cleanup_token="9" * 64,
        )
        cleanup_started_at = claimed.realization.cleanup_started_at
        time.sleep(0.02)
        reclaim_operation_id = self.op("cleanup-reclaim")
        reclaimed = self.ledger.reclaim_projection_cleanup(
            operation_id=reclaim_operation_id,
            actor_principal_id="operator",
            realization_id=closing.realization_id,
            expected_owner_generation=claimed.realization.owner_generation,
            owner_holder_id="recovery-owner",
            builder_holder_id="recovery-cleaner",
            builder_ttl_seconds=60,
            cleanup_token="9" * 64,
        )
        replayed = self.ledger.reclaim_projection_cleanup(
            operation_id=reclaim_operation_id,
            actor_principal_id="operator",
            realization_id=closing.realization_id,
            expected_owner_generation=claimed.realization.owner_generation,
            owner_holder_id="recovery-owner",
            builder_holder_id="recovery-cleaner",
            builder_ttl_seconds=60,
            cleanup_token="9" * 64,
        )
        self.assertEqual(replayed, reclaimed)
        self.assertEqual(reclaimed.realization.state, ProjectionRealizationState.CLEANING)
        self.assertEqual(reclaimed.realization.owner_generation, 2)
        self.assertNotEqual(reclaimed.owner_lease.lease_id, claimed.owner_lease.lease_id)
        self.assertNotEqual(reclaimed.builder_lease.lease_id, claimed.builder_lease.lease_id)
        self.assertEqual(reclaimed.realization.cleanup_token, "9" * 64)
        self.assertEqual(reclaimed.realization.cleanup_started_at, cleanup_started_at)
        connection = sqlite3.connect(self.ledger.database_path)
        try:
            old_states = dict(
                connection.execute(
                    "SELECT lease_id, state FROM leases WHERE lease_id IN (?, ?)",
                    (claimed.owner_lease.lease_id, claimed.builder_lease.lease_id),
                )
            )
            inherited_holds = connection.execute(
                "SELECT count(*) FROM lease_content WHERE lease_id IN (?, ?)",
                (reclaimed.owner_lease.lease_id, reclaimed.builder_lease.lease_id),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(old_states[claimed.owner_lease.lease_id], "active")
        self.assertNotEqual(old_states[claimed.builder_lease.lease_id], "active")
        self.assertEqual(inherited_holds, 0)
        completed = self.ledger.complete_projection_cleanup(
            operation_id=self.op("complete-reclaimed-cleanup"),
            actor_principal_id="operator",
            realization_id=closing.realization_id,
            owner_holder_id=reclaimed.owner_lease.holder_id,
            owner_fencing_token=reclaimed.owner_lease.fencing_token,
            builder_holder_id=reclaimed.builder_lease.holder_id,
            builder_fencing_token=reclaimed.builder_lease.fencing_token,
            cleanup_token="9" * 64,
        )
        self.assertEqual(completed.realization.state, ProjectionRealizationState.CLEANED)

    def test_current_cleaning_term_cannot_be_reclaimed(self) -> None:
        created = self.create()
        self.ledger.close_projection_realization(
            operation_id=self.op("close-current-cleaning"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
        )
        self.ledger.claim_projection_cleanup(
            operation_id=self.op("current-cleanup-claim"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
            builder_holder_id="current-cleaner",
            builder_ttl_seconds=60,
            cleanup_token="8" * 64,
        )
        with self.assertRaisesRegex(RealmConflict, "owner is still current"):
            self.ledger.reclaim_projection_cleanup(
                operation_id=self.op("premature-reclaim"),
                actor_principal_id="operator",
                realization_id=created.realization.realization_id,
                expected_owner_generation=created.realization.owner_generation,
                owner_holder_id="replacement-owner",
                builder_holder_id="replacement-cleaner",
                builder_ttl_seconds=60,
                cleanup_token="8" * 64,
            )

    def test_consumer_acquisition_renews_owner_without_shortening_it(self) -> None:
        # This test exercises monotonic renewal, not expiry.  Leave enough
        # scheduling margin that a loaded test process cannot consume the
        # owner term before the acquisition under test begins.
        ready = self.make_ready(ttl=0.5)
        previous_expiry = ready.owner_lease.expires_at
        time.sleep(0.01)
        consumer = self.ledger.acquire_projection_consumer(
            operation_id=self.op("long-consumer"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_holder_id="long-viewer",
            consumer_ttl_seconds=1,
            consumer_kind="inspection",
        )
        renewed_owner = self.ledger.validate_lease(
            actor_principal_id="operator",
            lease_id=ready.owner_lease.lease_id,
            holder_id=ready.owner_lease.holder_id,
            fencing_token=ready.owner_lease.fencing_token,
        )
        self.assertGreater(renewed_owner.expires_at, previous_expiry + 0.5)
        self.assertGreaterEqual(renewed_owner.expires_at, consumer.consumer_lease.expires_at)

        second = self.ledger.acquire_projection_consumer(
            operation_id=self.op("short-consumer"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_holder_id="short-viewer",
            consumer_ttl_seconds=0.05,
            consumer_kind="inspection",
        )
        after_short_consumer = self.ledger.validate_lease(
            actor_principal_id="operator",
            lease_id=ready.owner_lease.lease_id,
            holder_id=ready.owner_lease.holder_id,
            fencing_token=ready.owner_lease.fencing_token,
        )
        self.assertGreaterEqual(after_short_consumer.expires_at, renewed_owner.expires_at)
        self.assertLessEqual(second.consumer_lease.expires_at, after_short_consumer.expires_at)

        self.ledger.heartbeat_projection_consumer(
            operation_id=self.op("short-consumer-heartbeat"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_id=second.consumer.consumer_id,
            consumer_holder_id=second.consumer_lease.holder_id,
            consumer_fencing_token=second.consumer_lease.fencing_token,
            ttl_seconds=0.05,
        )
        after_short_heartbeat = self.ledger.validate_lease(
            actor_principal_id="operator",
            lease_id=ready.owner_lease.lease_id,
            holder_id=ready.owner_lease.holder_id,
            fencing_token=ready.owner_lease.fencing_token,
        )
        self.assertGreaterEqual(
            after_short_heartbeat.expires_at, after_short_consumer.expires_at
        )

    def test_first_consumer_can_atomically_release_materialization_grace(self) -> None:
        ready = self.make_ready(ttl=60)
        previous_expiry = ready.owner_lease.expires_at
        consumer = self.ledger.acquire_projection_consumer(
            operation_id=self.op("first-consumer-after-build"),
            actor_principal_id="operator",
            realization_id=ready.realization.realization_id,
            consumer_holder_id="short-viewer",
            consumer_ttl_seconds=0.05,
            consumer_kind="inspection",
            release_materialization_grace=True,
        )
        owner = self.ledger.validate_lease(
            actor_principal_id="operator",
            lease_id=ready.owner_lease.lease_id,
            holder_id=ready.owner_lease.holder_id,
            fencing_token=ready.owner_lease.fencing_token,
        )
        self.assertLess(owner.expires_at, previous_expiry - 1)
        self.assertGreaterEqual(owner.expires_at, consumer.consumer_lease.expires_at)
        self.assertLess(owner.expires_at - consumer.consumer.created_at, 0.2)

    def test_reserved_projection_leases_and_rows_reject_generic_or_replace_paths(self) -> None:
        with self.assertRaises(RealmConflict):
            self.ledger.acquire_lease(
                operation_id=self.op("generic"),
                actor_principal_id="operator",
                owner_id="workspace",
                lease_kind="projection-owner",
                audience="runtime",
                holder_id="forger",
                scope_key="projection-owner:forged",
                ttl_seconds=60,
            )
        created = self.create()
        with self.assertRaisesRegex(RealmConflict, "typed projection replacement"):
            self.ledger.acquire_lease(
                operation_id=self.op("generic-replacement"),
                actor_principal_id="operator",
                owner_id="workspace",
                lease_kind="ordinary-runtime",
                audience="runtime",
                holder_id="replacement",
                scope_key=created.owner_lease.scope_key,
                ttl_seconds=60,
                replace_lease_id=created.owner_lease.lease_id,
                replace_fencing_token=created.owner_lease.fencing_token,
            )
        for action in ("heartbeat", "release"):
            with self.subTest(action=action), self.assertRaises(RealmConflict):
                if action == "heartbeat":
                    self.ledger.heartbeat_lease(
                        operation_id=self.op("generic-heartbeat"),
                        actor_principal_id="operator",
                        lease_id=created.owner_lease.lease_id,
                        holder_id=created.owner_lease.holder_id,
                        fencing_token=created.owner_lease.fencing_token,
                        ttl_seconds=60,
                    )
                else:
                    self.ledger.release_lease(
                        operation_id=self.op("generic-release"),
                        actor_principal_id="operator",
                        lease_id=created.owner_lease.lease_id,
                        holder_id=created.owner_lease.holder_id,
                        fencing_token=created.owner_lease.fencing_token,
                    )
        connection = sqlite3.connect(self.ledger.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT OR REPLACE INTO projection_roots SELECT * FROM projection_roots "
                    "WHERE projection_root_id = ?",
                    (self.projection_root_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE leases SET state = 'revoked' WHERE lease_id = ?",
                    (created.owner_lease.lease_id,),
                )
        finally:
            connection.close()

    def test_expired_owner_expires_due_builder_without_generic_revocation(self) -> None:
        created = self.create(ttl=0.05)
        claimed = self.ledger.claim_projection_materialization(
            operation_id=self.op("claim-short-lived"),
            actor_principal_id="operator",
            realization_id=created.realization.realization_id,
            owner_holder_id=created.owner_lease.holder_id,
            owner_fencing_token=created.owner_lease.fencing_token,
            builder_holder_id="short-lived-builder",
            builder_ttl_seconds=0.05,
        )
        time.sleep(0.08)

        with self.assertRaises(RealmExpired):
            self.ledger.validate_projection_owner_lease(
                actor_principal_id="operator",
                realization_id=created.realization.realization_id,
                lease_id=created.owner_lease.lease_id,
                holder_id=created.owner_lease.holder_id,
                fencing_token=created.owner_lease.fencing_token,
            )

        connection = sqlite3.connect(self.ledger.database_path)
        try:
            states = dict(
                connection.execute(
                    "SELECT lease_id, state FROM leases WHERE lease_id IN (?, ?)",
                    (created.owner_lease.lease_id, claimed.builder_lease.lease_id),
                )
            )
        finally:
            connection.close()
        self.assertEqual(states[created.owner_lease.lease_id], "expired")
        self.assertEqual(states[claimed.builder_lease.lease_id], "expired")

    def test_request_key_coordinates_only_active_realizations(self) -> None:
        first = self.create(suffix="a")
        with self.assertRaises(RealmConflict):
            self.create(suffix="b")
        quarantined = self.ledger.quarantine_projection_realization(
            operation_id=self.op("quarantine"),
            actor_principal_id="operator",
            realization_id=first.realization.realization_id,
            reason="provider failed before namespace creation",
        )
        self.assertEqual(
            quarantined.realization.state, ProjectionRealizationState.QUARANTINED
        )
        replacement = self.create(suffix="b")
        self.assertEqual(replacement.realization.state, ProjectionRealizationState.CREATING)


if __name__ == "__main__":
    unittest.main()
