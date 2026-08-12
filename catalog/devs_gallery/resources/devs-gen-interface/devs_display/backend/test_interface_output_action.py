import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from default_tools.interface_output_action import (
    OUTPUT_ACTION_ID,
    OUTPUT_ACTION_REQUEST_SCHEMA,
    OUTPUT_ACTION_RESULT_SCHEMA,
    InterfaceOutputActionClient,
    OutputActionUnavailable,
)


class InterfaceOutputActionClientTests(unittest.TestCase):
    def _roots(self, root: Path) -> tuple[Path, Path]:
        output_root = root / "outputs"
        action_root = root / "actions"
        output_root.mkdir()
        action_root.mkdir()
        for name in ("inputs", "responses", "results", "cancellations"):
            (action_root / name).mkdir()
        return output_root, action_root

    @staticmethod
    def _wait_for_request(action_root: Path) -> dict:
        request_file = action_root / "requests.jsonl"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                lines = request_file.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            if lines:
                return json.loads(lines[-1])
            time.sleep(0.01)
        raise AssertionError("Client did not append an output-action request.")

    @staticmethod
    def _write_response(action_root: Path, request: dict, **updates) -> None:
        response = {
            "schema_version": OUTPUT_ACTION_RESULT_SCHEMA,
            "request_id": request["request_id"],
            "action_id": request["action_id"],
            "snapshot_ref": "snapshot:test",
            "status": "succeeded",
            "exit_code": 0,
            "duration_seconds": 0.125,
            "stdout": "simulation complete\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "result_files": [],
            "failure_code": None,
        }
        response.update(updates)
        target = action_root / "responses" / f"{request['request_id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def test_stages_exact_tree_and_retains_only_declared_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root, action_root = self._roots(root)
            source = root / "source"
            destination = root / "retained-results"
            source.mkdir()
            destination.mkdir()
            (source / "run.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "package").mkdir()
            (source / "package" / "model.py").write_text(
                "VALUE = 7\n", encoding="utf-8"
            )
            observed: dict = {}

            def broker() -> None:
                request = self._wait_for_request(action_root)
                observed.update(request)
                staged = (action_root / "inputs").joinpath(
                    *Path(request["output_path"]).parts
                )
                self.assertEqual(
                    (staged / "run.py").read_text(encoding="utf-8"),
                    "print('ok')\n",
                )
                payload = b'{"completed": true}\n'
                result_root = action_root / "results" / request["request_id"]
                result_root.mkdir()
                (result_root / "summary.json").write_bytes(payload)
                self._write_response(
                    action_root,
                    request,
                    result_files=[
                        {
                            "path": "summary.json",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ],
                )

            worker = threading.Thread(target=broker)
            worker.start()
            client = InterfaceOutputActionClient(
                output_root=output_root,
                action_root=action_root,
            )
            result = client.execute(
                source_directory=source,
                arguments=("--seed", "7"),
                results_directory=destination,
                request_id="exec_0123456789abcdef0123456789abcdef",
            )
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                (destination / "summary.json").read_bytes(),
                b'{"completed": true}\n',
            )
            self.assertEqual(
                set(observed),
                {
                    "schema_version",
                    "request_id",
                    "action_id",
                    "output_path",
                    "arguments",
                    "timeout_seconds",
                },
            )
            self.assertEqual(
                observed["schema_version"], OUTPUT_ACTION_REQUEST_SCHEMA
            )
            self.assertEqual(observed["action_id"], OUTPUT_ACTION_ID)
            self.assertEqual(observed["arguments"], ["--seed", "7"])
            self.assertIsNone(observed["timeout_seconds"])
            self.assertFalse(
                (action_root / "inputs" / observed["output_path"]).exists(),
                "terminal requests must remove their transient staged input",
            )
            self.assertEqual(list(output_root.iterdir()), [])

    def test_stop_creates_marker_and_waits_for_cancelled_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root, action_root = self._roots(root)
            source = root / "source"
            source.mkdir()
            (source / "run.py").write_text("pass\n", encoding="utf-8")
            observed: dict = {}

            def broker() -> None:
                request = self._wait_for_request(action_root)
                observed.update(request)
                marker = action_root / "cancellations" / request["request_id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                self._write_response(
                    action_root,
                    request,
                    status="cancelled",
                    snapshot_ref=None,
                    exit_code=None,
                    duration_seconds=0.01,
                    stdout="",
                    failure_code="cancelled",
                )

            worker = threading.Thread(target=broker)
            worker.start()
            result = InterfaceOutputActionClient(
                output_root=output_root,
                action_root=action_root,
            ).execute(
                source_directory=source,
                arguments=(),
                results_directory=None,
                should_cancel=lambda: True,
            )
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(
                (
                    action_root
                    / "cancellations"
                    / observed["request_id"]
                ).is_file()
            )
            self.assertFalse(
                (action_root / "inputs" / observed["output_path"]).exists()
            )
            self.assertEqual(list(output_root.iterdir()), [])

    def test_result_digest_mismatch_fails_closed_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root, action_root = self._roots(root)
            source = root / "source"
            destination = root / "retained-results"
            source.mkdir()
            destination.mkdir()
            (source / "run.py").write_text("pass\n", encoding="utf-8")
            observed: dict = {}

            def broker() -> None:
                request = self._wait_for_request(action_root)
                observed.update(request)
                result_root = action_root / "results" / request["request_id"]
                result_root.mkdir()
                (result_root / "result.txt").write_text(
                    "tampered", encoding="utf-8"
                )
                self._write_response(
                    action_root,
                    request,
                    result_files=[
                        {
                            "path": "result.txt",
                            "size": len("tampered"),
                            "sha256": "0" * 64,
                        }
                    ],
                )

            worker = threading.Thread(target=broker)
            worker.start()
            with self.assertRaisesRegex(
                OutputActionUnavailable, "integrity verification"
            ):
                InterfaceOutputActionClient(
                    output_root=output_root,
                    action_root=action_root,
                ).execute(
                    source_directory=source,
                    arguments=(),
                    results_directory=destination,
                )
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(
                (action_root / "inputs" / observed["output_path"]).exists()
            )
            self.assertEqual(list(destination.iterdir()), [])
            self.assertEqual(list(output_root.iterdir()), [])

    def test_request_carries_one_optional_positive_execution_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root, action_root = self._roots(root)
            source = root / "source"
            source.mkdir()
            (source / "run.py").write_text("pass\n", encoding="utf-8")
            observed: dict = {}

            def broker() -> None:
                request = self._wait_for_request(action_root)
                observed.update(request)
                self._write_response(action_root, request)

            worker = threading.Thread(target=broker)
            worker.start()
            InterfaceOutputActionClient(
                output_root=output_root,
                action_root=action_root,
            ).execute(
                source_directory=source,
                arguments=(),
                results_directory=None,
                timeout_seconds=7,
            )
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(observed["timeout_seconds"], 7)
            self.assertEqual(list(output_root.iterdir()), [])

            client = InterfaceOutputActionClient(
                output_root=output_root,
                action_root=action_root,
            )
            for invalid in (True, 0, -1, 1.5, "7"):
                with self.subTest(timeout_seconds=invalid):
                    with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                        client.execute(
                            source_directory=source,
                            arguments=(),
                            results_directory=None,
                            timeout_seconds=invalid,
                        )

    def test_replaced_broker_input_namespace_fails_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root, action_root = self._roots(root)
            source = root / "source"
            source.mkdir()
            (source / "run.py").write_text("pass\n", encoding="utf-8")
            client = InterfaceOutputActionClient(
                output_root=output_root,
                action_root=action_root,
            )
            (action_root / "inputs").rename(action_root / "original-inputs")
            (action_root / "inputs").mkdir()

            with self.assertRaisesRegex(
                OutputActionUnavailable,
                "identity changed",
            ):
                client.execute(
                    source_directory=source,
                    arguments=(),
                    results_directory=None,
                    timeout_seconds=5,
                )

            self.assertEqual(list((action_root / "inputs").iterdir()), [])
            self.assertEqual(list(output_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
