from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from optpilot.realm import ephemeral_volume_namespace as volume_namespace
from optpilot.realm.ephemeral_volume_namespace import (
    cleanup_ephemeral_volume_namespace,
    complete_ephemeral_volume_cleanup_namespace,
    create_ephemeral_volume_namespace,
    prepare_ephemeral_volume_root,
)
from optpilot.realm.ephemeral_volume_records import EphemeralVolumeState
from optpilot.realm.ephemeral_volume_service import RealmEphemeralVolumeService
from optpilot.realm.errors import (
    RealmConflict,
    RealmError,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
    RealmStorageIdentityChanged,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.layered_volume_realization import (
    compile_local_layered_volume_plan,
)
from optpilot.realm.local_attempt_protocol import publish_exact_record
from optpilot.realm.filesystem_quota import FilesystemQuota
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.refs import SnapshotRef, request_digest
from optpilot.runtime_binding import ProjectedInputLayer


TEST_VOLUME_QUOTA = FilesystemQuota(
    max_entries=1_000,
    max_file_bytes=16 * 1024**2,
    max_total_bytes=64 * 1024**2,
)


class RealmEphemeralVolumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="volume-test/principal", principal_id="operator", kind="human"
        )
        self.ledger.create_owner(
            operation_id="volume-test/owner",
            owner_id="runtime-owner",
            owner_kind="runtime-test",
            principal_id="operator",
        )
        self.parent = self.ledger.acquire_lease(
            operation_id="volume-test/parent",
            actor_principal_id="operator",
            owner_id="runtime-owner",
            lease_kind="runtime-session",
            audience="runtime",
            holder_id="runtime-parent",
            scope_key="runtime-session:test",
            ttl_seconds=60,
        )
        self.service = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def create(
        self,
        label: str = "a",
        *,
        ttl: float = 60,
        quota: FilesystemQuota = TEST_VOLUME_QUOTA,
    ):
        return self.service.create(
            operation_id=f"volume-test/create/{label}",
            actor_principal_id="operator",
            parent_lease=self.parent,
            holder_id=f"writer-{label}",
            quota=quota,
            ttl_seconds=ttl,
        )

    def test_registered_root_fact_mismatch_has_typed_operator_guidance(self) -> None:
        with (
            patch.object(
                self.ledger,
                "validate_ephemeral_volume_root",
                side_effect=RealmNotFound("injected changed root facts"),
            ),
            patch.object(
                self.ledger,
                "register_ephemeral_volume_root",
                side_effect=RealmConflict("injected existing root id"),
            ),
            self.assertRaises(RealmStorageIdentityChanged) as raised,
        ):
            self.service._ensure_registered_root()

        self.assertIn("No files were changed", str(raised.exception))
        self.assertIn("OPTPILOT_REALM_ROOT", str(raised.exception))

    def test_service_reopen_accepts_new_root_attachment_observation(self) -> None:
        root_path = self.root / "remounted-volumes"
        binding = prepare_ephemeral_volume_root(
            root_path, realm_id=self.ledger.realm_id
        )
        principal_digest = request_digest(
            {
                "format": "optpilot.ephemeral-volume-maintainer.v1",
                "realm_id": self.ledger.realm_id,
                "volume_root_id": binding.volume_root_id,
            }
        )
        maintainer = f"ephemeral-volume-maintainer-{principal_digest[:40]}"
        self.ledger.register_principal(
            operation_id="volume-test/remount-maintainer",
            principal_id=maintainer,
            kind="service",
        )
        marker_digest = self.ledger.ephemeral_volume_root_marker_digest(
            volume_root_id=binding.volume_root_id,
            backend_kind=binding.provider_kind,
            claim_nonce=binding.claim_nonce,
        )
        self.ledger.register_ephemeral_volume_root(
            operation_id="volume-test/remount-root",
            actor_principal_id=maintainer,
            volume_root_id=binding.volume_root_id,
            canonical_path=str(binding.path),
            backend_kind=binding.provider_kind,
            marker_digest=marker_digest,
            claim_nonce=binding.claim_nonce,
            device_id=binding.device_id + 1000,
            inode=binding.inode + 1000,
        )

        reopened = RealmEphemeralVolumeService(
            self.ledger, volume_root=root_path
        )

        self.assertEqual(reopened.root_binding, binding)
        volume = reopened.create(
            operation_id="volume-test/remount-create",
            actor_principal_id="operator",
            parent_lease=self.parent,
            holder_id="writer-remounted",
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )
        try:
            self.assertEqual(volume.record.state, EphemeralVolumeState.ACTIVE)
            self.assertEqual(
                volume.record.wrapper_device_id,
                volume.record.data_device_id,
            )
            self.assertEqual(
                volume.record.wrapper_device_id,
                binding.device_id,
            )
        finally:
            volume.close()

    def test_active_namespace_rebinds_descriptor_observations_from_claim(self) -> None:
        volume = self.create("remount-active")
        try:
            historical = replace(
                volume.record,
                wrapper_device_id=volume.record.wrapper_device_id + 1000,
                wrapper_inode=volume.record.wrapper_inode + 1000,
                data_device_id=volume.record.data_device_id + 1000,
                data_inode=volume.record.data_inode + 1000,
            )

            observed = self.service._current_identity(historical)

            self.assertEqual(observed, volume._namespace.identity)
            with self.assertRaises((RealmConflict, RealmIntegrityError)):
                self.service._current_identity(
                    replace(historical, claim_nonce="e" * 64)
                )
        finally:
            volume.close()

    def maintenance_record(self, volume_id: str):
        matches = [
            record
            for record in self.ledger.list_ephemeral_volumes(
                actor_principal_id=self.service.maintenance_principal_id,
                volume_root_id=self.service.root_binding.volume_root_id,
                states=tuple(EphemeralVolumeState),
            )
            if record.volume_id == volume_id
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    @staticmethod
    def _initialization_identity(label: str = "a") -> dict[str, object]:
        return {
            "lower_layers_digest": hashlib.sha256(
                f"layers-{label}".encode("utf-8")
            ).hexdigest(),
            "portable_spec_digest": hashlib.sha256(
                f"spec-{label}".encode("utf-8")
            ).hexdigest(),
            "projection_consumer_fencing_token": 1,
            "projection_consumer_id": f"consumer-{label}",
            "projection_plan_digest": hashlib.sha256(
                f"plan-{label}".encode("utf-8")
            ).hexdigest(),
            "projection_realization_id": f"realization-{label}",
            "projection_spec_digest": hashlib.sha256(
                f"projection-{label}".encode("utf-8")
            ).hexdigest(),
        }

    def _layered_plan(self, label: str = "seed"):
        source = self.root / f"source-{label}"
        partition = source / "environment"
        partition.mkdir(parents=True)
        (partition / "empty").mkdir()
        script = partition / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        (partition / "seed.txt").write_text("immutable seed\n", encoding="utf-8")
        snapshot = SnapshotRef.from_manifest_bytes(label.encode("utf-8"))
        plan = compile_local_layered_volume_plan(
            source,
            (
                ProjectedInputLayer(
                    scope="trial",
                    projection_name="environment-inputs",
                    projection_subpath="environment",
                    snapshot_ref=snapshot,
                ),
            ),
            TEST_VOLUME_QUOTA,
        )
        return source, plan

    def test_layered_initialization_is_once_replayable_and_cleanup_private(
        self,
    ) -> None:
        source, plan = self._layered_plan()
        volume = self.create("layered-once")
        authorizations: list[str] = []

        created = volume.initialize_layered(
            source_root=source,
            plan=plan,
            initialization_identity=self._initialization_identity(),
            authorize_publication=lambda: authorizations.append("authorized"),
        )

        self.assertTrue(created)
        self.assertEqual(authorizations, ["authorized"])
        self.assertEqual(
            (volume.path / "seed.txt").read_text(encoding="utf-8"),
            "immutable seed\n",
        )
        self.assertTrue((volume.path / "empty").is_dir())
        self.assertEqual(
            (volume.path / "run.sh").stat().st_mode & 0o777,
            0o700,
        )
        proof = volume.path.parent / ".optpilot-provider-initialization.json"
        self.assertTrue(proof.is_file())
        self.assertFalse((volume.path / proof.name).exists())

        (volume.path / "seed.txt").write_text("user mutation\n", encoding="utf-8")
        restarted = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )
        attached = restarted.reattach(
            actor_principal_id="operator",
            volume_id=volume.record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        with self.assertRaisesRegex(
            RealmIntegrityError, "proof has a different identity"
        ):
            attached.initialize_layered(
                source_root=source,
                plan=plan,
                initialization_identity=self._initialization_identity("other"),
                authorize_publication=lambda: authorizations.append("unexpected"),
            )
        with self.assertRaisesRegex(
            RealmIntegrityError, "changed before binding preflight"
        ):
            attached.initialize_layered(
                source_root=source,
                plan=plan,
                initialization_identity=self._initialization_identity(),
                authorize_publication=lambda: authorizations.append("unexpected"),
            )
        self.assertEqual(authorizations, ["authorized"])
        self.assertEqual(
            (attached.path / "seed.txt").read_text(encoding="utf-8"),
            "user mutation\n",
        )

        attached._detach_without_release()
        volume.close()
        self.assertFalse(proof.exists())

    def test_layered_initialization_rebuilds_unproved_partial_state(self) -> None:
        source, plan = self._layered_plan("partial")
        volume = self.create("layered-partial")

        with self.assertRaisesRegex(RealmConflict, "publication blocked"):
            volume.initialize_layered(
                source_root=source,
                plan=plan,
                initialization_identity=self._initialization_identity("partial"),
                authorize_publication=lambda: (_ for _ in ()).throw(
                    RealmConflict("publication blocked")
                ),
            )
        (volume.path / "junk.txt").write_text("partial", encoding="utf-8")

        self.assertTrue(
            volume.initialize_layered(
                source_root=source,
                plan=plan,
                initialization_identity=self._initialization_identity("partial"),
                authorize_publication=lambda: None,
            )
        )
        self.assertFalse((volume.path / "junk.txt").exists())
        self.assertEqual(
            (volume.path / "seed.txt").read_text(encoding="utf-8"),
            "immutable seed\n",
        )
        volume.close()

    def test_candidate_replace_layer_overlays_seed_tree_after_environment_inputs(
        self,
    ) -> None:
        source = self.root / "source-candidate-overlay"
        environment = source / "environment"
        candidate = source / "candidate"
        (environment / "candidate" / "obsolete").mkdir(parents=True)
        (environment / "candidate" / "solver.py").write_text(
            "SEED = True\n", encoding="utf-8"
        )
        (environment / "candidate" / "lib").write_text("seed-file\n", encoding="utf-8")
        (environment / "candidate" / "obsolete" / "child.txt").write_text(
            "remove me\n", encoding="utf-8"
        )
        (environment / "case.json").write_text("{}\n", encoding="utf-8")
        (candidate / "lib").mkdir(parents=True)
        (candidate / "solver.py").write_text("SEED = False\n", encoding="utf-8")
        (candidate / "lib" / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
        (candidate / "obsolete").write_text("candidate-file\n", encoding="utf-8")
        plan = compile_local_layered_volume_plan(
            source,
            (
                ProjectedInputLayer(
                    scope="trial",
                    projection_name="environment-inputs",
                    projection_subpath="environment",
                    snapshot_ref=SnapshotRef.from_manifest_bytes(b"environment"),
                    precedence=0,
                    collision_policy="identical",
                ),
                ProjectedInputLayer(
                    scope="trial",
                    projection_name="environment-inputs",
                    projection_subpath="candidate",
                    snapshot_ref=SnapshotRef.from_manifest_bytes(b"candidate"),
                    destination_subpath="candidate",
                    precedence=1,
                    collision_policy="replace",
                ),
            ),
            TEST_VOLUME_QUOTA,
        )
        volume = self.create("candidate-overlay")

        self.assertTrue(
            volume.initialize_layered(
                source_root=source,
                plan=plan,
                initialization_identity=self._initialization_identity(
                    "candidate-overlay"
                ),
                authorize_publication=lambda: None,
            )
        )

        self.assertEqual(
            (volume.path / "candidate" / "solver.py").read_text(encoding="utf-8"),
            "SEED = False\n",
        )
        self.assertEqual(
            (volume.path / "candidate" / "lib" / "helper.py").read_text(
                encoding="utf-8"
            ),
            "VALUE = 3\n",
        )
        self.assertEqual(
            (volume.path / "candidate" / "obsolete").read_text(encoding="utf-8"),
            "candidate-file\n",
        )
        self.assertEqual(
            (volume.path / "case.json").read_text(encoding="utf-8"), "{}\n"
        )
        volume.close()

    def test_concurrent_layered_initialization_publishes_exactly_once(self) -> None:
        source, plan = self._layered_plan("concurrent")
        first = self.create("layered-concurrent")
        second_service = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )
        second = second_service.reattach(
            actor_principal_id="operator",
            volume_id=first.record.volume_id,
            holder_id=first.lease.holder_id,
            fencing_token=first.lease.fencing_token,
        )
        results: list[bool] = []
        errors: list[BaseException] = []
        authorizations = 0
        authorization_lock = threading.Lock()

        def initialize(target) -> None:
            nonlocal authorizations

            def authorize() -> None:
                nonlocal authorizations
                with authorization_lock:
                    authorizations += 1
                time.sleep(0.01)

            try:
                results.append(
                    target.initialize_layered(
                        source_root=source,
                        plan=plan,
                        initialization_identity=self._initialization_identity(
                            "concurrent"
                        ),
                        authorize_publication=authorize,
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = (
            threading.Thread(target=initialize, args=(first,)),
            threading.Thread(target=initialize, args=(second,)),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(authorizations, 1)
        second._detach_without_release()
        first.close()

    def test_create_replay_write_heartbeat_and_close_are_single_use(self) -> None:
        volume = self.create()
        original_path = volume.path
        (original_path / "result.txt").write_text("runtime output", encoding="utf-8")
        heartbeat = volume.heartbeat(
            operation_id="volume-test/heartbeat", ttl_seconds=30
        )
        self.assertGreater(heartbeat.heartbeat_revision, 0)
        portable = volume.portable_record()
        self.assertNotIn(str(self.root), repr(portable))
        self.assertNotIn("canonical_path", portable)
        self.assertNotIn("volume_id", portable)
        self.assertNotIn("provider_kind", portable)
        self.assertEqual(portable["quota"], TEST_VOLUME_QUOTA.to_dict())
        self.assertEqual(portable["quota_enforcement"], "advisory")
        volume_id = volume.record.volume_id

        volume.close()
        self.assertFalse(original_path.exists())
        self.assertEqual(
            self.maintenance_record(volume_id).state,
            EphemeralVolumeState.CLEANED,
        )
        replay = self.service.reconcile_volume(
            operation_id="volume-test/reconcile/replay", volume_id=volume_id
        )
        self.assertTrue(replay.already_complete)
        with self.assertRaisesRegex(RealmConflict, "no longer available"):
            self.create()

        replacement = self.create("b")
        self.assertNotEqual(replacement.path, original_path)
        replacement.close()

    def test_exact_reattach_after_service_restart_uses_persisted_identity(self) -> None:
        original = self.create("exact-reattach")
        original_path = original.path
        (original_path / "before-restart.txt").write_text(
            "same exact volume", encoding="utf-8"
        )
        restarted = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )

        reopened = restarted.reattach(
            actor_principal_id="operator",
            volume_id=original.record.volume_id,
            holder_id=original.lease.holder_id,
            fencing_token=original.lease.fencing_token,
        )

        self.assertEqual(reopened.path, original_path)
        self.assertEqual(reopened.record, original.record)
        self.assertEqual(reopened.lease, original.lease)
        self.assertEqual(
            (reopened.path / "before-restart.txt").read_text(encoding="utf-8"),
            "same exact volume",
        )
        with self.assertRaises(RealmConflict):
            restarted.reattach(
                actor_principal_id="operator",
                volume_id=original.record.volume_id,
                holder_id=original.lease.holder_id,
                fencing_token=original.lease.fencing_token + 1,
            )

        reopened._detach_without_release()
        original.close()

    def test_expired_parent_revokes_reserved_volume_lease(self) -> None:
        volume = self.create("expired-parent")
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (self.parent.lease_id,),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(RealmExpired):
            self.ledger.validate_lease(
                actor_principal_id="operator",
                lease_id=self.parent.lease_id,
                holder_id=self.parent.holder_id,
                fencing_token=self.parent.fencing_token,
            )

        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            parent_state = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ?",
                (self.parent.lease_id,),
            ).fetchone()[0]
            usage_state = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ?",
                (volume.lease.lease_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(parent_state, "expired")
        self.assertEqual(usage_state, "revoked")

    def test_recover_existing_by_operation_crosses_actor_after_heartbeat(self) -> None:
        self.ledger.register_principal(
            operation_id="volume-recovery/delegate-principal",
            principal_id="recovery-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="volume-recovery/delegate-grant",
            actor_principal_id="operator",
            owner_id="runtime-owner",
            principal_id="recovery-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "volume-test/create/cross-actor-recovery"
        holder_id = "cross-actor-recovery-holder"
        original = self.service.create(
            operation_id=operation_id,
            actor_principal_id="operator",
            parent_lease=self.parent,
            holder_id=holder_id,
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )
        (original.path / "before-recovery.txt").write_text(
            "same exact writable volume", encoding="utf-8"
        )
        original.heartbeat(
            operation_id="volume-recovery/usage-heartbeat", ttl_seconds=60
        )
        restarted = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )

        recovered = restarted.recover_existing(
            operation_id=operation_id,
            actor_principal_id="recovery-delegate",
            parent_lease=self.parent,
            holder_id=holder_id,
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )

        self.assertEqual(recovered.record, original.record)
        self.assertEqual(recovered.lease, original.lease)
        self.assertEqual(recovered.path, original.path)
        self.assertEqual(
            (recovered.path / "before-recovery.txt").read_text(encoding="utf-8"),
            "same exact writable volume",
        )
        recovered._detach_without_release()
        original.close()

    def test_recover_existing_finishes_cross_actor_allocating_volume(self) -> None:
        self.ledger.register_principal(
            operation_id="volume-allocating/delegate-principal",
            principal_id="allocating-recovery-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="volume-allocating/delegate-grant",
            actor_principal_id="operator",
            owner_id="runtime-owner",
            principal_id="allocating-recovery-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "volume-test/create/allocating-recovery"
        holder_id = "allocating-recovery-holder"
        with patch.object(
            self.ledger,
            "activate_ephemeral_volume",
            side_effect=RuntimeError("crash before volume activation"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before volume activation"):
                self.service.create(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    parent_lease=self.parent,
                    holder_id=holder_id,
                    quota=TEST_VOLUME_QUOTA,
                    ttl_seconds=60,
                )
        allocating = self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.service.maintenance_principal_id,
            volume_root_id=self.service.root_binding.volume_root_id,
            states=(EphemeralVolumeState.ALLOCATING,),
        )
        self.assertEqual(len(allocating), 1)
        restarted = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )

        recovered = restarted.recover_existing(
            operation_id=operation_id,
            actor_principal_id="allocating-recovery-delegate",
            parent_lease=self.parent,
            holder_id=holder_id,
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )

        self.assertEqual(recovered.record.volume_id, allocating[0].volume_id)
        self.assertEqual(recovered.record.state, EphemeralVolumeState.ACTIVE)
        (recovered.path / "after-recovery.txt").write_text(
            "activated once", encoding="utf-8"
        )
        self.assertEqual(
            (recovered.path / "after-recovery.txt").read_text(encoding="utf-8"),
            "activated once",
        )
        recovered.close()
        self.assertEqual(
            self.maintenance_record(allocating[0].volume_id).state,
            EphemeralVolumeState.CLEANED,
        )

    def test_recover_existing_rejects_wrong_coordinate_and_semantics(self) -> None:
        operation_id = "volume-test/create/exact-operation-recovery"
        holder_id = "exact-operation-recovery-holder"
        original = self.service.create(
            operation_id=operation_id,
            actor_principal_id="operator",
            parent_lease=self.parent,
            holder_id=holder_id,
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )
        other_parent = self.ledger.acquire_lease(
            operation_id="volume-recovery/other-parent",
            actor_principal_id="operator",
            owner_id="runtime-owner",
            lease_kind="runtime-session",
            audience="runtime",
            holder_id="other-parent-holder",
            scope_key="runtime-session:other",
            ttl_seconds=60,
        )
        different_quota = FilesystemQuota(
            max_entries=999,
            max_file_bytes=TEST_VOLUME_QUOTA.max_file_bytes,
            max_total_bytes=TEST_VOLUME_QUOTA.max_total_bytes,
        )

        with self.assertRaises(RealmNotFound):
            self.service.recover_existing(
                operation_id="volume-test/create/substituted-operation",
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id=holder_id,
                quota=TEST_VOLUME_QUOTA,
                ttl_seconds=60,
            )
        with self.assertRaisesRegex(RealmConflict, "requested semantics"):
            self.service.recover_existing(
                operation_id=operation_id,
                actor_principal_id="operator",
                parent_lease=other_parent,
                holder_id=holder_id,
                quota=TEST_VOLUME_QUOTA,
                ttl_seconds=60,
            )
        with self.assertRaises(RealmConflict):
            self.service.recover_existing(
                operation_id=operation_id,
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id="substituted-holder",
                quota=TEST_VOLUME_QUOTA,
                ttl_seconds=60,
            )
        with self.assertRaisesRegex(RealmConflict, "requested semantics"):
            self.service.recover_existing(
                operation_id=operation_id,
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id=holder_id,
                quota=different_quota,
                ttl_seconds=60,
            )
        with self.assertRaisesRegex(RealmConflict, "different initial TTL"):
            self.service.recover_existing(
                operation_id=operation_id,
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id=holder_id,
                quota=TEST_VOLUME_QUOTA,
                ttl_seconds=30,
            )

        original.close()

    def test_recover_existing_rejects_tampered_operation_identity(self) -> None:
        operation_id = "volume-test/create/tampered-operation-identity"
        holder_id = "tampered-operation-identity-holder"
        original = self.service.create(
            operation_id=operation_id,
            actor_principal_id="operator",
            parent_lease=self.parent,
            holder_id=holder_id,
            quota=TEST_VOLUME_QUOTA,
            ttl_seconds=60,
        )
        tampered = replace(
            original.record, relative_name="volume-substituted-coordinate"
        )

        with patch.object(self.ledger, "read_ephemeral_volume", return_value=tampered):
            with self.assertRaisesRegex(RealmConflict, "requested semantics"):
                self.service.recover_existing(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    parent_lease=self.parent,
                    holder_id=holder_id,
                    quota=TEST_VOLUME_QUOTA,
                    ttl_seconds=60,
                )

        original.close()

    def test_parent_release_fences_stale_writer_and_becomes_cleanup_debt(self) -> None:
        volume = self.create()
        volume_id = volume.record.volume_id
        stale_path = volume.path
        self.ledger.release_lease(
            operation_id="volume-test/parent/release",
            actor_principal_id="operator",
            lease_id=self.parent.lease_id,
            holder_id=self.parent.holder_id,
            fencing_token=self.parent.fencing_token,
        )
        with self.assertRaises(RealmError):
            volume.validate()
        with self.assertRaises(RealmError):
            _ = volume.path
        # The v1 local provider fence is cooperative: a previously copied raw
        # host Path remains an OS-usable pathname until supervised cleanup.
        (stale_path / "advisory-only").write_text("trusted caller", encoding="utf-8")

        volume.close()
        self.assertFalse(stale_path.exists())
        self.assertEqual(
            self.maintenance_record(volume_id).state,
            EphemeralVolumeState.CLEANED,
        )

    def test_heartbeat_replay_does_not_revive_released_parent_authority(self) -> None:
        volume = self.create("heartbeat-replay")
        operation = "volume-test/heartbeat/replay"
        volume.heartbeat(operation_id=operation, ttl_seconds=30)
        self.ledger.release_lease(
            operation_id="volume-test/heartbeat/replay/parent-release",
            actor_principal_id="operator",
            lease_id=self.parent.lease_id,
            holder_id=self.parent.holder_id,
            fencing_token=self.parent.fencing_token,
        )
        with self.assertRaises(RealmError):
            volume.heartbeat(operation_id=operation, ttl_seconds=30)
        volume.close()

    def test_public_create_operation_rejects_changed_request(self) -> None:
        volume = self.create("operation-binding", ttl=60)
        with self.assertRaises(RealmConflict):
            self.create("operation-binding", ttl=30)
        active = self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.service.maintenance_principal_id,
            volume_root_id=self.service.root_binding.volume_root_id,
            states=(EphemeralVolumeState.ACTIVE,),
        )
        self.assertEqual(
            tuple(item.volume_id for item in active), (volume.record.volume_id,)
        )
        volume.close()

    def test_advisory_quota_is_exactly_persisted_and_fenced_at_checkpoints(
        self,
    ) -> None:
        quota = FilesystemQuota(
            max_entries=2,
            max_file_bytes=4,
            max_total_bytes=4,
        )
        volume = self.create("quota", quota=quota)
        path = volume.path
        self.assertEqual(volume.record.quota, quota)
        self.assertEqual(volume.record.quota_enforcement, "advisory")
        (path / "too-large").write_bytes(b"12345")
        with self.assertRaisesRegex(RealmIntegrityError, "per-file quota"):
            volume.validate()
        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )

    def test_advisory_quota_ignores_a_listed_file_removed_before_open(self) -> None:
        volume = self.create("quota-live-unlink")
        path = volume.path
        temporary = path / ".attempt-result.json.test.tmp"
        temporary.write_bytes(b'{"status":"complete"}')
        data_fd = volume._namespace._data_fd
        real_open = volume_namespace.os.open
        removed = False

        def unlink_before_open(name, flags, *args, **kwargs):
            nonlocal removed
            if (
                not removed
                and name == temporary.name
                and kwargs.get("dir_fd") == data_fd
            ):
                temporary.unlink()
                removed = True
            return real_open(name, flags, *args, **kwargs)

        with patch.object(
            volume_namespace.os, "open", side_effect=unlink_before_open
        ):
            volume.validate()

        self.assertTrue(removed)
        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.ACTIVE,
        )
        volume.close()

    def test_advisory_quota_ignores_a_file_created_after_listing(self) -> None:
        volume = self.create("quota-live-create")
        path = volume.path
        created = path / ".attempt-result.json.late.tmp"
        data_fd = volume._namespace._data_fd
        real_listdir = volume_namespace.os.listdir
        injected = False

        def create_after_listing(directory):
            nonlocal injected
            names = real_listdir(directory)
            if not injected and directory == data_fd:
                created.write_bytes(b'{"status":"publishing"}')
                injected = True
            return names

        with patch.object(
            volume_namespace.os, "listdir", side_effect=create_after_listing
        ):
            volume.validate()

        self.assertTrue(injected)
        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.ACTIVE,
        )
        volume.close()

    def test_advisory_quota_scans_during_exact_record_publication_churn(
        self,
    ) -> None:
        volume = self.create("quota-live-publication")
        result_path = volume.path / "attempt-result.json"
        encoded = b'{"status":"complete"}'
        self.assertTrue(publish_exact_record(result_path, encoded))
        started = threading.Event()
        stop = threading.Event()
        publications = 0
        writer_errors: list[BaseException] = []

        def republish() -> None:
            nonlocal publications
            try:
                while not stop.is_set():
                    publish_exact_record(result_path, encoded)
                    publications += 1
                    started.set()
            except BaseException as error:
                writer_errors.append(error)
                started.set()

        writer = threading.Thread(target=republish, daemon=True)
        writer.start()
        try:
            self.assertTrue(started.wait(timeout=5.0))
            for _index in range(100):
                volume.validate()
        finally:
            stop.set()
            writer.join(timeout=5.0)

        self.assertFalse(writer.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertGreater(publications, 0)
        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.ACTIVE,
        )
        volume.close()

    def test_live_advisory_scan_still_rejects_an_observed_symlink(self) -> None:
        volume = self.create("quota-live-symlink")
        path = volume.path
        (path / "target.txt").write_text("inside", encoding="utf-8")
        (path / "unsafe-link").symlink_to("target.txt")

        with self.assertRaisesRegex(
            RealmIntegrityError, "unsupported filesystem entry"
        ):
            volume.validate()

        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )

    def test_local_provider_rejects_claiming_hard_quota_enforcement(self) -> None:
        with self.assertRaisesRegex(ValueError, "advisory"):
            self.service.create(
                operation_id="volume-test/create/enforced",
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id="writer-enforced",
                quota=TEST_VOLUME_QUOTA,
                quota_enforcement="enforced",
            )

    def test_quota_round_trip_allows_effective_total_below_per_file_limit(self) -> None:
        quota = FilesystemQuota(
            max_entries=8,
            max_file_bytes=16,
            max_total_bytes=4,
        )
        volume = self.create("quota-independent-bounds", quota=quota)
        self.assertEqual(volume.record.quota, quota)
        self.assertEqual(volume.portable_record()["quota"], quota.to_dict())
        (volume.path / "within-total").write_bytes(b"1234")
        volume.validate()
        volume.close()

    def test_close_retries_transient_release_failure_without_stranding_active_tree(
        self,
    ) -> None:
        volume = self.create("close-precommit")
        path = volume.path
        real_release = self.ledger.release_ephemeral_volume
        with patch.object(
            self.ledger,
            "release_ephemeral_volume",
            side_effect=RuntimeError("transient pre-commit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                volume.close()
        self.assertFalse(volume.closed)
        self.assertTrue(path.is_dir())
        self.assertEqual(
            self.ledger.read_ephemeral_volume(
                actor_principal_id="operator", volume_id=volume.record.volume_id
            ).state,
            EphemeralVolumeState.ACTIVE,
        )
        with patch.object(self.ledger, "release_ephemeral_volume", wraps=real_release):
            volume.close()
        self.assertTrue(volume.closed)
        self.assertFalse(path.exists())

    def test_close_replays_release_after_commit_response_loss(self) -> None:
        volume = self.create("close-response-loss")
        path = volume.path
        real_release = self.ledger.release_ephemeral_volume

        def commit_then_lose(**kwargs):
            real_release(**kwargs)
            raise RuntimeError("lost release response")

        with patch.object(
            self.ledger,
            "release_ephemeral_volume",
            side_effect=commit_then_lose,
        ):
            with self.assertRaisesRegex(RuntimeError, "lost release response"):
                volume.close()
        self.assertFalse(volume.closed)
        self.assertTrue(path.is_dir())
        self.assertEqual(
            self.maintenance_record(volume.record.volume_id).state,
            EphemeralVolumeState.CLEANUP_PENDING,
        )
        volume.close()
        self.assertTrue(volume.closed)
        self.assertFalse(path.exists())

    def test_crash_between_namespace_create_and_activation_is_reconciled(self) -> None:
        with patch.object(
            self.ledger,
            "activate_ephemeral_volume",
            side_effect=RuntimeError("simulated process loss"),
        ):
            with self.assertRaisesRegex(RuntimeError, "process loss"):
                self.create("crash")
        records = self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.service.maintenance_principal_id,
            volume_root_id=self.service.root_binding.volume_root_id,
            states=(EphemeralVolumeState.ALLOCATING,),
        )
        self.assertEqual(len(records), 1)
        wrapper = self.service.root_binding.path / records[0].relative_name
        self.assertTrue(wrapper.is_dir())

        self.ledger.release_lease(
            operation_id="volume-test/crash/parent-release",
            actor_principal_id="operator",
            lease_id=self.parent.lease_id,
            holder_id=self.parent.holder_id,
            fencing_token=self.parent.fencing_token,
        )
        outcomes = self.service.reconcile_all(
            operation_id="volume-test/crash/reconcile"
        )
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].ok)
        self.assertFalse(wrapper.exists())
        self.assertEqual(
            self.maintenance_record(records[0].volume_id).state,
            EphemeralVolumeState.CLEANED,
        )

    def test_response_loss_after_physical_deletion_replays_to_completion(self) -> None:
        volume = self.create("response-loss")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        (volume.path / "payload").write_bytes(b"discard me")
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/response-loss/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        volume._namespace.close()
        volume._closed = True
        real_complete = self.ledger.complete_ephemeral_volume_cleanup

        def commit_then_lose(**kwargs):
            real_complete(**kwargs)
            raise RuntimeError("lost cleanup response")

        with patch.object(
            self.ledger,
            "complete_ephemeral_volume_cleanup",
            side_effect=commit_then_lose,
        ):
            with self.assertRaisesRegex(RuntimeError, "lost cleanup response"):
                self.service.reconcile_volume(
                    operation_id="volume-test/response-loss/first",
                    volume_id=record.volume_id,
                )
        self.assertFalse(wrapper.exists())
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.CLEANED,
        )
        replay = self.service.reconcile_volume(
            operation_id="volume-test/response-loss/replay",
            volume_id=record.volume_id,
        )
        self.assertTrue(replay.already_complete)

    def test_expired_cleanup_term_is_reclaimed_with_a_higher_fence(self) -> None:
        volume = self.create("reclaim")
        record = volume.record
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/reclaim/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        volume._namespace.close()
        volume._closed = True
        token = "a" * 64
        first = self.ledger.claim_ephemeral_volume_cleanup(
            operation_id=(
                f"ephemeral-volume.cleanup.claim/"
                f"{__import__('optpilot.realm.ephemeral_volume_service', fromlist=['_cleanup_key'])._cleanup_key(record.volume_id)}"
            ),
            actor_principal_id=self.service.maintenance_principal_id,
            volume_id=record.volume_id,
            cleaner_holder_id="expired-cleaner",
            cleaner_ttl_seconds=0.01,
            cleanup_token=token,
        )
        time.sleep(0.03)
        # The service's deterministic token is part of the cleanup claim, so
        # use a direct reclaim here to exercise the ledger fence independently.
        reclaimed = self.ledger.reclaim_ephemeral_volume_cleanup(
            operation_id="volume-test/reclaim/term-2",
            actor_principal_id=self.service.maintenance_principal_id,
            volume_id=record.volume_id,
            expected_cleanup_generation=1,
            cleaner_holder_id="cleaner-2",
            cleaner_ttl_seconds=60,
            cleanup_token=token,
        )
        self.assertEqual(reclaimed.volume.cleanup_generation, 2)
        self.assertGreater(
            reclaimed.cleanup_lease.fencing_token,
            first.cleanup_lease.fencing_token,
        )
        with self.assertRaises(RealmError):
            self.ledger.complete_ephemeral_volume_cleanup(
                operation_id="volume-test/reclaim/stale-complete",
                actor_principal_id=self.service.maintenance_principal_id,
                volume_id=record.volume_id,
                cleaner_holder_id=first.cleanup_lease.holder_id,
                cleaner_fencing_token=first.cleanup_lease.fencing_token,
                cleanup_token=token,
            )

    def test_replaced_data_path_fails_closed_and_is_quarantined(self) -> None:
        volume = self.create("replace")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        data = wrapper / "data"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe", encoding="utf-8")
        volume._namespace.close()
        volume._closed = True
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/replace/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        os.rmdir(data)
        data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RealmIntegrityError):
            self.service.reconcile_volume(
                operation_id="volume-test/replace/reconcile",
                volume_id=record.volume_id,
            )
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )
        self.assertEqual((outside / "keep").read_text(encoding="utf-8"), "safe")

    def test_heartbeat_checks_namespace_before_mutating_lease_and_quarantines(
        self,
    ) -> None:
        volume = self.create("heartbeat-identity")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        data = wrapper / "data"
        outside = self.root / "heartbeat-outside"
        outside.mkdir()
        revision = volume.lease.heartbeat_revision
        os.rmdir(data)
        data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RealmIntegrityError):
            volume.heartbeat(
                operation_id="volume-test/heartbeat/unsafe", ttl_seconds=120
            )
        connection = self.ledger._connect()
        try:
            row = connection.execute(
                "SELECT heartbeat_revision FROM leases WHERE lease_id = ?",
                (volume.lease.lease_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(int(row["heartbeat_revision"]), revision)
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )

    def test_validate_quarantines_unsafe_active_namespace(self) -> None:
        volume = self.create("validate-identity")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        data = wrapper / "data"
        outside = self.root / "validate-outside"
        outside.mkdir()
        os.rmdir(data)
        data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RealmIntegrityError):
            volume.validate()
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )

    def test_wrapper_renamed_before_cleanup_is_quarantined_not_cleaned(self) -> None:
        volume = self.create("rename-wrapper")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        stolen = self.service.root_binding.path / "stolen-wrapper"
        (volume.path / "payload").write_text("retained", encoding="utf-8")
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/rename-wrapper/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        volume._namespace.close()
        volume._closed = True
        wrapper.rename(stolen)
        with self.assertRaises(RealmIntegrityError):
            self.service.reconcile_volume(
                operation_id="volume-test/rename-wrapper/reconcile",
                volume_id=record.volume_id,
            )
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )
        self.assertEqual(
            (stolen / "data" / "payload").read_text(encoding="utf-8"), "retained"
        )

    def test_data_renamed_out_before_cleanup_is_quarantined_not_cleaned(self) -> None:
        volume = self.create("rename-data")
        record = volume.record
        wrapper = self.service.root_binding.path / record.relative_name
        stolen = self.service.root_binding.path / "stolen-data"
        (volume.path / "payload").write_text("retained", encoding="utf-8")
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/rename-data/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        volume._namespace.close()
        volume._closed = True
        (wrapper / "data").rename(stolen)
        with self.assertRaises(RealmIntegrityError):
            self.service.reconcile_volume(
                operation_id="volume-test/rename-data/reconcile",
                volume_id=record.volume_id,
            )
        self.assertEqual(
            self.maintenance_record(record.volume_id).state,
            EphemeralVolumeState.QUARANTINED,
        )
        self.assertEqual((stolen / "payload").read_text(encoding="utf-8"), "retained")

    def test_cleanup_replays_crashes_after_retirement_and_tombstone_publication(
        self,
    ) -> None:
        binding = self.service.root_binding

        class SimulatedCrash(BaseException):
            pass

        for label, crash_format in (
            ("after-retirement", volume_namespace._RETIREMENT_PROOF_SCHEMA),
            ("after-tombstone", volume_namespace._CLEANUP_TOMBSTONE_SCHEMA),
        ):
            claim, identity = create_ephemeral_volume_namespace(
                binding,
                directory_name=f"volume-{label}",
                volume_id=label,
                claim_nonce=("5" if label == "after-retirement" else "6") * 64,
            )
            public = binding.path / identity.directory_name
            (public / "data" / "payload").write_text("discard", encoding="utf-8")
            token = ("7" if label == "after-retirement" else "8") * 64
            real_publish = volume_namespace._publish_or_validate_cleanup_marker
            crashed = False

            def crash_at_phase(*args, **kwargs):
                nonlocal crashed
                if kwargs["expected_format"] == crash_format and not crashed:
                    crashed = True
                    if crash_format == volume_namespace._CLEANUP_TOMBSTONE_SCHEMA:
                        real_publish(*args, **kwargs)
                    raise SimulatedCrash()
                return real_publish(*args, **kwargs)

            with patch.object(
                volume_namespace,
                "_publish_or_validate_cleanup_marker",
                side_effect=crash_at_phase,
            ):
                with self.assertRaises(SimulatedCrash):
                    cleanup_ephemeral_volume_namespace(
                        binding, claim, identity, cleanup_token=token
                    )
            self.assertFalse(public.exists())
            self.assertTrue(
                cleanup_ephemeral_volume_namespace(
                    binding, claim, identity, cleanup_token=token
                )
            )
            complete_ephemeral_volume_cleanup_namespace(
                binding, claim, cleanup_token=token
            )

    def test_maintenance_principal_has_lifecycle_but_not_owner_read_authority(
        self,
    ) -> None:
        volume = self.create("least-privilege")
        with self.assertRaises(RealmNotFound):
            self.ledger.read_ephemeral_volume(
                actor_principal_id=self.service.maintenance_principal_id,
                volume_id=volume.record.volume_id,
            )
        listed = self.ledger.list_ephemeral_volumes(
            actor_principal_id=self.service.maintenance_principal_id,
            volume_root_id=self.service.root_binding.volume_root_id,
            states=(EphemeralVolumeState.ACTIVE,),
        )
        self.assertEqual(
            tuple(item.volume_id for item in listed), (volume.record.volume_id,)
        )
        volume.close()

    def test_disabled_root_can_restart_and_drain_existing_cleanup_debt(self) -> None:
        volume = self.create("disabled-root")
        record = volume.record
        self.ledger.release_ephemeral_volume(
            operation_id="volume-test/disabled-root/release",
            actor_principal_id="operator",
            volume_id=record.volume_id,
            holder_id=volume.lease.holder_id,
            fencing_token=volume.lease.fencing_token,
        )
        volume._namespace.close()
        volume._closed = True
        self.ledger.set_ephemeral_volume_root_state(
            operation_id="volume-test/disabled-root/disable",
            actor_principal_id=self.service.maintenance_principal_id,
            volume_root_id=self.service.root_binding.volume_root_id,
            state="disabled",
        )
        restarted = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.service.root_binding.path
        )
        with self.assertRaisesRegex(RealmConflict, "root is unavailable"):
            restarted.create(
                operation_id="volume-test/disabled-root/new",
                actor_principal_id="operator",
                parent_lease=self.parent,
                holder_id="new-writer",
                quota=TEST_VOLUME_QUOTA,
            )
        receipt = restarted.reconcile_volume(
            operation_id="volume-test/disabled-root/drain",
            volume_id=record.volume_id,
        )
        self.assertEqual(receipt.volume.state, EphemeralVolumeState.CLEANED)

    def test_reconcile_operation_is_bound_to_one_volume(self) -> None:
        first = self.create("coordinate-a")
        second = self.create("coordinate-b")
        for index, volume in enumerate((first, second), start=1):
            self.ledger.release_ephemeral_volume(
                operation_id=f"volume-test/coordinate/release/{index}",
                actor_principal_id="operator",
                volume_id=volume.record.volume_id,
                holder_id=volume.lease.holder_id,
                fencing_token=volume.lease.fencing_token,
            )
            volume._namespace.close()
            volume._closed = True
        operation = "volume-test/coordinate/reconcile"
        self.service.reconcile_volume(
            operation_id=operation, volume_id=first.record.volume_id
        )
        with self.assertRaises(RealmConflict):
            self.service.reconcile_volume(
                operation_id=operation, volume_id=second.record.volume_id
            )

    def test_reserved_volume_lease_kinds_require_typed_transactions(self) -> None:
        for lease_kind in ("ephemeral-volume", "ephemeral-volume-cleaner"):
            with self.assertRaisesRegex(RealmConflict, "Reserved lease"):
                self.ledger.acquire_lease(
                    operation_id=f"volume-test/reserved/{lease_kind}",
                    actor_principal_id="operator",
                    owner_id="runtime-owner",
                    lease_kind=lease_kind,
                    audience="runtime",
                    holder_id="invalid",
                    scope_key=f"invalid:{lease_kind}",
                    ttl_seconds=60,
                )

    def test_sql_guards_fence_quota_reserved_lease_and_cleanup_tampering(self) -> None:
        first = self.create("sql-guard-a")
        first_id = first.record.volume_id
        usage_id = first.lease.lease_id

        def rejected(sql: str, parameters: tuple[object, ...]) -> None:
            connection = self.ledger._connect()
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql, parameters)
                    connection.commit()
            finally:
                connection.rollback()
                connection.close()

        rejected(
            "UPDATE ephemeral_volumes SET quota_json = ? WHERE volume_id = ?",
            (
                '{"max_entries":2,"max_file_bytes":2,"max_total_bytes":2}',
                first_id,
            ),
        )
        rejected(
            "UPDATE leases SET holder_id = ? WHERE lease_id = ?",
            ("forged-holder", usage_id),
        )
        rejected("DELETE FROM leases WHERE lease_id = ?", (usage_id,))
        rejected(
            "UPDATE leases SET state = 'released', updated_at = updated_at + 1 "
            "WHERE lease_id = ?",
            (usage_id,),
        )
        rejected(
            "INSERT INTO leases(lease_id, owner_id, parent_lease_id, lease_kind, "
            "audience, holder_id, scope_key, fencing_token, heartbeat_revision, "
            "state, expires_at, created_at, updated_at, metadata_json) "
            "SELECT ?, owner_id, NULL, 'ephemeral-volume', audience, ?, ?, "
            "fencing_token + 100, 0, 'active', expires_at, created_at, updated_at, "
            "metadata_json FROM leases WHERE lease_id = ?",
            ("forged-volume-lease", "forged", "invalid:scope", usage_id),
        )

        second = self.create("sql-guard-b")
        claims = []
        for index, volume in enumerate((first, second), start=1):
            self.ledger.release_ephemeral_volume(
                operation_id=f"volume-test/sql-guard/release/{index}",
                actor_principal_id="operator",
                volume_id=volume.record.volume_id,
                holder_id=volume.lease.holder_id,
                fencing_token=volume.lease.fencing_token,
            )
            volume._namespace.close()
            volume._closed = True
            claims.append(
                self.ledger.claim_ephemeral_volume_cleanup(
                    operation_id=f"volume-test/sql-guard/claim/{index}",
                    actor_principal_id=self.service.maintenance_principal_id,
                    volume_id=volume.record.volume_id,
                    cleaner_holder_id=f"sql-cleaner-{index}",
                    cleaner_ttl_seconds=60,
                    cleanup_token=str(index) * 64,
                )
            )
        connection = self.ledger._connect()
        try:
            latest_txn = int(
                connection.execute(
                    "SELECT max(txn_id) AS txn_id FROM ledger_transactions"
                ).fetchone()["txn_id"]
            )
        finally:
            connection.close()
        rejected(
            "UPDATE ephemeral_volumes SET cleanup_lease_id = ?, "
            "cleanup_generation = cleanup_generation + 1, updated_txn_id = ? "
            "WHERE volume_id = ?",
            (claims[1].cleanup_lease.lease_id, latest_txn, first_id),
        )

        def forge_complete(connection, txn_id, now):
            connection.execute(
                "UPDATE ephemeral_volumes SET state = 'cleaned', cleaned_at = ?, "
                "updated_txn_id = ?, updated_at = ? WHERE volume_id = ?",
                (now, txn_id, now, first_id),
            )
            return {}

        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._operate(
                operation_id="volume-test/sql-guard/forged-complete",
                operation_kind="ephemeral-volume.cleanup.complete",
                request={"volume_id": first_id},
                body=forge_complete,
            )

    def test_writable_root_cannot_also_be_registered_as_read_only_projection_root(
        self,
    ) -> None:
        binding = self.service.root_binding
        nonce = "f" * 64
        marker = self.ledger.projection_root_marker_digest(
            projection_root_id="projection-on-volume-root",
            backend_kind="verified-copy-v1",
            claim_nonce=nonce,
        )
        with self.assertRaisesRegex(RealmConflict, "must be separate"):
            self.ledger.register_projection_root(
                operation_id="volume-test/root-separation",
                actor_principal_id="operator",
                projection_root_id="projection-on-volume-root",
                backend_kind="verified-copy-v1",
                canonical_path=str(binding.path),
                marker_digest=marker,
                claim_nonce=nonce,
                device_id=binding.device_id,
                inode=binding.inode,
            )

    def test_namespace_allocation_recovers_both_preclaim_and_predata_crashes(
        self,
    ) -> None:
        binding = self.service.root_binding

        class SimulatedCrash(BaseException):
            pass

        real_write = volume_namespace._write_file_exclusive

        def crash_before_claim(directory_fd, name, payload, *, mode):
            if name == "claim.json":
                raise SimulatedCrash()
            return real_write(directory_fd, name, payload, mode=mode)

        with patch.object(
            volume_namespace,
            "_write_file_exclusive",
            side_effect=crash_before_claim,
        ):
            with self.assertRaises(SimulatedCrash):
                create_ephemeral_volume_namespace(
                    binding,
                    directory_name="volume-preclaim",
                    volume_id="preclaim",
                    claim_nonce="1" * 64,
                )
        claim, identity = create_ephemeral_volume_namespace(
            binding,
            directory_name="volume-preclaim",
            volume_id="preclaim",
            claim_nonce="1" * 64,
        )
        self.assertTrue((binding.path / "volume-preclaim" / "data").is_dir())
        cleanup_ephemeral_volume_namespace(
            binding, claim, identity, cleanup_token="2" * 64
        )
        complete_ephemeral_volume_cleanup_namespace(
            binding, claim, cleanup_token="2" * 64
        )

        real_mkdir = volume_namespace.os.mkdir

        def crash_before_data(name, *args, **kwargs):
            if name == "data":
                raise SimulatedCrash()
            return real_mkdir(name, *args, **kwargs)

        with patch.object(volume_namespace.os, "mkdir", side_effect=crash_before_data):
            with self.assertRaises(SimulatedCrash):
                create_ephemeral_volume_namespace(
                    binding,
                    directory_name="volume-predata",
                    volume_id="predata",
                    claim_nonce="3" * 64,
                )
        claim, identity = create_ephemeral_volume_namespace(
            binding,
            directory_name="volume-predata",
            volume_id="predata",
            claim_nonce="3" * 64,
        )
        self.assertTrue((binding.path / "volume-predata" / "data").is_dir())
        cleanup_ephemeral_volume_namespace(
            binding, claim, identity, cleanup_token="4" * 64
        )
        complete_ephemeral_volume_cleanup_namespace(
            binding, claim, cleanup_token="4" * 64
        )

    def test_root_marker_hardlink_crash_is_repaired_on_restart(self) -> None:
        binding = self.service.root_binding
        marker = binding.path / ".optpilot-ephemeral-volume-root"
        temporary = binding.path / ".ephemeral-volume-root-crash.tmp"
        os.link(marker, temporary)
        self.assertEqual(marker.stat().st_nlink, 2)
        reopened = prepare_ephemeral_volume_root(
            binding.path, realm_id=self.ledger.realm_id
        )
        self.assertEqual(reopened.volume_root_id, binding.volume_root_id)
        self.assertFalse(temporary.exists())
        self.assertEqual(marker.stat().st_nlink, 1)


