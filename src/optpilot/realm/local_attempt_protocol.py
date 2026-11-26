"""Path-free control-volume protocol for one native process attempt.

The process provider passes host paths only in its private operational launch
request.  The two records defined here contain semantic identities, a portable
evaluation, and its neutral :class:`~optpilot.attempts.AttemptEnvelope`; they
never contain a realized host root.  Files are published with create-once
semantics so an exact replay can validate an existing record but cannot replace
it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from base64 import b64decode, b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..attempts import AttemptEnvelope, EvaluationSpec
from ..runtime_binding import (
    FileCandidateMaterialization,
    PythonCallableEntrypoint,
)
from ..retained_file_candidates import SEALED_FILE_CANDIDATE_SPEC_SCHEMA
from ..runtime_scopes import ENVIRONMENT_PREPARED_PYTHON_SCOPE
from ._validation import freeze_json, lower_hex_digest, required_text, thaw_json
from .errors import RealmConflict, RealmIntegrityError
from .refs import canonical_json_bytes
from .run_closure import ScopePath


JsonDict = dict[str, Any]

ATTEMPT_REQUEST_FILE = "attempt-request.json"
ATTEMPT_RESULT_FILE = "attempt-result.json"
LOCAL_ATTEMPT_REQUEST_SCHEMA = "optpilot.local-attempt.request.v2"
LOCAL_ATTEMPT_RESULT_SCHEMA = "optpilot.local-attempt.result.v2"

_REQUEST_DOMAIN = b"optpilot/local-attempt-request/v2"
_RESULT_DOMAIN = b"optpilot/local-attempt-result/v2"
_MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_LOCAL_ATTEMPT_LOG_BYTES = 64 * 1024
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@~-]*$")


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _safe_token(value: Any, label: str) -> str:
    value = required_text(value, label, max_bytes=512)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be a path-free ASCII token.") from error
    if _SAFE_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return value


def _domain_digest(domain: bytes, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_json_bytes(payload)).hexdigest()


def _canonical_string_sequence(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    items = tuple(value)
    if any(not isinstance(item, str) for item in items) or len(set(items)) != len(
        items
    ):
        return None
    return tuple(sorted(items, key=lambda item: item.encode("utf-8")))


@dataclass(frozen=True)
class LocalAttemptWorkerRequest:
    """Canonical semantic request consumed by the provider worker."""

    attempt_id: str
    binding_id: str
    launch_token: str
    evidence_fingerprint: str
    evaluation_spec: EvaluationSpec
    portable_spec_digest: str
    entrypoint: PythonCallableEntrypoint
    python_import_roots: tuple[ScopePath, ...]
    evaluator_settings: Mapping[str, Any]
    declared_metric_names: tuple[str, ...]
    file_materialization: FileCandidateMaterialization | None = None
    #: The compiled wall-clock limit for this one evaluation (the min of the
    #: evaluator's declared timeoutSeconds and the run's execution policy).
    #: None means the compile produced no limit.
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _safe_token(self.attempt_id, "local attempt id")
        _safe_token(self.binding_id, "local attempt binding id")
        _safe_token(self.launch_token, "local attempt launch token")
        lower_hex_digest(
            self.evidence_fingerprint, "local attempt evidence fingerprint"
        )
        if not isinstance(self.evaluation_spec, EvaluationSpec):
            raise TypeError("evaluation_spec must be an EvaluationSpec.")
        lower_hex_digest(self.portable_spec_digest, "portable runtime spec digest")
        if not isinstance(self.entrypoint, PythonCallableEntrypoint):
            raise TypeError("entrypoint must be a PythonCallableEntrypoint.")
        if isinstance(self.python_import_roots, (str, bytes)) or not isinstance(
            self.python_import_roots, Sequence
        ):
            raise TypeError("python_import_roots must be a sequence.")
        roots = tuple(self.python_import_roots)
        if not roots or any(not isinstance(item, ScopePath) for item in roots):
            raise TypeError("python_import_roots must contain ScopePath values.")
        if len(set(roots)) != len(roots):
            raise ValueError("python_import_roots must not contain duplicates.")
        if self.entrypoint.scope == ENVIRONMENT_PREPARED_PYTHON_SCOPE:
            raise ValueError("entrypoint must use the retained environment source scope.")
        allowed_scopes = {
            self.entrypoint.scope,
            ENVIRONMENT_PREPARED_PYTHON_SCOPE,
        }
        if any(item.scope not in allowed_scopes for item in roots) or not any(
            item.scope == self.entrypoint.scope for item in roots
        ):
            raise ValueError(
                "import roots must use the entrypoint source scope or the "
                "prepared environment Python scope."
            )
        prepared_root = ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, ".")
        prepared_roots = tuple(
            item
            for item in roots
            if item.scope == ENVIRONMENT_PREPARED_PYTHON_SCOPE
        )
        if prepared_roots and (
            prepared_roots != (prepared_root,) or roots[-1] != prepared_root
        ):
            raise ValueError(
                "the single prepared environment Python import root must be last."
            )
        settings = freeze_json(self.evaluator_settings, label="evaluator settings")
        if not isinstance(settings, Mapping):
            raise TypeError("evaluator_settings must be a mapping.")
        if isinstance(self.declared_metric_names, (str, bytes)) or not isinstance(
            self.declared_metric_names, Sequence
        ):
            raise TypeError("declared_metric_names must be a sequence.")
        metrics = tuple(
            required_text(item, "declared metric name", max_bytes=256)
            for item in self.declared_metric_names
        )
        if not metrics or len(set(metrics)) != len(metrics):
            raise ValueError("declared_metric_names must be nonempty and unique.")
        candidate = self.evaluation_spec.candidate
        validation = candidate.get("validation")
        materialization = candidate.get("materialization")
        if not isinstance(validation, Mapping) or not isinstance(
            materialization, Mapping
        ):
            raise ValueError(
                "local process attempt candidate contracts are malformed."
            )
        candidate_format = self.evaluation_spec.candidate_format
        file_materialization = self.file_materialization
        if candidate_format == "parameters":
            if (
                set(validation) != {"implementation", "config"}
                or validation.get("implementation")
                != "builtin.schema_validation"
                or not isinstance(validation.get("config"), Mapping)
                or set(materialization) != {"implementation", "config"}
                or materialization.get("implementation")
                != "builtin.parameter_to_config"
                or materialization.get("config") != {}
                or file_materialization is not None
            ):
                raise ValueError(
                    "parameter attempts require the retained parameter "
                    "validator/materializer contract."
                )
        elif candidate_format == "files":
            if not isinstance(file_materialization, FileCandidateMaterialization):
                raise ValueError(
                    "file attempts require a typed logical materialization."
                )
            validation_config = validation.get("config")
            materialization_config = materialization.get("config")
            if not isinstance(validation_config, Mapping) or not isinstance(
                materialization_config, Mapping
            ):
                raise ValueError("file attempt candidate configs are malformed.")
            candidate_options = materialization_config.get(
                "candidateOptions", {}
            )
            candidate_spec = candidate.get("spec")
            expected_spec = {
                "directories": list(file_materialization.directories),
                "entrypoint": file_materialization.entrypoint,
                "files": [item.to_dict() for item in file_materialization.files],
                "options": thaw_json(file_materialization.options),
                "schema": SEALED_FILE_CANDIDATE_SPEC_SCHEMA,
            }
            if (
                set(validation) != {"implementation", "config"}
                or validation.get("implementation")
                != "builtin.workspace_policy"
                or not set(validation_config)
                <= {"allow", "deny", "requiredFiles"}
                or _canonical_string_sequence(
                    validation_config.get("requiredFiles", ())
                )
                != file_materialization.required_files
                or _canonical_string_sequence(validation_config.get("allow", ()))
                != file_materialization.allow_patterns
                or _canonical_string_sequence(validation_config.get("deny", ()))
                != file_materialization.deny_patterns
                or set(materialization) != {"implementation", "config"}
                or materialization.get("implementation")
                != "builtin.workspace_bundle"
                or not set(materialization_config)
                <= {"candidateOptions", "candidateRoot", "entrypoint"}
                or not isinstance(candidate_options, Mapping)
                or materialization_config.get("candidateRoot", ".")
                != file_materialization.root.relative_path
                or materialization_config.get("entrypoint")
                != file_materialization.entrypoint
                or canonical_json_bytes(
                    {
                        "candidateRoot": file_materialization.root.relative_path,
                        **thaw_json(candidate_options),
                    }
                )
                != canonical_json_bytes(thaw_json(file_materialization.options))
                or canonical_json_bytes(thaw_json(candidate_spec))
                != canonical_json_bytes(expected_spec)
            ):
                raise ValueError(
                    "file attempt differs from its typed retained materialization."
                )
        else:
            raise ValueError("local process attempt candidate format is unsupported.")
        object.__setattr__(self, "python_import_roots", roots)
        object.__setattr__(self, "evaluator_settings", settings)
        object.__setattr__(self, "file_materialization", file_materialization)
        object.__setattr__(self, "declared_metric_names", metrics)
        if self.timeout_seconds is not None:
            if (
                isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(float(self.timeout_seconds))
                or float(self.timeout_seconds) <= 0
            ):
                raise ValueError(
                    "local attempt timeout_seconds must be a positive finite number."
                )
            object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if len(self.canonical_bytes) > _MAX_RECORD_BYTES:
            raise ValueError("local attempt request exceeds its size bound.")

    def _body_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "declared_metric_names": list(self.declared_metric_names),
            "entrypoint": self.entrypoint.to_dict(),
            "evaluation_spec": self.evaluation_spec.to_dict(),
            "evaluation_spec_digest": self.evaluation_spec.digest,
            "evaluator_settings": thaw_json(self.evaluator_settings),
            "file_materialization": (
                None
                if self.file_materialization is None
                else self.file_materialization.to_dict()
            ),
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_token": self.launch_token,
            "portable_spec_digest": self.portable_spec_digest,
            "python_import_roots": [
                item.to_dict() for item in self.python_import_roots
            ],
            "schema": LOCAL_ATTEMPT_REQUEST_SCHEMA,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def digest(self) -> str:
        return _domain_digest(_REQUEST_DOMAIN, self._body_dict())

    def to_dict(self) -> JsonDict:
        return {**self._body_dict(), "request_digest": self.digest}

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LocalAttemptWorkerRequest":
        expected = {
            "attempt_id",
            "binding_id",
            "declared_metric_names",
            "entrypoint",
            "evaluation_spec",
            "evaluation_spec_digest",
            "evaluator_settings",
            "file_materialization",
            "evidence_fingerprint",
            "launch_token",
            "portable_spec_digest",
            "python_import_roots",
            "request_digest",
            "schema",
            "timeout_seconds",
        }
        _exact_keys(payload, expected, "local attempt request")
        if payload["schema"] != LOCAL_ATTEMPT_REQUEST_SCHEMA:
            raise ValueError("local attempt request schema is unsupported.")
        if not isinstance(payload["python_import_roots"], list) or not isinstance(
            payload["declared_metric_names"], list
        ):
            raise TypeError("local attempt request lists are malformed.")
        spec = EvaluationSpec.from_dict(payload["evaluation_spec"])
        if payload["evaluation_spec_digest"] != spec.digest:
            raise ValueError("local attempt evaluation spec digest is invalid.")
        result = cls(
            attempt_id=payload["attempt_id"],
            binding_id=payload["binding_id"],
            launch_token=payload["launch_token"],
            evidence_fingerprint=payload["evidence_fingerprint"],
            evaluation_spec=spec,
            portable_spec_digest=payload["portable_spec_digest"],
            entrypoint=PythonCallableEntrypoint.from_dict(payload["entrypoint"]),
            python_import_roots=tuple(
                ScopePath.from_dict(item) for item in payload["python_import_roots"]
            ),
            evaluator_settings=payload["evaluator_settings"],
            file_materialization=(
                None
                if payload["file_materialization"] is None
                else FileCandidateMaterialization.from_dict(
                    payload["file_materialization"]
                )
            ),
            declared_metric_names=tuple(payload["declared_metric_names"]),
            timeout_seconds=payload["timeout_seconds"],
        )
        if payload["request_digest"] != result.digest:
            raise ValueError("local attempt request digest is invalid.")
        if result.to_dict() != dict(payload):
            raise ValueError("local attempt request is not canonical.")
        return result


@dataclass(frozen=True, order=True)
class LocalAttemptWorkerLog:
    """One bounded, authenticated semantic-worker stream excerpt.

    The excerpt remains inside the provider control record.  Adopters retain
    only its portable metadata and digest, so arbitrary evaluator output is
    not promoted into a public API response or a durable owner membership.
    """

    stream: str
    byte_count: int
    line_count: int
    truncated: bool
    content_base64: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("local attempt log stream is unsupported.")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("local attempt log byte count must be nonnegative.")
        if (
            isinstance(self.line_count, bool)
            or not isinstance(self.line_count, int)
            or self.line_count < 0
        ):
            raise ValueError("local attempt log line count must be nonnegative.")
        if not isinstance(self.truncated, bool):
            raise TypeError("local attempt log truncated must be a boolean.")
        if not isinstance(self.content_base64, str):
            raise TypeError("local attempt log content must be base64 text.")
        try:
            content = b64decode(self.content_base64, validate=True)
        except (TypeError, ValueError):
            raise ValueError("local attempt log content is not canonical base64.") from None
        if b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("local attempt log content is not canonical base64.")
        if len(content) > MAX_LOCAL_ATTEMPT_LOG_BYTES:
            raise ValueError("local attempt log excerpt exceeds its byte limit.")
        if len(content) > self.byte_count:
            raise ValueError("local attempt log excerpt exceeds its total byte count.")
        if self.truncated != (len(content) < self.byte_count):
            raise ValueError("local attempt log truncation metadata is inconsistent.")
        lower_hex_digest(self.content_digest, "local attempt log content digest")
        if hashlib.sha256(content).hexdigest() != self.content_digest:
            raise ValueError("local attempt log content digest is inconsistent.")

    @classmethod
    def build(
        cls,
        *,
        stream: str,
        byte_count: int,
        line_count: int,
        content: bytes,
    ) -> "LocalAttemptWorkerLog":
        if not isinstance(content, bytes):
            raise TypeError("local attempt log content must be bytes.")
        return cls(
            stream=stream,
            byte_count=byte_count,
            line_count=line_count,
            truncated=len(content) < byte_count,
            content_base64=b64encode(content).decode("ascii"),
            content_digest=hashlib.sha256(content).hexdigest(),
        )

    @property
    def content(self) -> bytes:
        return b64decode(self.content_base64, validate=True)

    def to_dict(self) -> JsonDict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LocalAttemptWorkerLog":
        _exact_keys(
            payload,
            {
                "byte_count",
                "content_base64",
                "content_digest",
                "line_count",
                "stream",
                "truncated",
            },
            "local attempt log",
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class LocalAttemptWorkerResult:
    """Canonical worker result containing exactly one neutral envelope."""

    request_digest: str
    attempt_id: str
    binding_id: str
    launch_token: str
    evidence_fingerprint: str
    envelope: AttemptEnvelope
    logs: tuple[LocalAttemptWorkerLog, ...] = ()

    def __post_init__(self) -> None:
        lower_hex_digest(self.request_digest, "local attempt request digest")
        _safe_token(self.attempt_id, "local attempt result attempt id")
        _safe_token(self.binding_id, "local attempt result binding id")
        _safe_token(self.launch_token, "local attempt result launch token")
        lower_hex_digest(
            self.evidence_fingerprint, "local attempt result evidence fingerprint"
        )
        if not isinstance(self.envelope, AttemptEnvelope):
            raise TypeError("envelope must be an AttemptEnvelope.")
        if (
            self.envelope.attempt_id != self.attempt_id
            or self.envelope.binding_id != self.binding_id
        ):
            raise ValueError("local attempt result identity differs from its envelope.")
        logs = tuple(self.logs)
        if len(logs) > 2 or any(
            not isinstance(item, LocalAttemptWorkerLog) for item in logs
        ):
            raise ValueError("local attempt result logs are invalid or unbounded.")
        if len({item.stream for item in logs}) != len(logs):
            raise ValueError("local attempt result contains duplicate log streams.")
        object.__setattr__(self, "logs", tuple(sorted(logs)))
        if len(self.canonical_bytes) > _MAX_RECORD_BYTES:
            raise ValueError("local attempt result exceeds its size bound.")

    def _body_dict(self) -> JsonDict:
        return {
            "attempt_id": self.attempt_id,
            "binding_id": self.binding_id,
            "envelope": self.envelope.to_dict(),
            "envelope_digest": self.envelope.digest,
            "evidence_fingerprint": self.evidence_fingerprint,
            "launch_token": self.launch_token,
            "logs": [item.to_dict() for item in self.logs],
            "request_digest": self.request_digest,
            "schema": LOCAL_ATTEMPT_RESULT_SCHEMA,
        }

    @property
    def digest(self) -> str:
        return _domain_digest(_RESULT_DOMAIN, self._body_dict())

    def to_dict(self) -> JsonDict:
        return {**self._body_dict(), "result_digest": self.digest}

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LocalAttemptWorkerResult":
        expected = {
            "attempt_id",
            "binding_id",
            "envelope",
            "envelope_digest",
            "evidence_fingerprint",
            "launch_token",
            "logs",
            "request_digest",
            "result_digest",
            "schema",
        }
        _exact_keys(payload, expected, "local attempt result")
        if payload["schema"] != LOCAL_ATTEMPT_RESULT_SCHEMA:
            raise ValueError("local attempt result schema is unsupported.")
        envelope = AttemptEnvelope.from_dict(payload["envelope"])
        if payload["envelope_digest"] != envelope.digest:
            raise ValueError("local attempt envelope digest is invalid.")
        result = cls(
            request_digest=payload["request_digest"],
            attempt_id=payload["attempt_id"],
            binding_id=payload["binding_id"],
            launch_token=payload["launch_token"],
            evidence_fingerprint=payload["evidence_fingerprint"],
            envelope=envelope,
            logs=tuple(
                LocalAttemptWorkerLog.from_dict(item) for item in payload["logs"]
            ),
        )
        if payload["result_digest"] != result.digest:
            raise ValueError("local attempt result digest is invalid.")
        if result.to_dict() != dict(payload):
            raise ValueError("local attempt result is not canonical.")
        return result


def read_canonical_record(path: Path) -> JsonDict:
    """Read one bounded, canonical, owner-local regular JSON record."""

    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RealmIntegrityError("local attempt control record cannot be opened.") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size > _MAX_RECORD_BYTES
        ):
            raise RealmIntegrityError("local attempt control record identity is invalid.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_RECORD_BYTES:
                raise RealmIntegrityError("local attempt control record is too large.")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    encoded = b"".join(chunks)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError("local attempt control record is not JSON.") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != encoded:
        raise RealmIntegrityError("local attempt control record is not canonical JSON.")
    return payload


def publish_exact_record(path: Path, encoded: bytes) -> bool:
    """Atomically create a fixed record, or validate its exact replay.

    Returns ``True`` only when this call publishes the fixed name.  Temporary
    files use ``O_EXCL`` and are fsynced before a no-replace hard-link publish.
    """

    path = Path(path)
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_RECORD_BYTES:
        raise ValueError("local attempt encoded record is invalid.")
    try:
        parsed = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("local attempt encoded record must be JSON.") from error
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != encoded:
        raise ValueError("local attempt encoded record must be canonical JSON.")
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
                raise OSError("short local attempt record write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = canonical_json_bytes(read_canonical_record(path))
            if existing != encoded:
                raise RealmConflict(
                    "local attempt control record already has different contents."
                )
            return False
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def require_host_paths_absent(encoded: bytes, host_paths: Sequence[Path]) -> None:
    """Fail if a realized provider root appears in a portable record.

    Log excerpts are base64 inside the canonical JSON, so scan their decoded
    content as well as the record bytes.  This is a second fail-closed check
    after worker-side redaction, not a substitute for redacting the logs.
    """

    inspected = [encoded]
    try:
        payload = json.loads(encoded.decode("utf-8"))
        logs = payload.get("logs", ()) if isinstance(payload, Mapping) else ()
        if isinstance(logs, list):
            for item in logs:
                if isinstance(item, Mapping) and isinstance(
                    item.get("content_base64"), str
                ):
                    inspected.append(
                        b64decode(item["content_base64"], validate=True)
                    )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        # Record decoding/canonical validation has its own caller-visible
        # error.  Raw-byte scanning below still applies on that path.
        pass

    variants: set[str] = set()
    for path in host_paths:
        candidate = Path(path)
        rendered = str(candidate)
        if rendered:
            variants.add(rendered)
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = ""
        if resolved:
            variants.add(resolved)
    for value in variants:
        needle = value.encode("utf-8")
        if any(needle in content for content in inspected):
            raise RealmIntegrityError(
                "local attempt portable record contains a realized host path."
            )


__all__ = [
    "ATTEMPT_REQUEST_FILE",
    "ATTEMPT_RESULT_FILE",
    "LOCAL_ATTEMPT_REQUEST_SCHEMA",
    "LOCAL_ATTEMPT_RESULT_SCHEMA",
    "MAX_LOCAL_ATTEMPT_LOG_BYTES",
    "LocalAttemptWorkerLog",
    "LocalAttemptWorkerRequest",
    "LocalAttemptWorkerResult",
]
