from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from optpilot_studio.ui.prepared_runtime_cache import PreparedRuntimeCache
from optpilot_studio.ui.server import (
    WorkspaceRuntimeManager,
    WorkspaceRuntimeOptions,
)


def _key_payload(
    cache: PreparedRuntimeCache,
    *,
    source: str = "a" * 64,
    setup_command: str = "prepare",
    image: str = "1" * 64,
    interpreter_abi: str = "cpython-312",
    architecture: str = "amd64",
) -> dict[str, object]:
    return cache.key_payload(
        source_identity={"selectionDigest": source},
        component_identity={"kind": "resource", "profileId": "default"},
        setup={
            "cache": "prepared",
            "steps": [{"uses": "command", "command": [setup_command]}],
        },
        provider_identity={
            "executor": "workspace-container",
            "imageDigest": f"sha256:{image}",
            "interpreterAbi": interpreter_abi,
            "os": "linux",
            "architecture": architecture,
        },
    )


class PreparedRuntimeCacheTest(unittest.TestCase):
    def test_successful_build_is_reused_after_launch_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache")
            key_payload = _key_payload(cache)
            builds: list[Path] = []

            def build(_entry: Path, payload: Path) -> None:
                builds.append(payload)
                executable = payload / "bin" / "tool"
                executable.parent.mkdir(parents=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                executable.chmod(0o755)

            first = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-first",
                build=build,
            )
            self.assertEqual(first.cache_status, "built")
            self.assertEqual(len(builds), 1)
            self.assertFalse(first.payload_root.stat().st_mode & 0o222)
            self.assertTrue(cache.release(first))

            second = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-second",
                build=build,
            )
            self.assertEqual(second.cache_status, "hit")
            self.assertEqual(len(builds), 1)
            self.assertEqual(second.payload_root, first.payload_root)
            self.assertTrue((second.payload_root / "bin" / "tool").is_file())
            self.assertTrue(cache.release(second))

    def test_key_changes_for_source_setup_interpreter_and_platform_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache")
            baseline = _key_payload(cache)
            keys = {
                cache.cache_key(baseline),
                cache.cache_key(_key_payload(cache, source="b" * 64)),
                cache.cache_key(_key_payload(cache, setup_command="prepare-v2")),
                cache.cache_key(_key_payload(cache, image="2" * 64)),
                cache.cache_key(
                    _key_payload(cache, interpreter_abi="cpython-313")
                ),
                cache.cache_key(_key_payload(cache, architecture="arm64")),
            }

        self.assertEqual(len(keys), 6)
        self.assertEqual(baseline["interpreter"]["abi"], "cpython-312")
        self.assertEqual(baseline["platform"]["architecture"], "amd64")
        self.assertEqual(baseline["cache_format"]["tree_digest"], "sha256-tree-v1")

    def test_failed_build_commits_no_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache")
            key_payload = _key_payload(cache)
            key = cache.cache_key(key_payload)

            def fail(_entry: Path, payload: Path) -> None:
                (payload / "partial").write_text("partial", encoding="utf-8")
                raise RuntimeError("setup failed")

            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                cache.acquire(
                    key_payload=key_payload,
                    launch_id="launch-failed",
                    build=fail,
                )

            self.assertFalse(cache.entry_root(key).exists())

    def test_invalid_manifest_or_writable_payload_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache")
            key_payload = _key_payload(cache)
            builds = 0

            def build(_entry: Path, payload: Path) -> None:
                nonlocal builds
                builds += 1
                (payload / "generation").write_text(str(builds), encoding="utf-8")

            first = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-first",
                build=build,
            )
            cache.release(first)
            first.payload_root.chmod(0o700)

            second = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-second",
                build=build,
            )
            self.assertEqual(second.cache_status, "built")
            self.assertEqual(builds, 2)
            self.assertEqual(
                (second.payload_root / "generation").read_text(encoding="utf-8"),
                "2",
            )
            cache.release(second)

    def test_payload_content_change_is_detected_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache")
            key_payload = _key_payload(cache)
            builds = 0

            def build(_entry: Path, payload: Path) -> None:
                nonlocal builds
                builds += 1
                (payload / "generation").write_text(str(builds), encoding="utf-8")

            first = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-first",
                build=build,
            )
            cache.release(first)
            generated = first.payload_root / "generation"
            generated.chmod(0o600)
            generated.write_text("poisoned", encoding="utf-8")

            second = cache.acquire(
                key_payload=key_payload,
                launch_id="launch-second",
                build=build,
            )
            self.assertEqual(second.cache_status, "built")
            self.assertEqual(builds, 2)
            self.assertEqual(generated.read_text(encoding="utf-8"), "2")
            cache.release(second)

    def test_same_key_build_is_single_flight_across_cache_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            first_cache = PreparedRuntimeCache(root)
            second_cache = PreparedRuntimeCache(root)
            key_payload = _key_payload(first_cache)
            build_entered = threading.Event()
            continue_build = threading.Event()
            result_lock = threading.Lock()
            builds = 0
            statuses: list[str] = []
            failures: list[BaseException] = []

            def build(_entry: Path, payload: Path) -> None:
                nonlocal builds
                with result_lock:
                    builds += 1
                build_entered.set()
                if not continue_build.wait(timeout=5):
                    raise RuntimeError("test timed out waiting to finish the build")
                (payload / "ready").touch()

            def acquire(cache: PreparedRuntimeCache, launch_id: str) -> None:
                try:
                    lease = cache.acquire(
                        key_payload=key_payload,
                        launch_id=launch_id,
                        build=build,
                    )
                    with result_lock:
                        statuses.append(lease.cache_status)
                    cache.release(lease)
                except BaseException as exc:
                    with result_lock:
                        failures.append(exc)

            first_thread = threading.Thread(
                target=acquire, args=(first_cache, "launch-first")
            )
            second_thread = threading.Thread(
                target=acquire, args=(second_cache, "launch-second")
            )
            first_thread.start()
            self.assertTrue(build_entered.wait(timeout=5))
            second_thread.start()
            continue_build.set()
            first_thread.join(timeout=5)
            second_thread.join(timeout=5)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(builds, 1)
            self.assertCountEqual(statuses, ["built", "hit"])

    def test_duplicate_live_launch_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            first_cache = PreparedRuntimeCache(root)
            key_payload = _key_payload(first_cache)

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").touch()

            lease = first_cache.acquire(
                key_payload=key_payload,
                launch_id="same-launch",
                build=build,
            )
            second_cache = PreparedRuntimeCache(root)
            with self.assertRaisesRegex(RuntimeError, "already holds a lease"):
                second_cache.acquire(
                    key_payload=key_payload,
                    launch_id="same-launch",
                    build=build,
                )
            self.assertTrue(first_cache.release(lease))

    def test_recovery_removes_incomplete_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            cache = PreparedRuntimeCache(root)
            key = cache.cache_key(_key_payload(cache))
            incomplete = cache.entry_root(key)
            (incomplete / "payload").mkdir(parents=True)
            (incomplete / "payload" / "partial").write_text(
                "partial", encoding="utf-8"
            )

            restarted = PreparedRuntimeCache(root)

            self.assertFalse(incomplete.exists())
            self.assertEqual(restarted.last_recovery_report["removed_entries"], 1)

    def test_recovery_removes_stale_lease_but_preserves_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            first_cache = PreparedRuntimeCache(root)
            key_payload = _key_payload(first_cache)

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").touch()

            lease = first_cache.acquire(
                key_payload=key_payload,
                launch_id="crashed-launch",
                build=build,
            )
            first_cache.close()
            self.assertTrue(lease.lease_path.exists())

            restarted = PreparedRuntimeCache(root)

            self.assertTrue(lease.entry_root.exists())
            self.assertFalse(lease.lease_path.exists())
            self.assertEqual(
                restarted.last_recovery_report["stale_leases_removed"], 1
            )
            reused = restarted.acquire(
                key_payload=key_payload,
                launch_id="new-launch",
                build=build,
            )
            self.assertEqual(reused.cache_status, "hit")
            restarted.release(reused)

    def test_active_lease_is_protected_from_other_instance_recovery_and_prune(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            owner = PreparedRuntimeCache(root)

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").write_bytes(b"prepared")

            lease = owner.acquire(
                key_payload=_key_payload(owner),
                launch_id="live-launch",
                build=build,
            )
            observer = PreparedRuntimeCache(root, max_bytes=1)
            observer.prune()

            self.assertTrue(lease.entry_root.exists())
            self.assertGreaterEqual(
                observer.last_recovery_report["active_leases"], 1
            )
            self.assertIn(lease.cache_key, observer.last_prune_report["active_keys"])

            self.assertTrue(owner.release(lease))
            observer.prune()
            self.assertFalse(lease.entry_root.exists())

    def test_release_launch_cannot_release_another_cache_instances_live_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            owner = PreparedRuntimeCache(root)

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").touch()

            lease = owner.acquire(
                key_payload=_key_payload(owner),
                launch_id="live-launch",
                build=build,
            )
            observer = PreparedRuntimeCache(root)

            self.assertEqual(observer.release_launch("live-launch"), 0)
            self.assertTrue(lease.lease_path.exists())
            self.assertEqual(owner.release_launch("live-launch"), 1)
            self.assertFalse(lease.lease_path.exists())

    def test_byte_quota_keeps_newest_entry_and_reports_reclaimed_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(
                Path(temporary) / "cache",
                max_entries=10,
                max_bytes=None,
            )

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").write_bytes(b"12345678")

            first = cache.acquire(
                key_payload=_key_payload(cache, source="a" * 64),
                launch_id="launch-first",
                build=build,
            )
            cache.release(first)
            second = cache.acquire(
                key_payload=_key_payload(cache, source="b" * 64),
                launch_id="launch-second",
                build=build,
            )
            cache.release(second)
            first_size = cache._logical_tree_bytes(first.entry_root)
            second_size = cache._logical_tree_bytes(second.entry_root)
            cache.max_bytes = max(first_size, second_size) + 1

            removed = cache.prune()

            self.assertIn(first.cache_key, removed)
            self.assertFalse(first.entry_root.exists())
            self.assertTrue(second.entry_root.exists())
            self.assertGreater(cache.last_prune_report["reclaimed_bytes"], 0)
            self.assertEqual(cache.last_prune_report["after_entries"], 1)

    def test_prune_never_removes_a_leased_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = PreparedRuntimeCache(Path(temporary) / "cache", max_entries=1)

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").touch()

            first = cache.acquire(
                key_payload=_key_payload(cache, source="a" * 64),
                launch_id="launch-first",
                build=build,
            )
            second = cache.acquire(
                key_payload=_key_payload(cache, source="b" * 64),
                launch_id="launch-second",
                build=build,
            )

            self.assertTrue(first.entry_root.exists())
            self.assertTrue(second.entry_root.exists())
            cache.release(first)
            cache.release(second)
            cache.prune(exclude_keys={second.cache_key})
            self.assertFalse(first.entry_root.exists())
            self.assertTrue(second.entry_root.exists())

    def test_workspace_runtime_mounts_cache_read_only_except_for_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = PreparedRuntimeCache(root / "prepared")

            def build(_entry: Path, payload: Path) -> None:
                (payload / "ready").touch()

            lease = cache.acquire(
                key_payload=_key_payload(cache),
                launch_id="launch-test",
                build=build,
            )
            source = root / "source"
            source.mkdir()
            manager = WorkspaceRuntimeManager(
                studio_root=root,
                runtime_root=root / "runtime",
                options=WorkspaceRuntimeOptions(image="workspace:test"),
                prepared_runtime_cache_root=cache.root,
            )
            launch = {
                "id": "interface-launch-test",
                "root": str(source),
                "mode": "read-only",
                "source_type": "catalog",
                "_prepared_runtime_entry": str(lease.entry_root),
                "_prepared_runtime_mount_mode": "ro",
            }
            command = manager._container_run_command(
                "/usr/bin/docker", launch, "container", 19000
            )

            self.assertIn(
                f"{lease.entry_root}:{lease.entry_root}:ro",
                command,
            )
            with self.assertRaisesRegex(PermissionError, "Only the prepared-runtime builder"):
                manager._container_run_command(
                    "/usr/bin/docker",
                    {**launch, "_prepared_runtime_mount_mode": "rw"},
                    "container",
                    19000,
                )
            builder = {
                **launch,
                "id": "prepared-build-test",
                "source_type": "prepared-runtime-build",
                "_prepared_runtime_mount_mode": "rw",
            }
            builder_command = manager._container_run_command(
                "/usr/bin/docker", builder, "builder", 19001
            )
            self.assertIn(
                f"{lease.entry_root}:{lease.entry_root}:rw",
                builder_command,
            )
            cache.release(lease)


if __name__ == "__main__":
    unittest.main()
