from __future__ import annotations

import gc
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import optpilot_studio.ui.server as studio_server
import optpilot_studio.ui.runtime_supervisor as runtime_supervisor
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot_studio.ui.server import (
    UiState,
    WorkspaceRuntimeStorageIdentityChanged,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeOptions,
    _create_ui_workspace,
    _schedule_operator_job_execution,
    _schedule_run_execution,
    _schedule_study_launch_execution,
)
from optpilot_studio.ui.runtime_supervisor import (
    StudioRuntimeSupervisorBusy,
    StudioRuntimeSupervisorClaim,
)


class StudioWorkspaceRuntimeSafetyTest(unittest.TestCase):
    def _manager(self, root: Path) -> WorkspaceRuntimeManager:
        studio_root = root / "studio"
        runtime_root = studio_root / ".optpilot-ui" / "runtime"
        studio_root.mkdir(parents=True, exist_ok=True)
        return WorkspaceRuntimeManager(
            studio_root=studio_root,
            runtime_root=runtime_root,
            options=WorkspaceRuntimeOptions(build_image=False),
        )

    def test_runtime_coordinate_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))

            with self.assertRaisesRegex(ValueError, "single non-empty path component"):
                manager._workspace_runtime_dir("../outside")

            self.assertFalse((Path(tmp_dir) / "outside").exists())

    def test_runtime_supervisor_claim_is_cross_process_and_crash_released(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ready = root / "ready"
            code = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from optpilot_studio.ui.runtime_supervisor import "
                "StudioRuntimeSupervisorClaim\n"
                "claim=StudioRuntimeSupervisorClaim.acquire(Path(sys.argv[1]))\n"
                "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(root), str(ready)]
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.is_file() and process.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("claim-holder subprocess did not become ready")
                    time.sleep(0.01)
                self.assertIsNone(process.poll())
                with self.assertRaises(StudioRuntimeSupervisorBusy):
                    StudioRuntimeSupervisorClaim.acquire(root)
            finally:
                process.kill()
                process.wait(timeout=10)

            recovered = StudioRuntimeSupervisorClaim.acquire(root)
            recovered.assert_active_for(root)
            recovered.close()

    def test_runtime_supervisor_can_use_os_local_project_control_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            studio_root = root / "synchronized-project"
            control_root = root / "local-state" / "project-a"
            studio_root.mkdir()

            claim = StudioRuntimeSupervisorClaim.acquire(
                studio_root,
                control_root=control_root,
            )
            try:
                claim.assert_active_for(studio_root)
                self.assertEqual(
                    claim.path,
                    control_root / "runtime-supervisor.lock",
                )
                legacy_lock = (
                    studio_root / ".optpilot-ui" / "runtime-supervisor.lock"
                )
                self.assertTrue(legacy_lock.is_file())
                with self.assertRaises(StudioRuntimeSupervisorBusy):
                    StudioRuntimeSupervisorClaim.acquire(studio_root)
                with self.assertRaises(StudioRuntimeSupervisorBusy):
                    StudioRuntimeSupervisorClaim.acquire(
                        studio_root,
                        control_root=control_root,
                    )
            finally:
                claim.close()

            legacy_replacement = StudioRuntimeSupervisorClaim.acquire(studio_root)
            legacy_replacement.close()

    def test_os_local_supervisor_rejects_live_pre_upgrade_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            studio_root = root / "project"
            control_root = root / "realm" / "studio" / "project-key"
            studio_root.mkdir()
            legacy_claim = StudioRuntimeSupervisorClaim.acquire(studio_root)
            try:
                with self.assertRaises(StudioRuntimeSupervisorBusy):
                    StudioRuntimeSupervisorClaim.acquire(
                        studio_root,
                        control_root=control_root,
                    )
                self.assertFalse(control_root.exists())
            finally:
                legacy_claim.close()

            migrated_claim = StudioRuntimeSupervisorClaim.acquire(
                studio_root,
                control_root=control_root,
            )
            migrated_claim.close()

    def test_runtime_supervisor_rejects_path_replaced_while_locking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            claim_path = root / ".optpilot-ui" / "runtime-supervisor.lock"
            real_flock = runtime_supervisor.fcntl.flock

            def lock_then_replace(descriptor: int, operation: int) -> None:
                real_flock(descriptor, operation)
                claim_path.unlink()
                claim_path.write_text("replacement\n", encoding="utf-8")

            with patch.object(
                runtime_supervisor.fcntl,
                "flock",
                side_effect=lock_then_replace,
            ):
                with self.assertRaisesRegex(RuntimeError, "claim was replaced"):
                    StudioRuntimeSupervisorClaim.acquire(root)

            self.assertEqual(
                claim_path.read_text(encoding="utf-8"), "replacement\n"
            )

    def test_runtime_supervisor_completes_partial_diagnostic_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            real_write = runtime_supervisor.os.write
            write_count = 0

            def partial_write(descriptor: int, payload) -> int:
                nonlocal write_count
                write_count += 1
                return real_write(descriptor, bytes(payload[:3]))

            with patch.object(
                runtime_supervisor.os,
                "write",
                side_effect=partial_write,
            ):
                claim = StudioRuntimeSupervisorClaim.acquire(root)
            try:
                diagnostic = json.loads(claim.path.read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["pid"], runtime_supervisor.os.getpid())
                self.assertEqual(diagnostic["studio_root"], str(root.resolve()))
                self.assertGreater(write_count, 1)
            finally:
                claim.close()

    def test_runtime_supervisor_can_be_retained_until_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            claim = StudioRuntimeSupervisorClaim.acquire(root)
            claim.retain_until_process_exit()
            reference = weakref.ref(claim)
            del claim
            gc.collect()

            retained = reference()
            self.assertIsNotNone(retained)
            with self.assertRaises(StudioRuntimeSupervisorBusy):
                StudioRuntimeSupervisorClaim.acquire(root)

            assert retained is not None
            retained.close()
            replacement = StudioRuntimeSupervisorClaim.acquire(root)
            replacement.close()

    def test_orphan_recovery_requires_live_matching_supervisor_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            workspace_id = "interface-launch-unowned-recovery"
            state.workspace_runtime._write_record(
                workspace_id,
                {
                    "status": "preparing",
                    "workspace_root": str(root),
                    "container_may_exist": False,
                    "transient": True,
                },
            )
            runtime_dir = state.workspace_runtime._workspace_runtime_dir(workspace_id)

            with self.assertRaisesRegex(
                RuntimeError, "runtime-supervisor claim"
            ):
                state._cleanup_orphaned_interface_runtimes()
            self.assertTrue(runtime_dir.is_dir())

            wrong_root_claim = StudioRuntimeSupervisorClaim.acquire(
                root / "another-studio"
            )
            try:
                state._runtime_supervisor_claim = wrong_root_claim
                with self.assertRaisesRegex(RuntimeError, "live matching"):
                    state._cleanup_orphaned_interface_runtimes()
                self.assertTrue(runtime_dir.is_dir())
            finally:
                wrong_root_claim.close()

            released_claim = StudioRuntimeSupervisorClaim.acquire(root)
            released_claim.close()
            state._runtime_supervisor_claim = released_claim
            with self.assertRaisesRegex(RuntimeError, "live matching"):
                state._cleanup_orphaned_interface_runtimes()
            self.assertTrue(runtime_dir.is_dir())

            claim = StudioRuntimeSupervisorClaim.acquire(root)
            try:
                state._runtime_supervisor_claim = claim
                state._cleanup_orphaned_interface_runtimes()
                self.assertFalse(runtime_dir.exists())
            finally:
                claim.close()

    def test_workspace_creation_rejects_id_as_a_storage_path(self) -> None:
        class _Runtime:
            @staticmethod
            def status(_workspace):
                return {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = SimpleNamespace(
                cwd=root,
                catalog_roots=[],
                run_roots=[],
                sessions_dir=root / "sessions",
                workspaces_dir=root / "workspaces",
                realm_runtime=None,
                workspace_runtime=_Runtime(),
            )
            state.sessions_dir.mkdir()
            state.workspaces_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "single non-empty path component"):
                _create_ui_workspace(state, {"id": "../outside"})

            self.assertFalse((root / "outside").exists())

    def test_coordination_close_quiesces_tracked_background_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state = UiState(cwd=root, catalog_roots=[], run_roots=[])
            finished = threading.Event()

            def worker() -> None:
                state._background_execution_closing.wait(timeout=5)
                finished.set()

            thread = threading.Thread(target=worker)
            with state._lock:
                state._run_execution_threads["test-run"] = thread
            thread.start()

            quiesced = state.close_coordination()

            self.assertTrue(quiesced)
            self.assertTrue(finished.is_set())
            self.assertFalse(thread.is_alive())

    def test_coordination_storage_stays_open_for_unquiesced_interfaces(self) -> None:
        state = object.__new__(UiState)
        state._background_execution_closing = threading.Event()
        state._study_launch_threads = {}
        state._run_execution_threads = {}
        state._operator_job_threads = {}
        state._lock = threading.Lock()
        state.coordination = Mock()

        quiesced = state.close_coordination(
            timeout_seconds=0.0,
            close_storage=False,
        )

        self.assertTrue(quiesced)
        state.coordination.close.assert_not_called()

    def test_interface_shutdown_joins_stopped_job_worker_not_done_event(self) -> None:
        state = object.__new__(UiState)
        state._lock = threading.Lock()
        state.interface_launches = {}
        state._cleanup_orphaned_interface_runtimes = Mock()
        release = threading.Event()
        worker_started = threading.Event()
        job = studio_server.UiLaunchJob(
            launch_id="launch-stopped-worker",
            kind="resource",
            uid="resource",
            label="Stopped worker",
            port=8000,
            status="stopped",
            launch_scope="catalog-transient",
        )
        job.execution_quiesced.set()
        job.worker_done.set()

        def worker() -> None:
            worker_started.set()
            release.wait(timeout=5)

        thread = threading.Thread(target=worker)
        job.worker_thread = thread
        state.interface_launches[job.launch_id] = job
        thread.start()
        self.assertTrue(worker_started.wait(timeout=1))
        started_at = time.monotonic()
        try:
            quiesced = state.stop_transient_interface_launches(
                timeout_seconds=0.05
            )
        finally:
            release.set()
            thread.join(timeout=1)

        self.assertFalse(quiesced)
        self.assertLess(time.monotonic() - started_at, 0.5)
        state._cleanup_orphaned_interface_runtimes.assert_not_called()

    def test_interface_shutdown_settlement_uses_entry_deadline(self) -> None:
        state = object.__new__(UiState)
        state._lock = threading.Lock()
        state.interface_launches = {}
        state._cleanup_orphaned_interface_runtimes = Mock()
        release = threading.Event()
        settlement_started = threading.Event()
        settlement_finished = threading.Event()
        job = studio_server.UiLaunchJob(
            launch_id="launch-slow-settlement",
            kind="resource",
            uid="resource",
            label="Slow settlement",
            port=8001,
            status="ready",
            launch_scope="catalog-transient",
        )
        job.execution_quiesced.set()
        job.worker_done.set()
        state.interface_launches[job.launch_id] = job

        def slow_settlement(*_args, **_kwargs):
            settlement_started.set()
            release.wait(timeout=5)
            settlement_finished.set()
            return {}

        started_at = time.monotonic()
        with patch.object(
            studio_server,
            "_settle_interface_launch",
            side_effect=slow_settlement,
        ):
            try:
                quiesced = state.stop_transient_interface_launches(
                    timeout_seconds=0.05
                )
                self.assertTrue(settlement_started.is_set())
            finally:
                release.set()
                self.assertTrue(settlement_finished.wait(timeout=1))

        self.assertFalse(quiesced)
        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_interface_shutdown_orphan_recovery_uses_entry_deadline(self) -> None:
        state = object.__new__(UiState)
        state._lock = threading.Lock()
        state.interface_launches = {}
        release = threading.Event()
        recovery_started = threading.Event()
        recovery_finished = threading.Event()

        def slow_recovery() -> None:
            recovery_started.set()
            release.wait(timeout=5)
            recovery_finished.set()

        state._cleanup_orphaned_interface_runtimes = slow_recovery
        started_at = time.monotonic()
        try:
            quiesced = state.stop_transient_interface_launches(
                timeout_seconds=0.05
            )
            self.assertTrue(recovery_started.is_set())
        finally:
            release.set()
            self.assertTrue(recovery_finished.wait(timeout=1))

        self.assertFalse(quiesced)
        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_background_quiescence_uses_one_global_deadline(self) -> None:
        now = [100.0]
        join_timeouts = []

        class _NeverStops:
            @staticmethod
            def is_alive() -> bool:
                return True

            @staticmethod
            def join(timeout: float) -> None:
                join_timeouts.append(timeout)
                now[0] += timeout

        state = object.__new__(UiState)
        state._background_execution_closing = threading.Event()
        state._study_launch_threads = {
            "study": _NeverStops(),
            "study-2": _NeverStops(),
        }
        state._run_execution_threads = {"run": _NeverStops()}
        state._operator_job_threads = {}
        state._lock = threading.Lock()

        with patch.object(studio_server.time, "monotonic", side_effect=lambda: now[0]):
            quiesced = state.quiesce_background_execution(timeout_seconds=0.1)

        self.assertFalse(quiesced)
        self.assertEqual(len(join_timeouts), 1)
        self.assertAlmostEqual(sum(join_timeouts), 0.1)

    def test_coordination_close_reports_unquiesced_background_execution(self) -> None:
        class _NeverStops:
            @staticmethod
            def is_alive() -> bool:
                return True

            @staticmethod
            def join(timeout: float) -> None:
                del timeout

        state = object.__new__(UiState)
        state._background_execution_closing = threading.Event()
        state._study_launch_threads = {"study": _NeverStops()}
        state._run_execution_threads = {}
        state._operator_job_threads = {}
        state._lock = threading.Lock()
        state.coordination = Mock()

        quiesced = state.close_coordination(timeout_seconds=0.0)

        self.assertFalse(quiesced)
        state.coordination.close.assert_not_called()

    def test_closing_state_rejects_scheduling_before_realm_reads(self) -> None:
        state = SimpleNamespace(_background_execution_closing=threading.Event())
        state._background_execution_closing.set()

        with (
            patch(
                "optpilot_studio.ui.server._study_launch_service_for_state",
                side_effect=AssertionError("Study service must not be read."),
            ) as study_service,
            patch(
                "optpilot_studio.ui.server._require_realm_runtime",
                side_effect=AssertionError("Run runtime must not be read."),
            ) as run_runtime,
            patch(
                "optpilot_studio.ui.server._operator_job_service_for_state",
                side_effect=AssertionError("Operator service must not be read."),
            ) as operator_service,
        ):
            self.assertFalse(
                _schedule_study_launch_execution(state, launch_id="study-launch")
            )
            self.assertFalse(_schedule_run_execution(state, run_id="run-closing"))
            self.assertFalse(
                _schedule_operator_job_execution(state, job_id="operator-job")
            )

        study_service.assert_not_called()
        run_runtime.assert_not_called()
        operator_service.assert_not_called()

    def test_catalog_edit_has_no_legacy_direct_copy_fallback(self) -> None:
        self.assertFalse(
            hasattr(studio_server, "_copy_catalog_source_to_workspace"),
            "Catalog editing must use the exact-selection Create Workspace service.",
        )

    def test_runtime_root_must_be_provider_owned_and_not_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            studio_root = root / "studio"
            studio_root.mkdir()
            outside = root / "outside"
            outside.mkdir()
            linked = studio_root / "runtime-link"
            linked.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                WorkspaceRuntimeManager(
                    studio_root=studio_root,
                    runtime_root=linked,
                    options=WorkspaceRuntimeOptions(build_image=False),
                )

            with self.assertRaisesRegex(ValueError, "escapes its root"):
                WorkspaceRuntimeManager(
                    studio_root=studio_root,
                    runtime_root=outside,
                    options=WorkspaceRuntimeOptions(build_image=False),
                )

            manager = WorkspaceRuntimeManager(
                studio_root=studio_root,
                runtime_root=outside,
                runtime_authority_root=root,
                options=WorkspaceRuntimeOptions(build_image=False),
            )
            self.assertEqual(manager.studio_root, studio_root.resolve())
            self.assertEqual(manager.runtime_authority_root, root.resolve())
            self.assertEqual(manager.runtime_root, outside.resolve())

    def test_runtime_root_accepts_canonical_path_beneath_aliased_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            real_parent = root / "real"
            real_parent.mkdir()
            aliased_parent = root / "alias"
            aliased_parent.symlink_to(real_parent, target_is_directory=True)
            lexical_authority = aliased_parent / "realm"
            lexical_authority.mkdir()
            studio_root = root / "studio"
            studio_root.mkdir()
            canonical_runtime = lexical_authority.resolve() / "runtime"
            canonical_runtime.mkdir()

            manager = WorkspaceRuntimeManager(
                studio_root=studio_root,
                runtime_root=canonical_runtime,
                runtime_authority_root=lexical_authority,
                options=WorkspaceRuntimeOptions(build_image=False),
            )

            self.assertEqual(
                manager.runtime_authority_root,
                lexical_authority.resolve(),
            )
            self.assertEqual(manager.runtime_root, canonical_runtime)

            outside = root / "outside"
            outside.mkdir()
            linked_runtime = lexical_authority.resolve() / "runtime-link"
            linked_runtime.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                WorkspaceRuntimeManager(
                    studio_root=studio_root,
                    runtime_root=linked_runtime,
                    runtime_authority_root=lexical_authority,
                    options=WorkspaceRuntimeOptions(build_image=False),
                )

    def test_realm_backed_ui_state_keeps_runtime_out_of_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            realm = LocalRealmRuntime.open(
                realm_root=root / "realm",
                actor_principal_id="studio-runtime-placement-test",
            )
            state_a = None
            state_a_reopened = None
            state_b = None
            try:
                studio_a = root / "studio-a"
                studio_b = root / "studio-b"
                studio_a.mkdir()
                studio_b.mkdir()
                state_a = UiState(
                    cwd=studio_a,
                    catalog_roots=[],
                    run_roots=[],
                    realm_runtime=realm,
                )
                runtime_a = state_a.runtime_dir
                runtime_a.relative_to(realm.root)
                with self.assertRaises(ValueError):
                    runtime_a.relative_to(studio_a)

                state_a.close_coordination()
                state_a = None
                state_a_reopened = UiState(
                    cwd=studio_a,
                    catalog_roots=[],
                    run_roots=[],
                    realm_runtime=realm,
                )
                self.assertEqual(state_a_reopened.runtime_dir, runtime_a)

                state_b = UiState(
                    cwd=studio_b,
                    catalog_roots=[],
                    run_roots=[],
                    realm_runtime=realm,
                )
                self.assertNotEqual(state_b.runtime_dir, runtime_a)
                self.assertEqual(
                    state_a_reopened.workspace_runtime.runtime_authority_root,
                    realm.root,
                )
            finally:
                for state in (state_a, state_a_reopened, state_b):
                    if state is not None:
                        state.close_coordination()
                realm.close()

    def test_container_names_do_not_alias_normalized_workspace_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))

            underscored = manager._container_name("workspace_a")
            dashed = manager._container_name("workspace-a")

            self.assertNotEqual(underscored, dashed)
            self.assertEqual(underscored, manager._container_name("workspace_a"))
            self.assertTrue(underscored.startswith("optpilot-ws-workspace-a-"))

            unicode_name = manager._container_name("工作区")
            self.assertTrue(unicode_name.startswith("optpilot-ws-workspace-"))
            self.assertTrue(unicode_name.isascii())

    def test_symlinked_runtime_namespace_and_record_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            outside = root / "outside"
            outside.mkdir()
            (manager.runtime_root / "linked").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(RuntimeError, "namespace must not be a symlink"):
                manager._workspace_runtime_dir("linked")

            runtime_dir = manager._ensure_workspace_runtime_dir("safe")
            target = outside / "runtime.json"
            target.write_text('{"outside": true}\n', encoding="utf-8")
            (runtime_dir / "runtime.json").symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "record must not be a symlink"):
                manager._read_record("safe")
            with self.assertRaisesRegex(RuntimeError, "record must not be a symlink"):
                manager._write_record("safe", {"status": "running"})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"outside": True}
            )

    def test_runtime_record_publication_is_atomic_and_leaves_no_staging_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))

            manager._write_record("workspace", {"status": "starting"})
            manager._write_record("workspace", {"status": "running"})

            record = manager._read_record("workspace")
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["workspace_id"], "workspace")
            self.assertEqual(
                record["schema"], "optpilot.studio-workspace-runtime.v2"
            )
            self.assertNotIn("runtime_directory_identity", record)
            self.assertEqual(len(record["runtime_claim_digest"]), 64)
            runtime_dir = manager._workspace_runtime_dir("workspace")
            self.assertEqual(
                sorted(path.name for path in runtime_dir.iterdir()),
                [".optpilot-runtime-claim.json", "runtime.json"],
            )

    def test_image_failure_remains_durably_before_container_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "prestart-image-failure",
                "root": str(workspace_root),
                "mode": "editable",
            }
            terminal_absence = {
                "terminal_confirmed": True,
                "state": "absent",
            }

            with (
                patch.object(manager, "_container_executable", return_value="docker"),
                patch.object(manager, "_container_running", return_value=False),
                patch.object(
                    manager,
                    "_remove_container",
                    return_value=terminal_absence,
                ),
                patch.object(
                    manager,
                    "_ensure_image_available",
                    side_effect=RuntimeError("injected image build failure"),
                ),
                patch.object(manager, "_container_run_command") as run_command,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected image build failure"
                ):
                    manager.start(workspace)

            record = manager._read_record("prestart-image-failure")
            self.assertEqual(record["status"], "preparing")
            self.assertIs(record["container_may_exist"], False)
            self.assertEqual(record["terminal_proof"], terminal_absence)
            self.assertNotIn("container_name", record)
            run_command.assert_not_called()

    def test_reopen_uses_durable_claim_not_prior_device_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = self._manager(root)
            first._write_record("workspace", {"status": "stopped"})
            runtime_dir = first._workspace_runtime_dir("workspace")
            observed = runtime_dir.stat()

            second = self._manager(root)
            second._observe_runtime_directory = lambda _path: SimpleNamespace(
                st_mode=observed.st_mode,
                st_dev=observed.st_dev + 1000,
                st_ino=observed.st_ino + 1000,
            )

            self.assertEqual(second._read_record("workspace")["status"], "stopped")

    def test_claim_marker_mismatch_is_not_silently_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))
            manager._write_record("workspace", {"status": "stopped"})
            runtime_dir = manager._workspace_runtime_dir("workspace")
            marker = runtime_dir / ".optpilot-runtime-claim.json"
            marker.chmod(0o600)
            marker.write_text(
                json.dumps(
                    {
                        "schema": "optpilot.studio-workspace-runtime-claim.v1",
                        "workspace_id": "another-workspace",
                        "claim_nonce": "a" * 64,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            marker.chmod(0o400)

            with self.assertRaises(WorkspaceRuntimeStorageIdentityChanged):
                self._manager(Path(tmp_dir))._read_record("workspace")

    def test_legacy_unclaimed_runtime_is_left_untouched_beside_v2_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))
            legacy = manager.runtime_root / "workspace"
            legacy.mkdir()
            legacy_record = legacy / "runtime.json"
            legacy_record.write_text(
                '{"schema":"optpilot.studio-workspace-runtime.v1"}\n',
                encoding="utf-8",
            )

            manager._write_record("workspace", {"status": "stopped"})

            current = manager._workspace_runtime_dir("workspace")
            self.assertNotEqual(current, legacy)
            self.assertEqual(
                legacy_record.read_text(encoding="utf-8"),
                '{"schema":"optpilot.studio-workspace-runtime.v1"}\n',
            )
            self.assertEqual(manager._read_record("workspace")["status"], "stopped")
            self.assertTrue((current / ".optpilot-runtime-claim.json").is_file())

    def test_primary_and_fallback_legacy_collisions_use_stable_claimed_sibling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            primary, fallback, sibling, *_rest = (
                manager._workspace_runtime_candidates("workspace")
            )
            primary.mkdir()
            fallback.mkdir()
            primary_sentinel = primary / "primary-legacy.txt"
            fallback_sentinel = fallback / "fallback-legacy.txt"
            primary_sentinel.write_text("keep primary\n", encoding="utf-8")
            fallback_sentinel.write_text("keep fallback\n", encoding="utf-8")

            manager._write_record("workspace", {"status": "stopped"})

            self.assertEqual(manager._workspace_runtime_dir("workspace"), sibling)
            self.assertEqual(
                primary_sentinel.read_text(encoding="utf-8"),
                "keep primary\n",
            )
            self.assertEqual(
                fallback_sentinel.read_text(encoding="utf-8"),
                "keep fallback\n",
            )
            self.assertEqual(
                sorted(path.name for path in primary.iterdir()),
                ["primary-legacy.txt"],
            )
            self.assertEqual(
                sorted(path.name for path in fallback.iterdir()),
                ["fallback-legacy.txt"],
            )
            self.assertTrue(
                (sibling / ".optpilot-runtime-claim.json").is_file()
            )
            self.assertEqual(
                self._manager(root)._workspace_runtime_dir("workspace"),
                sibling,
            )

    def test_start_recovers_claimed_marker_only_fallback_after_legacy_primary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            primary, fallback, *_rest = (
                manager._workspace_runtime_candidates("workspace")
            )
            primary.mkdir()
            legacy = primary / "legacy-runtime.json"
            legacy.write_text("leave this alone\n", encoding="utf-8")
            fallback.mkdir()
            manager._create_runtime_claim("workspace", fallback)
            marker = fallback / ".optpilot-runtime-claim.json"
            marker_before = marker.read_bytes()
            self.assertFalse((fallback / "runtime.json").exists())
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "workspace",
                "root": str(workspace_root),
                "mode": "editable",
            }

            with (
                patch.object(
                    manager, "_container_executable", return_value="docker"
                ),
                patch.object(
                    manager, "_container_running", return_value=False
                ),
                patch.object(
                    manager,
                    "_remove_container",
                    return_value={
                        "terminal_confirmed": True,
                        "state": "absent",
                    },
                ),
                patch.object(
                    manager,
                    "_ensure_image_available",
                    side_effect=RuntimeError("injected image preparation stop"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "injected image preparation stop"
                ):
                    manager.start(workspace)

            self.assertEqual(manager._workspace_runtime_dir("workspace"), fallback)
            self.assertEqual(marker.read_bytes(), marker_before)
            self.assertEqual(
                legacy.read_text(encoding="utf-8"),
                "leave this alone\n",
            )
            self.assertEqual(
                manager._read_record("workspace")["status"],
                "preparing",
            )

    def test_marker_only_claim_removes_running_name_before_fresh_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            runtime_dir = manager._ensure_workspace_runtime_dir("workspace")
            self.assertTrue(
                (runtime_dir / ".optpilot-runtime-claim.json").is_file()
            )
            self.assertFalse((runtime_dir / "runtime.json").exists())
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "workspace",
                "root": str(workspace_root),
                "mode": "editable",
            }
            terminal_absence = {
                "container_name": manager._container_name("workspace"),
                "terminal_confirmed": True,
                "state": "absent",
            }
            completed = subprocess.CompletedProcess(
                args=["docker", "run"],
                returncode=0,
                stdout="container-id\n",
                stderr="",
            )

            with (
                patch.object(
                    manager, "_container_executable", return_value="docker"
                ),
                patch.object(
                    manager,
                    "_container_running",
                    side_effect=[True, True],
                ),
                patch.object(
                    manager,
                    "health",
                    return_value={
                        "ok": True,
                        "available": True,
                        "executable": "docker",
                        "engine": "docker",
                    },
                ),
                patch.object(
                    manager,
                    "_remove_container",
                    return_value=terminal_absence,
                ) as remove_container,
                patch.object(manager, "_ensure_image_available") as ensure_image,
                patch.object(manager, "_host_port", return_value=19123),
                patch.object(
                    manager,
                    "_container_run_command",
                    return_value=["docker", "run"],
                ) as run_command,
                patch.object(
                    studio_server.subprocess,
                    "run",
                    return_value=completed,
                ) as run_process,
            ):
                status = manager.start(workspace)

            remove_container.assert_called_once_with(
                manager._container_name("workspace")
            )
            ensure_image.assert_called_once_with("docker")
            run_command.assert_called_once()
            self.assertEqual(
                [
                    call
                    for call in run_process.call_args_list
                    if call.args and call.args[0] == ["docker", "run"]
                ],
                [
                    call(
                        ["docker", "run"],
                        capture_output=True,
                        text=True,
                        timeout=90,
                        check=False,
                    )
                ],
            )
            self.assertEqual(status["status"], "running")
            record = manager._read_record("workspace")
            self.assertEqual(
                record["schema"], "optpilot.studio-workspace-runtime.v2"
            )
            self.assertEqual(
                record["container_name"], manager._container_name("workspace")
            )
            self.assertIs(record["container_may_exist"], True)

    def test_marker_only_claim_refuses_running_name_without_absence_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            manager._ensure_workspace_runtime_dir("workspace")
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "workspace",
                "root": str(workspace_root),
                "mode": "editable",
            }

            with (
                patch.object(
                    manager, "_container_executable", return_value="docker"
                ),
                patch.object(
                    manager, "_container_running", return_value=True
                ),
                patch.object(
                    manager,
                    "_remove_container",
                    return_value={
                        "terminal_confirmed": True,
                        "state": "stopped",
                    },
                ),
                patch.object(manager, "_ensure_image_available") as ensure_image,
                patch.object(manager, "_container_run_command") as run_command,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unrecorded workspace container was removed",
                ):
                    manager.start(workspace)

            ensure_image.assert_not_called()
            run_command.assert_not_called()
            self.assertFalse(
                (
                    manager._workspace_runtime_dir("workspace")
                    / "runtime.json"
                ).exists()
            )

    def test_valid_v2_record_reuses_its_running_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "workspace",
                "root": str(workspace_root),
                "mode": "editable",
            }
            container_name = manager._container_name("workspace")
            manager._write_record(
                "workspace",
                {
                    "container_name": container_name,
                    "container_may_exist": True,
                    "host_port": 19124,
                    "image": manager.options.image,
                    "status": "running",
                    "workspace_root": str(workspace_root.resolve()),
                },
            )

            with (
                patch.object(
                    manager, "_container_executable", return_value="docker"
                ),
                patch.object(
                    manager, "_container_running", return_value=True
                ),
                patch.object(
                    manager,
                    "health",
                    return_value={
                        "ok": True,
                        "available": True,
                        "executable": "docker",
                        "engine": "docker",
                    },
                ),
                patch.object(manager, "_remove_container") as remove_container,
                patch.object(manager, "_ensure_image_available") as ensure_image,
                patch.object(manager, "_container_run_command") as run_command,
            ):
                status = manager.start(workspace)

            remove_container.assert_not_called()
            ensure_image.assert_not_called()
            run_command.assert_not_called()
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["container_name"], container_name)
            self.assertEqual(
                manager._read_record("workspace")["container_name"],
                container_name,
            )

    def test_terminal_v2_record_never_reuses_a_reappeared_container_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(root)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            workspace = {
                "id": "workspace",
                "root": str(workspace_root),
                "mode": "editable",
            }
            container_name = manager._container_name("workspace")
            manager._write_record(
                "workspace",
                {
                    "container_name": container_name,
                    "container_may_exist": True,
                    "host_port": 19124,
                    "image": manager.options.image,
                    "status": "stopped",
                    "workspace_root": str(workspace_root.resolve()),
                    "terminal_proof": {
                        "terminal_confirmed": True,
                        "state": "absent",
                    },
                },
            )
            terminal_absence = {
                "container_name": container_name,
                "terminal_confirmed": True,
                "state": "absent",
            }
            completed = subprocess.CompletedProcess(
                args=["docker", "run"],
                returncode=0,
                stdout="replacement-container-id\n",
                stderr="",
            )

            with (
                patch.object(
                    manager, "_container_executable", return_value="docker"
                ),
                patch.object(
                    manager,
                    "_container_running",
                    side_effect=[True, True],
                ),
                patch.object(
                    manager,
                    "health",
                    return_value={
                        "ok": True,
                        "available": True,
                        "executable": "docker",
                        "engine": "docker",
                    },
                ),
                patch.object(
                    manager,
                    "_remove_container",
                    return_value=terminal_absence,
                ) as remove_container,
                patch.object(manager, "_ensure_image_available"),
                patch.object(manager, "_host_port", return_value=19125),
                patch.object(
                    manager,
                    "_container_run_command",
                    return_value=["docker", "run"],
                ),
                patch.object(
                    studio_server.subprocess,
                    "run",
                    return_value=completed,
                ),
            ):
                status = manager.start(workspace)

            remove_container.assert_called_once_with(container_name)
            self.assertEqual(status["status"], "running")
            refreshed = manager._read_record("workspace")
            self.assertEqual(refreshed["status"], "running")
            self.assertNotIn("terminal_proof", refreshed)

    def test_runtime_namespace_collision_chain_exhaustion_preserves_every_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))
            candidates = manager._workspace_runtime_candidates("workspace")
            for index, candidate in enumerate(candidates):
                candidate.mkdir()
                (candidate / "foreign.txt").write_text(
                    f"foreign-{index}\n",
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(
                RuntimeError, "allocation is exhausted"
            ):
                manager._ensure_workspace_runtime_dir("workspace")

            for index, candidate in enumerate(candidates):
                self.assertEqual(
                    sorted(path.name for path in candidate.iterdir()),
                    ["foreign.txt"],
                )
                self.assertEqual(
                    (candidate / "foreign.txt").read_text(encoding="utf-8"),
                    f"foreign-{index}\n",
                )

    def test_cleanup_refuses_unmarked_or_replaced_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))
            unmarked = manager._workspace_runtime_dir("unmarked")
            unmarked.mkdir()
            (unmarked / "do-not-delete.txt").write_text("keep\n", encoding="utf-8")

            self.assertFalse(manager.delete("unmarked"))
            self.assertTrue((unmarked / "do-not-delete.txt").is_file())

            manager._write_record("owned", {"status": "stopped"})
            owned = manager._workspace_runtime_dir("owned")
            moved = manager.runtime_root / "owned-original"
            owned.rename(moved)
            owned.mkdir()
            shutil.copy2(moved / "runtime.json", owned / "runtime.json")
            shutil.copy2(
                moved / ".optpilot-runtime-claim.json",
                owned / ".optpilot-runtime-claim.json",
            )

            with self.assertRaises(
                WorkspaceRuntimeStorageIdentityChanged
            ) as changed:
                manager._read_record("owned")
            self.assertEqual(changed.exception.workspace_id, "owned")
            self.assertEqual(changed.exception.runtime_path, owned)
            self.assertIn("source workspace was not modified", str(changed.exception))
            with self.assertRaisesRegex(RuntimeError, "directory identity changed"):
                manager.delete("owned")
            self.assertTrue(owned.exists())

    def test_cleanup_holds_owned_directory_when_namespace_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = self._manager(Path(tmp_dir))
            manager._write_record("racing", {"status": "running"})
            owned = manager._workspace_runtime_dir("racing")
            (owned / "owned.log").write_text("delete me\n", encoding="utf-8")
            moved = manager.runtime_root / "racing-original"

            def replace_during_stop(_workspace_id: str):
                owned.rename(moved)
                owned.mkdir()
                (owned / "replacement.txt").write_text("keep me\n", encoding="utf-8")
                return {"terminal_confirmed": True, "state": "absent"}

            manager._remove_container = replace_during_stop  # type: ignore[method-assign]

            self.assertFalse(manager.delete("racing"))
            self.assertEqual(
                (owned / "replacement.txt").read_text(encoding="utf-8"), "keep me\n"
            )
            self.assertTrue(moved.is_dir())
            self.assertEqual(list(moved.iterdir()), [])


