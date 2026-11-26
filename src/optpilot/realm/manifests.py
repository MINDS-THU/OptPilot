"""Canonical local-content manifests and portable path validation.

This module contains no filesystem or ledger operations.  It defines the bytes
that identify a local blob or tree and the deliberately conservative v1 path
contract shared by sealing, verification, and future projections.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, Tuple

from .errors import ContentCorrupt, ContentRejected
from .refs import BlobRef, SnapshotRef, canonical_json_bytes


_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_PORTABLE_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')


@dataclass(frozen=True)
class SealLimits:
    """Hard bounds checked before and during a local tree capture."""

    max_entries: int = 100_000
    max_depth: int = 64
    max_total_bytes: int = 100 * 1024**3
    max_file_bytes: int = 100 * 1024**3
    max_path_bytes: int = 4096
    max_component_bytes: int = 255

    def __post_init__(self) -> None:
        for field_name in (
            "max_entries",
            "max_depth",
            "max_total_bytes",
            "max_file_bytes",
            "max_path_bytes",
            "max_component_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")


def validate_portable_path(path: str, *, limits: SealLimits | None = None) -> str:
    """Validate one canonical, portable, non-root relative path.

    V1 rejects non-NFC spellings instead of silently changing a user's path.
    It also rejects names that become ambiguous on common case-insensitive or
    Windows filesystems.
    """

    limits = limits or SealLimits()
    if not isinstance(path, str) or not path:
        raise ContentRejected("Content paths must be non-empty strings.")
    if path.startswith("/") or path.endswith("/") or "//" in path or "\\" in path:
        raise ContentRejected(f"Content path is not a canonical relative path: {path!r}.")
    try:
        encoded_path = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ContentRejected(f"Content path is not valid UTF-8: {path!r}.") from error
    if len(encoded_path) > limits.max_path_bytes:
        raise ContentRejected(f"Content path exceeds {limits.max_path_bytes} UTF-8 bytes: {path!r}.")

    components = path.split("/")
    if len(components) > limits.max_depth:
        raise ContentRejected(f"Content path exceeds maximum depth {limits.max_depth}: {path!r}.")
    for component in components:
        _validate_portable_component(component, limits=limits, display_path=path)
    return path


def validate_portable_paths(
    paths: Iterable[str],
    *,
    limits: SealLimits | None = None,
) -> Tuple[str, ...]:
    """Validate a complete path set and reject NFC/case collisions."""

    limits = limits or SealLimits()
    canonical = []
    exact = set()
    portable = {}
    for path in paths:
        value = validate_portable_path(path, limits=limits)
        if value in exact:
            raise ContentRejected(f"Duplicate content path: {value!r}.")
        exact.add(value)
        key = "/".join(component.casefold() for component in value.split("/"))
        previous = portable.get(key)
        if previous is not None:
            raise ContentRejected(
                f"Paths collide on a case-insensitive target: {previous!r} and {value!r}."
            )
        portable[key] = value
        canonical.append(value)
        if len(canonical) > limits.max_entries:
            raise ContentRejected(f"Tree exceeds maximum entry count {limits.max_entries}.")
    return tuple(sorted(canonical, key=lambda item: item.encode("utf-8")))


def _validate_portable_component(
    component: str,
    *,
    limits: SealLimits,
    display_path: str,
) -> None:
    if component in {"", ".", ".."}:
        raise ContentRejected(f"Content path contains traversal or an empty component: {display_path!r}.")
    if unicodedata.normalize("NFC", component) != component:
        raise ContentRejected(f"Content path component is not NFC-normalized: {display_path!r}.")
    try:
        encoded = component.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ContentRejected(f"Content path component is not valid UTF-8: {display_path!r}.") from error
    if len(encoded) > limits.max_component_bytes:
        raise ContentRejected(
            f"Content path component exceeds {limits.max_component_bytes} UTF-8 bytes: {display_path!r}."
        )
    if component.endswith((" ", ".")):
        raise ContentRejected(f"Content path component has a reserved suffix: {display_path!r}.")
    if any(ord(character) < 32 or character in _PORTABLE_FORBIDDEN_CHARACTERS for character in component):
        raise ContentRejected(f"Content path contains a reserved character: {display_path!r}.")
    reserved_stem = component.split(".", 1)[0].casefold()
    if reserved_stem in _WINDOWS_RESERVED_STEMS:
        raise ContentRejected(f"Content path uses a reserved device name: {display_path!r}.")


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One directory or regular-file node in a canonical tree manifest."""

    path: str
    kind: str
    blob_ref: BlobRef | None = None
    size: int | None = None
    executable: bool | None = None

    @classmethod
    def directory(cls, path: str) -> "TreeEntry":
        return cls(path=path, kind="directory")

    @classmethod
    def file(
        cls,
        path: str,
        *,
        blob_ref: BlobRef,
        size: int,
        executable: bool,
    ) -> "TreeEntry":
        return cls(
            path=path,
            kind="file",
            blob_ref=blob_ref,
            size=size,
            executable=executable,
        )

    def __post_init__(self) -> None:
        validate_portable_path(self.path)
        if self.kind == "directory":
            if self.blob_ref is not None or self.size is not None or self.executable is not None:
                raise ValueError("Directory manifest entries cannot carry blob metadata.")
            return
        if self.kind != "file":
            raise ValueError(f"Unsupported tree entry kind: {self.kind!r}.")
        if not isinstance(self.blob_ref, BlobRef):
            raise ValueError("File manifest entries require a BlobRef.")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("File manifest entry size must be a nonnegative integer.")
        if not isinstance(self.executable, bool):
            raise ValueError("File manifest entries require an executable boolean.")

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "directory":
            return {"path": self.path, "type": "directory"}
        return {
            "blob": str(self.blob_ref),
            "executable": self.executable,
            "path": self.path,
            "size": self.size,
            "type": "file",
        }


