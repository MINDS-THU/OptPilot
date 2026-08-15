"""Holding one method container for the whole of one run.

The design gives a method one container per run: started once, receiving each
round of proposals over a stream that stays open, answering on the same stream.
This module owns that container's lifetime on the host side -- starting it,
exchanging frames with it, deciding whether it is still alive, and stopping it.

Liveness here is deliberately simpler than the process supervisor's. The engine
client is a direct child of this process, so a dead child is visible to an
ordinary poll, and the container itself can be asked about by name. What is NOT
supported is surviving a restart of the controller: the stream lives in this
process, so a new controller cannot re-attach to it. A leftover container from a
previous controller generation is removed, never adopted.
"""

from __future__ import annotations

import selectors
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping, Optional, Sequence

from .container_launch import LaunchSpec, build_container_command

__all__ = [
    "ContainerMethodProcess",
    "ContainerWorkerDied",
    "remove_container_if_present",
]

_HEADER = struct.Struct("!I")

#: One frame may not exceed this, matching the worker's own bound.
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class ContainerWorkerDied(RuntimeError):
    """The container's side of the stream is gone.

    Carries what could be learned about why, so the failure that reaches the
    record names the container's exit rather than a bare timeout.
    """

    def __init__(self, message: str, *, exit_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def remove_container_if_present(engine: str, name: str) -> None:
    """Remove a named container, treating absence as success.

    Used against leftovers from a previous controller generation: the stream
    they were started with died with their controller, so they can never be
    spoken to again and are removed rather than adopted.
    """

    subprocess.run(
        [engine, "rm", "--force", name],
        capture_output=True,
        timeout=60,
        check=False,
    )


@dataclass
class _Stream:
    reader: BinaryIO
    writer: BinaryIO


class ContainerMethodProcess:
    """One started method container and the stream it answers on."""

    def __init__(
        self,
        engine: str,
        spec: LaunchSpec,
        *,
        stderr_target: Any,
    ) -> None:
        self._engine = engine
        self._name = spec.name
        argv = build_container_command(engine, spec)
        # The engine client is a direct child: its death is an ordinary poll,
        # and killing it tears the container down with it (the engine forwards
        # the signal to the container's first process).
        # bufsize=0 keeps both pipes raw. A buffered reader would slurp every
        # available byte on the first read, leaving the descriptor empty -- and
        # a readiness wait on an empty descriptor never fires while the rest of
        # the frame sits invisible in the buffer.
        self._child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            bufsize=0,
        )
        assert self._child.stdin is not None and self._child.stdout is not None
        self._stream = _Stream(reader=self._child.stdout, writer=self._child.stdin)
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._stream.reader, selectors.EVENT_READ)

    @property
    def name(self) -> str:
        return self._name

    def exit_code(self) -> Optional[int]:
        """The engine client's exit status, or None while it runs."""

        return self._child.poll()

    def request(
        self, request: Mapping[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """One exchange: write a frame, wait bounded, read the answer.

        A dead stream raises :class:`ContainerWorkerDied` with the exit status
        when one exists, so the caller can record why rather than only that.
        """

        from .realm.refs import canonical_json_bytes

        payload = canonical_json_bytes(dict(request))
        if len(payload) > _MAX_FRAME_BYTES:
            raise ValueError("method exchange request exceeds the frame bound.")
        try:
            # A raw pipe write may be partial for a frame larger than the pipe
            # buffer, so write until nothing remains.
            data = _HEADER.pack(len(payload)) + payload
            while data:
                written = self._stream.writer.write(data)
                data = data[written or 0:]
            self._stream.writer.flush()
        except (BrokenPipeError, OSError) as error:
            raise ContainerWorkerDied(
                "The method container closed its stream before the request "
                "could be written.",
                exit_code=self.exit_code(),
            ) from error

        deadline = time.monotonic() + timeout
        header = self._read_exact(_HEADER.size, deadline)
        (size,) = _HEADER.unpack(header)
        if size > _MAX_FRAME_BYTES:
            raise ContainerWorkerDied(
                "The method container answered with a frame beyond the bound."
            )
        body = self._read_exact(size, deadline)
        import json

        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ContainerWorkerDied(
                "The method container answered with something other than an object."
            )
        return value

    def _read_exact(self, size: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError(
                    "The method container did not answer within its exchange "
                    "time limit."
                )
            if not self._selector.select(timeout=min(budget, 1.0)):
                # Nothing readable yet. If the child has died the stream will
                # never produce the rest, and saying so beats timing out.
                code = self.exit_code()
                if code is not None:
                    raise ContainerWorkerDied(
                        "The method container exited mid-exchange.",
                        exit_code=code,
                    )
                continue
            chunk = self._stream.reader.read(remaining)
            if not chunk:
                raise ContainerWorkerDied(
                    "The method container closed its stream mid-exchange.",
                    exit_code=self.exit_code(),
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def stop(self, *, grace_seconds: float = 10.0) -> Optional[int]:
        """End the container: close the stream, wait briefly, then remove.

        Closing stdin is the polite signal -- the worker treats end-of-file as
        "nobody left to answer" and exits on its own. The forced removal after
        the grace period covers a worker wedged inside authored code.
        """

        try:
            self._stream.writer.close()
        except OSError:
            pass
        try:
            code = self._child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self._child.terminate()
            try:
                code = self._child.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                self._child.kill()
                code = self._child.wait(timeout=grace_seconds)
        finally:
            remove_container_if_present(self._engine, self._name)
            self._selector.close()
        return code

    def request_client(
        self,
    ) -> Any:
        """A drop-in for the socket request client.

        The socket client's first argument is the path to connect to; the
        stream has no path, so it is accepted and ignored. Everything
        downstream keeps one calling convention.
        """

        def _client(
            _socket_path: Any, request: Mapping[str, Any], *, timeout: float = 5.0
        ) -> dict[str, Any]:
            return self.request(request, timeout=timeout)

        return _client
