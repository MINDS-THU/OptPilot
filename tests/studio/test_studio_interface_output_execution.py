from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optpilot.prepared_runtime_cache import PreparedRuntimeLease
from optpilot.realm.refs import SnapshotRef
from optpilot.realm.run_closure import InterfaceOutputActionSpec
import optpilot_studio.ui.interface_output_execution as output_execution
from optpilot_studio.ui.interface_output_execution import (
    ImmutableInterfaceOutputTree,
    InterfaceOutputExecutionRejected,
    InterfaceOutputExecutionRequest,
    InterfaceOutputExecutionUnavailable,
    InterfaceOutputSnapshotLimits,
    LocalContainerExecutionLimits,
    LocalContainerInterfaceOutputExecutor,
    OriginatingPreparedRuntime,
    OUTPUT_EXECUTION_REQUEST_SCHEMA,
    export_execution_result_tree,
    export_execution_result_tree_at,
    failed_execution_result,
    snapshot_interface_output_tree,
    write_execution_result,
    write_execution_result_at,
)


class _ImmediateProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _WaitingProcess(_ImmediateProcess):
    def __init__(self):
        super().__init__()
        self.returncode = None


#: What the executor records for the stub engine. It deliberately pins the
#: canonical path of the binary it is given -- Path.resolve() at construction
#: -- so that the file it will keep invoking is the real one, not a symlink
#: that could later point somewhere else. On Linux /bin is itself a symlink
#: into /usr/bin, so the "/bin/echo" handed in comes back as "/usr/bin/echo"
#: there, and unchanged on macOS. Every previous CI run died before this file
#: ran, so the hard-coded "/bin/echo" in these assertions was only ever
#: compared on developer Macs; the first full-suite run on Linux failed all
#: three command assertions at once.
_RESOLVED_ENGINE = str(Path("/bin/echo").resolve())


class StudioInterfaceOutputExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "immutable-output"
        self.output.mkdir()
        (self.output / "run.py").write_text("print('ok')\n", encoding="utf-8")
        self.entry = self.root / "prepared-entry"
        self.payload = self.entry / "payload"
        self.payload.mkdir(parents=True)
        self.lease_path = self.entry / "leases" / "launch.json"
        self.lease_path.parent.mkdir()
        self.lease_path.write_text("{}\n", encoding="utf-8")
        self.lease = PreparedRuntimeLease(
            cache_key="c" * 64,
            launch_id="launch-123",
            entry_root=self.entry,
            payload_root=self.payload,
            lease_path=self.lease_path,
            cache_status="hit",
            manifest={
                "key_payload": {
                    "provider": {
                        "imageDigest": "sha256:" + "d" * 64,
                    }
                }
            },
        )
        self.runtime = OriginatingPreparedRuntime.from_lease(self.lease)
        self.source = ImmutableInterfaceOutputTree(
            SnapshotRef("a" * 64), self.output
        )
        self.action = InterfaceOutputActionSpec(
            action_id="run",
            label="Run",
            command=("python", "run.py"),
            cwd=".",
            timeout_seconds=30,
            accepts_arguments=True,
        )
        self.request = InterfaceOutputExecutionRequest(
            request_id="request-1",
            action_id="run",
            output_path=".",
            arguments=("--seed", "7"),
        )
        self.executor = LocalContainerInterfaceOutputExecutor(
            executable="/bin/echo",
            runtime_root=self.root / "executions",
            limits=LocalContainerExecutionLimits(capture_bytes=1024),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_request_is_exact_bounded_and_cannot_supply_a_command(self) -> None:
        decoded = InterfaceOutputExecutionRequest.from_dict(
            {
                "schema_version": OUTPUT_EXECUTION_REQUEST_SCHEMA,
                "request_id": "request-1",
                "action_id": "run",
                "output_path": "generation-1",
                "arguments": ["--seed", "7"],
                "timeout_seconds": 12,
            }
        )
        self.assertEqual(decoded.output_path, "generation-1")
        self.assertEqual(decoded.arguments, ("--seed", "7"))
        self.assertEqual(decoded.timeout_seconds, 12)

        invalid = decoded.to_dict()
        invalid["command"] = ["sh", "-c", "id"]
        with self.assertRaises(InterfaceOutputExecutionRejected):
            InterfaceOutputExecutionRequest.from_dict(invalid)
        invalid = decoded.to_dict()
        invalid["image"] = "attacker/image:latest"
        with self.assertRaises(InterfaceOutputExecutionRejected):
            InterfaceOutputExecutionRequest.from_dict(invalid)
        invalid = decoded.to_dict()
        invalid["arguments"] = "--not-a-vector"
        with self.assertRaises(InterfaceOutputExecutionRejected):
            InterfaceOutputExecutionRequest.from_dict(invalid)
        for invalid_timeout in (True, 0, -1, 1.5, "5"):
            invalid = decoded.to_dict()
            invalid["timeout_seconds"] = invalid_timeout
            with self.subTest(timeout_seconds=invalid_timeout):
                with self.assertRaises(InterfaceOutputExecutionRejected):
                    InterfaceOutputExecutionRequest.from_dict(invalid)
        with self.assertRaises(InterfaceOutputExecutionRejected):
            InterfaceOutputExecutionRequest(
                "request-2", "run", "../outside", ()
            )
        with self.assertRaises(InterfaceOutputExecutionRejected):
            InterfaceOutputExecutionRequest(
                "request-2",
                "run",
                ".",
                tuple("x" * 600 for _ in range(65)),
            )

    def test_request_timeout_can_only_narrow_the_authored_maximum(self) -> None:
        narrowed = InterfaceOutputExecutionRequest(
            request_id="request-shorter",
            action_id="run",
            output_path=".",
            timeout_seconds=5,
        )
        process = _ImmediateProcess(returncode=0)
        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results"),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=narrowed,
            )
        self.assertEqual(result.status, "succeeded")

        extended = InterfaceOutputExecutionRequest(
            request_id="request-longer",
            action_id="run",
            output_path=".",
            timeout_seconds=self.action.timeout_seconds + 1,
        )
        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen"
            ) as popen,
            self.assertRaisesRegex(
                InterfaceOutputExecutionRejected,
                "may not exceed",
            ),
        ):
            self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=extended,
            )
        popen.assert_not_called()

    def test_prepared_runtime_comes_from_immutable_provider_image(self) -> None:
        self.assertEqual(
            self.runtime.image_digest,
            "sha256:" + "d" * 64,
        )
        self.assertEqual(self.runtime.payload_root, self.payload.resolve())

        invalid = PreparedRuntimeLease(
            cache_key="c" * 64,
            launch_id="launch-123",
            entry_root=self.entry,
            payload_root=self.payload,
            lease_path=self.lease_path,
            cache_status="hit",
            manifest={"key_payload": {"provider": {"imageReference": "latest"}}},
        )
        with self.assertRaises(InterfaceOutputExecutionUnavailable):
            OriginatingPreparedRuntime.from_lease(invalid)

    def test_container_command_is_a_networkless_sibling_with_only_owned_mounts(self) -> None:
        command = self.executor._container_command(
            container_name="output-test",
            source=self.source,
            runtime=self.runtime,
        )
        action_command = self.executor._action_command(
            container_name="output-test",
            runtime=self.runtime,
            action=self.action,
            request=self.request,
        )
        text = "\n".join(command)
        self.assertEqual(command[:3], [_RESOLVED_ENGINE, "run", "--detach"])
        self.assertIn("--network\nnone", text)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop\nALL", text)
        self.assertIn("no-new-privileges", command)
        result_tmpfs = (
            "/optpilot/results:rw,nosuid,nodev,noexec,"
            "size=67108864,nr_inodes=10001,mode=1777"
        )
        self.assertEqual(command[command.index(result_tmpfs) - 1], "--tmpfs")
        self.assertNotIn("type=tmpfs,destination=/optpilot/results", text)
        self.assertIn(
            f"type=bind,source={self.output.resolve()},target=/optpilot/output,readonly",
            command,
        )
        self.assertIn(
            f"type=bind,source={self.entry.resolve()},target={self.entry.resolve()},readonly",
            command,
        )
        self.assertIn("OPTPILOT_PREPARED_RUNTIME_ACCESS=read-only", command)
        image_digest = "sha256:" + "d" * 64
        self.assertIn(image_digest, command)
        entrypoint_index = command.index("--entrypoint")
        self.assertEqual(command[entrypoint_index + 1], "python3")
        self.assertEqual(command[entrypoint_index + 2], image_digest)
        self.assertEqual(action_command[:2], [_RESOLVED_ENGINE, "exec"])
        self.assertIn("--workdir", action_command)
        self.assertIn("/optpilot/output", action_command)
        self.assertEqual(
            action_command[-5:],
            ["output-test", "python", "run.py", "--seed", "7"],
        )
        self.assertNotIn("OPENROUTER_API_KEY", "\n".join(action_command))
        self.assertNotIn("OPENROUTER_API_KEY", text)
        self.assertNotIn(str(self.lease_path), text)
        self.assertNotIn("--env-file", command)

    def test_execute_returns_bounded_path_free_evidence_and_result_digests(self) -> None:
        process = _ImmediateProcess(stdout=b"x" * 2048, stderr=b"warning", returncode=0)

        def copy_results(_container_name: str, destination: Path) -> None:
            (destination / "summary.json").write_text(
                '{"metric":1}\n', encoding="utf-8"
            )

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results", side_effect=copy_results),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.stdout.encode("utf-8")), 1024)
        self.assertTrue(result.stdout_truncated)
        self.assertEqual(result.result_files[0].relative_path, "summary.json")
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("OPENROUTER_API_KEY", serialized)

        path = self.root / "published" / "request-1.json"
        write_execution_result(path, result)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["status"],
            "succeeded",
        )
        self.assertIsNotNone(result.local_results)
        assert result.local_results is not None
        retained_root = result.local_results.owned_root
        exported = self.root / "broker" / "results" / "request-1"
        export_execution_result_tree(result, exported)
        self.assertEqual(
            (exported / "summary.json").read_text(encoding="utf-8"),
            '{"metric":1}\n',
        )
        result.cleanup()
        self.assertFalse(retained_root.exists())

    def test_keeper_remains_live_until_streamed_results_are_retained(self) -> None:
        process = _ImmediateProcess(returncode=0)
        lifecycle: list[str] = []

        def start(_command: list[str]) -> None:
            lifecycle.append("keeper-started")

        def copy_results(_container_name: str, destination: Path) -> None:
            self.assertEqual(lifecycle, ["keeper-started"])
            lifecycle.append("results-retained")
            (destination / "result.txt").write_text("kept\n", encoding="utf-8")

        def remove(_container_name: str) -> None:
            lifecycle.append("keeper-removed")

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container", side_effect=start),
            patch.object(self.executor, "_copy_results", side_effect=copy_results),
            patch.object(self.executor, "_force_remove", side_effect=remove),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        try:
            self.assertEqual(
                lifecycle,
                ["keeper-started", "results-retained", "keeper-removed"],
            )
            self.assertEqual(result.result_files[0].relative_path, "result.txt")
        finally:
            result.cleanup()

    def test_streamed_result_archive_is_validated_before_private_extraction(
        self,
    ) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            directory = tarfile.TarInfo("./nested")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o777
            archive.addfile(directory)
            file_info = tarfile.TarInfo("./nested/result.txt")
            content = b"retained\n"
            file_info.size = len(content)
            file_info.mode = 0o777
            archive.addfile(file_info, io.BytesIO(content))
        process = _ImmediateProcess(stdout=payload.getvalue(), returncode=0)
        destination = self.root / "streamed-results"
        destination.mkdir()
        observed: list[str] = []

        def start(command, **_kwargs):
            observed.extend(command)
            return process

        with patch(
            "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
            side_effect=start,
        ):
            self.executor._copy_results("live-keeper", destination)

        self.assertEqual(
            (destination / "nested" / "result.txt").read_text(encoding="utf-8"),
            "retained\n",
        )
        self.assertEqual((destination / "nested").stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (destination / "nested" / "result.txt").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(observed[:2], [_RESOLVED_ENGINE, "exec"])
        self.assertIn("live-keeper", observed)
        self.assertIn("python3", observed)
        self.assertNotIn("cp", observed)

        unsafe = io.BytesIO()
        with tarfile.open(fileobj=unsafe, mode="w") as archive:
            escaped = tarfile.TarInfo("../escaped.txt")
            escaped.size = 1
            archive.addfile(escaped, io.BytesIO(b"x"))
        rejected_destination = self.root / "rejected-results"
        rejected_destination.mkdir()
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable,
            "unsafe path",
        ):
            output_execution._extract_result_archive(
                io.BytesIO(unsafe.getvalue()),
                rejected_destination,
                limits=self.executor.limits.result_tree_limits(),
            )
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_process_evidence_redacts_provider_paths_and_container_name(self) -> None:
        process = _ImmediateProcess(returncode=0)
        observed_container_name = ""

        def start(command, **_kwargs):
            nonlocal observed_container_name
            observed_container_name = command[command.index("python") - 1]
            leaked = "\n".join(
                (
                    str(self.source.root_path),
                    str(self.runtime.entry_root),
                    str(self.runtime.payload_root),
                    str(self.executor.runtime_root),
                    observed_container_name,
                )
            ).encode("utf-8")
            process.stdout = io.BytesIO(leaked)
            process.stderr = io.BytesIO(
                f"failed below {self.executor.runtime_root}/execution-secret".encode(
                    "utf-8"
                )
            )
            return process

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                side_effect=start,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results"),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        try:
            serialized = json.dumps(result.to_dict(), sort_keys=True)
            for coordinate in (
                str(self.source.root_path),
                str(self.runtime.entry_root),
                str(self.runtime.payload_root),
                str(self.executor.runtime_root),
                observed_container_name,
            ):
                self.assertNotIn(coordinate, serialized)
            self.assertIn("<interface-output>", result.stdout)
            self.assertIn("<prepared-runtime>", result.stdout)
            self.assertIn("<prepared-runtime-payload>", result.stdout)
            self.assertIn("<execution-runtime>", result.stdout)
            self.assertIn("<execution-container>", result.stdout)
            self.assertIn("<execution-runtime>/execution-secret", result.stderr)
        finally:
            result.cleanup()

    def test_nonzero_exit_is_code_failure_but_cleanup_failure_is_infrastructure(self) -> None:
        process = _ImmediateProcess(stderr=b"Traceback", returncode=2)
        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results"),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            failed = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.failure_code, "nonzero_exit")

        process = _ImmediateProcess(returncode=0)
        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results"),
            patch.object(self.executor, "_force_remove"),
            patch.object(
                self.executor,
                "_require_container_absent",
                side_effect=InterfaceOutputExecutionUnavailable("still live"),
            ),
        ):
            uncertain = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        self.assertEqual(uncertain.status, "infrastructure_failed")
        self.assertEqual(uncertain.failure_code, "container_cleanup_unconfirmed")

    def test_container_engine_exit_125_is_not_reported_as_generated_code_failure(
        self,
    ) -> None:
        process = _ImmediateProcess(stderr=b"container engine error", returncode=125)
        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results") as copy_results,
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        self.assertEqual(result.status, "infrastructure_failed")
        self.assertEqual(result.failure_code, "container_start_failed")
        self.assertEqual(result.exit_code, 125)
        copy_results.assert_not_called()

    def test_action_must_explicitly_allow_runtime_arguments(self) -> None:
        action = InterfaceOutputActionSpec(
            action_id="run",
            label="Run",
            command=("python", "run.py"),
            timeout_seconds=30,
            accepts_arguments=False,
        )
        with self.assertRaisesRegex(
            InterfaceOutputExecutionRejected, "does not accept"
        ):
            self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=action,
                request=self.request,
            )
        mismatch = InterfaceOutputExecutionRequest(
            request_id="request-other-action",
            action_id="inspect",
            output_path=".",
        )
        with self.assertRaisesRegex(
            InterfaceOutputExecutionRejected, "another action"
        ):
            self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=mismatch,
            )

    def test_projection_replacement_is_rejected_before_execution(self) -> None:
        moved = self.root / "moved"
        self.output.rename(moved)
        self.output.mkdir()
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "identity changed"
        ):
            self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )

    def test_result_symlinks_are_rejected(self) -> None:
        result_root = self.root / "unsafe-results"
        result_root.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (result_root / "escape").symlink_to(outside)
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "unsafe"
        ):
            self.executor._inspect_results(result_root)

    def test_result_tree_limits_bound_empty_file_count_depth_and_names(self) -> None:
        too_many = self.root / "too-many-results"
        too_many.mkdir()
        for index in range(4):
            (too_many / f"{index}.txt").touch()
        entry_bounded = LocalContainerInterfaceOutputExecutor(
            executable="/bin/echo",
            runtime_root=self.root / "entry-bounded-executions",
            limits=LocalContainerExecutionLimits(
                result_entries=3,
                capture_bytes=1024,
            ),
        )
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "over-limit"
        ):
            entry_bounded._inspect_results(too_many)

        too_deep = self.root / "too-deep-results"
        (too_deep / "a" / "b").mkdir(parents=True)
        (too_deep / "a" / "b" / "value.txt").touch()
        depth_bounded = LocalContainerInterfaceOutputExecutor(
            executable="/bin/echo",
            runtime_root=self.root / "depth-bounded-executions",
            limits=LocalContainerExecutionLimits(
                result_entries=100,
                result_depth=2,
                capture_bytes=1024,
            ),
        )
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "over-limit"
        ):
            depth_bounded._inspect_results(too_deep)

        long_name = self.root / "long-name-results"
        long_name.mkdir()
        (long_name / ("x" * 17)).touch()
        name_bounded = LocalContainerInterfaceOutputExecutor(
            executable="/bin/echo",
            runtime_root=self.root / "name-bounded-executions",
            limits=LocalContainerExecutionLimits(
                result_entries=100,
                result_component_bytes=16,
                capture_bytes=1024,
            ),
        )
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "over-limit"
        ):
            name_bounded._inspect_results(long_name)

    def test_snapshot_is_deterministic_read_only_and_explicitly_retired(self) -> None:
        live_root = self.root / "live"
        selected = live_root / "generations" / "demo"
        (selected / "nested").mkdir(parents=True)
        script = selected / "run.py"
        script.write_text("print('hello')\n", encoding="utf-8")
        script.chmod(0o755)
        (selected / "nested" / "data.txt").write_text("data\n", encoding="utf-8")
        snapshots = self.root / "snapshots"

        first = snapshot_interface_output_tree(
            live_root,
            snapshots,
            "snapshot-1",
            relative_path="generations/demo",
        )
        second = snapshot_interface_output_tree(
            live_root,
            snapshots,
            "snapshot-2",
            relative_path="generations/demo",
        )
        self.assertEqual(first.snapshot_ref, second.snapshot_ref)
        self.assertEqual(
            (first.root_path / "nested" / "data.txt").read_text(encoding="utf-8"),
            "data\n",
        )
        self.assertEqual(first.root_path.stat().st_mode & 0o777, 0o500)
        self.assertEqual((first.root_path / "run.py").stat().st_mode & 0o777, 0o500)
        self.assertEqual(
            (first.root_path / "nested" / "data.txt").stat().st_mode & 0o777,
            0o400,
        )
        first_root = first.root_path
        second_root = second.root_path
        first.cleanup()
        second.cleanup()
        self.assertFalse(first_root.exists())
        self.assertFalse(second_root.exists())

    def test_snapshot_rejects_symlinks_special_files_and_limits(self) -> None:
        live_root = self.root / "unsafe-live"
        selected = live_root / "selection"
        selected.mkdir(parents=True)
        outside = self.root / "outside-secret"
        outside.write_text("secret", encoding="utf-8")
        (selected / "escape").symlink_to(outside)
        with self.assertRaisesRegex(
            InterfaceOutputExecutionRejected, "symbolic links"
        ):
            snapshot_interface_output_tree(
                live_root,
                self.root / "unsafe-snapshots",
                "unsafe-link",
                relative_path="selection",
            )
        (selected / "escape").unlink()

        if hasattr(os, "mkfifo"):
            os.mkfifo(selected / "pipe")
            with self.assertRaisesRegex(
                InterfaceOutputExecutionRejected, "regular files"
            ):
                snapshot_interface_output_tree(
                    live_root,
                    self.root / "unsafe-snapshots",
                    "unsafe-fifo",
                    relative_path="selection",
                )
            (selected / "pipe").unlink()

        (selected / "one").write_text("1", encoding="utf-8")
        (selected / "two").write_text("2", encoding="utf-8")
        with self.assertRaisesRegex(
            InterfaceOutputExecutionRejected, "entry limit"
        ):
            snapshot_interface_output_tree(
                live_root,
                self.root / "unsafe-snapshots",
                "too-many",
                relative_path="selection",
                limits=InterfaceOutputSnapshotLimits(max_entries=1),
            )

    def test_snapshot_detects_content_race_and_selection_symlink_swap(self) -> None:
        live_root = self.root / "racing-live"
        selected = live_root / "selection"
        selected.mkdir(parents=True)
        source_file = selected / "value.txt"
        source_file.write_text("before\n", encoding="utf-8")
        original_copy = output_execution._copy_snapshot_file

        def mutate_after_copy(**kwargs):
            entry = original_copy(**kwargs)
            source_file.write_text("after\n", encoding="utf-8")
            return entry

        with (
            patch.object(
                output_execution,
                "_copy_snapshot_file",
                side_effect=mutate_after_copy,
            ),
            self.assertRaisesRegex(
                InterfaceOutputExecutionRejected, "changed during snapshot"
            ),
        ):
            snapshot_interface_output_tree(
                live_root,
                self.root / "race-snapshots",
                "race",
                relative_path="selection",
            )

        replacement = self.root / "replacement"
        replacement.mkdir()
        selected.rename(live_root / "old-selection")
        selected.symlink_to(replacement, target_is_directory=True)
        with self.assertRaisesRegex(
            InterfaceOutputExecutionRejected, "contained real directory"
        ):
            snapshot_interface_output_tree(
                live_root,
                self.root / "race-snapshots",
                "swap",
                relative_path="selection",
            )

        swap_root = self.root / "mid-swap-live"
        swap_selected = swap_root / "outer" / "selection"
        swap_selected.mkdir(parents=True)
        (swap_selected / "value.txt").write_text("stable\n", encoding="utf-8")
        attacker_root = self.root / "attacker-root"
        (attacker_root / "selection").mkdir(parents=True)

        def swap_intermediate_after_copy(**kwargs):
            entry = original_copy(**kwargs)
            (swap_root / "outer").rename(swap_root / "old-outer")
            (swap_root / "outer").symlink_to(attacker_root, target_is_directory=True)
            return entry

        with (
            patch.object(
                output_execution,
                "_copy_snapshot_file",
                side_effect=swap_intermediate_after_copy,
            ),
            self.assertRaisesRegex(
                InterfaceOutputExecutionRejected, "contained real directory"
            ),
        ):
            snapshot_interface_output_tree(
                swap_root,
                self.root / "race-snapshots",
                "mid-swap",
                relative_path="outer/selection",
            )

    def test_cancel_timeout_and_cleanup_commands_are_bounded(self) -> None:
        cancelled_process = _WaitingProcess()
        removed: list[str] = []

        def remove_cancelled(name: str) -> None:
            removed.append(name)
            cancelled_process.returncode = -9

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=cancelled_process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(
                self.executor,
                "_force_remove",
                side_effect=remove_cancelled,
            ),
            patch.object(self.executor, "_require_container_absent"),
        ):
            cancelled = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
                should_cancel=lambda: True,
            )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertGreaterEqual(len(removed), 2)

        timeout_action = InterfaceOutputActionSpec(
            action_id="run",
            label="Run",
            command=("python", "run.py"),
            timeout_seconds=30,
            accepts_arguments=True,
        )
        timeout_request = InterfaceOutputExecutionRequest(
            request_id="request-short-timeout",
            action_id="run",
            output_path=".",
            timeout_seconds=1,
        )
        timed_process = _WaitingProcess()

        def remove_timed(_name: str) -> None:
            timed_process.returncode = -9

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=timed_process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_force_remove", side_effect=remove_timed),
            patch.object(self.executor, "_require_container_absent"),
            patch.object(
                output_execution.time,
                "monotonic",
                side_effect=(0.0, 2.0, 3.0),
            ),
        ):
            timed = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=timeout_action,
                request=timeout_request,
            )
        self.assertEqual(timed.status, "timed_out")
        self.assertEqual(timed.failure_code, "timeout")

        completed = [
            output_execution.subprocess.CompletedProcess([], 0, "", ""),
            output_execution.subprocess.CompletedProcess([], 1, "", "not found"),
        ]
        with patch.object(
            output_execution.subprocess,
            "run",
            side_effect=completed,
        ) as run:
            self.executor._force_remove("bounded-container")
            self.executor._require_container_absent("bounded-container")
        self.assertEqual(
            run.call_args_list[0].args[0],
            [_RESOLVED_ENGINE, "rm", "-f", "bounded-container"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [_RESOLVED_ENGINE, "inspect", "bounded-container"],
        )

    def test_pre_execution_failure_is_terminal_and_does_not_fabricate_snapshot(self) -> None:
        result = failed_execution_result(
            self.request,
            failure_code="action_not_available",
            stderr="The registered action is unavailable.",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIsNone(result.snapshot_ref)
        payload = result.to_dict()
        self.assertIsNone(payload["snapshot_ref"])
        self.assertEqual(payload["failure_code"], "action_not_available")

    def test_result_publication_rejects_symlink_and_replaced_parents_and_fsyncs(
        self,
    ) -> None:
        terminal = failed_execution_result(
            self.request,
            failure_code="action_not_available",
        )
        real_parent = self.root / "real-publication"
        real_parent.mkdir()
        symlink_parent = self.root / "symlink-publication"
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(
            InterfaceOutputExecutionUnavailable, "real directory"
        ):
            write_execution_result(symlink_parent / "result.json", terminal)

        stable_parent = self.root / "stable-publication"
        target = stable_parent / "result.json"
        original_fsync = os.fsync
        with patch.object(
            output_execution.os,
            "fsync",
            wraps=original_fsync,
        ) as fsync:
            write_execution_result(target, terminal)
        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8"))["status"],
            "rejected",
        )

        replaced_parent = self.root / "replaced-publication"
        replaced_parent.mkdir()
        old_parent = self.root / "old-publication"
        original_require = output_execution._require_directory_binding
        swapped = False

        def swap_before_publication(path, identity):
            nonlocal swapped
            if not swapped and path == replaced_parent.resolve():
                swapped = True
                replaced_parent.rename(old_parent)
                replaced_parent.mkdir()
            return original_require(path, identity)

        with (
            patch.object(
                output_execution,
                "_require_directory_binding",
                side_effect=swap_before_publication,
            ),
            self.assertRaisesRegex(
                InterfaceOutputExecutionUnavailable, "identity changed"
            ),
        ):
            write_execution_result(replaced_parent / "result.json", terminal)
        self.assertFalse((replaced_parent / "result.json").exists())
        self.assertFalse((old_parent / "result.json").exists())

        bound_parent = self.root / "bound-publication"
        bound_parent.mkdir()
        bound_fd = os.open(bound_parent, os.O_RDONLY | os.O_DIRECTORY)
        bound_info = os.fstat(bound_fd)
        bound_identity = (bound_info.st_dev, bound_info.st_ino)
        relocated_parent = self.root / "relocated-bound-publication"
        bound_parent.rename(relocated_parent)
        bound_parent.mkdir()
        try:
            write_execution_result_at(
                bound_fd,
                "bound.json",
                terminal,
                expected_parent_identity=bound_identity,
            )
        finally:
            os.close(bound_fd)
        self.assertFalse((bound_parent / "bound.json").exists())
        self.assertEqual(
            json.loads(
                (relocated_parent / "bound.json").read_text(encoding="utf-8")
            )["status"],
            "rejected",
        )

    def test_result_tree_export_rejects_a_symlink_publication_parent(self) -> None:
        process = _ImmediateProcess(returncode=0)

        def copy_results(_container_name: str, destination: Path) -> None:
            (destination / "result.txt").write_text("ok\n", encoding="utf-8")

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results", side_effect=copy_results),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )
        real_parent = self.root / "real-export"
        real_parent.mkdir()
        symlink_parent = self.root / "symlink-export"
        symlink_parent.symlink_to(real_parent, target_is_directory=True)
        try:
            with self.assertRaisesRegex(
                InterfaceOutputExecutionUnavailable, "real directory"
            ):
                export_execution_result_tree(
                    result,
                    symlink_parent / "request-1",
                )
        finally:
            result.cleanup()

    def test_fd_anchored_result_export_detects_concurrent_parent_replacement(
        self,
    ) -> None:
        process = _ImmediateProcess(returncode=0)

        def copy_results(_container_name: str, destination: Path) -> None:
            (destination / "result.txt").write_text("ok\n", encoding="utf-8")

        with (
            patch(
                "optpilot_studio.ui.interface_output_execution.subprocess.Popen",
                return_value=process,
            ),
            patch.object(self.executor, "_start_container"),
            patch.object(self.executor, "_copy_results", side_effect=copy_results),
            patch.object(self.executor, "_force_remove"),
            patch.object(self.executor, "_require_container_absent"),
        ):
            result = self.executor.execute(
                source=self.source,
                runtime=self.runtime,
                action=self.action,
                request=self.request,
            )

        publication = self.root / "fd-export"
        publication.mkdir()
        publication_fd = os.open(publication, os.O_RDONLY | os.O_DIRECTORY)
        publication_info = os.fstat(publication_fd)
        publication_identity = (
            publication_info.st_dev,
            publication_info.st_ino,
        )
        original_snapshot = output_execution.snapshot_interface_output_tree
        relocated = self.root / "relocated-fd-export"
        swapped = False

        def snapshot_then_replace(*args, **kwargs):
            nonlocal swapped
            projection = original_snapshot(*args, **kwargs)
            if not swapped:
                swapped = True
                publication.rename(relocated)
                publication.mkdir()
            return projection

        try:
            with (
                patch.object(
                    output_execution,
                    "snapshot_interface_output_tree",
                    side_effect=snapshot_then_replace,
                ),
                self.assertRaisesRegex(
                    InterfaceOutputExecutionUnavailable, "identity changed"
                ),
            ):
                export_execution_result_tree_at(
                    result,
                    publication_fd,
                    publication,
                    "request-1",
                    expected_parent_identity=publication_identity,
                )
        finally:
            os.close(publication_fd)
            result.cleanup()
        self.assertFalse((publication / "request-1").exists())
        self.assertFalse((relocated / "request-1").exists())
        self.assertEqual(
            [path.name for path in relocated.iterdir()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
