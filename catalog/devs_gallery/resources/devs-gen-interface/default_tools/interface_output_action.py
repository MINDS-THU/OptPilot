"""Thin client for OptPilot's generic interface-output action broker.

The DEVS Generator can also run outside OptPilot, so this module deliberately
has no OptPilot imports.  When Studio launches the interface it supplies two
opaque, launch-local directories:

``OPTPILOT_INTERFACE_OUTPUT_ROOT``
    Writable output storage owned by this interface launch.

``OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT``
    A file broker used to request execution in a short-lived sibling of the
    originating prepared runtime.

Generated code is copied into the broker's dedicated ``inputs`` namespace
before the request is appended.  Studio then snapshots that directory and
executes only the action authored in the Catalog manifest.  The request can
select the tree, provide bounded arguments, and ask for a shorter timeout; it
cannot provide a command, environment, image, mount, or network policy.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence


OUTPUT_ACTION_REQUEST_SCHEMA = "optpilot.interface-output-execution-request.v1"
OUTPUT_ACTION_RESULT_SCHEMA = "optpilot.interface-output-execution-result.v1"
OUTPUT_ACTION_ID = "run-simulation"
OUTPUT_ROOT_ENV = "OPTPILOT_INTERFACE_OUTPUT_ROOT"
ACTION_ROOT_ENV = "OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT"

_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "rejected",
        "infrastructure_failed",
    }
)
_MAX_REQUEST_ARGUMENTS = 64
_MAX_ARGUMENT_BYTES = 4096
_MAX_ARGUMENT_VECTOR_BYTES = 32 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CAPTURE_BYTES = 1024 * 1024
_MAX_RESULT_ENTRIES = 512
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_FILE_BYTES = 32 * 1024 * 1024
_MAX_BUNDLE_ENTRIES = 20_000
_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
_MAX_BUNDLE_FILE_BYTES = 128 * 1024 * 1024
_POLL_INTERVAL_SECONDS = 0.05
_CANCELLATION_GRACE_SECONDS = 15.0


class OutputActionError(RuntimeError):
    """Base class for output-action client failures."""


class OutputActionUnavailable(OutputActionError):
    """The launch-local broker could not complete a trustworthy request."""


class OutputActionRejected(OutputActionError):
    """The supplied source, arguments, or broker response was unsafe."""


@dataclass(frozen=True)
class OutputActionResultFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class OutputActionResult:
    request_id: str
    action_id: str
    snapshot_ref: str | None
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    result_files: tuple[OutputActionResultFile, ...]
    failure_code: str | None


class OutputActionExecutor(Protocol):
    """Small execution protocol shared by the UI and generation agent."""

    def execute(
        self,
        *,
        source_directory: str | Path,
        arguments: Sequence[str],
        results_directory: str | Path | None,
        request_id: str | None = None,
        timeout_seconds: int | None = None,
        response_timeout_seconds: float = 90.0,
        should_cancel: Callable[[], bool] | None = None,
    ) -> OutputActionResult:
        """Execute one safely staged source tree."""


@dataclass(frozen=True)
class _SourceEntry:
    relative_path: str
    kind: str
    size: int
    mode: int
    modified_ns: int
    device: int
    inode: int


def _strict_json_loads(text: str) -> Any:
    def pairs_hook(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OutputActionRejected(
                    f"Output-action response repeats JSON field {key!r}."
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise OutputActionRejected(
            f"Output-action response contains non-finite number {value!r}."
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except OutputActionRejected:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OutputActionRejected(
            f"Output-action response is not valid JSON: {exc}"
        ) from exc


def _real_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise OutputActionUnavailable(f"{label} must be an absolute path.")
    try:
        supplied = path.lstat()
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise OutputActionUnavailable(f"{label} is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(supplied.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise OutputActionUnavailable(f"{label} must be a real directory.")
    return resolved


def _canonical_relative_path(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > 4096
    ):
        raise OutputActionRejected(f"{label} must be a canonical relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or value == "."
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OutputActionRejected(f"{label} must be a canonical relative path.")
    return value


def _bounded_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)) or not isinstance(arguments, Sequence):
        raise OutputActionRejected("Output-action arguments must be a sequence.")
    if len(arguments) > _MAX_REQUEST_ARGUMENTS:
        raise OutputActionRejected(
            f"Output-action arguments may contain at most {_MAX_REQUEST_ARGUMENTS} items."
        )
    result: list[str] = []
    total = 0
    for index, argument in enumerate(arguments):
        if not isinstance(argument, str) or "\x00" in argument:
            raise OutputActionRejected(
                f"Output-action argument {index} must be text without NUL bytes."
            )
        encoded = argument.encode("utf-8")
        if len(encoded) > _MAX_ARGUMENT_BYTES:
            raise OutputActionRejected(
                f"Output-action argument {index} exceeds its size limit."
            )
        total += len(encoded)
        result.append(argument)
    if total > _MAX_ARGUMENT_VECTOR_BYTES:
        raise OutputActionRejected(
            "Output-action arguments exceed their total size limit."
        )
    return tuple(result)


def _inventory_tree(root: Path) -> tuple[_SourceEntry, ...]:
    entries: list[_SourceEntry] = []
    total_bytes = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > 64:
            raise OutputActionRejected(
                "Generated simulator exceeds the maximum directory depth."
            )
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise OutputActionRejected(
                f"Cannot inspect generated simulator: {exc}"
            ) from exc
        for child in children:
            relative = Path(child.path).relative_to(root).as_posix()
            if len(relative.encode("utf-8")) > 4096:
                raise OutputActionRejected(
                    "Generated simulator contains an excessively long path."
                )
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise OutputActionRejected(
                    f"Cannot inspect generated simulator entry {relative!r}: {exc}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise OutputActionRejected(
                    f"Generated simulator may not contain symbolic links: {relative!r}."
                )
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    _SourceEntry(
                        relative,
                        "directory",
                        metadata.st_size,
                        metadata.st_mode,
                        metadata.st_mtime_ns,
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                )
                stack.append((Path(child.path), depth + 1))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > _MAX_BUNDLE_FILE_BYTES:
                    raise OutputActionRejected(
                        f"Generated simulator file exceeds its limit: {relative!r}."
                    )
                total_bytes += metadata.st_size
                if total_bytes > _MAX_BUNDLE_BYTES:
                    raise OutputActionRejected(
                        "Generated simulator exceeds its total byte limit."
                    )
                entries.append(
                    _SourceEntry(
                        relative,
                        "file",
                        metadata.st_size,
                        metadata.st_mode,
                        metadata.st_mtime_ns,
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                )
            else:
                raise OutputActionRejected(
                    f"Generated simulator contains a special file: {relative!r}."
                )
            if len(entries) > _MAX_BUNDLE_ENTRIES:
                raise OutputActionRejected(
                    "Generated simulator exceeds its entry-count limit."
                )
    return tuple(sorted(entries, key=lambda item: item.relative_path.encode("utf-8")))


def _copy_stable_tree(source: Path, destination: Path) -> None:
    root = _real_directory(source, label="Generated simulator source")
    root_identity = root.lstat()
    before = _inventory_tree(root)
    destination.mkdir(mode=0o700)
    read_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for entry in before:
            source_path = root.joinpath(*PurePosixPath(entry.relative_path).parts)
            target_path = destination.joinpath(
                *PurePosixPath(entry.relative_path).parts
            )
            expected = (
                entry.size,
                entry.mode,
                entry.modified_ns,
                entry.device,
                entry.inode,
            )
            if entry.kind == "directory":
                metadata = source_path.lstat()
                actual = (
                    metadata.st_size,
                    metadata.st_mode,
                    metadata.st_mtime_ns,
                    metadata.st_dev,
                    metadata.st_ino,
                )
                if actual != expected or not stat.S_ISDIR(metadata.st_mode):
                    raise OutputActionRejected(
                        "Generated simulator changed while it was being staged."
                    )
                target_path.mkdir(parents=True, exist_ok=False, mode=0o700)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(source_path, read_flags)
            try:
                opened = os.fstat(descriptor)
                identity = (
                    opened.st_size,
                    opened.st_mode,
                    opened.st_mtime_ns,
                    opened.st_dev,
                    opened.st_ino,
                )
                if identity != expected or not stat.S_ISREG(opened.st_mode):
                    raise OutputActionRejected(
                        "Generated simulator changed while it was being staged."
                    )
                target_descriptor = os.open(
                    target_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o700 if entry.mode & 0o111 else 0o600,
                )
                try:
                    while True:
                        chunk = os.read(descriptor, 128 * 1024)
                        if not chunk:
                            break
                        offset = 0
                        while offset < len(chunk):
                            offset += os.write(target_descriptor, chunk[offset:])
                    os.fsync(target_descriptor)
                finally:
                    os.close(target_descriptor)
                after_open = os.fstat(descriptor)
                if (
                    after_open.st_size,
                    after_open.st_mode,
                    after_open.st_mtime_ns,
                    after_open.st_dev,
                    after_open.st_ino,
                ) != expected:
                    raise OutputActionRejected(
                        "Generated simulator changed while it was being staged."
                    )
            finally:
                os.close(descriptor)
        current_root = root.lstat()
        if (
            current_root.st_dev,
            current_root.st_ino,
            current_root.st_mode,
        ) != (
            root_identity.st_dev,
            root_identity.st_ino,
            root_identity.st_mode,
        ) or before != _inventory_tree(root):
            raise OutputActionRejected(
                "Generated simulator changed while it was being staged."
            )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _atomic_marker(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise OutputActionUnavailable(
            f"Cannot inspect output-action cancellation marker: {exc}"
        ) from exc
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OutputActionUnavailable(
                "Output-action cancellation marker is unsafe."
            )
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes | None:
    try:
        linked = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OutputActionUnavailable(
            f"Cannot inspect output-action response: {exc}"
        ) from exc
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise OutputActionUnavailable("Output-action response is not a regular file.")
    if linked.st_size > maximum_bytes:
        raise OutputActionUnavailable("Output-action response exceeds its size limit.")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (linked.st_dev, linked.st_ino, linked.st_size)
        ):
            raise OutputActionUnavailable(
                "Output-action response changed while it was being opened."
            )
        chunks: list[bytes] = []
        retained = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - retained))
            if not chunk:
                break
            chunks.append(chunk)
            retained += len(chunk)
            if retained > maximum_bytes:
                raise OutputActionUnavailable(
                    "Output-action response exceeds its size limit."
                )
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise OutputActionUnavailable(
                "Output-action response changed while it was being read."
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_result_file(value: Any) -> OutputActionResultFile:
    if not isinstance(value, Mapping) or set(value) != {"path", "size", "sha256"}:
        raise OutputActionRejected(
            "Output-action result-file fields are not canonical."
        )
    path = _canonical_relative_path(value["path"], label="Output-action result path")
    size = value["size"]
    digest = value["sha256"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= _MAX_RESULT_FILE_BYTES
    ):
        raise OutputActionRejected("Output-action result size is invalid.")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise OutputActionRejected("Output-action result digest is invalid.")
    return OutputActionResultFile(path=path, size=size, sha256=digest)


def _parse_response(
    payload: bytes,
    *,
    request_id: str,
    action_id: str,
) -> OutputActionResult:
    try:
        document = _strict_json_loads(payload.decode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise OutputActionRejected(
            "Output-action response is not UTF-8 text."
        ) from exc
    expected = {
        "schema_version",
        "request_id",
        "action_id",
        "snapshot_ref",
        "status",
        "exit_code",
        "duration_seconds",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
        "result_files",
        "failure_code",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise OutputActionRejected("Output-action response fields are not canonical.")
    if document["schema_version"] != OUTPUT_ACTION_RESULT_SCHEMA:
        raise OutputActionRejected("Output-action response schema is unsupported.")
    if document["request_id"] != request_id or document["action_id"] != action_id:
        raise OutputActionRejected("Output-action response identity does not match.")
    status_value = document["status"]
    if status_value not in _TERMINAL_STATUSES:
        raise OutputActionRejected("Output-action response status is unsupported.")
    snapshot_ref = document["snapshot_ref"]
    if snapshot_ref is not None and (
        not isinstance(snapshot_ref, str)
        or not snapshot_ref
        or len(snapshot_ref.encode("utf-8")) > 4096
    ):
        raise OutputActionRejected("Output-action snapshot reference is invalid.")
    if snapshot_ref is None and status_value in {"succeeded", "timed_out"}:
        raise OutputActionRejected(
            "Completed output-action response is missing its snapshot reference."
        )
    exit_code = document["exit_code"]
    if isinstance(exit_code, bool) or (
        exit_code is not None and not isinstance(exit_code, int)
    ):
        raise OutputActionRejected("Output-action exit code is invalid.")
    duration = document["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
    ):
        raise OutputActionRejected("Output-action duration is invalid.")
    stdout = document["stdout"]
    stderr = document["stderr"]
    if (
        not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or len(stdout.encode("utf-8")) > _MAX_CAPTURE_BYTES
        or len(stderr.encode("utf-8")) > _MAX_CAPTURE_BYTES
    ):
        raise OutputActionRejected("Output-action captured output is invalid.")
    stdout_truncated = document["stdout_truncated"]
    stderr_truncated = document["stderr_truncated"]
    if type(stdout_truncated) is not bool or type(stderr_truncated) is not bool:
        raise OutputActionRejected("Output-action truncation flags are invalid.")
    raw_files = document["result_files"]
    if not isinstance(raw_files, list) or len(raw_files) > _MAX_RESULT_ENTRIES:
        raise OutputActionRejected("Output-action result-file inventory is invalid.")
    files = tuple(_parse_result_file(item) for item in raw_files)
    if len({item.path for item in files}) != len(files):
        raise OutputActionRejected("Output-action result-file paths are duplicated.")
    if sum(item.size for item in files) > _MAX_RESULT_BYTES:
        raise OutputActionRejected("Output-action results exceed their total size limit.")
    failure_code = document["failure_code"]
    if failure_code is not None and (
        not isinstance(failure_code, str)
        or not failure_code
        or len(failure_code.encode("utf-8")) > 128
    ):
        raise OutputActionRejected("Output-action failure code is invalid.")
    return OutputActionResult(
        request_id=request_id,
        action_id=action_id,
        snapshot_ref=snapshot_ref,
        status=status_value,
        exit_code=exit_code,
        duration_seconds=float(duration),
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        result_files=files,
        failure_code=failure_code,
    )


def _copy_declared_result(
    source_root: Path,
    destination_root: Path | None,
    declared: OutputActionResultFile,
) -> None:
    root = _real_directory(source_root, label="Output-action result root")
    relative = PurePosixPath(declared.path)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise OutputActionUnavailable(
                f"Output-action result directory is unavailable: {declared.path!r}."
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OutputActionUnavailable(
                f"Output-action result path is unsafe: {declared.path!r}."
            )
    source = current / relative.name
    try:
        linked = source.lstat()
    except OSError as exc:
        raise OutputActionUnavailable(
            f"Output-action result is unavailable: {declared.path!r}."
        ) from exc
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or linked.st_size != declared.size
    ):
        raise OutputActionUnavailable(
            f"Output-action result does not match its inventory: {declared.path!r}."
        )
    descriptor = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    target_descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (linked.st_dev, linked.st_ino, linked.st_size)
        ):
            raise OutputActionUnavailable(
                f"Output-action result changed while opening: {declared.path!r}."
            )
        if destination_root is not None:
            target = destination_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for parent in (destination_root, *target.parents):
                if parent == destination_root.parent:
                    break
                metadata = parent.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                ):
                    raise OutputActionUnavailable(
                        "Output-action result destination is unsafe."
                    )
            target_descriptor = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        copied = 0
        while True:
            chunk = os.read(descriptor, 128 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
            if target_descriptor is not None:
                offset = 0
                while offset < len(chunk):
                    offset += os.write(target_descriptor, chunk[offset:])
        if target_descriptor is not None:
            os.fsync(target_descriptor)
        after = os.fstat(descriptor)
        if (
            after.st_size,
            after.st_mtime_ns,
            after.st_dev,
            after.st_ino,
        ) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_dev,
            opened.st_ino,
        ):
            raise OutputActionUnavailable(
                f"Output-action result changed while reading: {declared.path!r}."
            )
        if copied != declared.size or digest.hexdigest() != declared.sha256:
            raise OutputActionUnavailable(
                f"Output-action result failed integrity verification: {declared.path!r}."
            )
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        os.close(descriptor)


class InterfaceOutputActionClient:
    """Append requests to, and consume responses from, one launch-local broker."""

    def __init__(
        self,
        *,
        output_root: str | Path,
        action_root: str | Path,
        action_id: str = OUTPUT_ACTION_ID,
    ) -> None:
        self.output_root = _real_directory(
            Path(output_root), label="OptPilot interface output root"
        )
        self.action_root = _real_directory(
            Path(action_root), label="OptPilot output-action root"
        )
        self.action_id = action_id
        if action_id != OUTPUT_ACTION_ID:
            raise ValueError(f"DEVS execution requires action {OUTPUT_ACTION_ID!r}.")
        self.requests_path = self.action_root / "requests.jsonl"
        self.inputs_root = _real_directory(
            self.action_root / "inputs",
            label="OptPilot output-action input root",
        )
        inputs_metadata = self.inputs_root.lstat()
        self._inputs_identity = (
            int(inputs_metadata.st_dev),
            int(inputs_metadata.st_ino),
        )
        self.responses_root = _real_directory(
            self.action_root / "responses",
            label="OptPilot output-action response root",
        )
        self.results_root = _real_directory(
            self.action_root / "results",
            label="OptPilot output-action result root",
        )
        self.cancellations_root = _real_directory(
            self.action_root / "cancellations",
            label="OptPilot output-action cancellation root",
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "InterfaceOutputActionClient | None":
        values = os.environ if environment is None else environment
        action_root = (values.get(ACTION_ROOT_ENV) or "").strip()
        if not action_root:
            return None
        output_root = (values.get(OUTPUT_ROOT_ENV) or "").strip()
        if not output_root:
            raise OutputActionUnavailable(
                f"{OUTPUT_ROOT_ENV} is required when {ACTION_ROOT_ENV} is supplied."
            )
        return cls(output_root=output_root, action_root=action_root)

    def execute(
        self,
        *,
        source_directory: str | Path,
        arguments: Sequence[str],
        results_directory: str | Path | None,
        request_id: str | None = None,
        timeout_seconds: int | None = None,
        response_timeout_seconds: float = 90.0,
        should_cancel: Callable[[], bool] | None = None,
    ) -> OutputActionResult:
        selected_id = request_id or f"devs_{uuid.uuid4().hex}"
        if (
            not selected_id
            or len(selected_id.encode("utf-8")) > 128
            or not selected_id[0].isalnum()
            or any(
                not (character.isascii() and (character.isalnum() or character in "._-"))
                for character in selected_id
            )
        ):
            raise OutputActionRejected("Output-action request id is invalid.")
        if (
            isinstance(response_timeout_seconds, bool)
            or not isinstance(response_timeout_seconds, (int, float))
            or not math.isfinite(float(response_timeout_seconds))
            or response_timeout_seconds <= 0
        ):
            raise ValueError("response_timeout_seconds must be positive and finite.")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer or null.")
        request_arguments = _bounded_arguments(arguments)
        inputs_root = _real_directory(
            self.inputs_root,
            label="OptPilot output-action input root",
        )
        inputs_metadata = inputs_root.lstat()
        if (
            int(inputs_metadata.st_dev),
            int(inputs_metadata.st_ino),
        ) != self._inputs_identity:
            raise OutputActionUnavailable(
                "OptPilot output-action input root identity changed."
            )
        staging = inputs_root / selected_id
        response_path = self.responses_root / f"{selected_id}.json"
        cancellation_path = self.cancellations_root / selected_id
        for path, label in (
            (staging, "staging path"),
            (response_path, "response path"),
            (cancellation_path, "cancellation path"),
            (self.results_root / selected_id, "result path"),
        ):
            if path.exists() or path.is_symlink():
                raise OutputActionRejected(
                    f"Output-action {label} already exists for this request."
                )

        appended = False
        terminal = False
        _copy_stable_tree(Path(source_directory), staging)
        request = {
            "schema_version": OUTPUT_ACTION_REQUEST_SCHEMA,
            "request_id": selected_id,
            "action_id": self.action_id,
            "output_path": staging.relative_to(inputs_root).as_posix(),
            "arguments": list(request_arguments),
            "timeout_seconds": timeout_seconds,
        }
        payload = (
            json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            self._append_request(payload)
            appended = True
            deadline = time.monotonic() + float(response_timeout_seconds)
            cancellation_deadline: float | None = None
            while True:
                raw_response = _read_regular_file(
                    response_path, maximum_bytes=_MAX_RESPONSE_BYTES
                )
                if raw_response is not None:
                    terminal = True
                    result = _parse_response(
                        raw_response,
                        request_id=selected_id,
                        action_id=self.action_id,
                    )
                    self._retain_results(result, results_directory)
                    return result

                now = time.monotonic()
                cancel_requested = should_cancel is not None and should_cancel()
                if cancel_requested and cancellation_deadline is None:
                    _atomic_marker(cancellation_path)
                    cancellation_deadline = now + _CANCELLATION_GRACE_SECONDS
                if now >= deadline and cancellation_deadline is None:
                    _atomic_marker(cancellation_path)
                    cancellation_deadline = now + _CANCELLATION_GRACE_SECONDS
                if cancellation_deadline is not None and now >= cancellation_deadline:
                    raise OutputActionUnavailable(
                        "OptPilot did not publish a terminal simulation response."
                    )
                time.sleep(_POLL_INTERVAL_SECONDS)
        except (OutputActionError, OSError):
            if response_path.exists() or response_path.is_symlink():
                terminal = True
            raise
        finally:
            if not appended or terminal:
                self._remove_staging(staging)

    def _append_request(self, payload: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.requests_path, flags, 0o600)
        except OSError as exc:
            raise OutputActionUnavailable(
                f"Cannot open the output-action request broker: {exc}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OutputActionUnavailable(
                    "Output-action request broker is not a regular file."
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        except OSError as exc:
            raise OutputActionUnavailable(
                f"Cannot append the output-action request: {exc}"
            ) from exc
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def _retain_results(
        self,
        result: OutputActionResult,
        destination: str | Path | None,
    ) -> None:
        if not result.result_files:
            return
        source_root = self.results_root / result.request_id
        target_root: Path | None = None
        temporary: Path | None = None
        destination_path: Path | None = None
        if destination is not None:
            destination_path = _real_directory(
                Path(destination), label="Simulation result destination"
            )
            try:
                if any(destination_path.iterdir()):
                    raise OutputActionRejected(
                        "Simulation result destination must be empty."
                    )
            except OSError as exc:
                raise OutputActionUnavailable(
                    f"Cannot inspect simulation result destination: {exc}"
                ) from exc
            temporary = destination_path.parent / (
                f".{destination_path.name}.{uuid.uuid4().hex}.tmp"
            )
            temporary.mkdir(mode=0o700)
            target_root = temporary
        try:
            for declared in result.result_files:
                _copy_declared_result(source_root, target_root, declared)
            if temporary is not None and destination_path is not None:
                destination_path.rmdir()
                os.replace(temporary, destination_path)
                _fsync_directory(destination_path.parent)
                temporary = None
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _remove_staging(staging: Path) -> None:
        try:
            metadata = staging.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "ACTION_ROOT_ENV",
    "InterfaceOutputActionClient",
    "OUTPUT_ACTION_ID",
    "OUTPUT_ACTION_REQUEST_SCHEMA",
    "OUTPUT_ACTION_RESULT_SCHEMA",
    "OUTPUT_ROOT_ENV",
    "OutputActionError",
    "OutputActionExecutor",
    "OutputActionRejected",
    "OutputActionResult",
    "OutputActionResultFile",
    "OutputActionUnavailable",
]
