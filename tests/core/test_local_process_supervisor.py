from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

try:
    import fcntl
except ImportError:  # pragma: no cover - this suite is explicitly POSIX-only
    fcntl = None  # type: ignore[assignment]

from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.local_process_supervisor import (
    LocalProcessSupervisor,
    ProcessLaunchPrivateEnvironment,
    ProcessLaunchReservation,
    ProcessLaunchRequest,
    ProcessTerminalReconciliation,
    WorkerStarted,
    WorkerTerminalProof,
)
from optpilot.realm.refs import canonical_json_bytes


_FINGERPRINT = "a" * 64


class _SimulatedParentCrash(BaseException):
    pass


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path.name}")
        time.sleep(0.01)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_pid_gone(pid: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_exists(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"process {pid} remained alive")
        time.sleep(0.01)


def _record_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        b"optpilot/local-process-record/v1\0" + canonical_json_bytes(payload)
    ).hexdigest()


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body["record_digest"] = _record_digest(body)
    path.write_bytes(canonical_json_bytes(body))


def _read_record(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("record_digest")
    return payload


@unittest.skipUnless(
    os.name == "posix" and fcntl is not None,
    "local process supervision currently requires POSIX flock",
)
class LocalProcessSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.provider_root = self.base / "provider"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _supervisor(self, **kwargs: Any) -> LocalProcessSupervisor:
        return LocalProcessSupervisor(self.provider_root, **kwargs)

    def _request(self, code: str) -> ProcessLaunchRequest:
        return ProcessLaunchRequest(
            argv=(sys.executable, "-c", code),
            cwd=str(self.base),
            env={},
        )

    def _launch(
        self,
        supervisor: LocalProcessSupervisor,
        request: ProcessLaunchRequest,
        *,
        token: str = "launch-a",
    ):
        return supervisor.launch(
            launch_token=token,
            binding_id="binding-a",
            evidence_fingerprint=_FINGERPRINT,
            request=request,
        )

    def _reserve(
        self,
        supervisor: LocalProcessSupervisor,
        request: ProcessLaunchRequest,
        *,
        token: str = "launch-a",
    ) -> ProcessLaunchReservation:
        return supervisor.reserve(
            launch_token=token,
            binding_id="binding-a",
            evidence_fingerprint=_FINGERPRINT,
            request=request,
        )

    def _launch_dir(self, token: str) -> Path:
        coordinate = hashlib.sha256(
            b"optpilot/local-process-coordinate/v1\0" + token.encode("ascii")
        ).hexdigest()
        return self.provider_root / "launches" / coordinate

    def _bind_private_socket(
        self, name: str = "control.sock"
    ) -> tuple[socket.socket, Path, os.stat_result]:
        namespace = self.base / "socket-namespace"
        namespace.mkdir(mode=0o700, exist_ok=True)
        os.chmod(namespace, 0o700)
        path = namespace / name
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
        except PermissionError:
            listener.close()
            # Some CI sandboxes deny pathname sockets.  Keep the registry and
            # identity tests live by representing the endpoint with one 0600
            # inode and narrowly teaching this module's type predicate to
            # regard regular files as the simulated socket type for this test.
            path.write_bytes(b"simulated pathname socket")
            original_is_socket = stat.S_ISSOCK
            patcher = mock.patch(
                "optpilot.realm.local_process_supervisor.stat.S_ISSOCK",
                side_effect=lambda mode: (
                    original_is_socket(mode) or stat.S_ISREG(mode)
                ),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        os.chmod(path, 0o600)

        def cleanup() -> None:
            listener.close()
            if os.path.lexists(path):
                path.unlink()

        self.addCleanup(cleanup)
        return listener, path, path.lstat()

    @staticmethod
    def _exact_coordinates(
        reservation: ProcessLaunchReservation,
    ) -> dict[str, str]:
        return {
            "launch_token": reservation.launch_token,
            "binding_id": reservation.binding_id,
            "evidence_fingerprint": reservation.evidence_fingerprint,
            "launch_request_digest": reservation.launch_request_digest,
        }

    def test_request_digest_is_exact_and_terminal_proof_is_path_free(self) -> None:
        first = ProcessLaunchRequest(
            argv=("/bin/tool", "--value", "1"),
            cwd=str(self.base),
            env={"B": "2", "A": "1"},
        )
        reordered = ProcessLaunchRequest(
            argv=["/bin/tool", "--value", "1"],
            cwd=str(self.base),
            env={"A": "1", "B": "2"},
        )
        changed = ProcessLaunchRequest(
            argv=("/bin/tool", "--value", "2"),
            cwd=str(self.base),
            env={"A": "1", "B": "2"},
        )
        self.assertEqual(first.digest, reordered.digest)
        self.assertEqual(first.canonical_bytes, reordered.canonical_bytes)
        self.assertNotEqual(first.digest, changed.digest)

        proof = WorkerTerminalProof(
            launch_token="launch-a",
            binding_id="binding-a",
            evidence_fingerprint=_FINGERPRINT,
            backend_token="b" * 64,
            launch_request_digest=first.digest,
            disposition="exited",
            provider_generation=1,
            terminal_at=2.0,
        )
        self.assertEqual(WorkerTerminalProof.from_dict(proof.to_dict()), proof)
        encoded = proof.canonical_bytes.decode("utf-8")
        self.assertNotIn(str(self.base), encoded)
        self.assertNotIn("argv", encoded)
        self.assertNotIn("env", encoded)

    def test_exact_reservation_is_path_free_passive_and_replayable(self) -> None:
        output = self.base / "reserved-output"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )
        first_supervisor = self._supervisor()
        reservation = self._reserve(first_supervisor, request)
        launch_dir = self._launch_dir("launch-a")

        self.assertEqual(
            ProcessLaunchReservation.from_dict(reservation.to_dict()), reservation
        )
        encoded = reservation.canonical_bytes.decode("utf-8")
        self.assertNotIn(str(self.base), encoded)
        self.assertNotIn("argv", encoded)
        self.assertNotIn("env", encoded)
        self.assertFalse(output.exists())
        self.assertFalse((launch_dir / "manifest.json").exists())
        self.assertFalse((launch_dir / "handshake.json").exists())
        self.assertFalse((launch_dir / "result.json").exists())

        restarted = self._supervisor()
        self.assertEqual(self._reserve(restarted, request), reservation)
        self.assertEqual(
            restarted.lookup_reservation(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            ),
            reservation,
        )
        for _ in range(3):
            self.assertIsNone(
                restarted.lookup_terminal_proof(
                    launch_token="launch-a",
                    binding_id="binding-a",
                    evidence_fingerprint=_FINGERPRINT,
                    launch_request_digest=request.digest,
                )
            )
        self.assertFalse((launch_dir / "result.json").exists())
        self.assertFalse(output.exists())

        with self.assertRaises(RealmConflict):
            restarted.lookup_reservation(
                launch_token="launch-a",
                binding_id="binding-substituted",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        with self.assertRaises(RealmConflict):
            self._reserve(restarted, self._request("print('changed')"))

        proof = restarted.start_reserved(reservation).wait(timeout=5.0)
        self.assertEqual(proof.disposition, "exited")
        self.assertEqual(output.read_text(encoding="utf-8"), "ran")

    def test_private_environment_uses_transient_pipe_and_replay_needs_no_value(
        self,
    ) -> None:
        marker = "private-value-that-must-never-be-persisted"
        output = self.base / "private-environment-output"
        gate = self.base / "private-environment-gate"
        request = ProcessLaunchRequest(
            argv=(
                sys.executable,
                "-c",
                (
                    "import os, time\n"
                    "from pathlib import Path\n"
                    f"Path({str(output)!r}).write_text("
                    "os.environ['PRIVATE_TEST_TOKEN'])\n"
                    f"gate = Path({str(gate)!r})\n"
                    "while not gate.exists():\n"
                    "    time.sleep(0.01)\n"
                ),
            ),
            cwd=str(self.base),
            env={},
            private_env_names=("PRIVATE_TEST_TOKEN",),
            private_env_binding_revision="settings-revision-1",
        )
        private_environment = ProcessLaunchPrivateEnvironment(
            binding_revision="settings-revision-1",
            values={"PRIVATE_TEST_TOKEN": marker},
        )
        self.assertNotIn(marker, repr(request))
        self.assertNotIn(marker.encode(), request.canonical_bytes)
        self.assertNotIn(marker, repr(private_environment))

        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        process = supervisor.start_reserved(
            reservation,
            private_environment=private_environment,
        )
        self.assertIsInstance(process.wait_started(timeout=5.0), WorkerStarted)
        _wait_for(output)
        self.assertEqual(output.read_text(encoding="utf-8"), marker)

        manifest = _read_record(self._launch_dir("launch-a") / "manifest.json")
        self.assertEqual(
            manifest["launch_request"]["private_env_names"],
            ["PRIVATE_TEST_TOKEN"],
        )
        self.assertEqual(
            manifest["launch_request"]["private_env_binding_revision"],
            "settings-revision-1",
        )
        self.assertNotIn(marker, json.dumps(manifest, sort_keys=True))
        for path in self.provider_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    marker.encode(),
                    path.read_bytes(),
                    msg=f"private value leaked to {path}",
                )

        restarted = self._supervisor()
        replayed = restarted.lookup_reservation(
            launch_token="launch-a",
            binding_id="binding-a",
            evidence_fingerprint=_FINGERPRINT,
            launch_request_digest=request.digest,
        )
        attached = restarted.start_reserved(replayed)
        self.assertIsInstance(attached.wait_started(timeout=5.0), WorkerStarted)

        gate.write_text("release", encoding="utf-8")
        proof = attached.wait(timeout=5.0)
        self.assertEqual(proof.disposition, "exited")
        self.assertEqual(process.wait(timeout=1.0), proof)

    def test_new_private_environment_start_requires_the_exact_binding(
        self,
    ) -> None:
        request = ProcessLaunchRequest(
            argv=(sys.executable, "-c", "pass"),
            cwd=str(self.base),
            env={},
            private_env_names=("PRIVATE_TEST_TOKEN",),
            private_env_binding_revision="settings-revision-1",
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)

        with self.assertRaises(RealmConflict):
            supervisor.start_reserved(reservation)
        self.assertEqual(supervisor.reservation_state(reservation), "reserved")
        self.assertFalse(
            (self._launch_dir("launch-a") / "manifest.json").exists()
        )

        wrong = ProcessLaunchPrivateEnvironment(
            binding_revision="settings-revision-2",
            values={"PRIVATE_TEST_TOKEN": "must-not-leak"},
        )
        with self.assertRaises(RealmConflict):
            supervisor.start_reserved(
                reservation,
                private_environment=wrong,
            )
        self.assertEqual(supervisor.reservation_state(reservation), "reserved")
        self.assertNotIn("must-not-leak", repr(wrong))

    def test_abandon_reserved_is_authenticated_terminal_without_spawn(self) -> None:
        output = self.base / "abandoned-output"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)

        proof = supervisor.abandon_reserved(reservation)

        self.assertEqual(proof.disposition, "never_started")
        self.assertFalse(proof.started)
        self.assertFalse(output.exists())
        self.assertEqual(supervisor.abandon_reserved(reservation), proof)
        self.assertEqual(
            self._supervisor().start_reserved(reservation).wait(timeout=1.0),
            proof,
        )
        self.assertEqual(
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            ),
            proof,
        )

    def test_terminal_retirement_redacts_paths_and_never_respawns(self) -> None:
        private_output = self.base / "retirement-output"
        request = self._request(
            "from pathlib import Path; "
            f"Path({str(private_output)!r}).write_text('once')"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        proof = supervisor.start_reserved(reservation).wait(timeout=5.0)
        launch_dir = self._launch_dir("launch-a")
        self.assertTrue(launch_dir.is_dir())

        self.assertEqual(supervisor.retire_terminal(proof), proof)
        self.assertEqual(supervisor.retire_terminal(proof), proof)
        self.assertFalse(launch_dir.exists())

        with sqlite3.connect(supervisor.database_path) as connection:
            request_json, retired = connection.execute(
                "SELECT request_json, retired FROM process_launches "
                "WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()
        tombstone = json.loads(bytes(request_json).decode("utf-8"))
        self.assertEqual(retired, 1)
        self.assertEqual(
            tombstone,
            {
                "launch_request_digest": request.digest,
                "schema": "optpilot.local-process-retired-request.v1",
            },
        )
        retained_bytes = b"".join(
            path.read_bytes()
            for path in self.provider_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(str(self.base).encode("utf-8"), retained_bytes)
        self.assertNotIn(b'"argv"', retained_bytes)
        self.assertNotIn(b'"cwd"', retained_bytes)
        self.assertNotIn(b'"env"', retained_bytes)

        restarted = self._supervisor()
        self.assertEqual(self._reserve(restarted, request), reservation)
        self.assertEqual(
            restarted.start_reserved(reservation).wait(timeout=1.0), proof
        )
        self.assertEqual(private_output.read_text(encoding="utf-8"), "once")
        with self.assertRaises(RealmConflict):
            self._reserve(restarted, self._request("print('changed')"))

    def test_retirement_retries_after_redaction_before_directory_delete(self) -> None:
        request = self._request("pass")
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        proof = supervisor.start_reserved(reservation).wait(timeout=5.0)
        with mock.patch.object(
            supervisor,
            "_delete_retired_launch_directory",
            side_effect=OSError("injected retirement delete failure"),
        ):
            with self.assertRaises(OSError):
                supervisor.retire_terminal(proof)

        # The database decision happens first, so even incomplete physical
        # cleanup is already non-spawnable.
        self.assertEqual(
            self._supervisor().start_reserved(reservation).wait(timeout=1.0),
            proof,
        )
        self.assertEqual(self._supervisor().retire_terminal(proof), proof)
        self.assertFalse(self._launch_dir("launch-a").exists())

    def test_exact_terminal_reconciliation_abandons_and_replays_passive_launch(
        self,
    ) -> None:
        output = self.base / "reconciliation-must-not-run"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)

        receipt = supervisor.reconcile_terminal_launch(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )
        replay = self._supervisor().reconcile_terminal_launch(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )

        self.assertIsInstance(receipt, ProcessTerminalReconciliation)
        self.assertEqual(receipt.prior_state, "reserved")
        self.assertEqual(receipt.proof.disposition, "never_started")
        self.assertTrue(receipt.retired)
        self.assertEqual(receipt.launch_token, reservation.launch_token)
        self.assertEqual(receipt.binding_id, reservation.binding_id)
        self.assertEqual(
            receipt.evidence_fingerprint,
            reservation.evidence_fingerprint,
        )
        self.assertEqual(
            receipt.launch_request_digest,
            reservation.launch_request_digest,
        )
        self.assertEqual(replay.prior_state, "retired")
        self.assertEqual(replay.proof, receipt.proof)
        self.assertFalse(output.exists())
        self.assertFalse(self._launch_dir("launch-a").exists())
        encoded = receipt.canonical_bytes.decode("utf-8")
        self.assertNotIn(str(self.base), encoded)
        self.assertNotIn("argv", encoded)
        self.assertNotIn("cwd", encoded)
        self.assertNotIn("env", encoded)

    def test_absence_seal_replays_and_rejects_every_late_reserve(
        self,
    ) -> None:
        output = self.base / "negative-seal-must-not-run"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )
        supervisor = self._supervisor()

        receipt = supervisor.seal_launch_if_absent(
            launch_token="launch-a", binding_id="binding-a"
        )
        replay = self._supervisor().seal_launch_if_absent(
            launch_token="launch-a", binding_id="binding-a"
        )

        self.assertEqual(receipt.prior_state, "absent")
        self.assertTrue(receipt.sealed)
        self.assertEqual(replay.prior_state, "sealed")
        self.assertEqual(replay.launch_token, receipt.launch_token)
        self.assertEqual(replay.binding_id, receipt.binding_id)
        self.assertFalse(output.exists())
        self.assertFalse(self._launch_dir("launch-a").exists())
        with self.assertRaises(RealmConflict):
            supervisor.reserve(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                request=request,
            )
        with self.assertRaises(RealmConflict):
            supervisor.reserve(
                launch_token="launch-a",
                binding_id="different-binding",
                evidence_fingerprint=_FINGERPRINT,
                request=request,
            )
        with self.assertRaises(RealmConflict):
            supervisor.seal_launch_if_absent(
                launch_token="launch-a", binding_id="different-binding"
            )

        with sqlite3.connect(supervisor.database_path) as connection:
            seal_row = connection.execute(
                "SELECT binding_id FROM process_launch_seals WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()
            launch_row = connection.execute(
                "SELECT 1 FROM process_launches WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()
        self.assertEqual(seal_row, ("binding-a",))
        self.assertIsNone(launch_row)

    def test_absence_seal_reports_existing_without_weak_cleanup_authority(
        self,
    ) -> None:
        supervisor = self._supervisor()
        request = self._request("raise AssertionError('must not run')")
        reservation = self._reserve(supervisor, request)

        receipt = supervisor.seal_launch_if_absent(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
        )

        self.assertEqual(receipt.prior_state, "existing")
        self.assertFalse(receipt.sealed)
        self.assertEqual(supervisor.reservation_state(reservation), "reserved")
        self.assertIsNone(
            supervisor.lookup_terminal_proof(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
            )
        )
        with self.assertRaises(RealmConflict):
            supervisor.seal_launch_if_absent(
                launch_token=reservation.launch_token,
                binding_id="different-binding",
            )

    def test_absence_seal_wins_after_reserve_filesystem_preparation(self) -> None:
        supervisor = self._supervisor()
        request = self._request("raise AssertionError('must not run')")
        insertion_entered = threading.Event()
        release_insertion = threading.Event()
        original_insert = supervisor._insert_reservation
        outcome: dict[str, Any] = {}

        def delayed_insert(row: Any) -> Any:
            insertion_entered.set()
            if not release_insertion.wait(timeout=5.0):
                raise TimeoutError("reservation insertion barrier timed out")
            return original_insert(row)

        def reserve() -> None:
            try:
                outcome["reservation"] = self._reserve(supervisor, request)
            except BaseException as error:
                outcome["error"] = error

        with mock.patch.object(
            supervisor, "_insert_reservation", side_effect=delayed_insert
        ):
            thread = threading.Thread(target=reserve, daemon=True)
            thread.start()
            self.assertTrue(insertion_entered.wait(timeout=5.0))
            try:
                seal = supervisor.seal_launch_if_absent(
                    launch_token="launch-a", binding_id="binding-a"
                )
            finally:
                release_insertion.set()
                thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(seal.prior_state, "absent")
        self.assertIsInstance(outcome.get("error"), RealmConflict)
        self.assertNotIn("reservation", outcome)
        self.assertFalse(self._launch_dir("launch-a").exists())
        with sqlite3.connect(supervisor.database_path) as connection:
            launch_count = connection.execute(
                "SELECT COUNT(*) FROM process_launches WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()[0]
            seal_count = connection.execute(
                "SELECT COUNT(*) FROM process_launch_seals WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()[0]
        self.assertEqual((launch_count, seal_count), (0, 1))

    def test_absence_seal_waits_for_crash_released_realization_gate(self) -> None:
        supervisor = self._supervisor()
        claim = supervisor.claim_launch_realization(
            launch_token="launch-a",
            binding_id="binding-a",
            timeout=1.0,
        )
        try:
            with self.assertRaises(TimeoutError):
                self._supervisor().seal_launch_if_absent(
                    launch_token="launch-a",
                    binding_id="binding-a",
                    timeout=0.01,
                )
        finally:
            supervisor.release_launch_realization(claim)

        receipt = self._supervisor().seal_launch_if_absent(
            launch_token="launch-a",
            binding_id="binding-a",
            timeout=1.0,
        )
        self.assertEqual(receipt.prior_state, "absent")
        self.assertTrue(claim.released)
        with self.assertRaises(RealmConflict):
            self._supervisor().claim_launch_realization(
                launch_token="launch-a",
                binding_id="binding-a",
                timeout=1.0,
            )

    def test_exact_terminal_reconciliation_recovers_lost_stop_response(
        self,
    ) -> None:
        pid_path = self.base / "reconciliation-live.pid"
        request = self._request(
            "import os,time; from pathlib import Path; "
            f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        process = supervisor.start_reserved(reservation)
        process.wait_started(timeout=5.0)
        _wait_for(pid_path)
        pid = int(pid_path.read_text(encoding="utf-8"))
        original_stop = supervisor._stop

        def lose_response(
            launch_token: str,
            *,
            grace_period: float,
            timeout: float | None,
        ) -> WorkerTerminalProof:
            original_stop(
                launch_token,
                grace_period=grace_period,
                timeout=timeout,
            )
            raise RuntimeError("simulated lost stop response")

        with mock.patch.object(supervisor, "_stop", side_effect=lose_response):
            receipt = supervisor.reconcile_terminal_launch(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
                grace_period=0.0,
                timeout=7.0,
            )

        self.assertEqual(receipt.prior_state, "start_requested")
        self.assertEqual(receipt.proof.disposition, "killed")
        self.assertTrue(receipt.retired)
        _wait_pid_gone(pid)
        replay = self._supervisor().reconcile_terminal_launch(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )
        self.assertEqual(replay.prior_state, "retired")
        self.assertEqual(replay.proof, receipt.proof)

    def test_exact_terminal_reconciliation_retries_lost_retirement_response(
        self,
    ) -> None:
        request = self._request("raise AssertionError('must not run')")
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        with mock.patch.object(
            supervisor,
            "_delete_retired_launch_directory",
            side_effect=OSError("simulated lost retirement response"),
        ):
            with self.assertRaisesRegex(OSError, "lost retirement response"):
                supervisor.reconcile_terminal_launch(
                    launch_token=reservation.launch_token,
                    binding_id=reservation.binding_id,
                    evidence_fingerprint=reservation.evidence_fingerprint,
                    launch_request_digest=reservation.launch_request_digest,
                )

        proof = self._supervisor().lookup_terminal_proof(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )
        self.assertIsNotNone(proof)
        replay = self._supervisor().reconcile_terminal_launch(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )
        self.assertEqual(replay.prior_state, "retired")
        self.assertEqual(replay.proof, proof)
        self.assertFalse(self._launch_dir("launch-a").exists())

    def test_exact_terminal_reconciliation_rejects_every_substituted_coordinate(
        self,
    ) -> None:
        pid_path = self.base / "reconciliation-guard.pid"
        request = self._request(
            "import os,time; from pathlib import Path; "
            f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        supervisor.start_reserved(reservation).wait_started(timeout=5.0)
        _wait_for(pid_path)
        pid = int(pid_path.read_text(encoding="utf-8"))
        exact = {
            "launch_token": reservation.launch_token,
            "binding_id": reservation.binding_id,
            "evidence_fingerprint": reservation.evidence_fingerprint,
            "launch_request_digest": reservation.launch_request_digest,
        }
        substitutions = (
            ("launch_token", "unrelated-launch", RealmNotFound),
            ("binding_id", "unrelated-binding", RealmConflict),
            ("evidence_fingerprint", "b" * 64, RealmConflict),
            ("launch_request_digest", "b" * 64, RealmConflict),
        )

        for field, value, error in substitutions:
            with self.subTest(field=field):
                changed = dict(exact)
                changed[field] = value
                with self.assertRaises(error):
                    supervisor.reconcile_terminal_launch(**changed)
                self.assertTrue(_pid_exists(pid))
                self.assertIsNone(
                    supervisor.lookup_terminal_proof(**exact)
                )

        receipt = supervisor.reconcile_terminal_launch(
            **exact, grace_period=0.0, timeout=7.0
        )
        self.assertEqual(receipt.proof.disposition, "killed")
        _wait_pid_gone(pid)

    def test_exact_terminal_reconciliation_retires_existing_terminal_proof(
        self,
    ) -> None:
        request = self._request("pass")
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        proof = supervisor.start_reserved(reservation).wait(timeout=5.0)

        receipt = supervisor.reconcile_terminal_launch(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )

        self.assertEqual(receipt.prior_state, "terminal")
        self.assertEqual(receipt.proof, proof)
        self.assertTrue(receipt.retired)
        self.assertFalse(self._launch_dir("launch-a").exists())

    def test_supervisor_owns_ready_unix_socket_identity_and_exact_cleanup(self) -> None:
        supervisor = self._supervisor()
        reservation = self._reserve(
            supervisor, self._request("import time; time.sleep(60)")
        )
        supervisor.start_reserved(reservation).wait_started(timeout=5.0)
        _listener, path, metadata = self._bind_private_socket()
        exact = self._exact_coordinates(reservation)

        registration = supervisor.record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )
        replay = self._supervisor().record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )

        self.assertEqual(registration.state, "recorded")
        self.assertEqual(replay, registration)
        self.assertNotIn(str(path), registration.canonical_bytes.decode("utf-8"))
        receipt = self._supervisor().reconcile_terminal_launch(
            **exact, grace_period=0.0, timeout=7.0
        )
        self.assertTrue(receipt.endpoints_reconciled)
        self.assertFalse(os.path.lexists(path))
        after = self._supervisor().record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )
        self.assertEqual(after.state, "reconciled")
        with sqlite3.connect(self.provider_root / "process_registry.sqlite3") as db:
            endpoint = db.execute(
                """
                SELECT endpoint_kind, path, state
                FROM process_unix_socket_endpoints
                WHERE launch_token = ? AND endpoint_name = 'control'
                """,
                (reservation.launch_token,),
            ).fetchone()
        self.assertEqual(endpoint, ("unix_socket", None, "reconciled"))

    def test_unix_socket_registration_rejects_substitution_and_forgery(self) -> None:
        supervisor = self._supervisor()
        reservation = self._reserve(
            supervisor, self._request("import time; time.sleep(60)")
        )
        supervisor.start_reserved(reservation).wait_started(timeout=5.0)
        _listener, path, metadata = self._bind_private_socket()
        exact = self._exact_coordinates(reservation)
        substitutions = (
            ("launch_token", "unrelated-launch", RealmNotFound),
            ("binding_id", "unrelated-binding", RealmConflict),
            ("evidence_fingerprint", "b" * 64, RealmConflict),
            ("launch_request_digest", "b" * 64, RealmConflict),
        )

        for field, value, error in substitutions:
            with self.subTest(field=field):
                changed = dict(exact)
                changed[field] = value
                with self.assertRaises(error):
                    supervisor.record_unix_socket_endpoint(
                        **changed,
                        endpoint_name="control",
                        path=path,
                        device_id=metadata.st_dev,
                        inode=metadata.st_ino,
                    )
                self.assertTrue(os.path.lexists(path))

        with self.assertRaises(RealmIntegrityError):
            supervisor.record_unix_socket_endpoint(
                **exact,
                endpoint_name="control",
                path=path,
                device_id=metadata.st_dev,
                inode=metadata.st_ino + 1,
            )
        forged = path.parent / "forged.sock"
        forged.write_bytes(b"not a socket")
        os.chmod(forged, 0o640)
        self.addCleanup(lambda: forged.unlink(missing_ok=True))
        forged_metadata = forged.lstat()
        with self.assertRaises(RealmIntegrityError):
            supervisor.record_unix_socket_endpoint(
                **exact,
                endpoint_name="forged",
                path=forged,
                device_id=forged_metadata.st_dev,
                inode=forged_metadata.st_ino,
            )
        with sqlite3.connect(self.provider_root / "process_registry.sqlite3") as db:
            count = db.execute(
                "SELECT COUNT(*) FROM process_unix_socket_endpoints"
            ).fetchone()[0]
        self.assertEqual(count, 0)

        supervisor.record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )
        supervisor.reconcile_terminal_launch(
            **exact, grace_period=0.0, timeout=7.0
        )

    def test_terminal_reconciliation_refuses_replaced_unix_socket(self) -> None:
        supervisor = self._supervisor()
        reservation = self._reserve(
            supervisor, self._request("import time; time.sleep(60)")
        )
        supervisor.start_reserved(reservation).wait_started(timeout=5.0)
        listener, path, metadata = self._bind_private_socket()
        exact = self._exact_coordinates(reservation)
        supervisor.record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )
        listener.close()
        path.unlink()
        path.write_bytes(b"foreign replacement")
        os.chmod(path, 0o600)
        replacement = path.lstat()

        with self.assertRaises(RealmIntegrityError):
            supervisor.reconcile_terminal_launch(
                **exact, grace_period=0.0, timeout=7.0
            )

        after = path.lstat()
        self.assertTrue(stat.S_ISREG(after.st_mode))
        self.assertEqual(
            (after.st_dev, after.st_ino),
            (replacement.st_dev, replacement.st_ino),
        )
        proof = supervisor.lookup_terminal_proof(**exact)
        self.assertIsNotNone(proof)
        assert proof is not None
        with self.assertRaises(RealmConflict):
            supervisor.retire_terminal(proof)

        path.unlink()
        receipt = self._supervisor().reconcile_terminal_launch(**exact)
        self.assertEqual(receipt.prior_state, "terminal")
        self.assertTrue(receipt.endpoints_reconciled)

    def test_unix_socket_unlink_response_loss_retries_from_registry(self) -> None:
        def crash(cut: str) -> None:
            if cut == "endpoint_unlinked":
                raise _SimulatedParentCrash()

        supervisor = self._supervisor(fault_injector=crash)
        reservation = self._reserve(
            supervisor, self._request("import time; time.sleep(60)")
        )
        supervisor.start_reserved(reservation).wait_started(timeout=5.0)
        _listener, path, metadata = self._bind_private_socket()
        exact = self._exact_coordinates(reservation)
        supervisor.record_unix_socket_endpoint(
            **exact,
            endpoint_name="control",
            path=path,
            device_id=metadata.st_dev,
            inode=metadata.st_ino,
        )

        with self.assertRaises(_SimulatedParentCrash):
            supervisor.reconcile_terminal_launch(
                **exact, grace_period=0.0, timeout=7.0
            )
        self.assertFalse(os.path.lexists(path))
        with sqlite3.connect(self.provider_root / "process_registry.sqlite3") as db:
            before = db.execute(
                """
                SELECT path, state FROM process_unix_socket_endpoints
                WHERE launch_token = ?
                """,
                (reservation.launch_token,),
            ).fetchone()
            retired = db.execute(
                "SELECT retired FROM process_launches WHERE launch_token = ?",
                (reservation.launch_token,),
            ).fetchone()[0]
        self.assertEqual(before, (str(path), "recorded"))
        self.assertEqual(retired, 0)

        receipt = self._supervisor().reconcile_terminal_launch(**exact)
        self.assertEqual(receipt.prior_state, "terminal")
        self.assertTrue(receipt.endpoints_reconciled)
        with sqlite3.connect(self.provider_root / "process_registry.sqlite3") as db:
            after = db.execute(
                """
                SELECT path, state FROM process_unix_socket_endpoints
                WHERE launch_token = ?
                """,
                (reservation.launch_token,),
            ).fetchone()
        self.assertEqual(after, (None, "reconciled"))

    def test_passive_orphan_retirement_never_touches_start_requested_row(self) -> None:
        passive_request = self._request("raise AssertionError('must not run')")
        passive = self._reserve(self._supervisor(), passive_request)
        proof = self._supervisor().retire_passive_orphan(
            launch_token=passive.launch_token, binding_id=passive.binding_id
        )
        self.assertEqual(proof.disposition, "never_started")
        self.assertFalse(self._launch_dir("launch-a").exists())
        self.assertEqual(
            self._supervisor().start_reserved(passive).wait(timeout=1.0), proof
        )

        def crash(_: str) -> None:
            raise _SimulatedParentCrash()

        started_request = self._request("raise AssertionError('must not run')")
        started_supervisor = self._supervisor(fault_injector=crash)
        started = self._reserve(
            started_supervisor, started_request, token="launch-start-requested"
        )
        with self.assertRaises(_SimulatedParentCrash):
            started_supervisor.start_reserved(started)
        with self.assertRaises(RealmConflict):
            self._supervisor().retire_passive_orphan(
                launch_token=started.launch_token,
                binding_id=started.binding_id,
            )

    def test_abandonment_result_replays_after_terminal_commit_crash(self) -> None:
        request = self._request("raise AssertionError('must not run')")
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        with mock.patch.object(
            supervisor,
            "_commit_terminal",
            side_effect=_SimulatedParentCrash(),
        ):
            with self.assertRaises(_SimulatedParentCrash):
                supervisor.abandon_reserved(reservation)

        restarted = self._supervisor()
        self.assertIsNone(
            restarted.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        )
        with sqlite3.connect(restarted.database_path) as connection:
            terminal_json = connection.execute(
                "SELECT terminal_json FROM process_launches WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()[0]
        self.assertIsNone(terminal_json)

        proof = restarted.abandon_reserved(reservation)
        self.assertEqual(proof.disposition, "never_started")
        self.assertEqual(restarted.validate_terminal_proof(proof), proof)

    def test_start_reserved_crash_after_intent_never_respawns(self) -> None:
        output = self.base / "reserved-crash-output"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )

        def crash(point: str) -> None:
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        crashing = self._supervisor(fault_injector=crash)
        reservation = self._reserve(crashing, request)
        with self.assertRaises(_SimulatedParentCrash):
            crashing.start_reserved(reservation)

        restarted = self._supervisor()
        with self.assertRaises(RealmConflict):
            restarted.abandon_reserved(reservation)
        proof = restarted.start_reserved(reservation).wait(timeout=2.0)
        self.assertEqual(proof.disposition, "never_started")
        self.assertFalse(output.exists())
        self.assertEqual(restarted.start_reserved(reservation).wait(), proof)

    def test_concurrent_start_reserved_spawns_exactly_once(self) -> None:
        count = self.base / "reservation-count.txt"
        request = self._request(
            "import time; "
            f"open({str(count)!r}, 'a', encoding='utf-8').write('x\\n'); "
            "time.sleep(0.25)"
        )
        reservation = self._reserve(self._supervisor(), request)
        supervisors = (self._supervisor(), self._supervisor())
        barrier = threading.Barrier(2)

        def start(supervisor: LocalProcessSupervisor):
            barrier.wait(timeout=2.0)
            return supervisor.start_reserved(reservation)

        with ThreadPoolExecutor(max_workers=2) as pool:
            handles = tuple(pool.map(start, supervisors))
        proofs = tuple(handle.wait(timeout=5.0) for handle in handles)

        self.assertEqual(proofs[0], proofs[1])
        self.assertEqual(count.read_text(encoding="utf-8").splitlines(), ["x"])

    def test_reserved_capability_and_artifact_tampering_fail_closed(self) -> None:
        output = self.base / "tamper-output"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )
        supervisor = self._supervisor()
        reservation = self._reserve(supervisor, request)
        with self.assertRaises(RealmConflict):
            supervisor.start_reserved(
                replace(reservation, backend_token="b" * 64)
            )

        launch_dir = self._launch_dir("launch-a")
        manifest_path = launch_dir / "manifest.json"
        _write_record(manifest_path, {"schema": "substituted"})
        with self.assertRaises(RealmIntegrityError):
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        with self.assertRaises(RealmIntegrityError):
            supervisor.start_reserved(reservation)
        manifest_path.unlink()

        result_path = launch_dir / "result.json"
        _write_record(result_path, {"schema": "substituted"})
        with self.assertRaises(RealmIntegrityError):
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        self.assertFalse(output.exists())
        result_path.unlink()
        supervisor.abandon_reserved(reservation)

    def test_legacy_registry_intent_migrates_as_start_requested(self) -> None:
        request = self._request("raise AssertionError('must not respawn')")
        coordinate = hashlib.sha256(
            b"optpilot/local-process-coordinate/v1\0launch-a"
        ).hexdigest()
        launch_dir = self.provider_root / "launches" / coordinate
        launch_dir.mkdir(parents=True, mode=0o700)
        lock_path = launch_dir / "liveness.lock"
        lock_path.touch(mode=0o600)
        metadata = lock_path.stat()
        database_path = self.provider_root / "process_registry.sqlite3"
        now = time.time()
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                PRAGMA user_version = 1;
                CREATE TABLE provider_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    next_generation INTEGER NOT NULL CHECK (next_generation > 0)
                );
                CREATE TABLE process_launches (
                    launch_token TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    evidence_fingerprint TEXT NOT NULL,
                    backend_token TEXT NOT NULL,
                    request_json BLOB NOT NULL,
                    launch_request_digest TEXT NOT NULL,
                    provider_generation INTEGER NOT NULL CHECK (provider_generation > 0),
                    coordinate TEXT NOT NULL UNIQUE,
                    lock_device INTEGER NOT NULL,
                    lock_inode INTEGER NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (stop_requested IN (0, 1)),
                    terminal_json BLOB,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO process_launches(
                    launch_token, binding_id, evidence_fingerprint, backend_token,
                    request_json, launch_request_digest, provider_generation,
                    coordinate, lock_device, lock_inode, stop_requested,
                    terminal_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                """,
                (
                    "launch-a",
                    "binding-a",
                    _FINGERPRINT,
                    "b" * 64,
                    request.canonical_bytes,
                    request.digest,
                    1,
                    coordinate,
                    metadata.st_dev,
                    metadata.st_ino,
                    now,
                    now,
                ),
            )

        supervisor = self._supervisor()
        reservation = supervisor.lookup_reservation(
            launch_token="launch-a",
            binding_id="binding-a",
            evidence_fingerprint=_FINGERPRINT,
            launch_request_digest=request.digest,
        )
        with sqlite3.connect(database_path) as connection:
            launch_state = connection.execute(
                "SELECT launch_state FROM process_launches WHERE launch_token = ?",
                ("launch-a",),
            ).fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual(launch_state, "start_requested")
        self.assertEqual(version, 5)
        proof = supervisor.start_reserved(reservation).wait(timeout=2.0)
        self.assertEqual(proof.disposition, "never_started")

    def test_exact_replay_runs_one_process_and_changed_request_conflicts(self) -> None:
        count = self.base / "count.txt"
        code = (
            "from pathlib import Path; import time; "
            f"p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
            "time.sleep(0.25)"
        )
        request = self._request(code)
        first = self._launch(self._supervisor(), request)
        second = self._launch(self._supervisor(), request)

        changed = self._request(code + "; print('changed')")
        with self.assertRaises(RealmConflict):
            self._launch(self._supervisor(), changed)

        first_proof = first.wait(timeout=5.0)
        second_proof = second.wait(timeout=5.0)
        self.assertEqual(first_proof, second_proof)
        self.assertEqual(count.read_text(encoding="utf-8"), "x")

    def test_intent_before_spawn_recovers_as_never_started_without_respawn(self) -> None:
        output = self.base / "must-not-exist"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('ran')"
        )

        def crash(point: str) -> None:
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        with self.assertRaises(_SimulatedParentCrash):
            self._launch(self._supervisor(fault_injector=crash), request)

        recovered = self._launch(self._supervisor(), request)
        observation = recovered.wait_started(timeout=2.0)
        self.assertIsInstance(observation, WorkerTerminalProof)
        self.assertFalse(observation.started)
        proof = recovered.wait(timeout=2.0)
        self.assertEqual(observation, proof)
        self.assertEqual(proof.disposition, "never_started")
        self.assertFalse(output.exists())
        self.assertEqual(self._launch(self._supervisor(), request).wait(), proof)
        self.assertTrue((self._launch_dir("launch-a") / "result.json").is_file())

    def test_restart_attaches_after_spawn_and_stop_replays_one_killed_proof(self) -> None:
        started = self.base / "started"
        request = self._request(
            "from pathlib import Path; import time; "
            f"Path({str(started)!r}).write_text('started'); time.sleep(60)"
        )
        first = self._launch(self._supervisor(), request)
        _wait_for(started)

        attached_supervisor = self._supervisor()
        attached = self._launch(attached_supervisor, request)
        first_started = first.wait_started(timeout=2.0)
        replay_started = attached.wait_started(timeout=2.0)
        self.assertIsInstance(first_started, WorkerStarted)
        self.assertEqual(first_started, replay_started)
        self.assertTrue(first_started.started)
        self.assertNotIn(
            str(self.base), first_started.canonical_bytes.decode("utf-8")
        )

        forged = WorkerTerminalProof(
            launch_token=first_started.launch_token,
            binding_id=first_started.binding_id,
            evidence_fingerprint=first_started.evidence_fingerprint,
            backend_token=first_started.backend_token,
            launch_request_digest=first_started.launch_request_digest,
            disposition="killed",
            provider_generation=first_started.provider_generation,
            terminal_at=time.time(),
        )
        with self.assertRaises(RealmConflict):
            attached_supervisor.validate_terminal_proof(forged)

        proof = attached.stop(grace_period=0.05, timeout=5.0)
        self.assertEqual(proof.disposition, "killed")
        self.assertEqual(first.wait(timeout=1.0), proof)
        self.assertEqual(self._launch(self._supervisor(), request).wait(), proof)
        self.assertEqual(attached_supervisor.validate_terminal_proof(proof), proof)
        with self.assertRaises(RealmIntegrityError):
            attached_supervisor.validate_terminal_proof(
                replace(proof, terminal_at=proof.terminal_at + 1.0)
            )

    def test_exit_proof_is_canonical_and_identical_after_restart(self) -> None:
        output = self.base / "output"
        request = self._request(
            f"from pathlib import Path; Path({str(output)!r}).write_text('done')"
        )
        supervisor = self._supervisor()
        proof = self._launch(supervisor, request).wait(timeout=5.0)
        replay_handle = self._launch(self._supervisor(), request)
        start_observation = replay_handle.wait_started(timeout=1.0)
        replay = replay_handle.wait(timeout=1.0)
        self.assertEqual(proof.disposition, "exited")
        self.assertIsInstance(start_observation, WorkerTerminalProof)
        self.assertTrue(start_observation.started)
        self.assertEqual(start_observation, proof)
        self.assertEqual(proof, replay)
        self.assertEqual(proof.canonical_bytes, replay.canonical_bytes)
        self.assertEqual(output.read_text(encoding="utf-8"), "done")
        self.assertEqual(supervisor.validate_terminal_proof(proof), proof)

    def test_stop_kills_worker_and_descendants_before_returning_proof(self) -> None:
        leader_path = self.base / "leader.pid"
        child_path = self.base / "child.pid"
        code = (
            "import os,subprocess,sys,time; from pathlib import Path; "
            f"Path({str(leader_path)!r}).write_text(str(os.getpid())); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(child_path)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        handle = self._launch(self._supervisor(), self._request(code))
        _wait_for(leader_path)
        _wait_for(child_path)
        leader_pid = int(leader_path.read_text())
        child_pid = int(child_path.read_text())

        proof = handle.stop(grace_period=0.05, timeout=7.0)

        self.assertEqual(proof.disposition, "killed")
        _wait_pid_gone(leader_pid)
        _wait_pid_gone(child_pid)

    def test_descendant_cannot_survive_naturally_exited_leader_proof(self) -> None:
        child_path = self.base / "orphan.pid"
        code = (
            "import subprocess,sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(child_path)!r}).write_text(str(p.pid))"
        )
        proof = self._launch(self._supervisor(), self._request(code)).wait(
            timeout=7.0
        )
        child_pid = int(child_path.read_text())

        self.assertEqual(proof.disposition, "exited")
        _wait_pid_gone(child_pid)

    def test_wrapper_death_recovers_after_restart_and_drains_descendants(self) -> None:
        leader_path = self.base / "recovered-leader.pid"
        child_path = self.base / "recovered-child.pid"
        code = (
            "import os,subprocess,sys,time; from pathlib import Path; "
            f"Path({str(leader_path)!r}).write_text(str(os.getpid())); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(child_path)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        request = self._request(code)
        self._launch(self._supervisor(), request)
        launch_dir = self._launch_dir("launch-a")
        handshake_path = launch_dir / "handshake.json"
        _wait_for(handshake_path)
        _wait_for(leader_path)
        _wait_for(child_path)
        handshake = _read_record(handshake_path)
        wrapper_pid = handshake["wrapper_pid"]
        leader_pid = int(leader_path.read_text())
        child_pid = int(child_path.read_text())

        os.kill(wrapper_pid, signal.SIGKILL)
        restarted = self._supervisor()
        attached = self._launch(restarted, request)
        proof = attached.wait(timeout=7.0)

        self.assertEqual(proof.disposition, "killed")
        self.assertTrue(proof.started)
        self.assertEqual(restarted.validate_terminal_proof(proof), proof)
        self.assertEqual(
            restarted.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            ),
            proof,
        )
        with self.assertRaises(RealmIntegrityError):
            restarted.validate_terminal_proof(
                replace(proof, terminal_at=proof.terminal_at + 1.0)
            )
        self.assertEqual(
            _read_record(launch_dir / "result.json")["schema"],
            "optpilot.local-process-recovered-result.v1",
        )
        _wait_pid_gone(wrapper_pid)
        _wait_pid_gone(leader_pid)
        _wait_pid_gone(child_pid)

    def test_primary_death_between_fork_and_handshake_recovers_exact_group(self) -> None:
        launch_dir = self._launch_dir("launch-a")
        recovery_gate = launch_dir / "fault-recovery-ready"
        leader_path = self.base / "pre-handshake-leader.pid"
        child_path = self.base / "pre-handshake-child.pid"
        code = (
            "import os,subprocess,sys,time; from pathlib import Path; "
            f"Path({str(leader_path)!r}).write_text(str(os.getpid())); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(child_path)!r}).write_text(str(p.pid)); "
            f"Path({str(recovery_gate)!r}).write_text('ready'); time.sleep(60)"
        )
        request = self._request(code)
        handle = self._launch(
            self._supervisor(
                _wrapper_fault_mode=(
                    "primary_exit_after_worker_fork_before_handshake"
                )
            ),
            request,
        )
        handshake_path = launch_dir / "handshake.json"
        _wait_for(handshake_path)
        _wait_for(leader_path)
        _wait_for(child_path)
        wrapper_pid = _read_record(handshake_path)["wrapper_pid"]
        leader_pid = int(leader_path.read_text())
        child_pid = int(child_path.read_text())
        _wait_pid_gone(wrapper_pid)

        restarted = self._supervisor()
        replay = self._launch(restarted, request)
        proof = replay.wait(timeout=7.0)

        self.assertEqual(handle.wait(timeout=1.0), proof)
        self.assertEqual(proof.disposition, "killed")
        self.assertTrue(proof.started)
        self.assertEqual(restarted.validate_terminal_proof(proof), proof)
        self.assertEqual(
            _read_record(launch_dir / "result.json")["schema"],
            "optpilot.local-process-recovered-result.v1",
        )
        _wait_pid_gone(leader_pid)
        _wait_pid_gone(child_pid)

    def test_stop_replays_monitor_loss_recovery_after_restart(self) -> None:
        started = self.base / "monitor-loss-started"
        request = self._request(
            "from pathlib import Path; import time; "
            f"Path({str(started)!r}).write_text('started'); time.sleep(60)"
        )
        self._launch(self._supervisor(), request)
        launch_dir = self._launch_dir("launch-a")
        handshake_path = launch_dir / "handshake.json"
        _wait_for(handshake_path)
        _wait_for(started)
        wrapper_pid = _read_record(handshake_path)["wrapper_pid"]

        os.kill(wrapper_pid, signal.SIGKILL)
        restarted = self._supervisor()
        attached = self._launch(restarted, request)
        proof = attached.stop(grace_period=0.0, timeout=7.0)

        self.assertEqual(proof.disposition, "killed")
        self.assertEqual(attached.wait(timeout=1.0), proof)
        self.assertEqual(
            self._launch(self._supervisor(), request).wait(timeout=1.0), proof
        )
        self.assertEqual(restarted.validate_terminal_proof(proof), proof)

    def test_exact_terminal_lookup_rejects_substituted_coordinates(self) -> None:
        request = self._request("import time; time.sleep(60)")
        supervisor = self._supervisor()
        handle = self._launch(supervisor, request)
        self.assertIsNone(
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        )
        with self.assertRaises(RealmConflict):
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-substituted",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            )
        with self.assertRaises(RealmConflict):
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint="b" * 64,
                launch_request_digest=request.digest,
            )
        with self.assertRaises(RealmConflict):
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest="b" * 64,
            )

        proof = handle.stop(grace_period=0.0, timeout=7.0)
        self.assertEqual(
            supervisor.lookup_terminal_proof(
                launch_token="launch-a",
                binding_id="binding-a",
                evidence_fingerprint=_FINGERPRINT,
                launch_request_digest=request.digest,
            ),
            proof,
        )

    def test_authenticated_result_is_not_returned_until_lock_is_released(self) -> None:
        request = self._request("pass")
        handle = self._launch(self._supervisor(), request)
        launch_dir = self._launch_dir("launch-a")
        result_path = launch_dir / "result.json"
        lock_path = launch_dir / "liveness.lock"
        held_marker = self.base / "lock-held"
        helper_code = (
            "import fcntl,os,time; from pathlib import Path; "
            f"result=Path({str(result_path)!r}); lock=Path({str(lock_path)!r}); "
            "\nwhile not result.exists(): time.sleep(0.001)\n"
            "fd=os.open(lock,os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX); "
            f"Path({str(held_marker)!r}).write_text('held'); time.sleep(0.3)"
        )
        helper = subprocess.Popen([sys.executable, "-c", helper_code])
        try:
            _wait_for(held_marker)
            with self.assertRaises(TimeoutError):
                handle.wait(timeout=0.05)
            helper.wait(timeout=2.0)
            proof = handle.wait(timeout=2.0)
            self.assertEqual(proof.disposition, "exited")
        finally:
            if helper.poll() is None:
                helper.kill()
                helper.wait()

    def test_late_stop_preserves_already_published_natural_exit(self) -> None:
        handle = self._launch(self._supervisor(), self._request("pass"))
        launch_dir = self._launch_dir("launch-a")
        _wait_for(launch_dir / "result.json")
        lock_path = launch_dir / "liveness.lock"
        deadline = time.monotonic() + 5.0
        while True:
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    break
            finally:
                os.close(descriptor)
            if time.monotonic() >= deadline:
                self.fail("wrapper did not release its launch lock")
            time.sleep(0.01)

        proof = handle.stop(grace_period=0.0, timeout=2.0)
        self.assertEqual(proof.disposition, "exited")

    def test_stale_and_tampered_handshakes_fail_closed(self) -> None:
        active_request = self._request("import time; time.sleep(60)")
        active = self._launch(self._supervisor(), active_request)
        handshake_path = self._launch_dir("launch-a") / "handshake.json"
        _wait_for(handshake_path)
        original = handshake_path.read_bytes()
        tampered = json.loads(original.decode("utf-8"))
        tampered["worker_pid"] += 1
        handshake_path.write_bytes(canonical_json_bytes(tampered))
        try:
            with self.assertRaises(RealmIntegrityError):
                self._launch(self._supervisor(), active_request)
        finally:
            handshake_path.write_bytes(original)
            active.stop(grace_period=0.05, timeout=5.0)

        source_request = self._request("pass")
        source = self._launch(
            self._supervisor(), source_request, token="launch-source"
        )
        source.wait(timeout=5.0)
        source_handshake = (
            self._launch_dir("launch-source") / "handshake.json"
        ).read_bytes()

        def crash(_: str) -> None:
            raise _SimulatedParentCrash()

        stale_request = self._request("print('must not run')")
        with self.assertRaises(_SimulatedParentCrash):
            self._launch(
                self._supervisor(fault_injector=crash),
                stale_request,
                token="launch-stale",
            )
        (self._launch_dir("launch-stale") / "handshake.json").write_bytes(
            source_handshake
        )
        with self.assertRaises(RealmIntegrityError):
            self._launch(
                self._supervisor(), stale_request, token="launch-stale"
            )

    def test_live_pid_without_inherited_lock_is_not_attachment_authority(self) -> None:
        request = self._request("print('must not run')")

        def crash(_: str) -> None:
            raise _SimulatedParentCrash()

        with self.assertRaises(_SimulatedParentCrash):
            self._launch(self._supervisor(fault_injector=crash), request)

        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            manifest = _read_record(self._launch_dir("launch-a") / "manifest.json")
            identity_keys = {
                "backend_token",
                "binding_id",
                "evidence_fingerprint",
                "launch_request_digest",
                "launch_token",
                "lock_device",
                "lock_inode",
                "provider_generation",
            }
            handshake = {key: manifest[key] for key in identity_keys}
            handshake.update(
                {
                    "schema": "optpilot.local-process-handshake.v1",
                    "worker_pgid": sleeper.pid,
                    "worker_pid": sleeper.pid,
                    "wrapper_pid": os.getpid(),
                }
            )
            _write_record(
                self._launch_dir("launch-a") / "handshake.json", handshake
            )

            with self.assertRaises(RealmIntegrityError):
                self._launch(self._supervisor(), request)
            self.assertTrue(_pid_exists(sleeper.pid))
        finally:
            try:
                os.killpg(sleeper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            sleeper.wait(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