class EphemeralVolumeMigrationTest(unittest.TestCase):
    def test_v11_database_migrates_through_v12_to_current_v36(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "realm.sqlite3"
            migration_directory = (
                Path(__file__).resolve().parents[2]
                / "src"
                / "optpilot"
                / "realm"
                / "migrations"
            )
            connection = sqlite3.connect(database)
            try:
                for version in range(1, 12):
                    path = next(migration_directory.glob(f"{version:04d}_*.sql"))
                    payload = path.read_bytes()
                    connection.executescript(payload.decode("utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations("
                        "version, migration_digest, applied_at) VALUES (?, ?, ?)",
                        (version, hashlib.sha256(payload).hexdigest(), float(version)),
                    )
                    if version == 1:
                        connection.executemany(
                            "INSERT INTO realm_meta(key, value) VALUES (?, ?)",
                            (("realm_id", "migration-test"), ("schema_version", "1")),
                        )
                    else:
                        connection.execute(
                            "UPDATE realm_meta SET value = ? "
                            "WHERE key = 'schema_version'",
                            (str(version),),
                        )
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
            finally:
                connection.close()

            migrated = RealmLedger(database)
            try:
                connection = sqlite3.connect(database)
                try:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    activation_trigger = connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND name = 'ephemeral_volume_activation_anchor'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(version, 36)
                self.assertIn("ephemeral_volume_roots", tables)
                self.assertIn("ephemeral_volumes", tables)
                self.assertIn(
                    "NEW.wrapper_device_id = NEW.data_device_id",
                    activation_trigger,
                )
                self.assertNotIn("root.device_id", activation_trigger)
                self.assertIn("run_attempt_execution_bindings", tables)
                self.assertIn("run_attempt_execution_projections", tables)
                self.assertIn("run_attempt_execution_volumes", tables)
                self.assertIn("run_attempt_execution_launch_intents", tables)
                self.assertIn("run_attempt_execution_terminal_evidence", tables)
                self.assertIn("run_attempt_execution_cleanup_authorizations", tables)
                self.assertEqual(migrated.integrity_check()["foreign_key_errors"], [])
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