class StudioWorkspaceRuntimeHealthProbeTest(unittest.TestCase):
    """An unanswered readiness probe must never read as a stopped engine."""

    def _manager(
        self, root: Path, *, executable: Path, timeout_seconds: int = 10
    ) -> WorkspaceRuntimeManager:
        studio_root = root / "studio"
        studio_root.mkdir(parents=True, exist_ok=True)
        return WorkspaceRuntimeManager(
            studio_root=studio_root,
            runtime_root=studio_root / ".optpilot-ui" / "runtime",
            options=WorkspaceRuntimeOptions(
                executable=str(executable),
                image="fake-workspace:latest",
                build_image=False,
                health_probe_timeout_seconds=timeout_seconds,
            ),
        )

    def _slow_executable(self, root: Path, seconds: float) -> Path:
        executable = root / "slow_container.py"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import time\n"
            f"time.sleep({seconds})\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def test_health_reports_probe_timeout_separately_from_stopped_engine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager = self._manager(
                root,
                executable=self._slow_executable(root, 30),
                timeout_seconds=1,
            )

            health = manager.health()

        self.assertFalse(health["ok"])
        self.assertTrue(health["available"])
        self.assertTrue(health["probe_timed_out"])
        self.assertIn("did not answer within 1s", health["error"])
        self.assertIn("--version", health["error"])
        self.assertNotIn(str(root), health["error"])

    def test_health_probe_timeout_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            executable = self._slow_executable(root, 0)
            manager = self._manager(root, executable=executable, timeout_seconds=42)
            completed = subprocess.CompletedProcess(
                args=[str(executable)], returncode=0, stdout="fake 1.0", stderr=""
            )

            with patch.object(
                studio_server.subprocess, "run", return_value=completed
            ) as run:
                health = manager.health()

        self.assertTrue(health["ok"])
        self.assertFalse(health["probe_timed_out"])
        self.assertEqual(
            [invocation.kwargs["timeout"] for invocation in run.call_args_list],
            [42, 42],
        )

    def test_health_probe_timeout_reads_environment_override(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPTPILOT_WORKSPACE_RUNTIME_HEALTH_TIMEOUT_SECONDS": "25"},
            clear=False,
        ):
            options = WorkspaceRuntimeOptions.from_env()

        self.assertEqual(options.health_probe_timeout_seconds, 25)

    def test_stopped_engine_still_reports_no_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            executable = self._slow_executable(root, 0)
            manager = self._manager(root, executable=executable)
            refused = subprocess.CompletedProcess(
                args=[str(executable), "info"],
                returncode=1,
                stdout="",
                stderr="Cannot connect to the Docker daemon.",
            )
            version = subprocess.CompletedProcess(
                args=[str(executable), "--version"],
                returncode=0,
                stdout="fake 1.0",
                stderr="",
            )

            with patch.object(
                studio_server.subprocess, "run", side_effect=[version, refused]
            ):
                health = manager.health()

        self.assertFalse(health["ok"])
        self.assertTrue(health["available"])
        self.assertFalse(health["probe_timed_out"])
        self.assertIn("Cannot connect to the Docker daemon.", health["error"])

    def test_interface_launch_reason_distinguishes_timeout_from_stopped(self) -> None:
        timed_out = SimpleNamespace(
            workspace_runtime=SimpleNamespace(
                health=lambda: {
                    "ok": False,
                    "available": True,
                    "probe_timed_out": True,
                }
            )
        )
        stopped = SimpleNamespace(
            workspace_runtime=SimpleNamespace(
                health=lambda: {
                    "ok": False,
                    "available": True,
                    "probe_timed_out": False,
                }
            )
        )

        timeout_capability = studio_server._interface_launch_runtime_capability(
            timed_out
        )
        stopped_capability = studio_server._interface_launch_runtime_capability(stopped)

        self.assertFalse(timeout_capability["eligible"])
        self.assertTrue(timeout_capability["probe_timed_out"])
        self.assertIn("did not answer the readiness probe", timeout_capability["reason"])
        self.assertNotIn("Start Docker or Podman", timeout_capability["reason"])
        self.assertFalse(stopped_capability["probe_timed_out"])
        self.assertIn("Start Docker or Podman", stopped_capability["reason"])


if __name__ == "__main__":
    unittest.main()
