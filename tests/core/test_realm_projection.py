from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import (
    ContentCorrupt,
    ContentRejected,
    RealmAuthorizationError,
    RealmConflict,
    RealmExpired,
    RealmIntegrityError,
    RealmNotFound,
    RealmStorageIdentityChanged,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.projection import (
    ProjectionQuota,
    ProjectionSpec,
    ProjectionTarget,
    TreeMapping,
    VerifiedCopyProjectionProvider,
    compile_tree_plan,
)
from optpilot.realm.projection_namespace import (
    cleanup_projection_namespace,
    prepare_projection_root,
)
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.projection_records import ProjectionRealizationState
from optpilot.realm.refs import BlobRef, SnapshotRef, request_digest


class _MemoryCapability:
    def __init__(
        self,
        *,
        owner_id: str,
        manifests: dict[SnapshotRef, TreeManifest],
        payloads: dict[BlobRef, bytes],
    ) -> None:
        self.owner_id = owner_id
        self.lease_id = "lease-memory"
        self.fencing_token = 7
        self.manifests = manifests
        self.payloads = payloads
        self.active = True
        self.revoke_when_opened = False
        self.assertions = 0

    def assert_current(self) -> None:
        self.assertions += 1
        if not self.active:
            raise RealmConflict("stale memory capability")

    def load_tree(self, snapshot_ref: SnapshotRef) -> TreeManifest:
        self.assert_current()
        try:
            return self.manifests[snapshot_ref]
        except KeyError as error:
            raise RealmAuthorizationError("Projection content is unavailable.") from error

    @contextmanager
    def open_blob(self, blob_ref: BlobRef):
        self.assert_current()
        try:
            payload = self.payloads[blob_ref]
        except KeyError as error:
            raise RealmAuthorizationError("Projection content is unavailable.") from error
        if self.revoke_when_opened:
            self.active = False
        yield io.BytesIO(payload)


def _tree(files: dict[str, tuple[bytes, bool]]) -> tuple[TreeManifest, dict[BlobRef, bytes]]:
    entries: list[TreeEntry] = []
    directories: set[str] = set()
    payloads: dict[BlobRef, bytes] = {}
    for path, (payload, executable) in files.items():
        components = path.split("/")
        for index in range(1, len(components)):
            directories.add("/".join(components[:index]))
        blob_ref = BlobRef.from_bytes(payload)
        payloads[blob_ref] = payload
        entries.append(
            TreeEntry.file(
                path,
                blob_ref=blob_ref,
                size=len(payload),
                executable=executable,
            )
        )
    entries.extend(TreeEntry.directory(path) for path in directories)
    manifest = TreeManifest.build(entries)
    return manifest, payloads


class TreePlanTest(unittest.TestCase):
    def test_subtree_plan_is_portable_deterministic_and_immutable(self) -> None:
        manifest, payloads = _tree(
            {
                "pkg/README.md": (b"read me", False),
                "pkg/bin/run": (b"#!/bin/sh\n", True),
                "outside.txt": (b"outside", False),
            }
        )
        source = _MemoryCapability(
            owner_id="workspace-a",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        spec = ProjectionSpec(
            owner_id="workspace-a",
            mappings=(TreeMapping(manifest.snapshot_ref, "component", "pkg"),),
        )

        first = compile_tree_plan(spec, source)
        second = compile_tree_plan(spec, source)

        self.assertEqual(first, second)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            tuple((entry.path, entry.kind) for entry in first.entries),
            (
                ("component", "directory"),
                ("component/README.md", "file"),
                ("component/bin", "directory"),
                ("component/bin/run", "file"),
            ),
        )
        serialized = json.dumps(spec.to_dict(), sort_keys=True)
        self.assertNotIn("allowed_root", serialized)
        self.assertNotIn("store", serialized)
        self.assertNotIn("/tmp", serialized)
        self.assertEqual(spec.mappings, tuple(spec.mappings))
        with self.assertRaisesRegex(Exception, "cannot assign"):
            spec.owner_id = "changed"  # type: ignore[misc]

    def test_identical_overlap_is_allowed_but_different_content_is_rejected(self) -> None:
        first, first_payloads = _tree({"same.txt": (b"same", False)})
        second, second_payloads = _tree({"same.txt": (b"different", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={first.snapshot_ref: first, second.snapshot_ref: second},
            payloads={**first_payloads, **second_payloads},
        )
        duplicate = ProjectionSpec(
            "owner",
            (TreeMapping(first.snapshot_ref), TreeMapping(first.snapshot_ref)),
        )
        plan = compile_tree_plan(duplicate, source)
        self.assertEqual(len(plan.entries), 1)

        conflict = ProjectionSpec(
            "owner",
            (TreeMapping(first.snapshot_ref), TreeMapping(second.snapshot_ref)),
        )
        with self.assertRaisesRegex(ContentRejected, "conflict"):
            compile_tree_plan(conflict, source)

    def test_casefold_and_file_ancestor_collisions_are_rejected(self) -> None:
        upper, upper_payloads = _tree({"Name.txt": (b"one", False)})
        lower, lower_payloads = _tree({"name.txt": (b"two", False)})
        nested, nested_payloads = _tree({"child.txt": (b"child", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={
                upper.snapshot_ref: upper,
                lower.snapshot_ref: lower,
                nested.snapshot_ref: nested,
            },
            payloads={**upper_payloads, **lower_payloads, **nested_payloads},
        )
        with self.assertRaisesRegex(ContentRejected, "case-insensitive"):
            compile_tree_plan(
                ProjectionSpec(
                    "owner",
                    (TreeMapping(upper.snapshot_ref), TreeMapping(lower.snapshot_ref)),
                ),
                source,
            )
        with self.assertRaisesRegex(ContentRejected, "conflict"):
            compile_tree_plan(
                ProjectionSpec(
                    "owner",
                    (
                        TreeMapping(upper.snapshot_ref, "node"),
                        TreeMapping(nested.snapshot_ref, "node/Name.txt"),
                    ),
                ),
                source,
            )

    def test_missing_or_file_subtree_and_quota_are_rejected(self) -> None:
        manifest, payloads = _tree({"dir/file.bin": (b"12345", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        for subpath, phrase in (("missing", "does not exist"), ("dir/file.bin", "not a directory")):
            with self.subTest(subpath=subpath):
                with self.assertRaisesRegex(ContentRejected, phrase):
                    compile_tree_plan(
                        ProjectionSpec(
                            "owner",
                            (TreeMapping(manifest.snapshot_ref, source_subpath=subpath),),
                        ),
                        source,
                    )
        with self.assertRaisesRegex(ContentRejected, "quota"):
            compile_tree_plan(
                ProjectionSpec(
                    "owner",
                    (TreeMapping(manifest.snapshot_ref),),
                    ProjectionQuota(max_entries=10, max_total_bytes=4, max_file_bytes=10),
                ),
                source,
            )

    def test_owner_and_snapshot_authorization_fail_closed(self) -> None:
        manifest, payloads = _tree({"file": (b"payload", False)})
        source = _MemoryCapability(
            owner_id="authorized-owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        with self.assertRaises(RealmAuthorizationError):
            compile_tree_plan(
                ProjectionSpec("another-owner", (TreeMapping(manifest.snapshot_ref),)),
                source,
            )
        absent = SnapshotRef.from_manifest_bytes(b"not authorized")
        with self.assertRaises(RealmAuthorizationError):
            compile_tree_plan(
                ProjectionSpec("authorized-owner", (TreeMapping(absent),)),
                source,
            )

        wrong_manifest, wrong_payloads = _tree({"wrong": (b"wrong", False)})
        confused = _MemoryCapability(
            owner_id="authorized-owner",
            manifests={manifest.snapshot_ref: wrong_manifest},
            payloads=wrong_payloads,
        )
        with self.assertRaisesRegex(ContentCorrupt, "wrong manifest"):
            compile_tree_plan(
                ProjectionSpec(
                    "authorized-owner", (TreeMapping(manifest.snapshot_ref),)
                ),
                confused,
            )


@unittest.skipIf(os.name == "nt", "The secure v1 local provider is POSIX-only.")
class VerifiedCopyProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.targets = self.root / "projections"
        self.targets.mkdir(mode=0o700)
        self.provider = VerifiedCopyProjectionProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_target_requires_an_absolute_trusted_root_and_one_fresh_component(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            ProjectionTarget(Path("relative"), "checkout")
        for name in ("../escape", "nested/checkout", "/absolute"):
            with self.subTest(name=name):
                with self.assertRaises(ContentRejected):
                    ProjectionTarget(self.targets, name)

    def test_copy_verifies_bytes_sets_protected_modes_and_cleans_explicitly(self) -> None:
        manifest, payloads = _tree(
            {"docs/readme.txt": (b"hello", False), "run.sh": (b"#!/bin/sh\n", True)}
        )
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        spec = ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),))
        lease = self.provider.project(
            spec=spec,
            source=source,
            target=ProjectionTarget(self.targets, "checkout"),
        )
        checkout = lease.root_path
        self.assertEqual((checkout / "docs" / "readme.txt").read_bytes(), b"hello")
        self.assertEqual((checkout / "run.sh").read_bytes(), b"#!/bin/sh\n")
        self.assertEqual(stat.S_IMODE((checkout / "docs" / "readme.txt").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((checkout / "run.sh").stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE(checkout.stat().st_mode), 0o500)
        self.assertEqual(lease.copied_payload_bytes, len(b"hello#!/bin/sh\n"))
        portable = json.dumps(lease.portable_record(), sort_keys=True)
        self.assertNotIn(str(self.root), portable)
        self.assertNotIn("checkout", portable)

        lease.cleanup()
        lease.cleanup()
        self.assertFalse(checkout.exists())
        with self.assertRaises(RealmExpired):
            lease.validate()

    def test_existing_or_symlink_destination_is_never_reused(self) -> None:
        manifest, payloads = _tree({"file": (b"safe", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        spec = ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),))
        existing = self.targets / "existing"
        existing.mkdir()
        marker = existing / "marker"
        marker.write_text("untouched", encoding="utf-8")
        with self.assertRaises(RealmConflict):
            self.provider.project(
                spec=spec,
                source=source,
                target=ProjectionTarget(self.targets, "existing"),
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")

        external = self.root / "external"
        external.mkdir()
        external_marker = external / "marker"
        external_marker.write_text("outside", encoding="utf-8")
        os.symlink(external, self.targets / "linked")
        with self.assertRaises(RealmConflict):
            self.provider.project(
                spec=spec,
                source=source,
                target=ProjectionTarget(self.targets, "linked"),
            )
        self.assertEqual(external_marker.read_text(encoding="utf-8"), "outside")

    def test_symlink_target_root_is_rejected(self) -> None:
        manifest, payloads = _tree({"file": (b"safe", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        outside = self.root / "outside"
        outside.mkdir()
        linked_root = self.root / "linked-root"
        os.symlink(outside, linked_root)
        with self.assertRaises(RealmIntegrityError):
            self.provider.project(
                spec=ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),)),
                source=source,
                target=ProjectionTarget(linked_root, "checkout"),
            )
        self.assertEqual(tuple(outside.iterdir()), ())

    def test_corrupt_source_or_revoked_fence_rolls_back_fresh_checkout(self) -> None:
        expected = b"expected"
        blob_ref = BlobRef.from_bytes(expected)
        manifest = TreeManifest.build(
            (TreeEntry.file("file", blob_ref=blob_ref, size=len(expected), executable=False),)
        )
        corrupt = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads={blob_ref: b"tampered"},
        )
        spec = ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),))
        with self.assertRaises(ContentCorrupt):
            self.provider.project(
                spec=spec,
                source=corrupt,
                target=ProjectionTarget(self.targets, "corrupt"),
            )
        self.assertFalse((self.targets / "corrupt").exists())

        current = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads={blob_ref: expected},
        )
        current.revoke_when_opened = True
        with self.assertRaisesRegex(RealmConflict, "stale"):
            self.provider.project(
                spec=spec,
                source=current,
                target=ProjectionTarget(self.targets, "revoked"),
            )
        self.assertFalse((self.targets / "revoked").exists())

    def test_cleanup_refuses_replaced_path_without_touching_external_tree(self) -> None:
        manifest, payloads = _tree({"file": (b"safe", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        lease = self.provider.project(
            spec=ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),)),
            source=source,
            target=ProjectionTarget(self.targets, "checkout"),
        )
        checkout = self.targets / "checkout"
        moved = self.targets / "moved"
        checkout.rename(moved)
        external = self.root / "external-cleanup"
        external.mkdir()
        marker = external / "marker"
        marker.write_text("outside", encoding="utf-8")
        os.symlink(external, checkout)

        with self.assertRaises(RealmIntegrityError):
            lease.cleanup()
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside")
        checkout.unlink()
        moved.rename(checkout)
        lease.cleanup()
        self.assertFalse(checkout.exists())

    def test_parent_path_replacement_never_redirects_the_published_root(self) -> None:
        manifest, payloads = _tree({"file": (b"safe", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        lease = self.provider.project(
            spec=ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),)),
            source=source,
            target=ProjectionTarget(self.targets, "checkout"),
        )
        moved_parent = self.root / "moved-projections"
        self.targets.rename(moved_parent)
        self.targets.mkdir(mode=0o700)
        replacement = self.targets / "checkout"
        replacement.mkdir()
        marker = replacement / "attacker"
        marker.write_text("outside", encoding="utf-8")

        with self.assertRaises(RealmIntegrityError):
            lease.validate()
        with self.assertRaises(RealmIntegrityError):
            _ = lease.root_path

        lease.cleanup()
        self.assertFalse((moved_parent / "checkout").exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside")

    def test_cleanup_retries_after_parent_fsync_fails_post_unlink(self) -> None:
        manifest, payloads = _tree({"file": (b"safe", False)})
        source = _MemoryCapability(
            owner_id="owner",
            manifests={manifest.snapshot_ref: manifest},
            payloads=payloads,
        )
        lease = self.provider.project(
            spec=ProjectionSpec("owner", (TreeMapping(manifest.snapshot_ref),)),
            source=source,
            target=ProjectionTarget(self.targets, "checkout"),
        )
        checkout = self.targets / "checkout"
        real_fsync = os.fsync
        failed = False

        def flaky_fsync(descriptor: int) -> None:
            nonlocal failed
            if not checkout.exists() and not failed:
                failed = True
                raise OSError("injected parent fsync failure")
            real_fsync(descriptor)

        with mock.patch("optpilot.realm.projection.os.fsync", side_effect=flaky_fsync):
            with self.assertRaisesRegex(OSError, "injected parent fsync failure"):
                lease.cleanup()

        self.assertFalse(checkout.exists())
        self.assertFalse(lease.cleaned)
        lease.cleanup()
        self.assertTrue(lease.cleaned)


@unittest.skipIf(os.name == "nt", "The secure v1 local provider is POSIX-only.")
class RealmProjectionServiceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        (self.source_root / "payload.txt").write_text("ledger projection", encoding="utf-8")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="principal", principal_id="operator", kind="human"
        )
        self.ledger.register_store(
            operation_id="store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="owner",
            owner_id="workspace-a",
            owner_kind="workspace",
            principal_id="operator",
        )
        change = self.ledger.begin_owner_change(
            operation_id="begin",
            actor_principal_id="operator",
            owner_id="workspace-a",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        authority = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        self.receipt = self.store.capture(
            change_id=change.change_id, authority=authority
        ).seal_tree(source=AllowedTreeSource(self.source_root))
        membership = OwnerMembership(
            self.store.store_id, self.receipt.snapshot_ref, "workspace-base"
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
        self.service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.projections = []

    def tearDown(self) -> None:
        for projection in reversed(self.projections):
            if not projection.closed:
                projection.close()
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _project(self, operation_id: str = "project"):
        projection = self.service.project_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                owner_id="workspace-a",
                mappings=(TreeMapping(self.receipt.snapshot_ref),),
            ),
            holder_id="worker-a",
            ttl_seconds=60,
        )
        self.projections.append(projection)
        return projection

    def test_registered_root_fact_mismatch_has_typed_operator_guidance(self) -> None:
        with (
            mock.patch.object(
                self.ledger,
                "validate_projection_root",
                side_effect=RealmNotFound("injected changed root facts"),
            ),
            mock.patch.object(
                self.ledger,
                "register_projection_root",
                side_effect=RealmConflict("injected existing root id"),
            ),
            self.assertRaises(RealmStorageIdentityChanged) as raised,
        ):
            self.service._ensure_registered_root()

        self.assertIn("No files were changed", str(raised.exception))
        self.assertIn("OPTPILOT_REALM_ROOT", str(raised.exception))

    def test_service_reopen_accepts_new_root_attachment_observation(self) -> None:
        root_path = self.root / "remounted-projections"
        binding = prepare_projection_root(
            root_path,
            realm_id=self.ledger.realm_id,
        )
        maintainer_digest = request_digest(
            {
                "format": "optpilot.projection-maintenance-principal.v1",
                "realm_id": self.ledger.realm_id,
                "projection_root_id": binding.projection_root_id,
            }
        )
        maintainer = f"projection-maintainer-{maintainer_digest[:40]}"
        self.ledger.register_principal(
            operation_id="projection-remount-maintainer",
            principal_id=maintainer,
            kind="service",
        )
        self.ledger.register_projection_root(
            operation_id="projection-remount-root",
            actor_principal_id=maintainer,
            projection_root_id=binding.projection_root_id,
            canonical_path=str(binding.path),
            backend_kind=VerifiedCopyProjectionProvider.PROVIDER_KIND,
            marker_digest=self.ledger.projection_root_marker_digest(
                projection_root_id=binding.projection_root_id,
                backend_kind=VerifiedCopyProjectionProvider.PROVIDER_KIND,
                claim_nonce=binding.claim_nonce,
            ),
            claim_nonce=binding.claim_nonce,
            device_id=binding.device_id + 1000,
            inode=binding.inode + 1000,
        )

        reopened = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=root_path,
        )
        projection = reopened.project_read_only(
            operation_id="projection-remount-project",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                owner_id="workspace-a",
                mappings=(TreeMapping(self.receipt.snapshot_ref),),
            ),
            holder_id="remounted-worker",
            ttl_seconds=60,
            sharing_policy="private",
        )
        self.assertEqual(
            (projection.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        projection._detach_without_release()
        recovered = reopened.recover_existing_private_read_only(
            operation_id="projection-remount-project",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                owner_id="workspace-a",
                mappings=(TreeMapping(self.receipt.snapshot_ref),),
            ),
            holder_id="remounted-worker",
            ttl_seconds=60,
        )
        self.projections.append(recovered)

        self.assertEqual(reopened.root_binding, binding)
        self.assertEqual(
            recovered.realization.realization_id,
            projection.realization.realization_id,
        )

    def test_ready_namespace_rebinds_descriptor_observations_from_claim(self) -> None:
        projection = self._project("projection-remount-ready")
        historical = replace(
            projection.realization,
            wrapper_device_id=projection.realization.wrapper_device_id + 1000,
            wrapper_inode=projection.realization.wrapper_inode + 1000,
            exposed_tree_device_id=(
                projection.realization.exposed_tree_device_id + 1000
            ),
            exposed_tree_inode=projection.realization.exposed_tree_inode + 1000,
        )

        observed = self.service._current_namespace_identity(historical)

        self.assertEqual(observed, projection._namespace.identity)
        with self.assertRaises((RealmConflict, RealmIntegrityError)):
            self.service._current_namespace_identity(
                replace(historical, claim_nonce="f" * 64)
            )

    def test_service_projects_exact_owned_cas_and_replays_in_process(self) -> None:
        projection = self._project()
        self.assertEqual(
            (projection.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        replay = self.service.project_read_only(
            operation_id="project",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                owner_id="workspace-a",
                mappings=(TreeMapping(self.receipt.snapshot_ref),),
            ),
            holder_id="worker-a",
            ttl_seconds=60,
        )
        self.assertIs(replay, projection)
        realizations = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.READY,),
        )
        self.assertEqual(len(realizations), 1)
        self.assertEqual(
            len(
                self.ledger.list_projection_consumers(
                    actor_principal_id="operator",
                    realization_id=projection.realization.realization_id,
                )
            ),
            1,
        )
        portable = json.dumps(projection.portable_record())
        self.assertNotIn(str(self.store.root), portable)
        self.assertNotIn(str(self.root / "projections"), portable)
        self.assertNotIn(projection.consumer_lease.lease_id, portable)
        self.assertNotIn("provider_kind", projection.portable_record())
        self.assertEqual(
            projection.realization.provider_kind, self.service._provider.PROVIDER_KIND
        )
        realized_root = (
            self.service.root_binding.path
            / projection.realization.relative_name
            / "root"
        )
        projection.close()
        self.assertTrue((realized_root / "payload.txt").is_file())
        self.store.verify_tree(self.receipt.snapshot_ref)

    def test_shared_policy_reuses_one_same_spec_realization(self) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch.object(
            self.service._provider,
            "project",
            wraps=self.service._provider.project,
        ) as materialize:
            first = self.service.project_read_only(
                operation_id="explicit-shared-a",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="shared-worker-a",
                ttl_seconds=60,
                sharing_policy="shared",
            )
            second = self.service.project_read_only(
                operation_id="explicit-shared-b",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="shared-worker-b",
                ttl_seconds=60,
                sharing_policy="shared",
            )

        self.assertEqual(
            first.realization.realization_id, second.realization.realization_id
        )
        self.assertNotEqual(first.consumer_id, second.consumer_id)
        self.assertEqual(materialize.call_count, 1)
        self.assertEqual(
            set(first.realization.availability_resolution),
            {
                "format",
                "store_id",
                "backend_kind",
                "root_marker",
                "snapshot_roots",
            },
        )
        self.projections.extend((first, second))

    def test_private_policy_copies_per_operation_without_changing_portable_semantics(
        self,
    ) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch.object(
            self.service._provider,
            "project",
            wraps=self.service._provider.project,
        ) as materialize:
            first = self.service.project_read_only(
                operation_id="private-copy-a",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="private-worker-a",
                ttl_seconds=60,
                sharing_policy="private",
            )
            second = self.service.project_read_only(
                operation_id="private-copy-b",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="private-worker-b",
                ttl_seconds=60,
                sharing_policy="private",
            )
            shared = self.service.project_read_only(
                operation_id="portable-shared-copy",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="portable-shared-worker",
                ttl_seconds=60,
                sharing_policy="shared",
            )

        self.assertNotEqual(
            first.realization.realization_id, second.realization.realization_id
        )
        self.assertNotEqual(
            first.realization.relative_name, second.realization.relative_name
        )
        self.assertNotEqual(
            first.realization.exposed_tree_inode,
            second.realization.exposed_tree_inode,
        )
        self.assertNotIn(
            shared.realization.realization_id,
            {
                first.realization.realization_id,
                second.realization.realization_id,
            },
        )
        self.assertEqual(materialize.call_count, 3)
        self.assertEqual(first.portable_record(), second.portable_record())
        self.assertEqual(first.portable_record(), shared.portable_record())

        first_resolution = first.realization.availability_resolution[
            "realization_sharing"
        ]
        second_resolution = second.realization.availability_resolution[
            "realization_sharing"
        ]
        self.assertEqual(first_resolution["policy"], "private")
        self.assertNotEqual(
            first_resolution["operation_coordinate_digest"],
            second_resolution["operation_coordinate_digest"],
        )
        for resolution in (first_resolution, second_resolution):
            digest = resolution["operation_coordinate_digest"]
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, digest.lower())
            int(digest, 16)
        operational = json.dumps(
            {
                "first": first.realization.to_dict()["availability_resolution"],
                "second": second.realization.to_dict()["availability_resolution"],
            },
            sort_keys=True,
        )
        portable = json.dumps(first.portable_record(), sort_keys=True)
        self.assertNotIn("private-copy-a", operational)
        self.assertNotIn("private-copy-b", operational)
        self.assertNotIn(str(self.root), operational)
        self.assertNotIn(str(self.store.root), operational)
        self.assertNotIn("realization_sharing", portable)
        self.assertNotIn(
            first_resolution["operation_coordinate_digest"], portable
        )
        self.assertNotIn(
            second_resolution["operation_coordinate_digest"], portable
        )
        self.projections.extend((first, second, shared))

    def test_exact_private_replay_after_restart_reattaches_same_consumer(self) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        first = self.service.project_read_only(
            operation_id="private-restart-replay",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id="private-restart-worker",
            ttl_seconds=60,
            sharing_policy="private",
        )
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        restarted._provider.project = mock.Mock(
            side_effect=AssertionError("exact private replay must not make another copy")
        )

        replay = restarted.project_read_only(
            operation_id="private-restart-replay",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id="private-restart-worker",
            ttl_seconds=60,
            sharing_policy="private",
        )

        self.assertEqual(
            replay.realization.realization_id, first.realization.realization_id
        )
        self.assertEqual(replay.consumer_id, first.consumer_id)
        self.assertEqual(restarted._provider.project.call_count, 0)
        self.assertEqual(replay.portable_record(), first.portable_record())
        replay.close()
        first.close()
        self.projections.extend((first, replay))

    def test_exact_private_reattach_is_current_actor_scoped(self) -> None:
        self.ledger.register_principal(
            operation_id="private-reattach-delegate-principal",
            principal_id="delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="private-reattach-delegate-grant",
            actor_principal_id="operator",
            owner_id="workspace-a",
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "private-actor-scoped-reattach"
        original = self.service.project_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="private-actor-scoped-holder",
            ttl_seconds=60,
            sharing_policy="private",
        )
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        arguments = {
            "expected_operation_id": operation_id,
            "realization_id": original.realization.realization_id,
            "consumer_id": original.consumer_id,
            "consumer_holder_id": original.consumer_lease.holder_id,
            "consumer_fencing_token": original.consumer_lease.fencing_token,
        }

        as_operator = restarted.reattach_private_read_only_consumer(
            actor_principal_id="operator", **arguments
        )
        as_delegate = restarted.reattach_private_read_only_consumer(
            actor_principal_id="delegate", **arguments
        )

        self.assertIsNot(as_operator, as_delegate)
        self.assertEqual(as_operator.root_path, as_delegate.root_path)
        self.assertEqual(
            (as_delegate.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        with self.assertRaisesRegex(RealmConflict, "another operation"):
            restarted.reattach_private_read_only_consumer(
                actor_principal_id="operator",
                **{**arguments, "expected_operation_id": "substituted-operation"},
            )
        as_operator.close()
        as_delegate.close()
        original.close()
        self.projections.extend((original, as_operator, as_delegate))

    def test_private_recovery_by_operation_crosses_actor_after_heartbeat(self) -> None:
        self.ledger.register_principal(
            operation_id="private-recovery-delegate-principal",
            principal_id="recovery-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="private-recovery-delegate-grant",
            actor_principal_id="operator",
            owner_id="workspace-a",
            principal_id="recovery-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "private-recovery-by-operation"
        holder_id = "private-recovery-holder"
        metadata = {
            "attempt_id": "attempt-recovery",
            "binding_id": "binding-recovery",
            "logical_name": "environment-inputs",
            "run_id": "run-recovery",
            "schema": "optpilot.run-attempt-projection-consumer.v1",
        }
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        original = self.service.project_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=60,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
            sharing_policy="private",
        )
        original.heartbeat(
            operation_id="private-recovery-by-operation/heartbeat",
            ttl_seconds=60,
        )
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        restarted._provider.project = mock.Mock(
            side_effect=AssertionError("recovery must not create a projection")
        )

        recovered = restarted.recover_existing_private_read_only(
            operation_id=operation_id,
            actor_principal_id="recovery-delegate",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=60,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
        )

        self.assertEqual(recovered.realization, original.realization)
        self.assertEqual(recovered.consumer_id, original.consumer_id)
        self.assertEqual(recovered.consumer_lease, original.consumer_lease)
        self.assertEqual(
            (recovered.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.assertEqual(restarted._provider.project.call_count, 0)
        recovered.close()
        original.close()
        self.projections.extend((original, recovered))

    def test_private_coordinate_only_crash_allows_replacement_actor_materialization(
        self,
    ) -> None:
        self.ledger.register_principal(
            operation_id="private-coordinate-only/delegate-principal",
            principal_id="coordinate-recovery-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="private-coordinate-only/delegate-grant",
            actor_principal_id="operator",
            owner_id="workspace-a",
            principal_id="coordinate-recovery-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "private-coordinate-only"
        holder_id = "private-coordinate-only-holder"
        metadata = {"schema": "private-coordinate-only.v1", "slot": "inputs"}
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        real_coordinate = self.ledger.coordinate_projection_consumer_request
        captured: dict[str, object] = {}

        def coordinate_then_crash(**arguments):
            captured.update(arguments)
            real_coordinate(**arguments)
            raise RuntimeError("crash after coordinate only")

        with mock.patch.object(
            self.ledger,
            "coordinate_projection_consumer_request",
            side_effect=coordinate_then_crash,
        ):
            with self.assertRaisesRegex(RuntimeError, "coordinate only"):
                self.service.project_read_only(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id=holder_id,
                    ttl_seconds=60,
                    consumer_kind="run-attempt",
                    consumer_metadata=metadata,
                    sharing_policy="private",
                )

        first = real_coordinate(**captured)
        self.assertEqual(real_coordinate(**captured), first)
        with self.assertRaises(RealmConflict):
            real_coordinate(**{**captured, "consumer_holder_id": "substituted"})

        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        recovered = restarted.project_read_only(
            operation_id=operation_id,
            actor_principal_id="coordinate-recovery-delegate",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=60,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
            sharing_policy="private",
        )
        self.assertEqual(
            (recovered.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.projections.append(recovered)

    def test_private_recovery_finishes_ready_zero_consumer_and_converges(self) -> None:
        self.ledger.register_principal(
            operation_id="private-zero-consumer/delegate-principal",
            principal_id="zero-consumer-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="private-zero-consumer/delegate-grant",
            actor_principal_id="operator",
            owner_id="workspace-a",
            principal_id="zero-consumer-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "private-zero-consumer-recovery"
        holder_id = "private-zero-consumer-holder"
        metadata = {"schema": "private-zero-consumer.v1", "slot": "inputs"}
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch.object(
            self.ledger,
            "acquire_projection_consumer",
            side_effect=RuntimeError("crash before consumer acquisition"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before consumer"):
                self.service.project_read_only(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id=holder_id,
                    ttl_seconds=60,
                    consumer_kind="run-attempt",
                    consumer_metadata=metadata,
                    sharing_policy="private",
                )
        ready = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.READY,),
        )
        self.assertEqual(len(ready), 1)
        self.assertEqual(
            self.ledger.list_projection_consumers(
                actor_principal_id="operator",
                realization_id=ready[0].realization_id,
            ),
            (),
        )
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        restarted._provider.project = mock.Mock(
            side_effect=AssertionError("ready recovery must not rematerialize")
        )
        real_acquire = self.ledger.acquire_projection_consumer

        def commit_then_report_race(**kwargs):
            real_acquire(**kwargs)
            raise RealmConflict("simulated concurrent recovery winner")

        with mock.patch.object(
            self.ledger,
            "acquire_projection_consumer",
            side_effect=commit_then_report_race,
        ):
            recovered = restarted.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="zero-consumer-delegate",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=60,
                consumer_kind="run-attempt",
                consumer_metadata=metadata,
            )

        self.assertEqual(recovered.realization.realization_id, ready[0].realization_id)
        self.assertEqual(
            (recovered.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.assertEqual(restarted._provider.project.call_count, 0)
        recovered.close()
        self.projections.append(recovered)

    def test_private_recovery_retires_expired_creating_realization(self) -> None:
        self.ledger.register_principal(
            operation_id="private-creating/delegate-principal",
            principal_id="creating-recovery-delegate",
            kind="agent",
        )
        self.ledger.grant_owner_permission(
            operation_id="private-creating/delegate-grant",
            actor_principal_id="operator",
            owner_id="workspace-a",
            principal_id="creating-recovery-delegate",
            permission=OwnerPermission.DERIVE,
        )
        operation_id = "private-creating-recovery"
        holder_id = "private-creating-recovery-holder"
        metadata = {"schema": "private-creating-recovery.v1"}
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch(
            "optpilot.realm.projection_service."
            "_ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS",
            0.12,
        ), mock.patch.object(
            self.ledger,
            "claim_projection_materialization",
            side_effect=RuntimeError("crash before builder claim"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before builder"):
                self.service.project_read_only(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id=holder_id,
                    ttl_seconds=0.12,
                    consumer_kind="run-attempt",
                    consumer_metadata=metadata,
                    sharing_policy="private",
                )
        creating = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.CREATING,),
        )
        self.assertEqual(len(creating), 1)
        old_id = creating[0].realization_id
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        with self.assertRaisesRegex(RealmConflict, "live provider term"):
            restarted.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="creating-recovery-delegate",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=0.12,
                consumer_kind="run-attempt",
                consumer_metadata=metadata,
            )
        time.sleep(0.18)

        recovered = restarted.recover_existing_private_read_only(
            operation_id=operation_id,
            actor_principal_id="creating-recovery-delegate",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=0.12,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
        )

        self.assertNotEqual(recovered.realization.realization_id, old_id)
        self.assertEqual(
            (recovered.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        records = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        self.assertEqual(
            {item.realization_id: item.state for item in records}[old_id],
            ProjectionRealizationState.CLEANED,
        )
        self.projections.append(recovered)

    def test_private_recovery_retires_expired_materializing_realization(self) -> None:
        operation_id = "private-materializing-recovery"
        holder_id = "private-materializing-recovery-holder"
        metadata = {"schema": "private-materializing-recovery.v1"}
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch(
            "optpilot.realm.projection_service."
            "_ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS",
            0.12,
        ), mock.patch.object(
            self.service,
            "_materialize",
            side_effect=RuntimeError("crash after builder claim"),
        ):
            with self.assertRaisesRegex(RuntimeError, "after builder"):
                self.service.project_read_only(
                    operation_id=operation_id,
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id=holder_id,
                    ttl_seconds=0.12,
                    consumer_kind="run-attempt",
                    consumer_metadata=metadata,
                    sharing_policy="private",
                )
        materializing = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.MATERIALIZING,),
        )
        self.assertEqual(len(materializing), 1)
        old_id = materializing[0].realization_id
        time.sleep(0.18)

        recovered = self.service.recover_existing_private_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=0.12,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
        )

        self.assertNotEqual(recovered.realization.realization_id, old_id)
        records = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        self.assertEqual(
            {item.realization_id: item.state for item in records}[old_id],
            ProjectionRealizationState.CLEANED,
        )
        self.projections.append(recovered)

    def test_private_recovery_rejects_wrong_coordinate_and_semantics(self) -> None:
        operation_id = "private-recovery-exact-semantics"
        holder_id = "private-recovery-exact-holder"
        metadata = {"schema": "private-recovery-test.v1", "slot": "inputs"}
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        original = self.service.project_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=60,
            consumer_kind="run-attempt",
            consumer_metadata=metadata,
            sharing_policy="private",
        )
        self.projections.append(original)

        with self.assertRaises(RealmNotFound):
            self.service.recover_existing_private_read_only(
                operation_id="private-recovery-substituted-operation",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=60,
                consumer_kind="run-attempt",
                consumer_metadata=metadata,
            )
        with self.assertRaisesRegex(RealmConflict, "consumer differs"):
            self.service.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=60,
                consumer_kind="run-attempt",
                consumer_metadata={**metadata, "slot": "substituted"},
            )
        with self.assertRaises(RealmConflict):
            self.service.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="substituted-holder",
                ttl_seconds=60,
                consumer_kind="run-attempt",
                consumer_metadata=metadata,
            )
        with self.assertRaisesRegex(RealmConflict, "different initial TTL"):
            self.service.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=30,
                consumer_kind="run-attempt",
                consumer_metadata=metadata,
            )

    def test_private_recovery_rejects_ambiguous_consumer_tampering(self) -> None:
        operation_id = "private-recovery-ambiguous-consumer"
        holder_id = "private-recovery-ambiguous-holder"
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        original = self.service.project_read_only(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id=holder_id,
            ttl_seconds=60,
            consumer_kind="run-attempt",
            consumer_metadata={"schema": "private-recovery-test.v1"},
            sharing_policy="private",
        )
        self.projections.append(original)
        injected = self.ledger.acquire_projection_consumer(
            operation_id="private-recovery/injected-consumer",
            actor_principal_id="operator",
            realization_id=original.realization.realization_id,
            consumer_holder_id="injected-holder",
            consumer_ttl_seconds=60,
            consumer_kind="injected",
            metadata={"schema": "injected.v1"},
        )

        with self.assertRaisesRegex(RealmConflict, "one exact consumer"):
            self.service.recover_existing_private_read_only(
                operation_id=operation_id,
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=holder_id,
                ttl_seconds=60,
                consumer_kind="run-attempt",
                consumer_metadata={"schema": "private-recovery-test.v1"},
            )

        self.ledger.release_projection_consumer(
            operation_id="private-recovery/release-injected-consumer",
            actor_principal_id="operator",
            realization_id=original.realization.realization_id,
            consumer_id=injected.consumer.consumer_id,
            consumer_holder_id=injected.consumer_lease.holder_id,
            consumer_fencing_token=injected.consumer_lease.fencing_token,
        )

    def test_private_reattach_rejects_shared_realization(self) -> None:
        shared = self.service.project_read_only(
            operation_id="shared-cannot-reattach-private",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="shared-cannot-reattach-holder",
            ttl_seconds=60,
            sharing_policy="shared",
        )

        with self.assertRaisesRegex(RealmConflict, "not operation-private"):
            self.service.reattach_private_read_only_consumer(
                actor_principal_id="operator",
                expected_operation_id="shared-cannot-reattach-private",
                realization_id=shared.realization.realization_id,
                consumer_id=shared.consumer_id,
                consumer_holder_id=shared.consumer_lease.holder_id,
                consumer_fencing_token=shared.consumer_lease.fencing_token,
            )
        self.projections.append(shared)

    def test_private_retirement_is_exact_and_retries_commit_response_loss(self) -> None:
        private = self.service.project_read_only(
            operation_id="private-retirement-response-loss",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="private-retirement-holder",
            ttl_seconds=60,
            sharing_policy="private",
        )
        wrapper = self.service.root_binding.path / private.realization.relative_name
        real_retire = self.ledger.retire_private_projection_consumer
        calls = 0

        def commit_then_lose(**arguments):
            nonlocal calls
            calls += 1
            result = real_retire(**arguments)
            if calls == 1:
                raise OSError("lost private retirement response")
            return result

        with mock.patch.object(
            self.ledger,
            "retire_private_projection_consumer",
            side_effect=commit_then_lose,
        ):
            with self.assertRaisesRegex(OSError, "lost private"):
                self.service.retire_private_projection(private)
            self.assertFalse(private.closed)
            self.assertEqual(
                self.ledger.read_projection_realization(
                    actor_principal_id="operator",
                    realization_id=private.realization.realization_id,
                ).state,
                ProjectionRealizationState.CLOSING,
            )
            cleaned = self.service.retire_private_projection(private)

        self.assertEqual(calls, 2)
        self.assertEqual(cleaned.state, ProjectionRealizationState.CLEANED)
        self.assertTrue(private.closed)
        self.assertFalse(wrapper.exists())
        self.projections.append(private)

    def test_private_retirement_retries_delayed_physical_cleanup(self) -> None:
        private = self.service.project_read_only(
            operation_id="private-retirement-delayed-cleanup",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="private-retirement-delayed-holder",
            ttl_seconds=60,
            sharing_policy="private",
        )
        wrapper = self.service.root_binding.path / private.realization.relative_name
        real_reconcile = self.service.reconcile_projection
        calls = 0

        def fail_once(**arguments):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected delayed physical cleanup")
            return real_reconcile(**arguments)

        with mock.patch.object(
            self.service, "reconcile_projection", side_effect=fail_once
        ):
            with self.assertRaisesRegex(RuntimeError, "delayed physical"):
                self.service.retire_private_projection(private)
            self.assertTrue(private.closed)
            cleaned = self.service.retire_private_projection(private)

        self.assertEqual(cleaned.state, ProjectionRealizationState.CLEANED)
        self.assertFalse(wrapper.exists())
        self.projections.append(private)

    def test_private_cleanup_removes_only_its_exact_realization(self) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        disposable = self.service.project_read_only(
            operation_id="private-cleanup-disposable",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id="private-cleanup-worker-a",
            ttl_seconds=0.08,
            sharing_policy="private",
        )
        survivor = self.service.project_read_only(
            operation_id="private-cleanup-survivor",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id="private-cleanup-worker-b",
            ttl_seconds=60,
            sharing_policy="private",
        )
        disposable_wrapper = (
            self.service.root_binding.path / disposable.realization.relative_name
        )
        survivor_wrapper = (
            self.service.root_binding.path / survivor.realization.relative_name
        )
        disposable.close()
        time.sleep(0.14)

        cleaned = self.service.reconcile_projection(
            operation_id="private-cleanup-exact-reconcile",
            realization_id=disposable.realization.realization_id,
            ttl_seconds=60,
        )

        self.assertEqual(cleaned.realization.state, ProjectionRealizationState.CLEANED)
        self.assertFalse(disposable_wrapper.exists())
        self.assertTrue((survivor_wrapper / "root" / "payload.txt").is_file())
        survivor.validate()
        self.projections.extend((disposable, survivor))

    def test_unsupported_sharing_policy_is_rejected_before_coordination(self) -> None:
        with mock.patch.object(
            self.ledger,
            "coordinate_projection_consumer_request",
            wraps=self.ledger.coordinate_projection_consumer_request,
        ) as coordinate:
            with self.assertRaisesRegex(ValueError, "sharing_policy"):
                self.service.project_read_only(
                    operation_id="unsupported-sharing-policy",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=ProjectionSpec(
                        "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                    ),
                    holder_id="unsupported-sharing-worker",
                    ttl_seconds=60,
                    sharing_policy="per-holder",
                )

        coordinate.assert_not_called()

    def test_valid_empty_lease_cannot_be_combined_with_another_owners_root(self) -> None:
        self.ledger.register_principal(
            operation_id="bob-principal", principal_id="bob", kind="human"
        )
        self.ledger.create_owner(
            operation_id="bob-owner",
            owner_id="workspace-b",
            owner_kind="workspace",
            principal_id="bob",
        )
        with self.assertRaises(RealmNotFound):
            self.service.project_read_only(
                operation_id="bob-forge",
                actor_principal_id="bob",
                store_id=self.store.store_id,
                spec=ProjectionSpec(
                    owner_id="workspace-b",
                    mappings=(TreeMapping(self.receipt.snapshot_ref),),
                ),
                holder_id="bob-worker",
                ttl_seconds=60,
            )

    def test_store_substitution_is_rejected_before_projection(self) -> None:
        replacement = LocalContentStore(
            self.root / "replacement-store", store_id=self.store.store_id
        )
        try:
            with self.assertRaises(RealmNotFound):
                RealmProjectionService(
                    self.ledger,
                    local_stores={replacement.store_id: replacement},
                    projection_root=self.root / "other-projections",
                )
        finally:
            replacement.close()

    def test_typed_consumer_release_invalidates_only_that_handle(self) -> None:
        projection = self._project("revoked-project")
        self.ledger.release_projection_consumer(
            operation_id="external-release",
            actor_principal_id="operator",
            realization_id=projection.realization.realization_id,
            consumer_id=projection.consumer_id,
            consumer_holder_id=projection.consumer_lease.holder_id,
            consumer_fencing_token=projection.consumer_lease.fencing_token,
        )
        with self.assertRaises(RealmConflict):
            projection.validate()
        projection.close()
        self.assertTrue(projection.closed)

    def test_closed_consumer_operation_cannot_be_reopened_by_replay(self) -> None:
        projection = self._project("closed-replay")
        projection.close()

        with self.assertRaisesRegex(RealmConflict, "unavailable consumer"):
            self.service.project_read_only(
                operation_id="closed-replay",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=ProjectionSpec(
                    "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                ),
                holder_id="worker-a",
                ttl_seconds=60,
            )

    def test_transient_consumer_release_keeps_close_retryable(self) -> None:
        projection = self._project("retryable-close")

        with mock.patch.object(
            self.ledger,
            "release_projection_consumer",
            side_effect=RuntimeError("transient release failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "transient release"):
                projection.close()

        self.assertFalse(projection.closed)
        projection.validate()
        projection.close()
        self.assertTrue(projection.closed)
        with self.assertRaises(RealmConflict):
            self.ledger.validate_projection_consumer(
                actor_principal_id="operator",
                realization_id=projection.realization.realization_id,
                consumer_id=projection.consumer_id,
                consumer_holder_id=projection.consumer_lease.holder_id,
                consumer_fencing_token=projection.consumer_lease.fencing_token,
            )

    def test_close_recovers_a_committed_release_after_response_loss(self) -> None:
        projection = self._project("close-response-loss")
        real_release = self.ledger.release_projection_consumer

        def commit_then_lose_response(**kwargs):
            real_release(**kwargs)
            raise RuntimeError("lost release response")

        with mock.patch.object(
            self.ledger,
            "release_projection_consumer",
            side_effect=commit_then_lose_response,
        ):
            with self.assertRaisesRegex(RuntimeError, "lost release response"):
                projection.close()

        self.assertFalse(projection.closed)
        projection.close()
        self.assertTrue(projection.closed)

    def test_validate_quarantines_a_renamed_projection_wrapper(self) -> None:
        projection = self._project("validate-renamed-wrapper")
        wrapper = self.service.root_binding.path / projection.realization.relative_name
        moved = self.service.root_binding.path / "validate-renamed-wrapper-stolen"
        wrapper.rename(moved)

        with self.assertRaisesRegex(RealmIntegrityError, "path identity changed"):
            projection.validate()

        quarantined = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.QUARANTINED,),
        )
        self.assertEqual(
            tuple(item.realization_id for item in quarantined),
            (projection.realization.realization_id,),
        )
        self.assertTrue((moved / "root" / "payload.txt").is_file())

    def test_heartbeat_checks_namespace_before_extending_and_quarantines(self) -> None:
        projection = self._project("heartbeat-renamed-wrapper")
        revision = projection.consumer_lease.heartbeat_revision
        wrapper = self.service.root_binding.path / projection.realization.relative_name
        moved = self.service.root_binding.path / "heartbeat-renamed-wrapper-stolen"
        wrapper.rename(moved)

        with self.assertRaisesRegex(RealmIntegrityError, "path identity changed"):
            projection.heartbeat(
                operation_id="heartbeat-renamed-wrapper/heartbeat",
                ttl_seconds=120,
            )

        self.assertEqual(projection.consumer_lease.heartbeat_revision, revision)
        quarantined = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.QUARANTINED,),
        )
        self.assertEqual(
            tuple(item.realization_id for item in quarantined),
            (projection.realization.realization_id,),
        )
        self.assertTrue((moved / "root" / "payload.txt").is_file())

    def test_heartbeat_rechecks_namespace_after_the_ledger_commit(self) -> None:
        projection = self._project("heartbeat-post-commit-race")
        revision = projection.consumer_lease.heartbeat_revision
        wrapper = self.service.root_binding.path / projection.realization.relative_name
        moved = self.service.root_binding.path / "heartbeat-post-commit-race-stolen"
        real_heartbeat = self.ledger.heartbeat_projection_consumer

        def commit_then_rename(**kwargs):
            receipt = real_heartbeat(**kwargs)
            wrapper.rename(moved)
            return receipt

        with mock.patch.object(
            self.ledger,
            "heartbeat_projection_consumer",
            side_effect=commit_then_rename,
        ):
            with self.assertRaisesRegex(RealmIntegrityError, "path identity changed"):
                projection.heartbeat(
                    operation_id="heartbeat-post-commit-race/heartbeat",
                    ttl_seconds=120,
                )

        self.assertGreater(projection.consumer_lease.heartbeat_revision, revision)
        quarantined = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.QUARANTINED,),
        )
        self.assertEqual(
            tuple(item.realization_id for item in quarantined),
            (projection.realization.realization_id,),
        )
        self.assertTrue((moved / "root" / "payload.txt").is_file())

    def test_concurrent_replay_is_single_flight_and_returns_one_handle(self) -> None:
        real_project = self.service._provider.project
        entered = threading.Event()
        release = threading.Event()
        provider_calls = 0
        provider_lock = threading.Lock()

        def delayed_project(**kwargs):
            nonlocal provider_calls
            with provider_lock:
                provider_calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return real_project(**kwargs)

        self.service._provider.project = delayed_project
        results = []
        errors = []

        def invoke() -> None:
            try:
                results.append(
                    self.service.project_read_only(
                        operation_id="concurrent-project",
                        actor_principal_id="operator",
                        store_id=self.store.store_id,
                        spec=ProjectionSpec(
                            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                        ),
                        holder_id="worker-a",
                        ttl_seconds=60,
                    )
                )
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(entered.wait(timeout=5))
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])
        self.assertEqual(provider_calls, 1)
        results[0].validate()
        self.projections.append(results[0])

    def test_cross_service_consumers_share_one_durable_realization(self) -> None:
        other = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        real_project = self.service._provider.project
        entered = threading.Event()
        release = threading.Event()

        def delayed_project(**kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return real_project(**kwargs)

        self.service._provider.project = delayed_project
        other._provider.project = mock.Mock(
            side_effect=AssertionError("the observing service must not rematerialize")
        )

        results = []
        errors = []

        def invoke(service, operation_id, holder_id, ttl_seconds) -> None:
            try:
                results.append(
                    service.project_read_only(
                        operation_id=operation_id,
                        actor_principal_id="operator",
                        store_id=self.store.store_id,
                        spec=ProjectionSpec(
                            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                        ),
                        holder_id=holder_id,
                        ttl_seconds=ttl_seconds,
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=invoke,
                args=(self.service, "cross-service-a", "worker-a", 60),
            ),
            threading.Thread(
                target=invoke,
                args=(other, "cross-service-b", "worker-b", 30),
            ),
        ]
        threads[0].start()
        self.assertTrue(entered.wait(timeout=5))
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        first, second = results
        self.assertEqual(
            first.realization.realization_id, second.realization.realization_id
        )
        self.assertNotEqual(first.consumer_id, second.consumer_id)
        self.assertEqual(other._provider.project.call_count, 0)
        first.close()
        second.validate()
        self.assertEqual(
            (second.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.projections.extend(results)

    def test_realization_heartbeats_and_exact_closure_is_authorized_once(self) -> None:
        real_project = self.service._provider.project
        real_claim = self.ledger.claim_projection_materialization
        real_publish = self.ledger.publish_projection_ready
        from optpilot.realm.projection_service import _BuilderHeartbeat
        from optpilot.realm.projection_namespace import (
            record_projection_tree_identity as real_record_tree_identity,
        )

        def slow_project(**kwargs):
            time.sleep(0.2)
            return real_project(**kwargs)

        def slow_claim(**kwargs):
            time.sleep(0.2)
            return real_claim(**kwargs)

        real_heartbeat_start = _BuilderHeartbeat.start

        def slow_heartbeat_start(heartbeat):
            time.sleep(0.2)
            return real_heartbeat_start(heartbeat)

        def slow_record_tree_identity(*args, **kwargs):
            time.sleep(0.2)
            return real_record_tree_identity(*args, **kwargs)

        def slow_publish(**kwargs):
            time.sleep(0.2)
            return real_publish(**kwargs)

        self.service._provider.project = slow_project
        with mock.patch.object(
            self.ledger,
            "validate_projection_lease",
            wraps=self.ledger.validate_projection_lease,
        ) as exact_validation, mock.patch.object(
            self.ledger,
            "claim_projection_materialization",
            side_effect=slow_claim,
        ), mock.patch.object(
            self.ledger,
            "heartbeat_projection_builder",
            wraps=self.ledger.heartbeat_projection_builder,
        ) as builder_heartbeat, mock.patch(
            "optpilot.realm.projection_service._BuilderHeartbeat.start",
            slow_heartbeat_start,
        ), mock.patch(
            "optpilot.realm.projection_service.record_projection_tree_identity",
            side_effect=slow_record_tree_identity,
        ), mock.patch.object(
            self.ledger,
            "publish_projection_ready",
            side_effect=slow_publish,
        ) as publish:
            projection = self.service.project_read_only(
                operation_id="heartbeat-project",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=ProjectionSpec(
                    "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                ),
                holder_id="worker-a",
                ttl_seconds=0.09,
            )
            projection.validate()
        self.assertEqual(exact_validation.call_count, 1)
        self.assertEqual(publish.call_count, 1)
        self.assertGreater(builder_heartbeat.call_count, 0)
        self.assertTrue(
            str(builder_heartbeat.call_args_list[0].kwargs["operation_id"]).endswith(
                "/initial"
            )
        )
        self.assertGreaterEqual(
            builder_heartbeat.call_args_list[0].kwargs["ttl_seconds"],
            5.0,
        )
        # The materialization grace is internal.  The delivered consumer still
        # receives the short lifetime requested by the caller.
        self.assertLess(
            projection.consumer_lease.expires_at
            - projection.consumer_lease.created_at,
            0.2,
        )
        renewed = projection.heartbeat(
            operation_id="heartbeat-consumer", ttl_seconds=60
        )
        self.assertGreater(renewed.heartbeat_revision, 0)
        self.projections.append(projection)

    def test_expired_ready_realization_is_reconciled_and_rematerialized(self) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        first = self.service.project_read_only(
            operation_id="short-lived-ready",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=spec,
            holder_id="short-lived-viewer",
            ttl_seconds=0.08,
        )
        old_id = first.realization.realization_id
        old_wrapper = (
            self.service.root_binding.path / first.realization.relative_name
        )
        first.close()
        time.sleep(0.14)

        with mock.patch.object(
            self.service._provider,
            "project",
            wraps=self.service._provider.project,
        ) as rematerialize:
            replacement = self.service.project_read_only(
                operation_id="recover-expired-ready",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="replacement-viewer",
                ttl_seconds=60,
            )

        self.assertNotEqual(replacement.realization.realization_id, old_id)
        self.assertEqual(rematerialize.call_count, 1)
        self.assertEqual(
            (replacement.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        records = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        by_id = {record.realization_id: record for record in records}
        self.assertEqual(by_id[old_id].state, ProjectionRealizationState.CLEANED)
        self.assertEqual(
            by_id[replacement.realization.realization_id].state,
            ProjectionRealizationState.READY,
        )
        self.assertFalse(old_wrapper.exists())
        self.projections.append(replacement)

    def test_shared_reader_takes_over_dead_builder_materializing_realization(
        self,
    ) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch(
            "optpilot.realm.projection_service."
            "_ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS",
            0.12,
        ), mock.patch.object(
            self.service,
            "_materialize",
            side_effect=RuntimeError("builder died mid-copy"),
        ):
            with self.assertRaisesRegex(RuntimeError, "builder died"):
                self.service.project_read_only(
                    operation_id="dead-builder-original",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id="dead-builder-holder",
                    ttl_seconds=0.12,
                )
        materializing = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.MATERIALIZING,),
        )
        self.assertEqual(len(materializing), 1)
        old_id = materializing[0].realization_id
        time.sleep(0.18)

        with mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_GRACE_SECONDS",
            0.02,
        ), mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_INTERVAL_SECONDS",
            0.02,
        ):
            replacement = self.service.project_read_only(
                operation_id="dead-builder-takeover",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="takeover-holder",
                ttl_seconds=60,
            )
        self.projections.append(replacement)

        self.assertNotEqual(replacement.realization.realization_id, old_id)
        self.assertEqual(
            (replacement.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        records = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        by_id = {record.realization_id: record for record in records}
        self.assertEqual(by_id[old_id].state, ProjectionRealizationState.CLEANED)
        self.assertEqual(
            by_id[replacement.realization.realization_id].state,
            ProjectionRealizationState.READY,
        )

    def test_reader_takeover_recovers_wrapperless_creating_realization(
        self,
    ) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        other = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
            coordination_timeout_seconds=0.3,
        )
        with mock.patch(
            "optpilot.realm.projection_service."
            "_ACTIVE_MATERIALIZATION_LEASE_MIN_SECONDS",
            0.12,
        ), mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_GRACE_SECONDS",
            0.03,
        ), mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_INTERVAL_SECONDS",
            0.03,
        ), mock.patch.object(
            self.ledger,
            "claim_projection_materialization",
            side_effect=RealmConflict("injected claim loss"),
        ):
            # Every claim fails, so each takeover fences the previous lapsed
            # creating row, recreates, and finally exhausts its budget.
            with self.assertRaisesRegex(RealmConflict, "Timed out waiting"):
                other.project_read_only(
                    operation_id="creating-crash",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id="creating-crash-holder",
                    ttl_seconds=0.12,
                )
        time.sleep(0.18)

        with mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_GRACE_SECONDS",
            0.02,
        ), mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_INTERVAL_SECONDS",
            0.02,
        ):
            replacement = other.project_read_only(
                operation_id="creating-takeover",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id="creating-takeover-holder",
                ttl_seconds=60,
            )
        self.projections.append(replacement)

        self.assertEqual(
            (replacement.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        records = self.ledger.list_projection_realizations(
            actor_principal_id=other.maintenance_principal_id,
            projection_root_id=other.root_binding.projection_root_id,
            states=tuple(ProjectionRealizationState),
        )
        by_state = {record.realization_id: record.state for record in records}
        self.assertEqual(
            by_state[replacement.realization.realization_id],
            ProjectionRealizationState.READY,
        )
        self.assertNotIn(ProjectionRealizationState.CREATING, by_state.values())
        self.assertNotIn(
            ProjectionRealizationState.MATERIALIZING, by_state.values()
        )

    def test_waiting_reader_never_fences_a_live_builder_term(self) -> None:
        spec = ProjectionSpec(
            "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
        )
        with mock.patch.object(
            self.service,
            "_materialize",
            side_effect=RuntimeError("crash after claim"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after claim"):
                self.service.project_read_only(
                    operation_id="live-term-original",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id="live-term-holder",
                    ttl_seconds=60,
                )
        materializing = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.MATERIALIZING,),
        )
        self.assertEqual(len(materializing), 1)
        old_id = materializing[0].realization_id

        other = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
            coordination_timeout_seconds=0.3,
        )
        with mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_GRACE_SECONDS",
            0.02,
        ), mock.patch(
            "optpilot.realm.projection_service."
            "_STALE_BUILD_TAKEOVER_INTERVAL_SECONDS",
            0.02,
        ):
            with self.assertRaisesRegex(RealmConflict, "Timed out waiting"):
                other.project_read_only(
                    operation_id="live-term-waiter",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=spec,
                    holder_id="live-term-waiter-holder",
                    ttl_seconds=60,
                )

        current = self.ledger.read_projection_realization(
            actor_principal_id="operator",
            realization_id=old_id,
        )
        self.assertEqual(
            current.state, ProjectionRealizationState.MATERIALIZING
        )

    def test_materialization_lease_term_is_capped_for_long_lived_consumers(
        self,
    ) -> None:
        from optpilot.realm.projection_service import (
            _ACTIVE_MATERIALIZATION_LEASE_MAX_SECONDS,
        )

        captured: dict[str, float] = {}
        real_materialize = self.service._materialize

        def capturing(**kwargs):
            captured["ttl_seconds"] = kwargs["ttl_seconds"]
            return real_materialize(**kwargs)

        with mock.patch.object(
            self.service, "_materialize", side_effect=capturing
        ):
            projection = self.service.project_read_only(
                operation_id="capped-materialization",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=ProjectionSpec(
                    "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                ),
                holder_id="day-long-viewer",
                ttl_seconds=86400,
            )
        self.projections.append(projection)

        self.assertEqual(
            captured["ttl_seconds"], _ACTIVE_MATERIALIZATION_LEASE_MAX_SECONDS
        )
        lease = projection.consumer_lease
        self.assertAlmostEqual(
            lease.expires_at - lease.created_at, 86400.0, delta=5.0
        )

    def test_expired_cleanup_worker_is_reclaimed_and_completed(self) -> None:
        projection = self.service.project_read_only(
            operation_id="cleanup-reclaim-source",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="cleanup-reclaim-viewer",
            ttl_seconds=0.08,
        )
        realization = projection.realization
        wrapper = self.service.root_binding.path / realization.relative_name
        projection.close()
        time.sleep(0.14)
        closing = self.ledger.close_projection_realization(
            operation_id="test-maintenance-close",
            actor_principal_id=self.service.maintenance_principal_id,
            realization_id=realization.realization_id,
            owner_holder_id=None,
            owner_fencing_token=None,
        )
        claimed = self.ledger.claim_projection_cleanup(
            operation_id="test-short-cleanup-claim",
            actor_principal_id=self.service.maintenance_principal_id,
            realization_id=closing.realization_id,
            owner_holder_id="test-cleanup-owner",
            owner_fencing_token=None,
            builder_holder_id="test-cleanup-builder",
            builder_ttl_seconds=0.08,
            cleanup_token="9" * 64,
        )
        from optpilot.realm.projection_service import (
            _BuilderHeartbeat,
            _cleanup_key,
        )

        # Simulate a cleanup worker that reached its first heartbeat and then
        # died.  Recovery must not replay that incarnation's cached operation
        # receipt with the reclaimed holder/fence credentials.
        old_heartbeat = _BuilderHeartbeat(
            ledger=self.ledger,
            actor_principal_id=self.service.maintenance_principal_id,
            realization_id=realization.realization_id,
            claim=claimed,
            operation_prefix=(
                "projection.maintenance.heartbeat/"
                f"{_cleanup_key(realization.realization_id)}"
            ),
            ttl_seconds=0.08,
        )
        old_heartbeat.start()
        old_heartbeat.stop()
        time.sleep(0.14)

        with mock.patch.object(
            self.ledger,
            "reclaim_projection_cleanup",
            wraps=self.ledger.reclaim_projection_cleanup,
        ) as reclaim:
            receipt = self.service.reconcile_projection(
                operation_id="recover-expired-cleanup",
                realization_id=realization.realization_id,
                ttl_seconds=60,
            )

        self.assertEqual(reclaim.call_count, 2)
        self.assertEqual(receipt.realization.state, ProjectionRealizationState.CLEANED)
        self.assertEqual(
            receipt.realization.owner_generation,
            claimed.realization.owner_generation + 1,
        )
        self.assertEqual(receipt.realization.cleanup_token, claimed.realization.cleanup_token)
        self.assertEqual(
            receipt.realization.cleanup_started_at,
            claimed.realization.cleanup_started_at,
        )
        self.assertTrue(receipt.namespace_removed)
        self.assertFalse(wrapper.exists())

    def test_maintenance_principal_has_only_root_lifecycle_authority(self) -> None:
        projection = self._project("maintenance-least-privilege")
        maintenance = self.service.maintenance_principal_id
        realization_id = projection.realization.realization_id
        listed = self.ledger.list_projection_realizations(
            actor_principal_id=maintenance,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.READY,),
        )
        self.assertEqual(tuple(item.realization_id for item in listed), (realization_id,))
        with self.assertRaises(RealmNotFound):
            self.ledger.read_projection_realization(
                actor_principal_id=maintenance,
                realization_id=realization_id,
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.acquire_projection_consumer(
                operation_id="maintenance-forged-consumer",
                actor_principal_id=maintenance,
                realization_id=realization_id,
                consumer_holder_id="forged-viewer",
                consumer_ttl_seconds=60,
                consumer_kind="inspection",
            )
        with self.assertRaises(RealmNotFound):
            self.ledger.claim_projection_materialization(
                operation_id="maintenance-forged-builder",
                actor_principal_id=maintenance,
                realization_id=realization_id,
                owner_holder_id="forged-owner",
                owner_fencing_token=1,
                builder_holder_id="forged-builder",
                builder_ttl_seconds=60,
            )
        with self.assertRaisesRegex(RealmConflict, "Active projection owner authority"):
            self.ledger.close_projection_realization(
                operation_id="maintenance-premature-close",
                actor_principal_id=maintenance,
                realization_id=realization_id,
                owner_holder_id=None,
                owner_fencing_token=None,
            )
        with self.assertRaisesRegex(RealmConflict, "Active projection owner authority"):
            self.ledger.quarantine_projection_realization(
                operation_id="maintenance-premature-quarantine",
                actor_principal_id=maintenance,
                realization_id=realization_id,
                reason="forged maintenance quarantine",
            )
        projection.validate()

    def test_reconcile_adopts_exact_namespace_removed_before_ledger_completion(
        self,
    ) -> None:
        projection = self.service.project_read_only(
            operation_id="cleanup-proof-before-ledger-source",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="cleanup-proof-before-ledger-viewer",
            ttl_seconds=0.08,
        )
        self.projections.append(projection)
        realization = projection.realization
        wrapper = self.service.root_binding.path / realization.relative_name
        projection.close()
        time.sleep(0.14)
        closing = self.ledger.close_projection_realization(
            operation_id="cleanup-proof-before-ledger-close",
            actor_principal_id=self.service.maintenance_principal_id,
            realization_id=realization.realization_id,
            owner_holder_id=None,
            owner_fencing_token=None,
        )
        cleanup_token = self.service._maintenance_cleanup_token(closing)
        claim = self.service._namespace_claim(closing)
        identity = self.service._preflight_cleanup_identity(
            record=closing,
            claim=claim,
            cleanup_token=cleanup_token,
        )
        self.assertTrue(
            cleanup_projection_namespace(
                self.service.root_binding,
                claim,
                identity,
                cleanup_token=cleanup_token,
            )
        )
        self.assertFalse(wrapper.exists())
        self.assertEqual(
            self.service._maintenance_realization(realization.realization_id).state,
            ProjectionRealizationState.CLOSING,
        )

        receipt = self.service.reconcile_projection(
            operation_id="cleanup-proof-before-ledger-reconcile",
            realization_id=realization.realization_id,
            ttl_seconds=60,
        )

        self.assertEqual(
            receipt.realization.state, ProjectionRealizationState.CLEANED
        )
        self.assertFalse(receipt.already_complete)
        self.assertFalse(wrapper.exists())

    def test_reconcile_adopts_winner_after_cleanup_claim_heartbeat_race(
        self,
    ) -> None:
        projection = self.service.project_read_only(
            operation_id="cleanup-heartbeat-race-source",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="cleanup-heartbeat-race-viewer",
            ttl_seconds=0.08,
        )
        self.projections.append(projection)
        realization_id = projection.realization.realization_id
        projection.close()
        time.sleep(0.14)
        from optpilot.realm.projection_service import _BuilderHeartbeat

        start = _BuilderHeartbeat.start
        injected = False

        def finish_in_winner_then_lose(heartbeat):
            nonlocal injected
            if injected:
                return start(heartbeat)
            injected = True
            with mock.patch.object(_BuilderHeartbeat, "start", start):
                winner = self.service.reconcile_projection(
                    operation_id="cleanup-heartbeat-race-winner",
                    realization_id=realization_id,
                    ttl_seconds=60,
                )
            self.assertEqual(
                winner.realization.state, ProjectionRealizationState.CLEANED
            )
            raise RealmNotFound("cleanup winner retired heartbeat authority")

        with mock.patch.object(
            _BuilderHeartbeat,
            "start",
            autospec=True,
            side_effect=finish_in_winner_then_lose,
        ):
            loser = self.service.reconcile_projection(
                operation_id="cleanup-heartbeat-race-loser",
                realization_id=realization_id,
                ttl_seconds=60,
            )

        self.assertEqual(
            loser.realization.state, ProjectionRealizationState.CLEANED
        )
        self.assertTrue(loser.already_complete)

    def test_reconcile_quarantines_wrapper_renamed_away_before_cleanup(self) -> None:
        projection = self.service.project_read_only(
            operation_id="rename-away-cleanup-source",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="rename-away-cleanup-viewer",
            ttl_seconds=0.08,
        )
        self.projections.append(projection)
        realization = projection.realization
        wrapper = self.service.root_binding.path / realization.relative_name
        stolen = self.service.root_binding.path / "rename-away-cleanup-stolen"
        projection.close()
        time.sleep(0.14)
        wrapper.rename(stolen)

        with self.assertRaisesRegex(
            RealmIntegrityError, "absent without exact retirement proof"
        ):
            self.service.reconcile_projection(
                operation_id="rename-away-cleanup-reconcile",
                realization_id=realization.realization_id,
                ttl_seconds=60,
            )

        quarantined = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.QUARANTINED,),
        )
        self.assertEqual(
            tuple(item.realization_id for item in quarantined),
            (realization.realization_id,),
        )
        self.assertEqual(
            (stolen / "root" / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.assertEqual(
            self.ledger.list_projection_realizations(
                actor_principal_id=self.service.maintenance_principal_id,
                projection_root_id=self.service.root_binding.projection_root_id,
                states=(ProjectionRealizationState.CLEANED,),
            ),
            (),
        )

    def test_reconcile_operation_cannot_be_reused_for_another_realization(self) -> None:
        specs = (
            ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            ProjectionSpec(
                "workspace-a",
                (TreeMapping(self.receipt.snapshot_ref, destination="nested"),),
            ),
        )
        projections = tuple(
            self.service.project_read_only(
                operation_id=f"reconcile-target-source-{index}",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=spec,
                holder_id=f"reconcile-target-viewer-{index}",
                ttl_seconds=0.08,
            )
            for index, spec in enumerate(specs)
        )
        for projection in projections:
            projection.close()
        time.sleep(0.14)

        first = self.service.reconcile_projection(
            operation_id="one-reconcile-operation",
            realization_id=projections[0].realization.realization_id,
            ttl_seconds=60,
        )
        self.assertEqual(first.realization.state, ProjectionRealizationState.CLEANED)
        with self.assertRaises(RealmConflict):
            self.service.reconcile_projection(
                operation_id="one-reconcile-operation",
                realization_id=projections[1].realization.realization_id,
                ttl_seconds=60,
            )
        second = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.READY,),
        )
        self.assertEqual(
            tuple(item.realization_id for item in second),
            (projections[1].realization.realization_id,),
        )

    def test_disabled_projection_root_can_still_drain_abandoned_realizations(self) -> None:
        projection = self.service.project_read_only(
            operation_id="disabled-root-source",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="disabled-root-viewer",
            ttl_seconds=0.08,
        )
        realization_id = projection.realization.realization_id
        projection.close()
        time.sleep(0.14)
        self.ledger.set_projection_root_state(
            operation_id="disable-projection-root",
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            state="disabled",
        )

        receipt = self.service.reconcile_projection(
            operation_id="drain-disabled-projection-root",
            realization_id=realization_id,
            ttl_seconds=60,
        )

        self.assertEqual(receipt.realization.state, ProjectionRealizationState.CLEANED)

    def test_new_service_attaches_ready_realization_without_copying_again(self) -> None:
        first = self._project("initial-realization")
        realization_id = first.realization.realization_id
        first.close()
        restarted = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        restarted._provider.project = mock.Mock(
            side_effect=AssertionError("a ready realization must be attached")
        )

        attached = restarted.project_read_only(
            operation_id="restart-attach",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            spec=ProjectionSpec(
                "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
            ),
            holder_id="worker-after-restart",
            ttl_seconds=60,
        )

        self.assertEqual(attached.realization.realization_id, realization_id)
        self.assertEqual(
            (attached.root_path / "payload.txt").read_text(encoding="utf-8"),
            "ledger projection",
        )
        self.assertEqual(restarted._provider.project.call_count, 0)
        self.projections.append(attached)

    def test_attach_failure_releases_the_new_consumer_lease(self) -> None:
        with mock.patch(
            "optpilot.realm.projection_service.attach_projection_namespace",
            side_effect=RealmIntegrityError("injected attach failure"),
        ):
            with self.assertRaisesRegex(RealmIntegrityError, "injected attach"):
                self.service.project_read_only(
                    operation_id="broken-attach",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=ProjectionSpec(
                        "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                    ),
                    holder_id="broken-viewer",
                    ttl_seconds=60,
                )

        realization = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.QUARANTINED,),
        )[0]
        self.assertIn("injected attach failure", realization.quarantine_reason)
        consumer = self.ledger.list_projection_consumers(
            actor_principal_id="operator",
            realization_id=realization.realization_id,
        )[0]
        with self.assertRaises(RealmConflict):
            self.ledger.validate_projection_consumer(
                actor_principal_id="operator",
                realization_id=realization.realization_id,
                consumer_id=consumer.consumer_id,
                consumer_holder_id="broken-viewer",
                consumer_fencing_token=1,
            )

    def test_stale_builder_cannot_leave_a_wrapper_after_cleanup_completed(self) -> None:
        from optpilot.realm.projection_namespace import create_projection_wrapper

        real_record = self.ledger.record_projection_namespace_claim

        def complete_cleanup_then_recreate(**kwargs):
            claim = real_record(**kwargs)
            cleaned = self.service._close_and_cleanup(
                actor_principal_id="operator",
                realization_id=claim.realization.realization_id,
                owner_lease=claim.owner_lease,
                ttl_seconds=60,
            )
            self.assertEqual(cleaned.state, ProjectionRealizationState.CLEANED)
            with self.assertRaisesRegex(RealmConflict, "permanently retired"):
                create_projection_wrapper(
                    self.service.root_binding,
                    directory_name=claim.realization.relative_name,
                    realization_id=claim.realization.realization_id,
                    claim_nonce=claim.realization.claim_nonce,
                )
            raise RealmConflict("injected stale builder resume")

        with mock.patch.object(
            self.ledger,
            "record_projection_namespace_claim",
            side_effect=complete_cleanup_then_recreate,
        ):
            with self.assertRaisesRegex(RealmConflict, "stale builder"):
                self.service.project_read_only(
                    operation_id="stale-builder",
                    actor_principal_id="operator",
                    store_id=self.store.store_id,
                    spec=ProjectionSpec(
                        "workspace-a", (TreeMapping(self.receipt.snapshot_ref),)
                    ),
                    holder_id="stale-worker",
                    ttl_seconds=60,
                )

        cleaned = self.ledger.list_projection_realizations(
            actor_principal_id=self.service.maintenance_principal_id,
            projection_root_id=self.service.root_binding.projection_root_id,
            states=(ProjectionRealizationState.CLEANED,),
        )
        self.assertEqual(len(cleaned), 1)
        self.assertFalse(
            (self.service.root_binding.path / cleaned[0].relative_name).exists()
        )
        self.assertEqual(
            tuple(self.service.root_binding.path.glob(".projection-cleanup-*.json")),
            (),
        )


