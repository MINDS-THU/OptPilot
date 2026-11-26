"""Standalone POSIX SSH-side supervisor for frozen, trusted native trials."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


def write_json(path, value):
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    os.replace(scratch, path)


def state(path):
    return json.loads((path / "state.json").read_text())


def supervise(path):
    with (path / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if state(path)["state"] != "queued":
            return
        if (path / "cancel").exists():
            write_json(path / "state.json", {"state": "cancelled"})
            return
        started = time.monotonic()
        write_json(path / "state.json", {"state": "running"})
        try:
            payload = json.loads((path / "payload.json").read_text())
            timeout = payload.pop("_timeout")
            payload["context"]["workspace"] = str(path / "grader")
            if payload["candidate"]["format"] == "program":
                payload["candidate"]["directory"] = str(path / "candidate")
            with (path / "input.json").open("wb") as src:
                src.write(json.dumps(payload).encode() + b"\n")
            with (path / "input.json").open("rb") as src, (path / "out").open("wb") as out, (path / "err").open("wb") as err:
                child = subprocess.Popen([sys.executable, "-I", "-B", "-u", str(path / "worker.py"), "evaluate"],
                                         cwd=path / "grader", stdin=src, stdout=out, stderr=err, start_new_session=True)
                reason = None
                while child.poll() is None:
                    if (path / "cancel").exists() or time.monotonic() - started >= timeout:
                        reason = "cancelled" if (path / "cancel").exists() else "timed out"
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        break
                    time.sleep(.15)
            with (path / "err").open("rb") as err:
                err.seek(max(0, (path / "err").stat().st_size - 16000))
                logs = err.read().decode("utf-8", "replace")
            if reason:
                result = {"state": "cancelled" if reason == "cancelled" else "failed", "error": reason}
            elif child.returncode or (path / "out").stat().st_size > 8 * 1024 * 1024:
                result = {"state": "failed", "error": logs or "Worker failed or exceeded the result size limit"}
            else:
                reply = json.loads((path / "out").read_bytes())
                result = ({"state": "failed", "error": str(reply["error"])[-16000:]} if "error" in reply
                          else {"state": "succeeded", "value": reply["value"], "logs": logs})
        except Exception as exc:
            result = {"state": "failed", "error": str(exc)}
        result["elapsedSeconds"] = time.monotonic() - started
        write_json(path / "state.json", result)


def unpack(data, directory):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        size = 0
        for item in archive:
            name = Path(item.name)
            if (not name.parts or name.is_absolute() or ".." in name.parts or
                (name.parts[0] not in ("grader", "candidate") and item.name not in ("worker.py", "driver.py", "payload.json"))):
                raise ValueError("Invalid trial archive entry")
            target = directory / name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                size += item.size
                if size > 640 * 1024 * 1024:
                    raise ValueError("Trial bundle exceeds the size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                target.chmod(0o700 if item.mode & 0o111 else 0o600)
            else:
                raise ValueError("Expected regular files and directories")


def dispatch(action, identity):
    if action == "probe":
        return {"state": "ready", "python": sys.version.split()[0]}
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid trial identity")
    base = Path.home() / ".local" / "share" / "optpilot" / "ssh-trials"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = base / identity
    if action == "submit":
        data = sys.stdin.buffer.read(256 * 1024 * 1024 + 1)
        if len(data) > 256 * 1024 * 1024:
            raise ValueError("Trial bundle exceeds upload limit")
        with (base / (identity + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not path.exists():
                with tempfile.TemporaryDirectory(dir=base) as scratch:
                    stage = Path(scratch) / "trial"
                    stage.mkdir()
                    unpack(data, stage)
                    write_json(stage / "state.json", {"state": "queued"})
                    stage.rename(path)
            if state(path)["state"] == "queued":
                subprocess.Popen([sys.executable, "-I", str(path / "driver.py"), "supervise", str(path)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
    if not path.is_dir():
        return {"state": "missing"}
    if action == "cancel":
        (path / "cancel").touch()
    result = state(path)
    if result["state"] == "running":
        with (path / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                result = state(path)
                if result["state"] == "running":
                    result = {"state": "uncertain", "error": "Supervisor stopped before recording an outcome"}
    return result


if __name__ == "__main__":
    if sys.argv[1] == "supervise":
        supervise(Path(sys.argv[2]))
    else:
        print(json.dumps(dispatch(sys.argv[1], sys.argv[2]), allow_nan=False))

