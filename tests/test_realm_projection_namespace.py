from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from optpilot.realm.errors import RealmConflict, RealmIntegrityError
from optpilot.realm import projection_namespace as namespace_module
from optpilot.realm.ephemeral_volume_namespace import (
    prepare_ephemeral_volume_root,
)
from optpilot.realm.projection_namespace import (
    attach_projection_namespace,
    cleanup_projection_namespace,
    complete_projection_cleanup_namespace,
    create_projection_wrapper,
    find_projection_wrapper_identity,
    prepare_projection_root,
    ProjectionNamespaceClaim,
    record_projection_tree_identity,
    validate_projection_root,
)


class ProjectionNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.binding = prepare_projection_root(
            self.base / "projections", realm_id="realm-a"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ready_namespace(self):
        claim, identity = create_projection_wrapper(
            self.binding,
            directory_name="realization-a",
            realization_id="realization-a",
            claim_nonce="a" * 64,
        )
        tree = self.binding.path / identity.directory_name / "root"
        tree.mkdir(mode=0o700)
        (tree / "payload.txt").write_text("ready", encoding="utf-8")
        os.chmod(tree / "payload.txt", 0o400)
        os.chmod(tree, 0o500)
        identity = record_projection_tree_identity(self.binding, claim, identity)
        return claim, identity

    def test_root_marker_is_stable_and_portable_record_hides_local_path(self) -> None:
        replay = prepare_projection_root(
            self.base / "projections", realm_id="realm-a"
        )

        self.assertEqual(replay, self.binding)
        validate_projection_root(replay)
        self.assertEqual(replay.portable_record(), {})
        self.assertNotIn(str(self.binding.path), str(replay.portable_record()))
        with self.assertRaisesRegex(RealmIntegrityError, "another realm"):
            prepare_projection_root(self.base / "projections", realm_id="realm-b")

    def test_projection_and_volume_root_markers_are_mutually_exclusive(self) -> None:
        projection_path = self.base / "projection-kind"
        prepare_projection_root(projection_path, realm_id="realm-a")
        with self.assertRaisesRegex(RealmConflict, "cannot also be used"):
            prepare_ephemeral_volume_root(projection_path, realm_id="realm-a")

        volume_path = self.base / "volume-kind"
        prepare_ephemeral_volume_root(volume_path, realm_id="realm-a")
        with self.assertRaisesRegex(RealmConflict, "cannot also be used"):
            prepare_projection_root(volume_path, realm_id="realm-a")

    def test_root_marker_recovers_linked_and_orphan_publication_temps(self) -> None:
        marker = self.binding.path / ".optpilot-projection-root"
        linked = self.binding.path / f".projection-root-{'a' * 32}.tmp"
        orphan = self.binding.path / f".projection-root-{'b' * 32}.tmp"
        os.link(marker, linked)
        orphan.write_bytes(b"uncommitted marker")
        os.chmod(orphan, 0o400)
        self.assertEqual(marker.stat().st_nlink, 2)

        replay = prepare_projection_root(
            self.base / "projections", realm_id="realm-a"
        )

        self.assertEqual(replay, self.binding)
        self.assertFalse(linked.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(marker.stat().st_nlink, 1)

    def test_root_marker_rejects_an_unknown_hard_link_alias(self) -> None:
        marker = self.binding.path / ".optpilot-projection-root"
        alias = self.binding.path / "unknown-marker-alias"
        os.link(marker, alias)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "unknown hard-link alias"):
                prepare_projection_root(
                    self.base / "projections", realm_id="realm-a"
                )
        finally:
            alias.unlink()
        validate_projection_root(self.binding)

    def test_root_and_marker_permission_drift_is_rejected(self) -> None:
        os.chmod(self.binding.path, 0o755)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "private permissions"):
                validate_projection_root(self.binding)
        finally:
            os.chmod(self.binding.path, 0o700)

        marker = self.binding.path / ".optpilot-projection-root"
        os.chmod(marker, 0o600)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "unsafe"):
                validate_projection_root(self.binding)
        finally:
            os.chmod(marker, 0o400)
        validate_projection_root(self.binding)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_root_type_and_reserved_fifo_drift_fail_without_opening_streams(self) -> None:
        root = self.binding.path
        moved_root = root.with_name("saved-projection-root")
        root.rename(moved_root)
        os.mkfifo(root, 0o600)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "unavailable or unsafe"):
                validate_projection_root(self.binding)
        finally:
            root.unlink()
            moved_root.rename(root)

        lock = root / ".optpilot-storage-root.lock"
        lock.unlink()
        os.mkfifo(lock, 0o600)
        with self.assertRaisesRegex(RealmIntegrityError, "unsafe identity"):
            create_projection_wrapper(
                self.binding,
                directory_name="fifo-lock",
                realization_id="fifo-lock",
                claim_nonce="9" * 64,
            )
        self.assertTrue(lock.exists())

    def test_wrapper_is_fresh_and_claim_stays_outside_exposed_tree(self) -> None:
        claim, identity = self._ready_namespace()

        self.assertTrue(
            (self.binding.path / identity.directory_name / "claim.json").is_file()
        )
        self.assertFalse(
            (self.binding.path / identity.directory_name / "root" / "claim.json").exists()
        )
        with self.assertRaisesRegex(RealmConflict, "another or incomplete claim"):
            create_projection_wrapper(
                self.binding,
                directory_name=identity.directory_name,
                realization_id="other",
                claim_nonce="b" * 64,
            )
        with attach_projection_namespace(self.binding, claim, identity) as attached:
            self.assertEqual(
                (attached.root_path / "payload.txt").read_text(encoding="utf-8"),
                "ready",
            )

    def test_exact_wrapper_creation_replay_adopts_the_same_claimed_inode(self) -> None:
        claim, identity = create_projection_wrapper(
            self.binding,
            directory_name="replay-a",
            realization_id="replay-a",
            claim_nonce="8" * 64,
        )

        replay_claim, replay_identity = create_projection_wrapper(
            self.binding,
            directory_name="replay-a",
            realization_id="replay-a",
            claim_nonce="8" * 64,
        )

        self.assertEqual(replay_claim, claim)
        self.assertEqual(replay_identity, identity)

    def test_incomplete_wrapper_is_not_adopted_or_removed(self) -> None:
        wrapper = self.binding.path / "incomplete-a"
        wrapper.mkdir(mode=0o700)

        with self.assertRaisesRegex(RealmConflict, "another or incomplete claim"):
            create_projection_wrapper(
                self.binding,
                directory_name="incomplete-a",
                realization_id="incomplete-a",
                claim_nonce="7" * 64,
            )

        self.assertTrue(wrapper.is_dir())
        self.assertEqual(list(wrapper.iterdir()), [])

    def test_find_wrapper_identity_returns_exact_claim_or_proven_absence(self) -> None:
        absent_claim = ProjectionNamespaceClaim(
            self.binding.realm_id,
            self.binding.projection_root_id,
            "absent-a",
            "3" * 64,
        )
        self.assertIsNone(
            find_projection_wrapper_identity(
                self.binding, absent_claim, directory_name="absent-a"
            )
        )
        self.assertFalse((self.binding.path / "absent-a").exists())

        claim, identity = create_projection_wrapper(
            self.binding,
            directory_name="found-a",
            realization_id="found-a",
            claim_nonce="2" * 64,
        )
        self.assertEqual(
            find_projection_wrapper_identity(
                self.binding, claim, directory_name="found-a"
            ),
            identity,
        )

    def test_cleanup_lookup_captures_tree_under_the_same_root_lock(self) -> None:
        claim, identity = self._ready_namespace()

        observed = find_projection_wrapper_identity(
            self.binding,
            claim,
            directory_name=identity.directory_name,
            cleanup_token="4" * 64,
        )

        self.assertEqual(observed, identity)

    def test_find_wrapper_identity_does_not_recreate_a_missing_root_lock(self) -> None:
        claim = ProjectionNamespaceClaim(
            self.binding.realm_id,
            self.binding.projection_root_id,
            "absent-without-lock",
            "c" * 64,
        )
        lock = self.binding.path / ".optpilot-storage-root.lock"
        lock.unlink()

        with self.assertRaisesRegex(RealmIntegrityError, "lock is unavailable"):
            find_projection_wrapper_identity(
                self.binding, claim, directory_name="absent-without-lock"
            )

        self.assertFalse(lock.exists())
        self.assertFalse((self.binding.path / "absent-without-lock").exists())

    def test_cleanup_absence_without_identity_fails_closed_and_retires_claim(self) -> None:
        claim = ProjectionNamespaceClaim(
            self.binding.realm_id,
            self.binding.projection_root_id,
            "retired-before-create",
            "b" * 64,
        )

        with self.assertRaisesRegex(
            RealmIntegrityError, "absent without exact retirement proof"
        ):
            find_projection_wrapper_identity(
                self.binding,
                claim,
                directory_name="retired-before-create",
                cleanup_token="6" * 64,
            )
        self.assertEqual(
            len(tuple(self.binding.path.glob(".projection-retired-*.json"))), 1
        )
        with self.assertRaisesRegex(RealmConflict, "permanently retired"):
            create_projection_wrapper(
                self.binding,
                directory_name="retired-before-create",
                realization_id=claim.realization_id,
                claim_nonce=claim.claim_nonce,
            )

    def test_find_wrapper_identity_rejects_foreign_and_incomplete_names(self) -> None:
        claim, _identity = create_projection_wrapper(
            self.binding,
            directory_name="foreign-a",
            realization_id="foreign-a",
            claim_nonce="1" * 64,
        )
        foreign_claim = ProjectionNamespaceClaim(
            claim.realm_id,
            claim.projection_root_id,
            "other-realization",
            "0" * 64,
        )
        with self.assertRaisesRegex(RealmConflict, "another or incomplete claim"):
            find_projection_wrapper_identity(
                self.binding, foreign_claim, directory_name="foreign-a"
            )

        (self.binding.path / "incomplete-find").mkdir(mode=0o700)
        incomplete_claim = ProjectionNamespaceClaim(
            claim.realm_id,
            claim.projection_root_id,
            "incomplete-find",
            "f" * 64,
        )
        with self.assertRaisesRegex(RealmConflict, "another or incomplete claim"):
            find_projection_wrapper_identity(
                self.binding, incomplete_claim, directory_name="incomplete-find"
            )

    def test_find_wrapper_identity_rejects_link_replacement_during_inspection(self) -> None:
        claim, _identity = create_projection_wrapper(
            self.binding,
            directory_name="inspect-swap",
            realization_id="inspect-swap",
            claim_nonce="e" * 64,
        )
        wrapper = self.binding.path / "inspect-swap"
        moved = self.binding.path / "inspect-swap-moved"
        validate_claim = namespace_module._validate_claim

        def validate_then_swap(wrapper_fd, expected):
            validate_claim(wrapper_fd, expected)
            wrapper.rename(moved)
            wrapper.mkdir(mode=0o700)

        with mock.patch.object(
            namespace_module, "_validate_claim", side_effect=validate_then_swap
        ):
            with self.assertRaisesRegex(RealmIntegrityError, "path identity changed"):
                find_projection_wrapper_identity(
                    self.binding, claim, directory_name="inspect-swap"
                )
        self.assertTrue(wrapper.is_dir())
        self.assertTrue((moved / "claim.json").is_file())

    def test_find_wrapper_identity_never_converts_an_appearance_to_absence(self) -> None:
        claim = ProjectionNamespaceClaim(
            self.binding.realm_id,
            self.binding.projection_root_id,
            "appearing-a",
            "d" * 64,
        )
        wrapper = self.binding.path / "appearing-a"
        open_file = namespace_module.os.open

        def open_while_appearing(path, flags, *args, **kwargs):
            if path == "appearing-a":
                wrapper.mkdir(mode=0o700)
                raise FileNotFoundError("simulated absent open")
            return open_file(path, flags, *args, **kwargs)

        with mock.patch.object(
            namespace_module.os, "open", side_effect=open_while_appearing
        ):
            with self.assertRaisesRegex(RealmIntegrityError, "appeared"):
                find_projection_wrapper_identity(
                    self.binding, claim, directory_name="appearing-a"
                )
        self.assertTrue(wrapper.is_dir())

    def test_failed_claim_write_rolls_back_only_the_proven_created_wrapper(self) -> None:
        wrapper = self.binding.path / "failed-write"
        with mock.patch.object(
            namespace_module,
            "_write_file_exclusive",
            side_effect=OSError("simulated marker write failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated marker write failure"):
                create_projection_wrapper(
                    self.binding,
                    directory_name="failed-write",
                    realization_id="failed-write",
                    claim_nonce="6" * 64,
                )
        self.assertFalse(wrapper.exists())

    def test_failed_claim_write_does_not_remove_a_replacement_link(self) -> None:
        wrapper = self.binding.path / "swapped-write"
        moved = self.binding.path / "created-but-moved"

        def swap_before_failure(*_args, **_kwargs):
            wrapper.rename(moved)
            wrapper.mkdir(mode=0o700)
            raise OSError("simulated marker write failure after swap")

        with mock.patch.object(
            namespace_module,
            "_write_file_exclusive",
            side_effect=swap_before_failure,
        ):
            with self.assertRaisesRegex(OSError, "failure after swap"):
                create_projection_wrapper(
                    self.binding,
                    directory_name="swapped-write",
                    realization_id="swapped-write",
                    claim_nonce="5" * 64,
                )

        self.assertTrue(wrapper.is_dir())
        self.assertTrue(moved.is_dir())

    def test_wrapper_replacement_is_rejected_without_following_it(self) -> None:
        claim, identity = self._ready_namespace()
        original = self.binding.path / identity.directory_name
        moved = self.binding.path / "moved-wrapper"
        original.rename(moved)
        original.mkdir()
        (original / "claim.json").write_text("{}", encoding="utf-8")
        (original / "root").mkdir()

        with self.assertRaises(RealmIntegrityError):
            attach_projection_namespace(self.binding, claim, identity)
        self.assertTrue((original / "root").is_dir())
        self.assertTrue((moved / "root" / "payload.txt").is_file())

    def test_exposed_tree_replacement_is_rejected(self) -> None:
        claim, identity = self._ready_namespace()
        wrapper = self.binding.path / identity.directory_name
        tree = wrapper / "root"
        moved = wrapper / "old-root"
        tree.rename(moved)
        tree.mkdir()
        (tree / "foreign.txt").write_text("foreign", encoding="utf-8")

        with self.assertRaises(RealmIntegrityError):
            attach_projection_namespace(self.binding, claim, identity)
        self.assertEqual((tree / "foreign.txt").read_text(), "foreign")
        self.assertEqual((moved / "payload.txt").read_text(), "ready")

    def test_root_marker_replacement_is_rejected(self) -> None:
        marker = self.binding.path / ".optpilot-projection-root"
        marker.unlink()
        marker.write_text("{}", encoding="utf-8")

        with self.assertRaises(RealmIntegrityError):
            validate_projection_root(self.binding)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_claim_fifo_and_writable_claim_are_rejected(self) -> None:
        claim, identity = self._ready_namespace()
        claim_path = self.binding.path / identity.directory_name / "claim.json"
        saved = claim_path.with_name("saved-claim.json")
        claim_path.rename(saved)
        os.mkfifo(claim_path, 0o400)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "unsafe file type"):
                attach_projection_namespace(self.binding, claim, identity)
        finally:
            claim_path.unlink()
            saved.rename(claim_path)

        os.chmod(claim_path, 0o600)
        try:
            with self.assertRaisesRegex(RealmIntegrityError, "unsafe file type"):
                attach_projection_namespace(self.binding, claim, identity)
        finally:
            os.chmod(claim_path, 0o400)

    def test_cleanup_removes_only_the_exact_claimed_namespace_and_replays(self) -> None:
        claim, identity = self._ready_namespace()
        wrapper = self.binding.path / identity.directory_name

        self.assertTrue(
            cleanup_projection_namespace(
                self.binding, claim, identity, cleanup_token="d" * 64
            )
        )
        self.assertFalse(wrapper.exists())
        tombstones = list(self.binding.path.glob(".projection-cleanup-*.json"))
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0].stat().st_mode & 0o777, 0o400)
        with self.assertRaisesRegex(RealmIntegrityError, "retirement marker"):
            complete_projection_cleanup_namespace(
                self.binding, claim, cleanup_token="0" * 64
            )
        self.assertTrue(tombstones[0].exists())
        with self.assertRaisesRegex(RealmIntegrityError, "retirement marker"):
            cleanup_projection_namespace(
                self.binding, claim, identity, cleanup_token="0" * 64
            )
        self.assertFalse(
            cleanup_projection_namespace(
                self.binding, claim, identity, cleanup_token="d" * 64
            )
        )
        complete_projection_cleanup_namespace(
            self.binding, claim, cleanup_token="d" * 64
        )
        self.assertFalse(tombstones[0].exists())
        retirement = list(self.binding.path.glob(".projection-retired-*.json"))
        self.assertEqual(len(retirement), 1)
        complete_projection_cleanup_namespace(
            self.binding, claim, cleanup_token="d" * 64
        )
        self.assertTrue(retirement[0].exists())
        with self.assertRaisesRegex(RealmConflict, "permanently retired"):
            create_projection_wrapper(
                self.binding,
                directory_name=identity.directory_name,
                realization_id=claim.realization_id,
                claim_nonce=claim.claim_nonce,
            )

    def test_cleanup_resumes_after_tombstone_before_wrapper_removal(self) -> None:
        claim, identity = self._ready_namespace()
        wrapper = self.binding.path / identity.directory_name
        token = "4" * 64
        tombstone_name = namespace_module._cleanup_tombstone_name(claim, token)
        real_rmdir = namespace_module.os.rmdir
        failed = False

        def fail_once_after_tombstone(name, *args, **kwargs):
            nonlocal failed
            if not failed and str(name).startswith(".projection-retiring-"):
                failed = True
                raise OSError("simulated crash before wrapper removal")
            return real_rmdir(name, *args, **kwargs)

        with mock.patch.object(
            namespace_module.os, "rmdir", side_effect=fail_once_after_tombstone
        ):
            with self.assertRaisesRegex(RealmIntegrityError, "exact removable wrapper"):
                cleanup_projection_namespace(
                    self.binding,
                    claim,
                    identity,
                    cleanup_token=token,
                )

        self.assertFalse(wrapper.exists())
        self.assertTrue((self.binding.path / tombstone_name).is_file())

        self.assertTrue(
            cleanup_projection_namespace(
                self.binding,
                claim,
                identity,
                cleanup_token=token,
            )
        )
        self.assertFalse(wrapper.exists())
        self.assertTrue((self.binding.path / tombstone_name).is_file())

        complete_projection_cleanup_namespace(
            self.binding, claim, cleanup_token=token
        )
        self.assertFalse((self.binding.path / tombstone_name).exists())
        self.assertEqual(
            len(list(self.binding.path.glob(".projection-retired-*.json"))), 1
        )

    def test_cleanup_rejects_replaced_tree_without_touching_it(self) -> None:
        claim, identity = self._ready_namespace()
        wrapper = self.binding.path / identity.directory_name
        original = wrapper / "root"
        moved = wrapper / "old-root"
        original.rename(moved)
        original.mkdir()
        (original / "foreign.txt").write_text("keep", encoding="utf-8")

        with self.assertRaises(RealmIntegrityError):
            cleanup_projection_namespace(
                self.binding, claim, identity, cleanup_token="e" * 64
            )
        self.assertEqual((original / "foreign.txt").read_text(), "keep")
        self.assertEqual((moved / "payload.txt").read_text(), "ready")

    def test_cleanup_can_reconcile_a_claimed_partial_materialization(self) -> None:
        claim, wrapper_identity = create_projection_wrapper(
            self.binding,
            directory_name="partial-a",
            realization_id="partial-a",
            claim_nonce="c" * 64,
        )
        tree = self.binding.path / "partial-a" / "root"
        tree.mkdir()
        (tree / "partial.txt").write_text("partial", encoding="utf-8")

        self.assertTrue(
            cleanup_projection_namespace(
                self.binding,
                claim,
                wrapper_identity,
                cleanup_token="f" * 64,
            )
        )
        self.assertFalse((self.binding.path / "partial-a").exists())


if __name__ == "__main__":
    unittest.main()