class ProjectionMaterializationAmortizationTest(unittest.TestCase):
    """Per-file owner-lease revalidation must stay amortized during builds."""

    FILE_COUNT = 40

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        for index in range(self.FILE_COUNT):
            subdirectory = self.source_root / f"dir{index % 4}"
            subdirectory.mkdir(exist_ok=True)
            (subdirectory / f"file{index:03d}.txt").write_text(
                f"payload {index}", encoding="utf-8"
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="principal", principal_id="operator", kind="human"
        )
        self.ledger.register_store(
            operation_id="store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.ledger.create_owner(
            operation_id="owner",
            owner_id="workspace-a",
            owner_kind="workspace",
            principal_id="operator",
        )
        change = self.ledger.begin_owner_change(
            operation_id="begin",
            actor_principal_id="operator",
            owner_id="workspace-a",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        authority = self.ledger.content_capture_handle(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        )
        self.receipt = self.store.capture(
            change_id=change.change_id, authority=authority
        ).seal_tree(source=AllowedTreeSource(self.source_root))
        membership = OwnerMembership(
            self.store.store_id, self.receipt.snapshot_ref, "workspace-base"
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
        self.service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.projections = []

    def tearDown(self) -> None:
        for projection in reversed(self.projections):
            if not projection.closed:
                projection.close()
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def test_materialization_amortizes_owner_lease_revalidation(self) -> None:
        with mock.patch.object(
            self.ledger,
            "validate_projection_owner_lease",
            wraps=self.ledger.validate_projection_owner_lease,
        ) as validate:
            projection = self.service.project_read_only(
                operation_id="amortized-project",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                spec=ProjectionSpec(
                    owner_id="workspace-a",
                    mappings=(TreeMapping(self.receipt.snapshot_ref),),
                ),
                holder_id="worker-a",
                ttl_seconds=60,
            )
        self.projections.append(projection)
        # The builder must not revalidate the owner lease per copied file;
        # fencing at publish and the builder heartbeat carry that duty.
        self.assertLess(validate.call_count, self.FILE_COUNT)
        materialized = {
            path.relative_to(projection.root_path).as_posix()
            for path in projection.root_path.rglob("*.txt")
        }
        self.assertEqual(
            materialized,
            {
                f"dir{index % 4}/file{index:03d}.txt"
                for index in range(self.FILE_COUNT)
            },
        )


if __name__ == "__main__":
    unittest.main()
