"""Run one local Harbor job independently of the lifetime of an OptPilot client."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def atomic_json(path, payload):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def claim(directory):
    """Also pass the claim to Harbor on POSIX so an orphan cannot be launched twice."""
    with (directory / "supervisor.lock").open("a+b") as lock:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                lock.seek(0)
                if not lock.read(1):
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, PermissionError):
            yield None
            return
        yield lock


def supervise(directory, executable):
    with claim(directory) as lock:
        receipt = directory / "exit.json"
        if lock is None or receipt.is_file():
            return
        job = directory / "jobs" / "evaluation"
        result = job / "result.json"
        if result.is_file() and json.loads(result.read_bytes()).get("finished_at"):
            atomic_json(receipt, {"code": 0})
            return
        if (directory / "cancel").exists():
            atomic_json(receipt, {"cancelled": True, "code": 0})
            return
        argv = ([executable, "job", "resume", "--job-path", str(job)] if (job / "config.json").exists()
                else [executable, "run", "--config", str(directory / "study.json")])
        options = ({"start_new_session": True, "pass_fds": (lock.fileno(),)}
                   if os.name == "posix" else {})
        with (directory / "runner.log").open("ab") as log:
            child = subprocess.Popen(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, **options)
            cancelled_at = None
            while child.poll() is None:
                if (directory / "cancel").exists():
                    if cancelled_at is None:
                        cancelled_at = time.monotonic()
                        if os.name == "posix":
                            os.killpg(child.pid, signal.SIGINT)
                        else:
                            child.terminate()
                    elif time.monotonic() - cancelled_at > 20:
                        if os.name == "posix":
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
                time.sleep(0.2)
            atomic_json(receipt, {"code": child.returncode, "cancelled": cancelled_at is not None})


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    try:
        supervise(directory, sys.argv[2])
    except Exception as exc:
        atomic_json(directory / "exit.json", {"code": 1, "error": str(exc)})

