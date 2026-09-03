"""Publish completed DEVS projects through OptPilot's generic output contract.

This module deliberately knows nothing about Studio, catalogs, or environment
registration.  It only turns a completed runnable project into an immutable
launch-local generation and appends one bounded, path-free JSONL selection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


OUTPUT_ROOT_ENV = "OPTPILOT_INTERFACE_OUTPUT_ROOT"
OUTPUTS_FILE_ENV = "OPTPILOT_INTERFACE_OUTPUTS_FILE"
OUTPUT_SCHEMA = "optpilot.interface.output.v1"

_REQUIRED_BUNDLE_ENTRIES = {
    "run.py": "file",
    "simulation.json": "file",
    "README.md": "file",
    "devs_project": "directory",
}
_MAX_CONTROL_BYTES = 1024 * 1024
_MAX_CONTROL_RECORDS = 256
_MAX_RECORD_BYTES = 16 * 1024
_MAX_GENERATION_ENTRIES = 20_000
_MAX_GENERATION_DEPTH = 64
_MAX_RELATIVE_PATH_BYTES = 4096
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_GENERATION_BYTES = 512 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_OUTPUT_ENTRIES = 50_000
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PUBLISHED_DIRECTORY_MODE = 0o700
_PUBLISHED_FILE_MODE = 0o600
_PUBLISHED_EXECUTABLE_FILE_MODE = 0o700
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_CONTROL_FLAGS = (
    os.O_WRONLY
    | os.O_APPEND
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class _TreeEntry:
    relative_path: str
    kind: str
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int


class InterfaceOutputPublisher:
    """Freeze and announce complete runnable bundles below one output handle."""

    def __init__(self, output_root: Path, outputs_file: Path):
        self.output_root = Path(output_root)
        self.outputs_file = Path(outputs_file)
        if not self.output_root.is_absolute() or not self.outputs_file.is_absolute():
            raise ValueError("Interface output paths must be absolute launch handles.")
        self.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.output_root.is_dir():
            raise ValueError("Interface output root must be a directory.")
        self._generations_root = self.output_root / "generations"
        self._ensure_private_directory(self._generations_root)
        self.outputs_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._prepare_control_file()
        self._lock = threading.Lock()
        self._emitted, self._control_record_count = self._load_existing_records()
        self._published_bytes = self._inventory_committed_generations()

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "InterfaceOutputPublisher | None":
        environment = os.environ if environment is None else environment
        output_root = str(environment.get(OUTPUT_ROOT_ENV, "")).strip()
        outputs_file = str(environment.get(OUTPUTS_FILE_ENV, "")).strip()
        if not output_root and not outputs_file:
            return None
        if not output_root or not outputs_file:
            raise ValueError(
                f"{OUTPUT_ROOT_ENV} and {OUTPUTS_FILE_ENV} must be supplied together."
            )
        return cls(Path(output_root), Path(outputs_file))

    def publish_ready_project(
        self,
        *,
        session_id: str,
        request_id: str,
        workspace: Path,
        project: Mapping[str, Any],
        expected_content_digest: str | None = None,
    ) -> dict[str, str] | None:
        """Publish one complete project, or return ``None`` if it is incomplete.

        A private staging tree is copied with no-follow file opens while source
        identities are stable.  Renaming that tree commits the generation;
        appending the complete newline commits its control record.
        """

        project_path = _safe_relative_path(project.get("path"))
        project_parts = PurePosixPath(project_path).parts
        source = Path(workspace).joinpath(*project_parts)
        before = _snapshot_tree(source)
        # The visualizer indexes the xDEVS marker directory itself.  The
        # runnable bundle produced by the constructor is its parent, where
        # run.py and README.md sit alongside devs_project/.
        if (
            not _is_complete_bundle(before)
            and len(project_parts) > 1
            and project_parts[-1] == "devs_project"
        ):
            source = source.parent
            before = _snapshot_tree(source)
        if not _is_complete_bundle(before):
            return None
        bundle_bytes = _logical_bytes(before)

        with self._lock:
            staging = self._generations_root / f".staging-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700)
            directory_modes: tuple[tuple[Path, int], ...] = ()
            try:
                content_digest, directory_modes = _copy_stable_tree(
                    source, staging, before
                )
                if (
                    expected_content_digest is not None
                    and content_digest != expected_content_digest
                ):
                    raise RuntimeError(
                        "Generated project changed after its successful validation."
                    )
                # The output represents one exact simulation version, not the
                # UI action that happened to publish it.  Automatic validation
                # and a later student Run may both reach this point; excluding
                # request_id keeps those retries idempotent instead of showing
                # duplicate output cards for identical work.
                label = _output_label(project.get("display_name"))
                identity_material = "\0".join(
                    (
                        str(session_id),
                        str(project.get("project_id", "")),
                        str(project.get("version", "")),
                        content_digest,
                        label,
                    )
                ).encode("utf-8", errors="strict")
                output_id = f"devs-{hashlib.sha256(identity_material).hexdigest()[:32]}"
                record = {
                    "schema_version": OUTPUT_SCHEMA,
                    "id": output_id,
                    "label": label,
                    "kind": "tree",
                    "root": "output",
                    "path": f"generations/{output_id}",
                }
                prior = self._emitted.get(output_id)
                if prior is not None and prior != record:
                    raise RuntimeError("Interface output id was reused with new metadata.")
                if prior is None and self._control_record_count >= _MAX_CONTROL_RECORDS:
                    raise RuntimeError("Interface output record limit has been reached.")
                final = self._generations_root / output_id
                if final.exists():
                    if final.is_symlink() or not final.is_dir():
                        raise RuntimeError("Committed interface output name is not a directory.")
                    if _stable_tree_digest(final) != content_digest:
                        raise RuntimeError(
                            "Committed interface output id resolved to different content."
                        )
                    shutil.rmtree(staging)
                else:
                    if self._published_bytes + bundle_bytes > _MAX_TOTAL_OUTPUT_BYTES:
                        raise RuntimeError("Interface output byte limit has been reached.")
                    _set_directory_modes(directory_modes, seal=True)
                    os.replace(staging, final)
                    _fsync_directory(self._generations_root)
                    self._published_bytes += bundle_bytes

                if prior is not None:
                    return record
                self._append_record(record)
                self._emitted[output_id] = record
                self._control_record_count += 1
                return record
            finally:
                if staging.exists():
                    _set_directory_modes(directory_modes, seal=False)
                    shutil.rmtree(staging)

    def _prepare_control_file(self) -> None:
        descriptor = os.open(self.outputs_file, _CONTROL_FLAGS, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Interface output control path must be a regular file.")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _append_record(self, record: Mapping[str, str]) -> None:
        payload = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8", errors="strict")
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("Interface output record exceeds 16 KiB.")
        descriptor = os.open(self.outputs_file, _CONTROL_FLAGS, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Interface output control path must be a regular file.")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PermissionError("Interface output control file is not private.")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("Interface output record append was incomplete.")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load_existing_records(self) -> tuple[dict[str, dict[str, str]], int]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(self.outputs_file, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Interface output control path must be a regular file.")
            if metadata.st_size > _MAX_CONTROL_BYTES:
                raise ValueError("Interface output control file exceeds 1 MiB.")
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            payload = b""
            while len(payload) <= _MAX_CONTROL_BYTES:
                chunk = os.read(descriptor, min(64 * 1024, _MAX_CONTROL_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload += chunk
            if len(payload) > _MAX_CONTROL_BYTES:
                raise ValueError("Interface output control file exceeds 1 MiB.")
        finally:
            os.close(descriptor)

        if not payload.endswith(b"\n"):
            final_newline = payload.rfind(b"\n")
            payload = b"" if final_newline < 0 else payload[: final_newline + 1]
        records: dict[str, dict[str, str]] = {}
        record_count = 0
        for line in payload.splitlines():
            if not line.strip():
                continue
            record_count += 1
            if record_count > _MAX_CONTROL_RECORDS:
                raise ValueError("Interface output control file exceeds 256 records.")
            if len(line) > _MAX_RECORD_BYTES:
                raise ValueError("Interface output record exceeds 16 KiB.")
            try:
                decoded = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Interface output control file contains invalid JSON.") from exc
            record = _validate_control_record(decoded)
            previous = records.get(record["id"])
            if previous is not None and previous != record:
                raise ValueError("Interface output id has conflicting existing records.")
            records[record["id"]] = record
        return records, record_count

    def _inventory_committed_generations(self) -> int:
        logical_bytes = 0
        entry_count = 0
        generation_count = 0
        for child in self._generations_root.iterdir():
            if child.name.startswith(".staging-"):
                continue
            if child.is_symlink() or not child.is_dir():
                raise ValueError("Interface generations root contains an invalid entry.")
            generation_count += 1
            if generation_count > _MAX_CONTROL_RECORDS:
                raise ValueError("Interface generations root exceeds 256 generations.")
            entries = _snapshot_tree(child)
            entry_count += len(entries)
            logical_bytes += _logical_bytes(entries)
            if entry_count > _MAX_TOTAL_OUTPUT_ENTRIES:
                raise ValueError("Interface generations exceed the total entry limit.")
            if logical_bytes > _MAX_TOTAL_OUTPUT_BYTES:
                raise ValueError("Interface generations exceed the total byte limit.")
        return logical_bytes

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Interface generations path must be a directory.")


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Generated project path must be a portable relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix():
        raise ValueError("Generated project path must be a portable relative path.")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Generated project path cannot traverse its session workspace.")
    return value


def _output_label(display_name: Any) -> str:
    name = str(display_name or "Generated simulator").strip() or "Generated simulator"
    label = f"Generated simulator: {name}"
    encoded = label.encode("utf-8", errors="replace")
    if len(encoded) <= 512:
        return label
    encoded = encoded[:509]
    while True:
        try:
            return encoded.decode("utf-8") + "..."
        except UnicodeDecodeError:
            encoded = encoded[:-1]


def _snapshot_tree(root: Path) -> tuple[_TreeEntry, ...]:
    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Generated project must be a real directory.")
    entries: list[_TreeEntry] = []
    logical_bytes = 0

    def visit(directory: Path, prefix: PurePosixPath | None = None) -> None:
        nonlocal logical_bytes
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            metadata = child.stat(follow_symlinks=False)
            relative = PurePosixPath(child.name) if prefix is None else prefix / child.name
            if len(relative.parts) > _MAX_GENERATION_DEPTH:
                raise ValueError("Generated project exceeds the maximum tree depth.")
            if len(relative.as_posix().encode("utf-8", errors="strict")) > _MAX_RELATIVE_PATH_BYTES:
                raise ValueError("Generated project contains an overlong relative path.")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("Generated project cannot contain symbolic links.")
            if stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                if metadata.st_size > _MAX_FILE_BYTES:
                    raise ValueError("Generated project contains an oversized file.")
                logical_bytes += metadata.st_size
            else:
                raise ValueError("Generated project cannot contain special files.")
            entries.append(
                _TreeEntry(
                    relative_path=relative.as_posix(),
                    kind=kind,
                    mode=stat.S_IMODE(metadata.st_mode),
                    size=metadata.st_size,
                    modified_ns=metadata.st_mtime_ns,
                    changed_ns=metadata.st_ctime_ns,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                )
            )
            if len(entries) > _MAX_GENERATION_ENTRIES:
                raise ValueError("Generated project exceeds the entry limit.")
            if logical_bytes > _MAX_GENERATION_BYTES:
                raise ValueError("Generated project exceeds the byte limit.")
            if kind == "directory":
                visit(Path(child.path), relative)

    visit(root)
    return tuple(entries)


def _is_complete_bundle(entries: tuple[_TreeEntry, ...]) -> bool:
    by_path = {entry.relative_path: entry.kind for entry in entries}
    return all(by_path.get(path) == kind for path, kind in _REQUIRED_BUNDLE_ENTRIES.items())


def _logical_bytes(entries: tuple[_TreeEntry, ...] | list[_TreeEntry]) -> int:
    return sum(entry.size for entry in entries if entry.kind == "file")


def _validate_control_record(value: Any) -> dict[str, str]:
    expected = {"schema_version", "id", "label", "kind", "root", "path"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Interface output control record has an invalid shape.")
    if value["schema_version"] != OUTPUT_SCHEMA:
        raise ValueError("Interface output control record has an invalid schema.")
    for field in expected:
        if not isinstance(value[field], str):
            raise ValueError("Interface output control record fields must be strings.")
    output_id = value["id"]
    if (
        not output_id
        or len(output_id.encode("utf-8")) > 128
        or _IDENTIFIER_RE.fullmatch(output_id) is None
    ):
        raise ValueError("Interface output control record has an invalid id.")
    if not value["label"].strip() or len(value["label"].encode("utf-8")) > 512:
        raise ValueError("Interface output control record has an invalid label.")
    if value["kind"] not in {"file", "tree"}:
        raise ValueError("Interface output control record has an invalid kind.")
    root = value["root"]
    if not root or len(root.encode("utf-8")) > 128 or _IDENTIFIER_RE.fullmatch(root) is None:
        raise ValueError("Interface output control record has an invalid root handle.")
    if value["path"] != ".":
        _safe_relative_path(value["path"])
    return dict(value)


def _metadata_matches(entry: _TreeEntry, metadata: os.stat_result) -> bool:
    actual_kind = (
        "directory"
        if stat.S_ISDIR(metadata.st_mode)
        else "file"
        if stat.S_ISREG(metadata.st_mode)
        else "other"
    )
    return (
        actual_kind == entry.kind
        and stat.S_IMODE(metadata.st_mode) == entry.mode
        and metadata.st_size == entry.size
        and metadata.st_mtime_ns == entry.modified_ns
        and metadata.st_ctime_ns == entry.changed_ns
        and metadata.st_dev == entry.device
        and metadata.st_ino == entry.inode
    )


def _published_mode(entry: _TreeEntry) -> int:
    """Return the private, portable mode used by a published generation.

    Docker Desktop represents chmod operations on bind mounts with a private
    ownership xattr.  On some macOS filesystems the visible host mode can stay
    owner-writable even when that xattr records a read-only source mode.  Such
    a mismatch is deliberately rejected by OptPilot's content store.  Output
    generations therefore preserve the only mode distinction represented by
    a tree manifest--whether a file is executable--while using canonical
    owner-only modes for everything else.
    """

    if entry.kind == "directory":
        return _PUBLISHED_DIRECTORY_MODE
    if entry.mode & 0o111:
        return _PUBLISHED_EXECUTABLE_FILE_MODE
    return _PUBLISHED_FILE_MODE


def _digest_header(digest: "hashlib._Hash", entry: _TreeEntry) -> None:
    digest.update(entry.kind.encode("ascii"))
    digest.update(b"\0")
    digest.update(entry.relative_path.encode("utf-8", errors="strict"))
    digest.update(b"\0")
    digest.update(f"{_published_mode(entry):o}".encode("ascii"))
    digest.update(b"\0")


def _copy_stable_tree(
    source: Path, destination: Path, before: tuple[_TreeEntry, ...]
) -> tuple[str, tuple[tuple[Path, int], ...]]:
    digest = hashlib.sha256()
    directory_modes: list[tuple[Path, int]] = []
    for entry in before:
        source_path = source.joinpath(*PurePosixPath(entry.relative_path).parts)
        target_path = destination.joinpath(*PurePosixPath(entry.relative_path).parts)
        _digest_header(digest, entry)
        if entry.kind == "directory":
            metadata = source_path.lstat()
            if not _metadata_matches(entry, metadata):
                raise RuntimeError("Generated project changed while it was published.")
            # Published directories stay private and writable while the tree
            # is assembled.  They intentionally do not inherit a read-only
            # source mode: that metadata is unstable across Docker Desktop
            # bind mounts and is not represented by OptPilot tree manifests.
            target_path.mkdir(mode=_PUBLISHED_DIRECTORY_MODE)
            os.chmod(target_path, _PUBLISHED_DIRECTORY_MODE)
            directory_modes.append((target_path, _published_mode(entry)))
            continue

        descriptor = os.open(source_path, _READ_FLAGS)
        try:
            if not _metadata_matches(entry, os.fstat(descriptor)):
                raise RuntimeError("Generated project changed while it was published.")
            published_mode = _published_mode(entry)
            output = os.open(
                target_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                published_mode,
            )
            try:
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        count = os.write(output, view)
                        view = view[count:]
                os.fchmod(output, published_mode)
                os.fsync(output)
            finally:
                os.close(output)
            if not _metadata_matches(entry, os.fstat(descriptor)):
                raise RuntimeError("Generated project changed while it was published.")
        finally:
            os.close(descriptor)
        digest.update(b"\0")

    if _snapshot_tree(source) != before:
        raise RuntimeError("Generated project changed while it was published.")
    if not _is_complete_bundle(_snapshot_tree(destination)):
        raise RuntimeError("Published interface generation is not runnable.")
    return digest.hexdigest(), tuple(directory_modes)


def _set_directory_modes(
    directory_modes: tuple[tuple[Path, int], ...], *, seal: bool
) -> None:
    """Finalize copied directories bottom-up, or reopen staging for cleanup."""

    items = reversed(directory_modes) if seal else iter(directory_modes)
    for path, mode in items:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("Interface output staging directory was replaced.")
            os.fchmod(descriptor, mode if seal else 0o700)
        finally:
            os.close(descriptor)


def _stable_tree_digest(root: Path) -> str:
    before = _snapshot_tree(root)
    digest = hashlib.sha256()
    for entry in before:
        _digest_header(digest, entry)
        if entry.kind == "file":
            path = root.joinpath(*PurePosixPath(entry.relative_path).parts)
            descriptor = os.open(path, _READ_FLAGS)
            try:
                if not _metadata_matches(entry, os.fstat(descriptor)):
                    raise RuntimeError("Interface generation changed during verification.")
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                if not _metadata_matches(entry, os.fstat(descriptor)):
                    raise RuntimeError("Interface generation changed during verification.")
            finally:
                os.close(descriptor)
            digest.update(b"\0")
    if _snapshot_tree(root) != before:
        raise RuntimeError("Interface generation changed during verification.")
    return digest.hexdigest()


def stable_tree_digest(root: Path) -> str:
    """Return the canonical digest used to fence validation and publication."""

    return _stable_tree_digest(Path(root))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
