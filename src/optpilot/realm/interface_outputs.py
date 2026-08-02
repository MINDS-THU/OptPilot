"""Generic launch-scoped interface output generations.

Interfaces do not export host paths or ask Studio to register a domain-specific
artifact.  They append bounded records that select a file or tree below one of
the root handles granted for that launch.  OptPilot validates those records and
immediately seals each selected generation through the ordinary content
capture boundary.  Keeping or publishing the resulting immutable ref is a
separate owner transaction.

This module intentionally owns no watcher, workspace, or catalog policy.  It is
the small content-plane contract shared by catalog interfaces, contextual
previews, and editable-workspace interfaces.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .content import (
    AllowedFileSource,
    AllowedTreeSource,
    LocalContentCapture,
)
from .errors import ContentRejected
from .manifests import SealLimits, validate_portable_path
from .refs import BlobRef, PhysicalContentRef, SnapshotRef, canonical_json_bytes


INTERFACE_OUTPUT_SCHEMA = "optpilot.interface.output.v1"

_MAX_CONTROL_BYTES = 1024 * 1024
_MAX_RECORD_BYTES = 16 * 1024
_MAX_RECORDS = 256
_MAX_ID_BYTES = 128
_MAX_LABEL_BYTES = 512
_MAX_ROOT_BYTES = 128
_MAX_PATH_BYTES = 4096
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DIRECTORY_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_DIRECTORY", 0)
)


class InterfaceOutputKind(str, Enum):
    FILE = "file"
    TREE = "tree"


@dataclass(frozen=True)
class InterfaceOutputRecordRejection:
    """Path-free diagnostic for one complete control-file record."""

    line_number: int
    code: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.line_number, bool)
            or not isinstance(self.line_number, int)
            or self.line_number <= 0
        ):
            raise ValueError("line_number must be a positive integer.")
        if (
            not isinstance(self.code, str)
            or _IDENTIFIER_RE.fullmatch(self.code) is None
        ):
            raise ValueError("code must be a bounded identifier.")

    def to_dict(self) -> dict[str, object]:
        return {"line": self.line_number, "code": self.code}


@dataclass(frozen=True)
class InterfaceOutputRecord:
    """One untrusted, launch-relative output selection."""

    output_id: str
    label: str
    kind: InterfaceOutputKind
    root_handle: str
    relative_path: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InterfaceOutputRecord":
        expected = {"schema_version", "id", "label", "kind", "root", "path"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ContentRejected(
                "Interface output record must contain exactly schema_version, "
                "id, label, kind, root, and path."
            )
        if value["schema_version"] != INTERFACE_OUTPUT_SCHEMA:
            raise ContentRejected("Interface output record schema is unsupported.")
        output_id = _bounded_identifier(
            value["id"], "interface output id", _MAX_ID_BYTES
        )
        root_handle = _bounded_identifier(
            value["root"], "interface output root handle", _MAX_ROOT_BYTES
        )
        label = _bounded_text(
            value["label"], "interface output label", _MAX_LABEL_BYTES
        )
        try:
            kind = InterfaceOutputKind(value["kind"])
        except (TypeError, ValueError) as error:
            raise ContentRejected(
                "Interface output kind must be 'file' or 'tree'."
            ) from error
        relative_path = _relative_selection(value["path"])
        return cls(
            output_id=output_id,
            label=label,
            kind=kind,
            root_handle=root_handle,
            relative_path=relative_path,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": INTERFACE_OUTPUT_SCHEMA,
            "id": self.output_id,
            "label": self.label,
            "kind": self.kind.value,
            "root": self.root_handle,
            "path": self.relative_path,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class SealedInterfaceOutput:
    """Path-free result of freezing one interface output generation."""

    record: InterfaceOutputRecord
    content_ref: PhysicalContentRef
    logical_bytes: int

    def __post_init__(self) -> None:
        if self.record.kind is InterfaceOutputKind.FILE:
            if not isinstance(self.content_ref, BlobRef):
                raise TypeError("A file output must seal to a BlobRef.")
        elif not isinstance(self.content_ref, SnapshotRef):
            raise TypeError("A tree output must seal to a SnapshotRef.")
        if (
            isinstance(self.logical_bytes, bool)
            or not isinstance(self.logical_bytes, int)
            or self.logical_bytes < 0
        ):
            raise ValueError("logical_bytes must be a nonnegative integer.")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "optpilot.interface.output.sealed.v1",
            "id": self.record.output_id,
            "label": self.record.label,
            "kind": self.record.kind.value,
            "content_ref": str(self.content_ref),
            "logical_bytes": self.logical_bytes,
            "status": "ready",
        }


def read_interface_output_records(
    control_file: Path,
    *,
    max_control_bytes: int = _MAX_CONTROL_BYTES,
    max_record_bytes: int = _MAX_RECORD_BYTES,
    max_records: int = _MAX_RECORDS,
    tolerate_invalid_records: bool = False,
    rejected_records: list[InterfaceOutputRecordRejection] | None = None,
    accepted_record_lines: dict[str, int] | None = None,
) -> tuple[InterfaceOutputRecord, ...]:
    """Read complete JSONL records from one no-follow control file.

    The final unterminated line is ignored even if it currently happens to be
    valid JSON.  Newline publication is the record commit boundary, so a reader
    can never accept bytes while an interface is still appending them.
    Exact duplicate ids are idempotent; reusing an id for different content is
    rejected before any generation is captured.
    """

    control_file = Path(control_file)
    _positive_limit(max_control_bytes, "max_control_bytes")
    _positive_limit(max_record_bytes, "max_record_bytes")
    _positive_limit(max_records, "max_records")
    if not isinstance(tolerate_invalid_records, bool):
        raise TypeError("tolerate_invalid_records must be a boolean.")
    if rejected_records is not None and not isinstance(rejected_records, list):
        raise TypeError("rejected_records must be a list or None.")
    if accepted_record_lines is not None and not isinstance(
        accepted_record_lines, dict
    ):
        raise TypeError("accepted_record_lines must be a dict or None.")
    payload = _read_bounded_regular_file(control_file, max_control_bytes)
    if payload.endswith(b"\n"):
        complete = payload
    else:
        final_newline = payload.rfind(b"\n")
        complete = b"" if final_newline < 0 else payload[: final_newline + 1]
    lines = complete.splitlines()
    records: list[InterfaceOutputRecord] = []
    by_id: dict[str, InterfaceOutputRecord] = {}
    record_count = 0
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        record_count += 1
        if record_count > max_records:
            raise ContentRejected(
                f"Interface output control file exceeds {max_records} records."
            )
        if len(raw_line) > max_record_bytes:
            if tolerate_invalid_records:
                if rejected_records is not None:
                    rejected_records.append(
                        InterfaceOutputRecordRejection(line_number, "record_too_large")
                    )
                continue
            raise ContentRejected(
                f"Interface output record {line_number} exceeds {max_record_bytes} bytes."
            )
        try:
            decoded = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if tolerate_invalid_records:
                if rejected_records is not None:
                    rejected_records.append(
                        InterfaceOutputRecordRejection(line_number, "invalid_json")
                    )
                continue
            raise ContentRejected(
                f"Interface output record {line_number} is not valid JSON."
            ) from error
        if not isinstance(decoded, dict):
            if tolerate_invalid_records:
                if rejected_records is not None:
                    rejected_records.append(
                        InterfaceOutputRecordRejection(line_number, "not_object")
                    )
                continue
            raise ContentRejected(
                f"Interface output record {line_number} must be a JSON object."
            )
        try:
            record = InterfaceOutputRecord.from_dict(decoded)
        except ContentRejected:
            if tolerate_invalid_records:
                if rejected_records is not None:
                    rejected_records.append(
                        InterfaceOutputRecordRejection(line_number, "invalid_record")
                    )
                continue
            raise
        previous = by_id.get(record.output_id)
        if previous is not None:
            if previous != record:
                if tolerate_invalid_records:
                    if rejected_records is not None:
                        rejected_records.append(
                            InterfaceOutputRecordRejection(
                                line_number, "conflicting_duplicate_id"
                            )
                        )
                    continue
                raise ContentRejected(
                    f"Interface output id {record.output_id!r} was reused for a "
                    "different generation."
                )
            continue
        by_id[record.output_id] = record
        records.append(record)
        if accepted_record_lines is not None:
            accepted_record_lines[record.output_id] = line_number
    return tuple(records)


def seal_interface_output_generation(
    capture: LocalContentCapture,
    *,
    record: InterfaceOutputRecord,
    root_handles: Mapping[str, Path],
    operation_id: str | None = None,
    limits: SealLimits | None = None,
) -> SealedInterfaceOutput:
    """Freeze one record below its granted root through the normal CAS capture.

    ``root_handles`` is trusted launch context assembled by the supervisor; the
    record can only name one of those opaque handles.  The content store opens
    every selected component with no-follow descriptors and performs stable
    before/after identity checks, so symlink and mutation races fail capture.
    """

    if not isinstance(capture, LocalContentCapture):
        raise TypeError("capture must be a LocalContentCapture.")
    if not isinstance(record, InterfaceOutputRecord):
        raise TypeError("record must be an InterfaceOutputRecord.")
    try:
        allowed_root = Path(root_handles[record.root_handle])
    except (KeyError, TypeError) as error:
        raise ContentRejected(
            f"Interface output root handle {record.root_handle!r} was not granted."
        ) from error
    if not allowed_root.is_absolute():
        raise ValueError(
            "Interface output root handles must resolve to absolute paths."
        )
    if record.kind is InterfaceOutputKind.TREE:
        receipt = capture.seal_tree(
            source=AllowedTreeSource(allowed_root, record.relative_path),
            limits=limits,
            operation_id=operation_id,
        )
        return SealedInterfaceOutput(
            record=record,
            content_ref=receipt.snapshot_ref,
            logical_bytes=receipt.manifest.logical_bytes,
        )
    receipt = capture.seal_blob(
        source=AllowedFileSource(allowed_root, record.relative_path),
        limits=limits,
    )
    return SealedInterfaceOutput(
        record=record,
        content_ref=receipt.blob_ref,
        logical_bytes=receipt.publication.logical_bytes,
    )


def list_interface_output_tree_paths(
    root: Path,
    *,
    max_entries: int = 512,
    max_depth: int = 24,
) -> tuple[str, ...]:
    """Return bounded portable directory choices below one trusted output root.

    This is an advisory picker snapshot, not capture authority. Every directory
    is opened relative to an already-open parent descriptor with no-follow
    semantics. The selected path is validated and opened again by
    :func:`seal_interface_output_generation`, so a mutation after listing can
    never redirect the later capture outside ``root``.
    """

    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Interface output roots must be absolute paths.")
    _positive_limit(max_entries, "max_entries")
    _positive_limit(max_depth, "max_depth")
    try:
        root_fd = os.open(root, _DIRECTORY_READ_FLAGS)
    except OSError as error:
        raise ContentRejected("Interface output tree picker is unavailable.") from error

    results = ["."]
    inspected_entries = 0

    def visit(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal inspected_entries
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise ContentRejected(
                "Interface output tree picker root is not a directory."
            )
        try:
            names = []
            with os.scandir(directory_fd) as iterator:
                for child in iterator:
                    inspected_entries += 1
                    if inspected_entries > max_entries:
                        raise ContentRejected(
                            "Interface output tree picker exceeds "
                            f"{max_entries} filesystem entries."
                        )
                    names.append(child.name)
        except OSError as error:
            raise ContentRejected(
                "Interface output tree picker is unavailable."
            ) from error
        for name in sorted(names):
            try:
                node = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise ContentRejected(
                    "Interface output tree changed while it was listed."
                ) from error
            if not stat.S_ISDIR(node.st_mode):
                continue
            relative_path = f"{prefix}/{name}" if prefix else name
            try:
                portable_path = validate_portable_path(relative_path)
            except ContentRejected:
                # Non-portable names can remain runtime data, but Studio must
                # never offer a choice that the content plane cannot retain.
                continue
            if len(results) >= max_entries:
                raise ContentRejected(
                    f"Interface output tree picker exceeds {max_entries} directories."
                )
            try:
                child_fd = os.open(name, _DIRECTORY_READ_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise ContentRejected(
                    "Interface output tree changed while it was listed."
                ) from error
            try:
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev != node.st_dev
                    or opened.st_ino != node.st_ino
                    or not stat.S_ISDIR(opened.st_mode)
                ):
                    raise ContentRejected(
                        "Interface output tree changed while it was listed."
                    )
                results.append(portable_path)
                if depth < max_depth:
                    visit(child_fd, portable_path, depth + 1)
            finally:
                os.close(child_fd)
        after = os.fstat(directory_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ContentRejected("Interface output tree changed while it was listed.")

    try:
        visit(root_fd, "", 1)
        return tuple(results)
    finally:
        os.close(root_fd)


def require_idempotent_generation(
    previous: SealedInterfaceOutput,
    current: SealedInterfaceOutput,
) -> SealedInterfaceOutput:
    """Accept duplicate generation ids only when their sealed identity matches."""

    if not isinstance(previous, SealedInterfaceOutput) or not isinstance(
        current, SealedInterfaceOutput
    ):
        raise TypeError("generation values must be SealedInterfaceOutput instances.")
    if previous.record.output_id != current.record.output_id:
        raise ValueError("Cannot compare different interface output ids.")
    if previous != current:
        raise ContentRejected(
            f"Interface output id {current.record.output_id!r} resolved to a "
            "different sealed generation."
        )
    return previous


def _read_bounded_regular_file(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise ContentRejected(
            "Interface output control file is unavailable."
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ContentRejected(
                "Interface output control path is not a regular file."
            )
        if before.st_size > limit:
            raise ContentRejected(
                f"Interface output control file exceeds {limit} bytes."
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise ContentRejected(
                f"Interface output control file exceeds {limit} bytes."
            )
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise ContentRejected(
                "Interface output control file changed while it was read."
            )
        return payload
    finally:
        os.close(descriptor)


def _relative_selection(value: Any) -> str:
    if value == ".":
        return "."
    if not isinstance(value, str):
        raise ContentRejected("Interface output path must be a string.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ContentRejected("Interface output path must be valid UTF-8.") from error
    if len(encoded) > _MAX_PATH_BYTES:
        raise ContentRejected(
            f"Interface output path exceeds {_MAX_PATH_BYTES} UTF-8 bytes."
        )
    return validate_portable_path(value)


def _bounded_text(value: Any, label: str, byte_limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentRejected(f"{label.capitalize()} must be a non-empty string.")
    if value != value.strip():
        raise ContentRejected(
            f"{label.capitalize()} must not have surrounding whitespace."
        )
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ContentRejected(f"{label.capitalize()} must be valid UTF-8.") from error
    if len(encoded) > byte_limit:
        raise ContentRejected(f"{label.capitalize()} exceeds {byte_limit} UTF-8 bytes.")
    return value


def _bounded_identifier(value: Any, label: str, byte_limit: int) -> str:
    result = _bounded_text(value, label, byte_limit)
    if not _IDENTIFIER_RE.fullmatch(result):
        raise ContentRejected(
            f"{label.capitalize()} must contain only letters, digits, '.', '_', or '-'."
        )
    return result


def _positive_limit(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


__all__: Sequence[str] = (
    "INTERFACE_OUTPUT_SCHEMA",
    "InterfaceOutputKind",
    "InterfaceOutputRecord",
    "InterfaceOutputRecordRejection",
    "SealedInterfaceOutput",
    "list_interface_output_tree_paths",
    "read_interface_output_records",
    "require_idempotent_generation",
    "seal_interface_output_generation",
)