@dataclass(frozen=True)
class BlobManifest:
    """Canonical metadata stored beside one immutable blob payload."""

    blob_ref: BlobRef
    size: int

    def __post_init__(self) -> None:
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("Blob manifest size must be a nonnegative integer.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "blob": str(self.blob_ref),
            "format": "optpilot.blob.v1",
            "size": self.size,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "BlobManifest":
        value = _decode_manifest_object(payload, expected_format="optpilot.blob.v1")
        if set(value) != {"blob", "format", "size"}:
            raise ContentCorrupt("Blob manifest has unexpected fields.")
        try:
            result = cls(blob_ref=BlobRef.parse(value["blob"]), size=value["size"])
        except (TypeError, ValueError) as error:
            raise ContentCorrupt(f"Blob manifest is invalid: {error}") from error
        if result.to_bytes() != payload:
            raise ContentCorrupt("Blob manifest bytes are not canonical.")
        return result


@dataclass(frozen=True)
class TreeManifest:
    """Canonical, provider-independent identity for one immutable tree."""

    entries: Tuple[TreeEntry, ...]

    @classmethod
    def build(
        cls,
        entries: Sequence[TreeEntry],
        *,
        limits: SealLimits | None = None,
    ) -> "TreeManifest":
        limits = limits or SealLimits()
        return cls(_validate_tree_entries(entries, limits=limits))

    def __post_init__(self) -> None:
        ordered = _validate_tree_entries(self.entries, limits=SealLimits())
        if self.entries != ordered:
            raise ValueError("Tree manifest entries must be sorted by UTF-8 path bytes.")

    @property
    def logical_bytes(self) -> int:
        return sum(entry.size or 0 for entry in self.entries if entry.kind == "file")

    @property
    def snapshot_ref(self) -> SnapshotRef:
        return SnapshotRef.from_manifest_bytes(self.to_bytes())

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "format": "optpilot.tree.v1",
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_bytes(cls, payload: bytes) -> "TreeManifest":
        value = _decode_manifest_object(payload, expected_format="optpilot.tree.v1")
        if set(value) != {"entries", "format"} or not isinstance(value["entries"], list):
            raise ContentCorrupt("Tree manifest has unexpected fields.")
        entries = []
        try:
            for raw in value["entries"]:
                if not isinstance(raw, Mapping):
                    raise ValueError("tree entry is not an object")
                if raw.get("type") == "directory" and set(raw) == {"path", "type"}:
                    entries.append(TreeEntry.directory(raw["path"]))
                elif raw.get("type") == "file" and set(raw) == {
                    "blob",
                    "executable",
                    "path",
                    "size",
                    "type",
                }:
                    entries.append(
                        TreeEntry.file(
                            raw["path"],
                            blob_ref=BlobRef.parse(raw["blob"]),
                            size=raw["size"],
                            executable=raw["executable"],
                        )
                    )
                else:
                    raise ValueError("tree entry has an invalid shape")
            result = cls.build(entries)
        except (ContentRejected, TypeError, ValueError) as error:
            raise ContentCorrupt(f"Tree manifest is invalid: {error}") from error
        if result.to_bytes() != payload:
            raise ContentCorrupt("Tree manifest bytes are not canonical.")
        return result


def _decode_manifest_object(payload: bytes, *, expected_format: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContentCorrupt(f"Manifest is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict) or value.get("format") != expected_format:
        raise ContentCorrupt(f"Manifest format is not {expected_format!r}.")
    return value


def _validate_tree_entries(
    entries: Sequence[TreeEntry],
    *,
    limits: SealLimits,
) -> Tuple[TreeEntry, ...]:
    ordered_paths = validate_portable_paths((entry.path for entry in entries), limits=limits)
    by_path = {entry.path: entry for entry in entries}
    ordered = tuple(by_path[path] for path in ordered_paths)
    total_bytes = 0
    for entry in ordered:
        if entry.kind == "file":
            assert entry.size is not None
            if entry.size > limits.max_file_bytes:
                raise ContentRejected(
                    f"File {entry.path!r} exceeds maximum size {limits.max_file_bytes} bytes."
                )
            total_bytes += entry.size
        components = entry.path.split("/")
        for index in range(1, len(components)):
            parent_path = "/".join(components[:index])
            parent = by_path.get(parent_path)
            if parent is None:
                raise ContentRejected(
                    f"Tree entry {entry.path!r} has an unrepresented parent {parent_path!r}."
                )
            if parent.kind != "directory":
                raise ContentRejected(
                    f"Tree entry {entry.path!r} descends through file {parent_path!r}."
                )
    if total_bytes > limits.max_total_bytes:
        raise ContentRejected(f"Tree exceeds maximum logical size {limits.max_total_bytes} bytes.")
    return ordered


__all__ = [
    "BlobManifest",
    "SealLimits",
    "TreeEntry",
    "TreeManifest",
    "validate_portable_path",
    "validate_portable_paths",
]
