"""Holding one method container and exchanging frames with it.

No container software here: the "engine" is a stand-in program that speaks the
same stream, so these prove the host side's behaviour -- bounded waits, honest
death reporting, polite stop -- independently of Docker being installed.
"""

import json
import os
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from optpilot.container_launch import LaunchSpec
from optpilot.container_method_process import (
    ContainerMethodProcess,
    ContainerWorkerDied,
)

_HEADER = struct.Struct("!I")
IMAGE = "ghcr.io/example/pkg@sha256:" + "a" * 64

_FAKE_ENGINE = '''#!{python}
import json, struct, sys
if len(sys.argv) > 1 and sys.argv[1] == "rm":
    sys.exit(0)  # removal of the named container: always succeeds
mode = {mode!r}
header = struct.Struct("!I")
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
if mode == "silent":
    stdin.read()  # hold the stream open, never answer
    sys.exit(0)
if mode == "die-after-read":
    raw = stdin.read(header.size)
    if raw:
        (size,) = header.unpack(raw)
        stdin.read(size)
    sys.exit(7)
while True:  # mode == "echo"
    raw = stdin.read(header.size)
    if not raw:
        sys.exit(0)  # end-of-file: nobody left to answer
    (size,) = header.unpack(raw)
    body = stdin.read(size)
    request = json.loads(body)
    answer = json.dumps({{"ok": True, "echo": request.get("op")}}).encode()
    stdout.write(header.pack(len(answer)) + answer)
    stdout.flush()
'''


class ContainerMethodProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _engine(self, mode: str) -> str:
        path = self.root / f"fake-engine-{mode}"
        path.write_text(_FAKE_ENGINE.format(python=sys.executable, mode=mode))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _spec(self) -> LaunchSpec:
        return LaunchSpec(
            image=IMAGE,
            platform="linux/amd64",
            name="optpilot-mw-test",
            command=["python3", "-m", "optpilot.retained_batch_worker", "--stdio", "x"],
            interactive=True,
        )

    def _start(self, mode: str) -> ContainerMethodProcess:
        process = ContainerMethodProcess(
            self._engine(mode), self._spec(), stderr_target=None
        )
        self.addCleanup(process.stop, grace_seconds=5.0)
        return process

    def test_an_exchange_rides_the_stream_and_back(self) -> None:
        process = self._start("echo")
        answer = process.request({"op": "status"}, timeout=30.0)
        self.assertEqual(answer, {"ok": True, "echo": "status"})
        again = process.request({"op": "propose"}, timeout=30.0)
        self.assertEqual(again["echo"], "propose")

    def test_a_worker_that_never_answers_is_a_bounded_wait(self) -> None:
        process = self._start("silent")
        with self.assertRaises(TimeoutError) as caught:
            process.request({"op": "status"}, timeout=1.5)
        self.assertIn("time limit", str(caught.exception))

    def test_a_death_mid_exchange_reports_the_exit_code(self) -> None:
        process = self._start("die-after-read")
        with self.assertRaises(ContainerWorkerDied) as caught:
            process.request({"op": "status"}, timeout=30.0)
        self.assertEqual(caught.exception.exit_code, 7)

    def test_stop_is_polite_first(self) -> None:
        # Closing the stream is the signal; the responder exits on end-of-file
        # without being terminated.
        process = self._start("echo")
        process.request({"op": "status"}, timeout=30.0)
        code = process.stop(grace_seconds=10.0)
        self.assertEqual(code, 0)
        self.assertTrue(process._stream.reader.closed)
        self.assertTrue(process._stream.writer.closed)

    def test_the_drop_in_client_ignores_the_socket_path(self) -> None:
        process = self._start("echo")
        client = process.request_client()
        answer = client(None, {"op": "ack"}, timeout=30.0)
        self.assertEqual(answer["echo"], "ack")


if __name__ == "__main__":
    unittest.main()
