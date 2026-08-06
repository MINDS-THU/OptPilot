"""Filesystem-safety checks for the disposable tree-snapshot spike."""

from __future__ import annotations

import os
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts.spikes.tree_snapshot_spike import (
    APFSCloneProvider,
    CaptureRejected,
    SourceChanged,
    TreeObjectStoreSpike,
    UnsupportedProvider,
    VerifiedCopyProvider,
    apfs_clone_supported,
    canonical_relative_path,
    validate_canonical_paths,
)


class _MutatingProvider(VerifiedCopyProvider):
    name = "test-mutating-provider"

    def __init__(self, source_file: Path, *, add_entry: bool = False) -> None:
        self.source_file = source_file
        self.add_entry = add_entry
        self.mutated = False

    def copy_file(self, source_fd: int, destination_directory_fd: int, destination_name: str):
        result = super().copy_file(source_fd, destination_directory_fd, destination_name)
        if not self.mutated:
            if self.add_entry:
                (self.source_file.parent / "appeared.txt").write_text(
                    "new entry\n", encoding="utf-8"
                )
            else:
                self.source_file.write_text("changed bytes\n", encoding="utf-8")
            self.mutated = True
        return result


class TreeSnapshotSpikeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = TreeObjectStoreSpike(self.root / "store")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_representative_tree(self) -> None:
        (self.source / "empty").mkdir()
        nested = self.source / "nested"
        nested.mkdir()
        (self.source / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        executable = nested / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        (nested / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt").write_text(
            "coffee\n", encoding="utf-8"
        )
        os.symlink("alpha.txt", self.source / "alpha-link")

    def _wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 5.0
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for {path}")
            time.sleep(0.01)

    def _capture_then_mutate_through_open_descriptor(self, provider) -> None:
        source_file = self.source / "payload.txt"
        original = b"original bytes\n"
        source_file.write_bytes(original)
        ready = self.root / f"{provider.name}.ready"
        go = self.root / f"{provider.name}.go"
        fixture = Path(__file__).parent.parent / "fixtures" / "tree_snapshot_spike_writer.py"
        process = subprocess.Popen(
            [
                sys.executable,
                str(fixture),
                "--source",
                str(source_file),
                "--ready",
                str(ready),
                "--go",
                str(go),
                "--replacement",
                "mutated through old fd",
            ]
        )
        try:
            self._wait_for(ready)
            receipt = self.store.capture(self.root, "source", provider=provider)
            sealed_file = self.store.tree_path(receipt.tree_ref) / "payload.txt"
            sealed_inode = sealed_file.stat().st_ino
            source_inode = source_file.stat().st_ino
            self.assertNotEqual(sealed_inode, source_inode, "capture must not hardlink/adopt")
            go.write_text("go\n", encoding="utf-8")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertNotEqual(source_file.read_bytes(), original)
            self.assertEqual(sealed_file.read_bytes(), original)
            self.store.verify_object(receipt.tree_ref)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_verified_copy_has_deterministic_complete_manifest(self) -> None:
        self._build_representative_tree()
        for index, path in enumerate([self.source, *self.source.rglob("*")], start=1):
            os.utime(path, (index * 100, index * 100), follow_symlinks=False)

        first = self.store.capture(self.root, "source")
        manifest = self.store.manifest(first.tree_ref)
        entries = manifest["entries"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(paths, key=lambda value: value.encode("utf-8")))
        self.assertIn({"path": "empty", "type": "directory"}, entries)
        self.assertIn({"path": "nested", "type": "directory"}, entries)
        self.assertIn(
            {"path": "alpha-link", "target": "alpha.txt", "type": "symlink"},
            entries,
        )
        run_entry = next(entry for entry in entries if entry["path"] == "nested/run.sh")
        self.assertTrue(run_entry["executable"])
        self.assertEqual(run_entry["size"], len(b"#!/bin/sh\nexit 0\n"))
        self.assertTrue(run_entry["digest"].startswith("sha256:"))
        sealed_root = self.store.tree_path(first.tree_ref)
        self.assertFalse(sealed_root.stat().st_mode & stat_module.S_IWUSR)
        self.assertFalse((sealed_root / "alpha.txt").stat().st_mode & stat_module.S_IWUSR)
        self.assertEqual(list(self.store.staging.iterdir()), [])

        for index, path in enumerate([self.source, *self.source.rglob("*")], start=20):
            os.utime(path, (index * 100, index * 100), follow_symlinks=False)
        second_store = TreeObjectStoreSpike(self.root / "second-store")
        second = second_store.capture(self.root, "source")
        self.assertEqual(second.tree_ref, first.tree_ref)
        self.assertEqual(
            second_store.manifest_bytes(second.tree_ref),
            self.store.manifest_bytes(first.tree_ref),
        )
        self.store.verify_object(first.tree_ref)

    def test_apfs_clone_and_verified_copy_have_identical_identity(self) -> None:
        self._build_representative_tree()
        if not apfs_clone_supported(self.source, self.root):
            self.skipTest("strict fclonefileat assertions require one APFS device")

        verified_store = TreeObjectStoreSpike(self.root / "verified-store")
        clone_store = TreeObjectStoreSpike(self.root / "clone-store")
        verified = verified_store.capture(
            self.root,
            "source",
            provider=VerifiedCopyProvider(),
        )
        cloned = clone_store.capture(self.root, "source", provider=APFSCloneProvider())
        self.assertEqual(cloned.provider, "apfs-fclonefileat")
        self.assertEqual(cloned.tree_ref, verified.tree_ref)
        self.assertEqual(
            clone_store.manifest_bytes(cloned.tree_ref),
            verified_store.manifest_bytes(verified.tree_ref),
        )
        clone_store.verify_object(cloned.tree_ref)

    def test_apfs_provider_is_strict_and_does_not_fallback(self) -> None:
        provider = APFSCloneProvider()
        with mock.patch(
            "scripts.spikes.tree_snapshot_spike._apfs_clone_supported_fd",
            return_value=False,
        ):
            with self.assertRaisesRegex(UnsupportedProvider, "APFS"):
                self.store.capture(self.root, "source", provider=provider)

    def test_open_descriptor_mutation_cannot_change_verified_copy(self) -> None:
        self._capture_then_mutate_through_open_descriptor(VerifiedCopyProvider())

    def test_open_descriptor_mutation_cannot_change_apfs_clone(self) -> None:
        if not apfs_clone_supported(self.source, self.root):
            self.skipTest("strict fclonefileat assertions require one APFS device")
        self._capture_then_mutate_through_open_descriptor(APFSCloneProvider())

    def test_mutation_during_file_capture_fails_without_publication(self) -> None:
        source_file = self.source / "payload.txt"
        source_file.write_text("original bytes\n", encoding="utf-8")
        with self.assertRaises(SourceChanged):
            self.store.capture(
                self.root,
                "source",
                provider=_MutatingProvider(source_file),
            )
        self.assertEqual(self.store.sealed_refs(), [])
        self.assertEqual(list(self.store.staging.iterdir()), [])

    def test_whole_tree_inventory_detects_appearing_entry(self) -> None:
        source_file = self.source / "payload.txt"
        source_file.write_text("original bytes\n", encoding="utf-8")
        with self.assertRaises(SourceChanged):
            self.store.capture(
                self.root,
                "source",
                provider=_MutatingProvider(source_file, add_entry=True),
            )
        self.assertEqual(self.store.sealed_refs(), [])

    def test_sealed_objects_are_provisionally_protected_before_adoption(self) -> None:
        (self.source / "one.txt").write_text("one\n", encoding="utf-8")
        first = self.store.capture(self.root, "source")
        (self.source / "two.txt").write_text("two\n", encoding="utf-8")
        second = self.store.capture(self.root, "source")

        self.assertEqual(set(self.store.sealed_refs()), {first.tree_ref, second.tree_ref})
        self.assertTrue(all(self.store.is_protected(ref) for ref in self.store.sealed_refs()))
        self.assertEqual(self.store.gc_eligible_refs(), [])

        self.store.release(second.tree_ref, second.retention_token)
        self.assertEqual(self.store.gc_eligible_refs(), [second.tree_ref])
        self.store.adopt(first.tree_ref, first.retention_token, owner_id="workspace-1")
        self.assertTrue(self.store.is_protected(first.tree_ref))
        self.assertEqual(self.store.gc_eligible_refs(), [second.tree_ref])

    def test_deduplicated_capture_gets_an_independent_retention_token(self) -> None:
        (self.source / "one.txt").write_text("one\n", encoding="utf-8")
        first = self.store.capture(self.root, "source")
        second = self.store.capture(self.root, "source")
        self.assertEqual(first.tree_ref, second.tree_ref)
        self.assertNotEqual(first.retention_token, second.retention_token)

        self.store.release(first.tree_ref, first.retention_token)
        self.assertTrue(self.store.is_protected(first.tree_ref))
        self.assertEqual(self.store.gc_eligible_refs(), [])
        self.store.adopt(second.tree_ref, second.retention_token, owner_id="workspace-1")
        self.assertTrue(self.store.is_protected(first.tree_ref))

    def test_collect_rechecks_protection_and_atomically_removes_eligible_object(self) -> None:
        (self.source / "one.txt").write_text("one\n", encoding="utf-8")
        receipt = self.store.capture(self.root, "source")
        self.store.release(receipt.tree_ref, receipt.retention_token)
        self.assertEqual(self.store.gc_eligible_refs(), [receipt.tree_ref])

        self.assertTrue(self.store.collect(receipt.tree_ref))
        self.assertEqual(self.store.sealed_refs(), [])
        self.assertEqual(list(self.store.trash.iterdir()), [])
        self.assertFalse(self.store.collect(receipt.tree_ref))

    def test_recapture_and_collect_are_serialized_without_deleting_protected_object(self) -> None:
        (self.source / "one.txt").write_text("one\n", encoding="utf-8")
        initial = self.store.capture(self.root, "source")
        self.store.release(initial.tree_ref, initial.retention_token)

        capture_holds_lock = threading.Event()
        allow_capture = threading.Event()
        collect_blocked = threading.Event()

        def hook(step: str) -> None:
            if step == "capture_publish_lock_acquired":
                capture_holds_lock.set()
                if not allow_capture.wait(timeout=5):
                    raise TimeoutError("test did not release capture")
            elif step == "collect_lock_blocked":
                collect_blocked.set()

        self.store.fault_hook = hook
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                capture_future = executor.submit(self.store.capture, self.root, "source")
                self.assertTrue(capture_holds_lock.wait(timeout=5))
                collect_future = executor.submit(self.store.collect, initial.tree_ref)
                self.assertTrue(collect_blocked.wait(timeout=5))
                self.assertFalse(collect_future.done())
                allow_capture.set()
                recaptured = capture_future.result(timeout=5)
                self.assertFalse(collect_future.result(timeout=5))
        finally:
            allow_capture.set()
            self.store.fault_hook = None

        self.assertEqual(recaptured.tree_ref, initial.tree_ref)
        self.assertTrue(self.store.is_protected(initial.tree_ref))
        self.assertIn(initial.tree_ref, self.store.sealed_refs())

    def test_adopt_and_collect_are_serialized_without_a_protection_gap(self) -> None:
        (self.source / "one.txt").write_text("one\n", encoding="utf-8")
        receipt = self.store.capture(self.root, "source")
        adoption_holds_lock = threading.Event()
        allow_adoption = threading.Event()
        collect_blocked = threading.Event()

        def hook(step: str) -> None:
            if step == "adopt_transition_complete":
                adoption_holds_lock.set()
                if not allow_adoption.wait(timeout=5):
                    raise TimeoutError("test did not release adoption")
            elif step == "collect_lock_blocked":
                collect_blocked.set()

        self.store.fault_hook = hook
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                adopt_future = executor.submit(
                    self.store.adopt,
                    receipt.tree_ref,
                    receipt.retention_token,
                    owner_id="workspace-1",
                )
                self.assertTrue(adoption_holds_lock.wait(timeout=5))
                collect_future = executor.submit(self.store.collect, receipt.tree_ref)
                self.assertTrue(collect_blocked.wait(timeout=5))
                self.assertFalse(collect_future.done())
                allow_adoption.set()
                adopt_future.result(timeout=5)
                self.assertFalse(collect_future.result(timeout=5))
        finally:
            allow_adoption.set()
            self.store.fault_hook = None

        self.assertTrue(self.store.is_protected(receipt.tree_ref))
        self.assertIn(receipt.tree_ref, self.store.sealed_refs())

    def test_selection_walk_rejects_intermediate_symlink_and_traversal(self) -> None:
        outside = self.root / "outside"
        nested = outside / "nested"
        nested.mkdir(parents=True)
        (nested / "secret.txt").write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.source / "gateway")

        with self.assertRaisesRegex(CaptureRejected, "symlink"):
            self.store.capture(self.root, "source/gateway/nested")
        with self.assertRaises(CaptureRejected):
            self.store.capture(self.root, "source/../outside")
        with self.assertRaises(CaptureRejected):
            self.store.capture(self.root, "/outside")
        self.assertEqual(self.store.sealed_refs(), [])

    def test_hard_crash_before_publish_leaves_no_half_published_object(self) -> None:
        (self.source / "payload.txt").write_text("durable staging\n", encoding="utf-8")
        crash_store_path = self.root / "crash-store"
        fixture = Path(__file__).parent.parent / "fixtures" / "tree_snapshot_spike_writer.py"
        process = subprocess.run(
            [
                sys.executable,
                str(fixture),
                "--mode",
                "crash-before-publish",
                "--allowed-root",
                str(self.root),
                "--selection",
                "source",
                "--store",
                str(crash_store_path),
            ],
            check=False,
        )
        self.assertEqual(process.returncode, 73)

        recovered = TreeObjectStoreSpike(crash_store_path)
        self.assertEqual(recovered.sealed_refs(), [])
        self.assertEqual(list(recovered.objects.iterdir()), [])
        staged = list(recovered.staging.glob("*/object"))
        self.assertEqual(len(staged), 1)
        self.assertTrue((staged[0] / "manifest.json").is_file())
        self.assertEqual(len(list((staged[0] / "meta" / "provisional").glob("*.json"))), 1)

    def test_rejects_unsafe_paths_and_symlinks(self) -> None:
        for path in (
            "/absolute",
            "../escape",
            "a/../escape",
            "a\\b",
            "a//b",
            "./a",
            "bad\udcff",
        ):
            with self.subTest(path=path):
                with self.assertRaises(CaptureRejected):
                    canonical_relative_path(path)

        (self.source / "bad\\name").write_text("bad\n", encoding="utf-8")
        with self.assertRaisesRegex(CaptureRejected, "backslash"):
            self.store.capture(self.root, "source")
        (self.source / "bad\\name").unlink()
        os.symlink("/etc/passwd", self.source / "absolute-link")
        with self.assertRaisesRegex(CaptureRejected, "unsafe symlink"):
            self.store.capture(self.root, "source")
        (self.source / "absolute-link").unlink()
        os.symlink("../escape", self.source / "traversal-link")
        with self.assertRaisesRegex(CaptureRejected, "unsafe symlink"):
            self.store.capture(self.root, "source")

    def test_rejects_case_and_nfc_collisions(self) -> None:
        with self.assertRaisesRegex(CaptureRejected, "NFC"):
            validate_canonical_paths(["caf\N{LATIN SMALL LETTER E WITH ACUTE}", "cafe\u0301"])
        with self.assertRaisesRegex(CaptureRejected, "case-insensitive"):
            validate_canonical_paths(["Stra\N{LATIN SMALL LETTER SHARP S}e", "STRASSE"])

    def test_rejects_special_nodes(self) -> None:
        fifo = self.source / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(CaptureRejected, "special filesystem node"):
            self.store.capture(self.root, "source")

    def test_rejects_sparse_files(self) -> None:
        sparse = self.source / "sparse.bin"
        with sparse.open("wb") as stream:
            stream.seek(64 * 1024 * 1024)
            stream.write(b"x")
        if sparse.stat().st_blocks * 512 >= sparse.stat().st_size:
            self.skipTest("filesystem did not create a sparse file")
        with self.assertRaisesRegex(CaptureRejected, "sparse"):
            self.store.capture(self.root, "source")

    def test_rejects_extended_attributes(self) -> None:
        attributed = self.source / "attributed.txt"
        attributed.write_text("data\n", encoding="utf-8")
        if hasattr(os, "setxattr"):
            os.setxattr(attributed, b"user.optpilot-spike", b"present")  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(
                ["/usr/bin/xattr", "-w", "com.example.optpilot-spike", "present", str(attributed)],
                check=True,
            )
        else:
            self.skipTest("platform cannot create an extended attribute")
        with self.assertRaisesRegex(CaptureRejected, "extended attributes"):
            self.store.capture(self.root, "source")

    def test_capture_root_must_not_be_a_symlink(self) -> None:
        real = self.source / "real"
        real.mkdir()
        linked = self.root / "linked-root"
        os.symlink(real, linked)
        with self.assertRaisesRegex(CaptureRejected, "real directory"):
            self.store.capture(linked, ".")


if __name__ == "__main__":
    unittest.main()
