"""Private POSIX monitor used by :mod:`local_process_supervisor`.

This file deliberately uses only the Python standard library so the provider
can invoke it by absolute path without depending on an installed OptPilot
package.  The parent supervisor passes one already-locked file descriptor.
The primary monitor, its worker child, and a small recovery watchdog retain
that descriptor until the worker is terminal.  The watchdog normally only
verifies the primary's published result.  If the primary dies after the
authenticated handshake, the watchdog still has continuous lock authority,
kills and drains the exact process group, and publishes a distinct recovered
result.  Thus a monitor crash cannot turn a recorded PID/PGID into standalone
authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Any


_MANIFEST_SCHEMA = "optpilot.local-process-launch-manifest.v1"
_HANDSHAKE_SCHEMA = "optpilot.local-process-handshake.v1"
_RESULT_SCHEMA = "optpilot.local-process-result.v1"
_RECOVERED_RESULT_SCHEMA = "optpilot.local-process-recovered-result.v1"
_STOP_SCHEMA = "optpilot.local-process-stop.v1"
_REQUEST_FORMAT = "optpilot.local-process-launch-request.v2"
_PRIVATE_ENVIRONMENT_SCHEMA = "optpilot.local-process-private-environment.v1"
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_DESCENDANT_TERM_GRACE_SECONDS = 0.25
_DESCENDANT_KILL_WAIT_SECONDS = 5.0
_GROUP_POLL_SECONDS = 0.01
_AUTHORITY_READY = b"H"
_AUTHORITY_TERM = b"T"
_AUTHORITY_KILL = b"K"
_SENTINEL_READY = b"R"
_FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK = (
    "primary_exit_after_worker_fork_before_handshake"
)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_digest(request: dict[str, Any]) -> str:
    payload = {"format": _REQUEST_FORMAT, **request}
    return hashlib.sha256(
        b"optpilot/request/v1\0" + _canonical_json_bytes(payload)
    ).hexdigest()


def _record_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        b"optpilot/local-process-record/v1\0" + _canonical_json_bytes(payload)
    ).hexdigest()


def _read_canonical_record(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("provider record is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RECORD_BYTES:
                raise RuntimeError("provider record exceeds its size bound")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    encoded = b"".join(chunks)
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, dict) or _canonical_json_bytes(payload) != encoded:
        raise RuntimeError("provider record is not canonical JSON")
    recorded_digest = payload.pop("record_digest", None)
    if recorded_digest != _record_digest(payload):
        raise RuntimeError("provider record digest does not match")
    return payload


def _publish_record(path: Path, payload: dict[str, Any]) -> None:
    body = dict(payload)
    body["record_digest"] = _record_digest(body)
    encoded = _canonical_json_bytes(body)
    if len(encoded) > _MAX_RECORD_BYTES:
        raise RuntimeError("provider record exceeds its size bound")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("short provider record write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # Linking rather than replacing makes duplicate publication fail
        # closed; a stale or injected handshake can never be hidden.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validate_manifest(manifest: dict[str, Any], lock_fd: int) -> None:
    expected = {
        "backend_token",
        "binding_id",
        "coordinate",
        "evidence_fingerprint",
        "handshake_path",
        "launch_request",
        "launch_request_digest",
        "launch_token",
        "lock_device",
        "lock_inode",
        "provider_generation",
        "result_path",
        "schema",
        "stop_path",
    }
    if set(manifest) != expected or manifest.get("schema") != _MANIFEST_SCHEMA:
        raise RuntimeError("launch manifest fields differ")
    request = manifest["launch_request"]
    if not isinstance(request, dict) or set(request) != {
        "argv",
        "cwd",
        "env",
        "private_env_binding_revision",
        "private_env_names",
    }:
        raise RuntimeError("launch request fields differ")
    private_names = request["private_env_names"]
    if (
        not isinstance(private_names, list)
        or any(
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            for name in private_names
        )
        or private_names != sorted(set(private_names))
    ):
        raise RuntimeError("private environment names are invalid")
    private_revision = request["private_env_binding_revision"]
    if private_names:
        if not isinstance(private_revision, str) or not private_revision:
            raise RuntimeError("private environment binding revision is invalid")
    elif private_revision is not None:
        raise RuntimeError("private environment binding revision is unexpected")
    environment = request["env"]
    if (
        not isinstance(environment, dict)
        or any(
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or not isinstance(value, str)
            or "\x00" in value
            for name, value in environment.items()
        )
        or set(environment).intersection(private_names)
    ):
        raise RuntimeError("launch request environment is invalid")
    if _request_digest(request) != manifest["launch_request_digest"]:
        raise RuntimeError("launch request digest does not match")
    metadata = os.fstat(lock_fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("launch lock is not a regular file")
    if (
        metadata.st_dev != manifest["lock_device"]
        or metadata.st_ino != manifest["lock_inode"]
    ):
        raise RuntimeError("launch lock identity does not match")


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise RuntimeError("private environment pipe ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_private_environment(
    descriptor: int,
    request: dict[str, Any],
) -> dict[str, str]:
    """Read one canonical private environment frame from an anonymous pipe."""

    private_names = request["private_env_names"]
    if not private_names:
        if descriptor >= 0:
            raise RuntimeError("private environment pipe is unexpected")
        return {}
    if descriptor < 0:
        raise RuntimeError("private environment pipe is missing")
    try:
        header = _read_exact(descriptor, 8)
        payload_size = int.from_bytes(header, "big")
        if payload_size <= 0 or payload_size > _MAX_RECORD_BYTES:
            raise RuntimeError("private environment payload size is invalid")
        encoded = _read_exact(descriptor, payload_size)
        if os.read(descriptor, 1):
            raise RuntimeError("private environment pipe has trailing data")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("private environment payload is not JSON") from error
    if (
        not isinstance(payload, dict)
        or _canonical_json_bytes(payload) != encoded
        or set(payload) != {"binding_revision", "env", "schema"}
        or payload.get("schema") != _PRIVATE_ENVIRONMENT_SCHEMA
        or payload.get("binding_revision")
        != request["private_env_binding_revision"]
    ):
        raise RuntimeError("private environment payload identity differs")
    private_environment = payload["env"]
    if (
        not isinstance(private_environment, dict)
        or set(private_environment) != set(private_names)
        or any(
            not isinstance(value, str) or "\x00" in value
            for value in private_environment.values()
        )
    ):
        raise RuntimeError("private environment payload values are invalid")
    return private_environment


def _identity_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend_token": manifest["backend_token"],
        "binding_id": manifest["binding_id"],
        "evidence_fingerprint": manifest["evidence_fingerprint"],
        "launch_request_digest": manifest["launch_request_digest"],
        "launch_token": manifest["launch_token"],
        "lock_device": manifest["lock_device"],
        "lock_inode": manifest["lock_inode"],
        "provider_generation": manifest["provider_generation"],
    }


def _stop_was_requested(
    manifest: dict[str, Any], stop_path: Path
) -> float | None:
    if not os.path.lexists(stop_path):
        return None
    stop = _read_canonical_record(stop_path)
    expected_keys = set(_identity_fields(manifest)) | {"grace_period", "schema"}
    if set(stop) != expected_keys or stop.get("schema") != _STOP_SCHEMA:
        raise RuntimeError("stop record does not match launch identity")
    for key, value in _identity_fields(manifest).items():
        if stop.get(key) != value:
            raise RuntimeError("stop record does not match launch identity")
    grace_period = stop.get("grace_period")
    if (
        isinstance(grace_period, bool)
        or not isinstance(grace_period, (int, float))
        or not float("-inf") < float(grace_period) < float("inf")
        or grace_period < 0
    ):
        raise RuntimeError("stop record grace period is invalid")
    return float(grace_period)


def _read_validated_handshake(
    manifest: dict[str, Any], handshake_path: Path
) -> dict[str, Any]:
    handshake = _read_canonical_record(handshake_path)
    expected_keys = set(_identity_fields(manifest)) | {
        "schema",
        "worker_pgid",
        "worker_pid",
        "wrapper_pid",
    }
    if (
        set(handshake) != expected_keys
        or handshake.get("schema") != _HANDSHAKE_SCHEMA
    ):
        raise RuntimeError("local process handshake fields differ")
    for key, value in _identity_fields(manifest).items():
        if handshake.get(key) != value:
            raise RuntimeError("local process handshake identity does not match")
    worker_pid = handshake.get("worker_pid")
    worker_pgid = handshake.get("worker_pgid")
    wrapper_pid = handshake.get("wrapper_pid")
    if (
        isinstance(worker_pid, bool)
        or not isinstance(worker_pid, int)
        or worker_pid <= 1
        or worker_pgid != worker_pid
        or isinstance(wrapper_pid, bool)
        or not isinstance(wrapper_pid, int)
        or wrapper_pid <= 1
        or wrapper_pid == worker_pid
    ):
        raise RuntimeError("local process handshake PID fields are invalid")
    return handshake


def _waitpid(worker_pid: int) -> int:
    while True:
        try:
            waited_pid, status = os.waitpid(worker_pid, 0)
        except InterruptedError:
            continue
        if waited_pid != worker_pid:
            raise RuntimeError("waitpid returned an unexpected worker")
        return status


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group that exists but cannot be signalled is not terminal.  The
        # eventual real signal below will fail closed instead of publishing a
        # false result.
        return True
    return True


def _wait_for_group_exit(process_group: int, deadline: float) -> bool:
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_POLL_SECONDS)
    return True


def _drain_process_group(process_group: int) -> None:
    """Ensure descendants cannot outlive the result published for a leader."""

    if not _process_group_exists(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_for_group_exit(
        process_group, time.monotonic() + _DESCENDANT_TERM_GRACE_SECONDS
    ):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not _wait_for_group_exit(
        process_group, time.monotonic() + _DESCENDANT_KILL_WAIT_SECONDS
    ):
        raise RuntimeError("worker process group survived SIGKILL")


def _read_pipe_byte(read_fd: int) -> bytes:
    while True:
        try:
            return os.read(read_fd, 1)
        except InterruptedError:
            continue


def _write_pipe_byte(write_fd: int, value: bytes) -> None:
    if len(value) != 1:
        raise RuntimeError("wrapper pipe messages must be one byte")
    while True:
        try:
            written = os.write(write_fd, value)
        except InterruptedError:
            continue
        if written != 1:
            raise RuntimeError("short wrapper pipe write")
        return


def _wait_for_primary_exit(read_fd: int) -> None:
    os.set_blocking(read_fd, True)
    if _read_pipe_byte(read_fd):
        raise RuntimeError("primary monitor pipe carried unexpected data")


def _wait_for_primary_exit_or_stop(
    read_fd: int,
    manifest: dict[str, Any],
) -> tuple[str, float | None]:
    os.set_blocking(read_fd, False)
    stop_path = Path(manifest["stop_path"])
    while True:
        try:
            value = os.read(read_fd, 1)
        except BlockingIOError:
            pass
        except InterruptedError:
            continue
        else:
            if value:
                raise RuntimeError("primary monitor pipe carried unexpected data")
            return ("primary_lost", None)
        grace_period = _stop_was_requested(manifest, stop_path)
        if grace_period is not None:
            return ("stop_requested", grace_period)
        time.sleep(_GROUP_POLL_SECONDS)


def _wait_for_authority_exit(read_fd: int, deadline: float) -> None:
    os.set_blocking(read_fd, False)
    while True:
        try:
            value = os.read(read_fd, 1)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("worker group authority did not close")
            time.sleep(_GROUP_POLL_SECONDS)
            continue
        except InterruptedError:
            continue
        if value:
            raise RuntimeError("worker group authority sent unexpected data")
        return


def _wait_for_fault_recovery_gate(manifest_path: Path) -> None:
    """Deterministic test gate for the injected pre-handshake crash window."""

    path = manifest_path.parent / "fault-recovery-ready"
    deadline = time.monotonic() + _DESCENDANT_KILL_WAIT_SECONDS
    while not os.path.lexists(path):
        if time.monotonic() >= deadline:
            raise RuntimeError("wrapper recovery fault gate was not published")
        time.sleep(_GROUP_POLL_SECONDS)
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise RuntimeError("wrapper recovery fault gate identity is invalid")


def _run_group_authority_sentinel(
    state_write_fd: int,
    command_read_fd: int,
    ready_write_fd: int,
) -> None:
    """Hold exact group authority and execute recovery from inside the group."""

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        _write_pipe_byte(state_write_fd, _AUTHORITY_READY)
        _write_pipe_byte(ready_write_fd, _SENTINEL_READY)
    finally:
        os.close(ready_write_fd)
    # EOF is itself a monitor-loss command: if the watchdog disappears, the
    # sentinel fails closed instead of leaving an unsupervised user group.
    while True:
        command = _read_pipe_byte(command_read_fd)
        signal_number = (
            signal.SIGTERM if command == _AUTHORITY_TERM else signal.SIGKILL
        )
        try:
            # The sentinel is a member of the exact worker group.  It
            # deliberately derives the target from its own kernel identity
            # rather than using the recorded PGID as a signalling capability.
            os.killpg(os.getpgrp(), signal_number)
        except BaseException:
            os._exit(70)
        if signal_number == signal.SIGKILL:
            os._exit(70)


def _publish_recovered_result(
    manifest: dict[str, Any],
    handshake: dict[str, Any],
    result_path: Path,
) -> None:
    proof = {
        "backend_token": manifest["backend_token"],
        "binding_id": manifest["binding_id"],
        "disposition": "killed",
        "evidence_fingerprint": manifest["evidence_fingerprint"],
        "launch_request_digest": manifest["launch_request_digest"],
        "launch_token": manifest["launch_token"],
        "provider_generation": manifest["provider_generation"],
        "terminal_at": time.time(),
    }
    result = {
        **_identity_fields(manifest),
        "proof": proof,
        "recovery_reason": "primary_monitor_lost",
        "schema": _RECOVERED_RESULT_SCHEMA,
        "worker_pgid": handshake["worker_pgid"],
        "worker_pid": handshake["worker_pid"],
    }
    _publish_record(result_path, result)


def _watch_primary(
    manifest_path: Path,
    lock_fd: int,
    primary_read_fd: int,
    state_read_fd: int,
    command_write_fd: int,
    fault_mode: str | None,
) -> int:
    """Recover if the primary monitor exits without publishing a result.

    The watchdog inherited the already-locked open file description before
    the worker was forked.  It therefore retains continuous provider authority
    across primary-monitor death and never treats a recorded PID alone as a
    signalling capability.
    """

    manifest = _read_canonical_record(manifest_path)
    _validate_manifest(manifest, lock_fd)
    result_path = Path(manifest["result_path"])
    event, grace_period = _wait_for_primary_exit_or_stop(
        primary_read_fd, manifest
    )
    if os.path.lexists(result_path):
        # Complete schema validation stays in the supervisor.  A canonical
        # read here proves the primary did not leave a partial publication.
        _read_canonical_record(result_path)
        return 0

    authority_state = _read_pipe_byte(state_read_fd)
    handshake_path = Path(manifest["handshake_path"])
    if authority_state == b"":
        # EOF before the sentinel's ready byte proves that no user executable
        # was entered: the controlled worker publishes its handshake first,
        # then establishes the sentinel, and only proceeds to exec after the
        # ready byte.  A published handshake still means physical start and
        # therefore receives a recovered killed proof.
        if not os.path.lexists(handshake_path):
            return 125
        handshake = _read_validated_handshake(manifest, handshake_path)
    elif authority_state == _AUTHORITY_READY:
        handshake = _read_validated_handshake(manifest, handshake_path)
        if fault_mode == _FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK:
            _wait_for_fault_recovery_gate(manifest_path)
        if event == "stop_requested" and grace_period is not None:
            try:
                _write_pipe_byte(command_write_fd, _AUTHORITY_TERM)
            except BrokenPipeError:
                pass
            if grace_period > 0:
                time.sleep(grace_period)
        try:
            _write_pipe_byte(command_write_fd, _AUTHORITY_KILL)
        except BrokenPipeError as error:
            if not os.path.lexists(result_path):
                raise RuntimeError(
                    "worker group authority was lost before recovery command"
                ) from error
        _wait_for_authority_exit(
            state_read_fd, time.monotonic() + _DESCENDANT_KILL_WAIT_SECONDS
        )
        # This is a terminal check only.  Signalling was performed from inside
        # the exact group by the authority sentinel, never from this old PGID.
        if not _wait_for_group_exit(
            handshake["worker_pgid"],
            time.monotonic() + _DESCENDANT_KILL_WAIT_SECONDS,
        ):
            raise RuntimeError("recovered worker process group remained present")
    else:
        raise RuntimeError("worker group authority state is invalid")

    if event == "stop_requested":
        _wait_for_primary_exit(primary_read_fd)
        if os.path.lexists(result_path):
            _read_canonical_record(result_path)
            return 0

    # Validate an optional stop request, but monitor loss itself is sufficient
    # reason for the fail-closed killed disposition.
    _stop_was_requested(manifest, Path(manifest["stop_path"]))
    _publish_recovered_result(manifest, handshake, result_path)
    return 0


def _run(
    manifest_path: Path,
    lock_fd: int,
    private_environment_fd: int = -1,
    *,
    fault_mode: str | None = None,
) -> int:
    try:
        manifest = _read_canonical_record(manifest_path)
        _validate_manifest(manifest, lock_fd)
    except BaseException:
        if private_environment_fd >= 0:
            os.close(private_environment_fd)
        raise
    handshake_path = Path(manifest["handshake_path"])
    result_path = Path(manifest["result_path"])
    stop_path = Path(manifest["stop_path"])

    if fault_mode not in {None, _FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK}:
        raise RuntimeError("unsupported wrapper fault mode")

    wrapper_pid = os.getpid()
    primary_read_fd, primary_write_fd = os.pipe()
    state_read_fd, state_write_fd = os.pipe()
    command_read_fd, command_write_fd = os.pipe()
    watchdog_pid = os.fork()
    if watchdog_pid == 0:
        try:
            if private_environment_fd >= 0:
                os.close(private_environment_fd)
            os.close(primary_write_fd)
            os.close(state_write_fd)
            os.close(command_read_fd)
            return _watch_primary(
                manifest_path,
                lock_fd,
                primary_read_fd,
                state_read_fd,
                command_write_fd,
                fault_mode,
            )
        except BaseException:
            return 70
        finally:
            os.close(primary_read_fd)
            os.close(state_read_fd)
            os.close(command_write_fd)

    os.close(primary_read_fd)
    os.close(state_read_fd)
    os.close(command_write_fd)
    fault_read_fd: int | None = None
    fault_write_fd: int | None = None
    if fault_mode == _FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK:
        fault_read_fd, fault_write_fd = os.pipe()
    worker_pid = os.fork()
    if worker_pid == 0:
        try:
            os.close(primary_write_fd)
            if fault_read_fd is not None and fault_write_fd is not None:
                os.close(fault_write_fd)
                # The primary exits without writing.  EOF therefore proves the
                # fault happened after fork but before this worker can publish
                # its handshake.
                if _read_pipe_byte(fault_read_fd) != b"":
                    raise RuntimeError("wrapper fault gate carried data")
                os.close(fault_read_fd)
            os.setsid()
            worker_pid = os.getpid()
            worker_pgid = os.getpgrp()
            os.set_inheritable(lock_fd, True)
            handshake = {
                **_identity_fields(manifest),
                "schema": _HANDSHAKE_SCHEMA,
                "worker_pgid": worker_pgid,
                "worker_pid": worker_pid,
                "wrapper_pid": wrapper_pid,
            }
            _publish_record(handshake_path, handshake)

            # Establish an exact, provider-controlled member of this process
            # group before entering user code.  It reports readiness through
            # ``state_write_fd`` and accepts recovery only through
            # ``command_read_fd``.  A short double fork prevents the sentinel
            # from becoming a child visible to the user's executable.
            sentinel_ready_read_fd, sentinel_ready_write_fd = os.pipe()
            sentinel_parent_pid = os.fork()
            if sentinel_parent_pid == 0:
                try:
                    if private_environment_fd >= 0:
                        os.close(private_environment_fd)
                    os.close(sentinel_ready_read_fd)
                    sentinel_pid = os.fork()
                    if sentinel_pid == 0:
                        _run_group_authority_sentinel(
                            state_write_fd,
                            command_read_fd,
                            sentinel_ready_write_fd,
                        )
                    os._exit(0)
                except BaseException:
                    os._exit(70)
            os.close(sentinel_ready_write_fd)
            sentinel_parent_status = _waitpid(sentinel_parent_pid)
            if not os.WIFEXITED(sentinel_parent_status) or os.WEXITSTATUS(
                sentinel_parent_status
            ) != 0:
                raise RuntimeError("worker group authority fork failed")
            if _read_pipe_byte(sentinel_ready_read_fd) != _SENTINEL_READY:
                raise RuntimeError("worker group authority did not become ready")
            os.close(sentinel_ready_read_fd)
            os.close(command_read_fd)
            os.set_inheritable(state_write_fd, True)
            request = manifest["launch_request"]
            private_environment = _read_private_environment(
                private_environment_fd,
                request,
            )
            private_environment_fd = -1
            os.chdir(request["cwd"])
            argv = request["argv"]
            env = dict(request["env"])
            env.update(private_environment)
            os.execvpe(argv[0], argv, env)
        except BaseException:
            os._exit(127)

    if private_environment_fd >= 0:
        os.close(private_environment_fd)
        private_environment_fd = -1
    os.close(state_write_fd)
    os.close(command_read_fd)
    if fault_mode == _FAULT_PRIMARY_EXIT_AFTER_WORKER_FORK:
        # The child is blocked on ``fault_read_fd`` until this process exits
        # and the kernel closes ``fault_write_fd``.  This makes the injected
        # pre-handshake primary-loss window deterministic rather than timed.
        os._exit(91)
    if fault_read_fd is not None:
        os.close(fault_read_fd)
    if fault_write_fd is not None:
        os.close(fault_write_fd)

    try:
        status = _waitpid(worker_pid)
        try:
            handshake = _read_validated_handshake(manifest, handshake_path)
        except (FileNotFoundError, RuntimeError, ValueError, OSError):
            # The worker never crossed the authenticated pre-exec handshake.
            # The supervisor will issue a durable ``never_started`` proof once
            # both primary and watchdog release the inherited lock.
            return 125

        _drain_process_group(handshake["worker_pgid"])

        was_stopped = _stop_was_requested(manifest, stop_path)
        disposition = (
            "killed"
            if was_stopped is not None or os.WIFSIGNALED(status)
            else "exited"
        )
        proof = {
            "backend_token": manifest["backend_token"],
            "binding_id": manifest["binding_id"],
            "disposition": disposition,
            "evidence_fingerprint": manifest["evidence_fingerprint"],
            "launch_request_digest": manifest["launch_request_digest"],
            "launch_token": manifest["launch_token"],
            "provider_generation": manifest["provider_generation"],
            "terminal_at": time.time(),
        }
        result = {
            **_identity_fields(manifest),
            "proof": proof,
            "schema": _RESULT_SCHEMA,
            "wait_status": status,
            "worker_pgid": handshake["worker_pgid"],
            "worker_pid": handshake["worker_pid"],
        }
        _publish_record(result_path, result)
        return 0
    finally:
        os.close(primary_write_fd)
        # Reap the watchdog on the normal path.  If this primary is killed, EOF
        # still wakes the orphaned watchdog and it performs recovery.
        try:
            _waitpid(watchdog_pid)
        except (ChildProcessError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in {2, 3, 4}:
        return 64
    private_environment_fd = -1
    try:
        manifest_path = Path(arguments[0])
        lock_fd = int(arguments[1])
        fault_mode = None
        if len(arguments) == 3:
            # Accept the old private wrapper form for tests and a clean
            # provider upgrade.  New supervisors always pass an integer
            # descriptor (``-1`` when no private environment is declared).
            try:
                private_environment_fd = int(arguments[2])
            except ValueError:
                fault_mode = arguments[2]
        elif len(arguments) == 4:
            private_environment_fd = int(arguments[2])
            fault_mode = arguments[3]
        selected_private_environment_fd = private_environment_fd
        private_environment_fd = -1
        return _run(
            manifest_path,
            lock_fd,
            selected_private_environment_fd,
            fault_mode=fault_mode,
        )
    except BaseException:
        return 70
    finally:
        if private_environment_fd >= 0:
            try:
                os.close(private_environment_fd)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
