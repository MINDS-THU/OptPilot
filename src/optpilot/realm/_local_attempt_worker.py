"""Private child entrypoint for one retained native process attempt.

Host roots arrive only as operational argv values.  The child validates the
path-free request in the control volume against the digest bound into that
argv, constructs the retained candidate evaluator chain, and publishes one
canonical :class:`AttemptEnvelope` result before exiting.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

# Inside a container this file runs from OptPilot's own mounted source, where no
# interpreter has OptPilot installed. The worker locates its package from its
# own position rather than from the import path, so the PYTHONPATH check below
# stays what it is: strict equality with the declared evaluator import roots and
# nothing else. On the host this insertion resolves to the same package the
# interpreter already provides.
_LAUNCHER_ROOT = str(Path(__file__).resolve().parents[2])
if _LAUNCHER_ROOT not in sys.path:
    sys.path.insert(0, _LAUNCHER_ROOT)

from optpilot.attempts import AttemptExecutor, AttemptWorkspaceBinding
from optpilot.candidate_materialization import (
    BoundsCandidateValidator,
    MaterializationRecord,
    ParameterPassthroughMaterializer,
    ValidationReport,
)
from optpilot.runtime_binding import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    ENVIRONMENT_SOURCE_SCOPE,
    FileCandidateMaterialization,
    FileCandidateManifestEntry,
)
from optpilot.realm._validation import thaw_json
from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.local_attempt_protocol import (
    ATTEMPT_REQUEST_FILE,
    ATTEMPT_RESULT_FILE,
    MAX_LOCAL_ATTEMPT_LOG_BYTES,
    LocalAttemptWorkerLog,
    LocalAttemptWorkerRequest,
    LocalAttemptWorkerResult,
    publish_exact_record,
    read_canonical_record,
    require_host_paths_absent,
)


_EXIT_USAGE = 64
_EXIT_PLATFORM_ERROR = 70


class _BoundedBinaryCapture:
    def __init__(self, owner: "_BoundedTextCapture") -> None:
        self._owner = owner

    def write(self, value: Any) -> int:
        try:
            encoded = bytes(value)
        except (TypeError, ValueError):
            raise TypeError("captured stream bytes must be bytes-like") from None
        self._owner._accept(encoded)
        return len(encoded)

    def flush(self) -> None:
        return None

    def writable(self) -> bool:
        return True


class _BoundedTextCapture:
    """Drain one Python stream while retaining only its bounded prefix."""

    encoding = "utf-8"
    errors = "strict"

    def __init__(self, stream: str) -> None:
        self._stream = stream
        self._content = bytearray()
        self._byte_count = 0
        self._line_breaks = 0
        self._last_byte: int | None = None
        self.buffer = _BoundedBinaryCapture(self)

    def write(self, value: Any) -> int:
        if not isinstance(value, str):
            raise TypeError("captured stream text must be a string")
        # Bound transient encoding memory as well as retained memory.  The
        # source string already belongs to the evaluator; this loop avoids a
        # second arbitrarily large allocation in the capture layer.
        for offset in range(0, len(value), 4096):
            self._accept(value[offset : offset + 4096].encode("utf-8"))
        return len(value)

    def _accept(self, encoded: bytes) -> None:
        self._byte_count += len(encoded)
        self._line_breaks += encoded.count(b"\n")
        if encoded:
            self._last_byte = encoded[-1]
        remaining = MAX_LOCAL_ATTEMPT_LOG_BYTES - len(self._content)
        if remaining > 0:
            self._content.extend(encoded[:remaining])

    def flush(self) -> None:
        return None

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("bounded semantic stream has no file descriptor")

    def evidence(
        self, host_paths: Sequence[Path] = ()
    ) -> LocalAttemptWorkerLog | None:
        if self._byte_count == 0:
            return None
        line_count = self._line_breaks + int(self._last_byte != ord("\n"))
        raw_content = bytes(self._content)
        content = _redact_runtime_path_bytes(raw_content, host_paths)
        if len(raw_content) == self._byte_count:
            redacted_byte_count = len(content)
        else:
            redacted_byte_count = max(self._byte_count, len(content) + 1)
        content = content[:MAX_LOCAL_ATTEMPT_LOG_BYTES]
        return LocalAttemptWorkerLog.build(
            stream=self._stream,
            byte_count=max(redacted_byte_count, len(content)),
            line_count=line_count,
            content=content,
        )


def _runtime_path_replacements(
    host_paths: Sequence[Path],
) -> tuple[tuple[str, str], ...]:
    by_path: dict[str, str] = {}
    for index, path in enumerate(host_paths):
        rendered = str(Path(path))
        if rendered:
            by_path.setdefault(rendered, f"[runtime-scope-{index}]")
    return tuple(
        sorted(by_path.items(), key=lambda item: len(item[0]), reverse=True)
    )


def _redact_runtime_path_bytes(
    content: bytes, host_paths: Sequence[Path]
) -> bytes:
    result = content
    for raw_path, replacement in _runtime_path_replacements(host_paths):
        raw = raw_path.encode("utf-8")
        label = replacement.encode("ascii")
        result = result.replace(raw, label)
        # The capture is a bounded prefix.  If its boundary bisects one host
        # path, redact the retained path prefix rather than leaking it merely
        # because the remaining bytes were truncated.
        upper = min(len(raw) - 1, len(result))
        for size in range(upper, 7, -1):
            if result.endswith(raw[:size]):
                result = result[:-size] + label + b"[truncated]"
                break
    return result


def _required_root(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise RealmIntegrityError(f"{label} must be a real absolute directory.")
    try:
        metadata = path.stat()
    except OSError as error:
        raise RealmIntegrityError(f"{label} cannot be inspected.") from error
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RealmIntegrityError(f"{label} identity is invalid.")
    return path.resolve()


def _inside(root: Path, relative_path: str) -> Path:
    candidate = root if relative_path == "." else root.joinpath(
        *relative_path.split("/")
    )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RealmIntegrityError("Python import root does not exist.") from error
    if resolved != root and root not in resolved.parents:
        raise RealmIntegrityError("Python import root escapes its source scope.")
    if not resolved.is_dir():
        raise RealmIntegrityError("Python import root is not a directory.")
    return resolved


class _PathFreeParameterMaterializer:
    """Use the retained materializer while removing its operational path note."""

    def __init__(self, definition: Mapping[str, Any]) -> None:
        self._inner = ParameterPassthroughMaterializer(dict(definition), None)

    def materialize(
        self,
        candidate: dict[str, Any],
        workspace: Path,
        context: dict[str, Any],
    ) -> MaterializationRecord:
        record = self._inner.materialize(candidate, workspace, context)
        # The legacy retained implementation records its host workspace as
        # diagnostic metadata.  Native results are portable, so retain only
        # the semantic implementation identity.
        return MaterializationRecord(
            runtime_spec=dict(record.runtime_spec),
            output_files=list(record.output_files),
            metadata={
                "implementation": str(
                    record.metadata.get(
                        "implementation", "builtin.parameter_to_config"
                    )
                )
            },
        )


_DIRECTORY_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _open_directory_beneath(root_fd: int, relative_path: str) -> int:
    current = os.dup(root_fd)
    try:
        if relative_path == ".":
            return current
        for component in relative_path.split("/"):
            child = os.open(component, _DIRECTORY_READ_FLAGS, dir_fd=current)
            metadata = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (linked.st_dev, linked.st_ino)
            ):
                os.close(child)
                raise RealmIntegrityError(
                    "Candidate directory identity is unsafe."
                )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _candidate_root_path(
    trial_root: Path, descriptor: FileCandidateMaterialization
) -> Path:
    root_fd = os.open(trial_root, _DIRECTORY_READ_FLAGS)
    try:
        candidate_fd = _open_directory_beneath(
            root_fd, descriptor.root.relative_path
        )
        os.close(candidate_fd)
    except OSError as error:
        raise RealmIntegrityError(
            "Candidate materialization root is unavailable or unsafe."
        ) from error
    finally:
        os.close(root_fd)
    if descriptor.root.relative_path == ".":
        return trial_root
    return trial_root.joinpath(*descriptor.root.relative_path.split("/"))


def _inspect_candidate_file(
    candidate_fd: int, entry: FileCandidateManifestEntry
) -> str | None:
    components = entry.path.split("/")
    parent_path = "." if len(components) == 1 else "/".join(components[:-1])
    try:
        parent_fd = _open_directory_beneath(candidate_fd, parent_path)
    except FileNotFoundError:
        return f"Candidate file is missing: {entry.path!r}."
    try:
        name = components[-1]
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return f"Candidate file is missing: {entry.path!r}."
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RealmIntegrityError("Candidate file is not a safe regular file.")
        try:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise RealmIntegrityError("Candidate file cannot be opened safely.") from error
        try:
            opened = os.fstat(file_fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise RealmIntegrityError("Candidate file identity changed.")
            # Manifest ``sha256`` stores the Realm BlobRef digest, whose
            # namespace separator prevents raw-file hashes from being reused
            # as typed content authority.
            digest = hashlib.sha256(b"optpilot/blob/v1\0")
            size = 0
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(file_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            identity = (before.st_dev, before.st_ino)
            if (
                identity != (after.st_dev, after.st_ino)
                or identity != (linked.st_dev, linked.st_ino)
                or after.st_nlink != 1
                or linked.st_nlink != 1
            ):
                raise RealmIntegrityError("Candidate file changed during validation.")
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)
    if size != entry.size_bytes:
        return f"Candidate file size differs from its manifest: {entry.path!r}."
    if digest.hexdigest() != entry.sha256:
        return f"Candidate file hash differs from its manifest: {entry.path!r}."
    if bool(before.st_mode & 0o111) != entry.executable:
        return f"Candidate file mode differs from its manifest: {entry.path!r}."
    return None


class _RetainedFileCandidateValidator:
    """Validate the already-layered candidate; never resolve a contentRef."""

    def __init__(self, descriptor: FileCandidateMaterialization) -> None:
        self._descriptor = descriptor

    def validate(
        self, candidate: dict[str, Any], context: dict[str, Any]
    ) -> ValidationReport:
        workspace = Path(context["workspace"])
        workspace_fd = os.open(workspace, _DIRECTORY_READ_FLAGS)
        try:
            root_fd = _open_directory_beneath(
                workspace_fd, self._descriptor.root.relative_path
            )
        finally:
            os.close(workspace_fd)
        errors: list[str] = []
        try:
            for directory in self._descriptor.directories:
                try:
                    directory_fd = _open_directory_beneath(root_fd, directory)
                except FileNotFoundError:
                    errors.append(
                        f"Candidate directory is missing: {directory!r}."
                    )
                else:
                    os.close(directory_fd)
            for entry in self._descriptor.files:
                error = _inspect_candidate_file(root_fd, entry)
                if error is not None:
                    errors.append(error)
        finally:
            os.close(root_fd)
        return ValidationReport(
            accepted=not errors,
            errors=errors,
            metadata={
                "implementation": "builtin.workspace_policy",
                "candidate_file_count": len(self._descriptor.files),
                "candidate_directory_count": len(self._descriptor.directories),
            },
        )


class _RetainedFileCandidateMaterializer:
    """Expose the layered tree operationally without copying it again."""

    def __init__(self, descriptor: FileCandidateMaterialization) -> None:
        self._descriptor = descriptor

    def materialize(
        self,
        candidate: dict[str, Any],
        workspace: Path,
        context: dict[str, Any],
    ) -> MaterializationRecord:
        candidate_root = _candidate_root_path(workspace, self._descriptor)
        files = [
            {
                **entry.to_dict(),
                "destination": str(
                    candidate_root.joinpath(*entry.path.split("/"))
                ),
            }
            for entry in self._descriptor.files
        ]
        return MaterializationRecord(
            runtime_spec={
                "candidateRoot": str(candidate_root),
                "entrypoint": self._descriptor.entrypoint,
                "files": files,
                "options": thaw_json(self._descriptor.options),
                "workspace": str(workspace),
            },
            output_files=[],
            metadata={
                "implementation": "builtin.workspace_bundle",
                "candidate_file_count": len(files),
                "candidate_directory_count": len(self._descriptor.directories),
                "copy_performed": False,
            },
        )


class _RetainedConfiguredPythonAdapter:
    """Exact configured-Python subset carried by PortableAttemptRuntimeSpec."""

    def __init__(
        self,
        request: LocalAttemptWorkerRequest,
        import_roots: Sequence[Path],
    ) -> None:
        self._request = request
        self._import_roots = tuple(import_roots)
        self._callable: Any = None

    def _load(self) -> Any:
        if self._callable is not None:
            return self._callable
        entrypoint = self._request.entrypoint
        module = importlib.import_module(entrypoint.module)
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            raise RealmIntegrityError(
                "Configured evaluator module has no file-backed origin."
            )
        resolved_origin = Path(origin).resolve(strict=True)
        if not any(
            resolved_origin == root or root in resolved_origin.parents
            for root in self._import_roots
        ):
            raise RealmIntegrityError(
                "Configured evaluator resolved outside its declared import roots."
            )
        callback = getattr(module, entrypoint.attribute)
        if not callable(callback):
            raise TypeError("Configured evaluator entrypoint is not callable.")
        self._callable = callback
        return callback

    def evaluate(
        self, candidate_runtime: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        callback = self._load()
        deadline_seconds = self._request.timeout_seconds
        configured_context = {
            **context,
            # Request records freeze nested JSON lists and objects into tuples
            # and mapping proxies.  Authored evaluators receive the public JSON
            # shape, not that internal immutable representation.
            "settings": thaw_json(self._request.evaluator_settings),
        }
        with _evaluation_deadline(deadline_seconds):
            payload = callback(candidate_runtime, configured_context)
        if not isinstance(payload, dict):
            raise TypeError("Configured Python evaluator must return a dict.")
        if "metric_values" in payload:
            metric_values = payload["metric_values"]
        elif "metrics" in payload:
            metric_values = payload["metrics"]
        else:
            reserved = {
                "status",
                "constraint_results",
                "output_files",
                "event_summary",
            }
            metric_values = {
                name: value
                for name, value in payload.items()
                if name not in reserved
            }
        if not isinstance(metric_values, Mapping):
            raise TypeError("Configured evaluator metrics must be a mapping.")
        actual_names = set(metric_values)
        expected_names = set(self._request.declared_metric_names)
        if actual_names != expected_names:
            raise ValueError(
                "Configured evaluator metrics differ from the declared metric names."
            )
        constraint_results = payload.get("constraint_results", {})
        output_files = payload.get("output_files", [])
        event_summary = payload.get("event_summary", {})
        if not isinstance(constraint_results, Mapping):
            raise TypeError("Configured evaluator constraints must be a mapping.")
        if isinstance(output_files, (str, bytes)) or not isinstance(
            output_files, Sequence
        ):
            raise TypeError("Configured evaluator output_files must be a sequence.")
        if not isinstance(event_summary, Mapping):
            raise TypeError("Configured evaluator event_summary must be a mapping.")
        return {
            "status": str(payload.get("status", "success")),
            "metric_values": dict(metric_values),
            "constraint_results": dict(constraint_results),
            "output_files": list(output_files),
            "event_summary": dict(event_summary),
        }


@contextmanager
def _evaluation_deadline(seconds: float | None):
    """Bound one evaluation by wall clock (design: a limit per piece of work).

    Raises subprocess.TimeoutExpired when the limit passes, which the attempt
    machinery already maps to the public "timeout" outcome -- so a slow
    evaluator produces an honest typed result with its logs, not a killed
    worker. A C extension that never returns to the interpreter cannot be
    interrupted this way; the provider's coarser wait bound covers that.
    """

    if (
        not seconds
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def _expired(_signum, _frame):
        raise subprocess.TimeoutExpired(
            cmd="environment evaluation", timeout=float(seconds)
        )

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _run(
    control_root: Path,
    trial_root: Path,
    source_root: Path,
    prepared_root: Path | None,
    expected_request_digest: str,
) -> None:
    if Path.cwd().resolve() != trial_root:
        raise RealmIntegrityError("local attempt process cwd differs from trial scope.")
    payload = read_canonical_record(control_root / ATTEMPT_REQUEST_FILE)
    try:
        request = LocalAttemptWorkerRequest.from_dict(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise RealmIntegrityError("local attempt request is invalid.") from error
    if request.digest != expected_request_digest:
        raise RealmIntegrityError(
            "local attempt request differs from its supervised launch digest."
        )

    scope_roots = {ENVIRONMENT_SOURCE_SCOPE: source_root}
    if prepared_root is not None:
        scope_roots[ENVIRONMENT_PREPARED_PYTHON_SCOPE] = prepared_root
    if {item.scope for item in request.python_import_roots} != set(scope_roots):
        raise RealmIntegrityError(
            "worker import scopes differ from its supervised runtime roots."
        )
    try:
        import_roots = tuple(
            _inside(scope_roots[item.scope], item.relative_path)
            for item in request.python_import_roots
        )
    except KeyError as error:  # pragma: no cover - exact scope set checked above
        raise RealmIntegrityError("worker import scope is unavailable.") from error
    expected_python_path = os.pathsep.join(str(path) for path in import_roots)
    if os.environ.get("PYTHONPATH") != expected_python_path:
        raise RealmIntegrityError(
            "worker Python path differs from the declared import-root order."
        )

    candidate = request.evaluation_spec.candidate
    if request.file_materialization is None:
        validator = BoundsCandidateValidator(dict(candidate["validation"]), None)
        materializer = _PathFreeParameterMaterializer(candidate["materialization"])
    else:
        validator = _RetainedFileCandidateValidator(request.file_materialization)
        materializer = _RetainedFileCandidateMaterializer(
            request.file_materialization
        )
    adapter = _RetainedConfiguredPythonAdapter(request, import_roots)
    binding = AttemptWorkspaceBinding(
        binding_id=request.binding_id,
        workspace=trial_root,
        backend_identity={
            "implementation": "builtin.realm_local_process_attempt",
        },
        backend_worker={"launch_token": request.launch_token},
        context={
            "evidence_fingerprint": request.evidence_fingerprint,
            "portable_spec_digest": request.portable_spec_digest,
        },
    )
    stdout = _BoundedTextCapture("stdout")
    stderr = _BoundedTextCapture("stderr")
    with redirect_stdout(stdout), redirect_stderr(stderr):
        envelope = AttemptExecutor(validator, materializer, adapter).execute(
            request.evaluation_spec,
            binding,
            attempt_id=request.attempt_id,
        )
    host_paths = (
        control_root,
        trial_root,
        source_root,
        *((prepared_root,) if prepared_root is not None else ()),
        *import_roots,
    )
    envelope = _portable_envelope(envelope, host_paths)
    logs = tuple(
        item
        for item in (stdout.evidence(host_paths), stderr.evidence(host_paths))
        if item is not None
    )
    result = LocalAttemptWorkerResult(
        request_digest=request.digest,
        attempt_id=request.attempt_id,
        binding_id=request.binding_id,
        launch_token=request.launch_token,
        evidence_fingerprint=request.evidence_fingerprint,
        envelope=envelope,
        logs=logs,
    )
    require_host_paths_absent(result.canonical_bytes, host_paths)
    publish_exact_record(
        control_root / ATTEMPT_RESULT_FILE,
        result.canonical_bytes,
    )


def _portable_envelope(
    envelope: Any,
    host_paths: Sequence[Path],
):
    """Replace provider roots in evaluator diagnostics with logical labels.

    Evaluator exceptions are semantic attempt results, not platform failures.
    Their tracebacks may mention the realized source or trial root, while the
    retained result record must remain path-free.  Redact only the exact roots
    supplied by this binding and rebuild the immutable envelope so every later
    digest covers the portable form.
    """

    from optpilot.attempts import AttemptEnvelope

    if not isinstance(envelope, AttemptEnvelope):
        raise TypeError("envelope must be an AttemptEnvelope.")
    replacements = _runtime_path_replacements(host_paths)

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for path, replacement in replacements:
                value = value.replace(path, replacement)
            return value
        if isinstance(value, dict):
            return {key: redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return value

    return AttemptEnvelope.from_dict(redact(envelope.to_dict()))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in {4, 5}:
        return _EXIT_USAGE
    try:
        control_root = _required_root(arguments[0], "control root")
        trial_root = _required_root(arguments[1], "trial root")
        source_root = _required_root(arguments[2], "source root")
        prepared_root = (
            _required_root(arguments[3], "prepared Python root")
            if len(arguments) == 5
            else None
        )
        scope_roots = {
            control_root,
            trial_root,
            source_root,
            *((prepared_root,) if prepared_root is not None else ()),
        }
        if len(scope_roots) != (4 if prepared_root is not None else 3):
            raise RealmIntegrityError("local attempt scope roots must be distinct.")
        expected_digest = arguments[-1]
        if not re_full_lower_hex(expected_digest):
            raise RealmIntegrityError("local attempt request digest is invalid.")
        _run(
            control_root,
            trial_root,
            source_root,
            prepared_root,
            expected_digest,
        )
        return 0
    except BaseException:
        return _EXIT_PLATFORM_ERROR


def re_full_lower_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


if __name__ == "__main__":
    raise SystemExit(main())
