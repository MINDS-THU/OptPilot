"""Actor-bound, no-copy content inspection for exact selection references.

This service is the byte-oriented counterpart to ``RealmSelectionActionService``.
It never materializes a projection, creates an owner, or derives a workspace.
Every operation resolves the immutable ``SelectionRef`` through Realm authority
both before and after bounded access to the local immutable content store.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping

from . import content as content_module
from .content import LocalContentStore
from .errors import ContentCorrupt, RealmIntegrityError, RealmNotFound
from .ledger import PrincipalRecord, RealmLedger
from .manifests import BlobManifest, TreeEntry, TreeManifest, validate_portable_path
from .refs import BlobRef, SnapshotRef, request_digest
from .selections import (
    ResolvedSelectionContent,
    SelectionEligibility,
    SelectionRef,
)


DEFAULT_TREE_PAGE_LIMIT = 100
MAX_TREE_PAGE_LIMIT = 200
DEFAULT_BYTE_READ_LENGTH = 64 * 1024
MAX_BYTE_READ_LENGTH = 1024 * 1024
_MAX_CURSOR_BYTES = 256
_MAX_OFFSET = 2**63 - 1
_CURSOR_PREFIX = "selection-content-v1"


@dataclass(frozen=True)
class SelectionContentSummary:
    """Path-free capability facts for one exact selection."""

    selection_digest: str
    eligibility: SelectionEligibility
    content_kind: str | None
    entry_count: int | None
    total_bytes: int | None

    def __post_init__(self) -> None:
        _selection_digest(self.selection_digest)
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.content_kind not in {None, "semantic", "tree", "blob"}:
            raise ValueError("selection content kind is invalid.")
        if self.eligibility.eligible:
            if self.content_kind not in {"tree", "blob"}:
                raise ValueError("eligible content must be a tree or blob.")
            if self.total_bytes is None:
                raise ValueError("eligible content requires its total byte count.")
            if self.content_kind == "tree" and self.entry_count is None:
                raise ValueError("eligible tree content requires its entry count.")
            if self.content_kind == "blob" and self.entry_count is not None:
                raise ValueError("blob content cannot have an entry count.")
        elif self.content_kind not in {None, "semantic"}:
            raise ValueError("ineligible content cannot expose a physical kind.")
        for value, label in (
            (self.entry_count, "selection content entry count"),
            (self.total_bytes, "selection content total bytes"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{label} must be a nonnegative integer or null.")

    def to_dict(self) -> dict[str, object]:
        return {
            "selection_digest": self.selection_digest,
            "eligibility": self.eligibility.to_dict(),
            "content_kind": self.content_kind,
            "entry_count": self.entry_count,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True)
class SelectionTreeEntry:
    """One portable manifest entry, without a host path or physical ref."""

    relative_path: str
    kind: str
    size: int | None
    executable: bool | None

    @classmethod
    def from_manifest_entry(cls, entry: TreeEntry) -> "SelectionTreeEntry":
        if not isinstance(entry, TreeEntry):
            raise TypeError("entry must be a TreeEntry.")
        return cls(entry.path, entry.kind, entry.size, entry.executable)

    def __post_init__(self) -> None:
        validate_portable_path(self.relative_path)
        if self.kind == "directory":
            if self.size is not None or self.executable is not None:
                raise ValueError("directory entries cannot carry file metadata.")
            return
        if self.kind != "file":
            raise ValueError("selection tree entry kind is invalid.")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("file entry size must be a nonnegative integer.")
        if not isinstance(self.executable, bool):
            raise ValueError("file entry executable must be a boolean.")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "size": self.size,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class SelectionTreePage:
    """One deterministic bounded page over a canonical tree manifest."""

    selection_digest: str
    eligibility: SelectionEligibility
    entries: tuple[SelectionTreeEntry, ...]
    total_entries: int | None
    next_cursor: str | None

    def __post_init__(self) -> None:
        _selection_digest(self.selection_digest)
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        entries = tuple(self.entries)
        if any(not isinstance(item, SelectionTreeEntry) for item in entries):
            raise TypeError("entries must contain SelectionTreeEntry values.")
        if entries != tuple(
            sorted(entries, key=lambda item: item.relative_path.encode("utf-8"))
        ):
            raise ValueError("selection tree page entries must be canonical.")
        object.__setattr__(self, "entries", entries)
        if self.eligibility.eligible:
            if (
                isinstance(self.total_entries, bool)
                or not isinstance(self.total_entries, int)
                or self.total_entries < len(entries)
            ):
                raise ValueError("eligible tree page requires a valid total entry count.")
        elif entries or self.total_entries is not None or self.next_cursor is not None:
            raise ValueError("ineligible tree page cannot expose manifest data.")
        if self.next_cursor is not None:
            _bounded_cursor(self.next_cursor)

    def to_dict(self) -> dict[str, object]:
        return {
            "selection_digest": self.selection_digest,
            "eligibility": self.eligibility.to_dict(),
            "entries": [item.to_dict() for item in self.entries],
            "total_entries": self.total_entries,
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True)
class SelectionByteRead:
    """A bounded immutable byte range selected without exposing store paths."""

    selection_digest: str
    eligibility: SelectionEligibility
    relative_path: str | None
    offset: int | None
    total_size: int | None
    data: bytes | None
    eof: bool | None

    def __post_init__(self) -> None:
        _selection_digest(self.selection_digest)
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.relative_path is not None:
            validate_portable_path(self.relative_path)
        if self.eligibility.eligible:
            if (
                isinstance(self.offset, bool)
                or not isinstance(self.offset, int)
                or self.offset < 0
                or isinstance(self.total_size, bool)
                or not isinstance(self.total_size, int)
                or self.total_size < 0
                or not isinstance(self.data, bytes)
                or not isinstance(self.eof, bool)
            ):
                raise ValueError("eligible byte read has invalid range metadata.")
            if self.offset + len(self.data) > self.total_size:
                raise ValueError("byte read exceeds the selected content size.")
            if self.eof != (self.offset + len(self.data) == self.total_size):
                raise ValueError("byte read EOF flag differs from its range.")
        elif any(
            value is not None
            for value in (self.offset, self.total_size, self.data, self.eof)
        ):
            raise ValueError("ineligible byte read cannot expose content data.")


class RealmSelectionContentService:
    """Read retained selection content without projection or semantic copy.

    Realm authority admits only ``verified_local`` content.  Direct reads then
    rely on the local store's immutable-object contract: they revalidate the
    canonical manifest and descriptor identities around bounded access, but do
    not rehash an entire blob for every Workbench capability or byte range.
    """

    def __init__(
        self,
        ledger: RealmLedger,
        principal: PrincipalRecord,
        *,
        local_stores: Mapping[str, LocalContentStore],
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")
        stores = dict(local_stores)
        for store_id, store in stores.items():
            if not isinstance(store_id, str) or not isinstance(store, LocalContentStore):
                raise TypeError(
                    "local_stores must map store ids to LocalContentStore values."
                )
            if store_id != store.store_id:
                raise ValueError("local store mapping key differs from its store id.")
        self._ledger = ledger
        self._principal = principal
        self._local_stores = stores

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    def describe(self, *, selection: SelectionRef) -> SelectionContentSummary:
        resolution = self._resolve(selection)
        if not resolution.eligibility.eligible:
            return SelectionContentSummary(
                selection.selection_digest,
                resolution.eligibility,
                (
                    "semantic"
                    if resolution.eligibility.code
                    == "parameter_candidate_semantic_only"
                    else None
                ),
                None,
                None,
            )
        root = _required_root(resolution)
        store = self._store(root.store_id)
        if isinstance(root.content_ref, SnapshotRef):
            manifest = store.verify_tree(root.content_ref, verify_children=False)
            summary = SelectionContentSummary(
                selection.selection_digest,
                SelectionEligibility.ready(),
                "tree",
                len(manifest.entries),
                manifest.logical_bytes,
            )
        else:
            with _open_verified_blob_metadata(store, root.content_ref) as opened:
                manifest, _data_fd = opened
            summary = SelectionContentSummary(
                selection.selection_digest,
                SelectionEligibility.ready(),
                "blob",
                None,
                manifest.size,
            )
        self._assert_current(selection, resolution)
        return summary

    def list_tree(
        self,
        *,
        selection: SelectionRef,
        cursor: str | None = None,
        limit: int = DEFAULT_TREE_PAGE_LIMIT,
    ) -> SelectionTreePage:
        limit = _page_limit(limit)
        if cursor is not None:
            cursor = _bounded_cursor(cursor)
        resolution = self._resolve(selection)
        if not resolution.eligibility.eligible:
            return SelectionTreePage(
                selection.selection_digest,
                resolution.eligibility,
                (),
                None,
                None,
            )
        root = _required_root(resolution)
        if not isinstance(root.content_ref, SnapshotRef):
            return SelectionTreePage(
                selection.selection_digest,
                SelectionEligibility.unsupported(
                    "selection_content_not_tree",
                    "The selected content is a file, not a browsable tree.",
                ),
                (),
                None,
                None,
            )
        manifest = self._store(root.store_id).verify_tree(
            root.content_ref, verify_children=False
        )
        start = _decode_cursor(
            cursor,
            selection_digest=selection.selection_digest,
            snapshot_ref=root.content_ref,
            entry_count=len(manifest.entries),
        )
        stop = min(start + limit, len(manifest.entries))
        entries = tuple(
            SelectionTreeEntry.from_manifest_entry(item)
            for item in manifest.entries[start:stop]
        )
        next_cursor = (
            _encode_cursor(
                stop,
                selection_digest=selection.selection_digest,
                snapshot_ref=root.content_ref,
                entry_count=len(manifest.entries),
            )
            if stop < len(manifest.entries)
            else None
        )
        self._assert_current(selection, resolution)
        return SelectionTreePage(
            selection.selection_digest,
            SelectionEligibility.ready(),
            entries,
            len(manifest.entries),
            next_cursor,
        )

    def read_range(
        self,
        *,
        selection: SelectionRef,
        relative_path: str | None = None,
        offset: int = 0,
        length: int = DEFAULT_BYTE_READ_LENGTH,
    ) -> SelectionByteRead:
        offset = _byte_offset(offset)
        length = _byte_length(length)
        resolution = self._resolve(selection)
        if not resolution.eligibility.eligible:
            return SelectionByteRead(
                selection.selection_digest,
                resolution.eligibility,
                relative_path,
                None,
                None,
                None,
                None,
            )
        root = _required_root(resolution)
        store = self._store(root.store_id)
        if isinstance(root.content_ref, SnapshotRef):
            if relative_path is None:
                raise ValueError("A tree byte read requires relative_path.")
            relative_path = validate_portable_path(relative_path)
            manifest = store.verify_tree(root.content_ref, verify_children=False)
            entry = _manifest_file(manifest, relative_path)
            assert entry.blob_ref is not None and entry.size is not None
            blob_ref = entry.blob_ref
            expected_size = entry.size
        else:
            if relative_path is not None:
                raise ValueError("A blob byte read does not accept relative_path.")
            blob_ref = root.content_ref
            expected_size = None
        data, total_size = _read_blob_range(
            store,
            blob_ref,
            offset=offset,
            length=length,
            expected_size=expected_size,
        )
        self._assert_current(selection, resolution)
        return SelectionByteRead(
            selection.selection_digest,
            SelectionEligibility.ready(),
            relative_path,
            offset,
            total_size,
            data,
            offset + len(data) == total_size,
        )

    def _resolve(self, selection: SelectionRef) -> ResolvedSelectionContent:
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        resolution = self._ledger.resolve_selection_for_content_read(
            actor_principal_id=self.principal_id,
            selection=selection,
        )
        if resolution.selection != selection:
            raise RealmIntegrityError(
                "Resolved content selection differs from the requested selection."
            )
        return resolution

    def _assert_current(
        self,
        selection: SelectionRef,
        expected: ResolvedSelectionContent,
    ) -> None:
        current = self._resolve(selection)
        if current != expected:
            raise RealmNotFound("Entity not found.")

    def _store(self, store_id: str) -> LocalContentStore:
        try:
            return self._local_stores[store_id]
        except (KeyError, TypeError) as error:
            raise RealmNotFound("Entity not found.") from error


def _required_root(resolution: ResolvedSelectionContent):
    root = resolution.root
    if not resolution.eligibility.eligible or root is None:
        raise RealmIntegrityError("Eligible content resolution has no root.")
    return root


def _manifest_file(manifest: TreeManifest, relative_path: str) -> TreeEntry:
    # Manifest order is canonical but a linear scan keeps this boundary small;
    # v1 manifests are already bounded at capture time.
    for entry in manifest.entries:
        if entry.path == relative_path:
            if entry.kind != "file":
                raise RealmNotFound("Entity not found.")
            return entry
    raise RealmNotFound("Entity not found.")


def _read_blob_range(
    store: LocalContentStore,
    blob_ref: BlobRef,
    *,
    offset: int,
    length: int,
    expected_size: int | None,
) -> tuple[bytes, int]:
    with _open_verified_blob_metadata(store, blob_ref) as opened:
        manifest, data_fd = opened
        if expected_size is not None and manifest.size != expected_size:
            raise ContentCorrupt(
                "Selected tree entry size differs from its immutable blob."
            )
        if offset > manifest.size:
            raise ValueError("byte offset exceeds the selected content size.")
        remaining = min(length, manifest.size - offset)
        chunks: list[bytes] = []
        position = offset
        while remaining:
            if hasattr(os, "pread"):
                chunk = os.pread(data_fd, remaining, position)
            else:  # pragma: no cover - POSIX local Realm is the primary provider.
                os.lseek(data_fd, position, os.SEEK_SET)
                chunk = os.read(data_fd, remaining)
            if not chunk:
                raise ContentCorrupt("Immutable blob ended before its manifest size.")
            chunks.append(chunk)
            position += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), manifest.size


@contextmanager
def _open_verified_blob_metadata(
    store: LocalContentStore,
    blob_ref: BlobRef,
) -> Iterator[tuple[BlobManifest, int]]:
    """Open one already-verified immutable blob without rehashing its payload.

    The ledger resolution preceding this helper proves the exact retained
    object is live and ``verified_local``.  This physical check remains fully
    descriptor-rooted and bounded: it verifies the canonical blob manifest,
    immutable object/data/manifest nodes, exact data size, and stable data
    identity before and after the caller's requested range access.
    """

    object_fd = store._open_live_object_fd(blob_ref)
    data_fd: int | None = None
    try:
        content_module._require_immutable_object_directory(object_fd, blob_ref)
        if set(os.listdir(object_fd)) != {"data", "manifest.json"}:
            raise ContentCorrupt("Selected blob object has unexpected entries.")
        manifest_bytes = content_module._read_child_immutable_regular(
            object_fd,
            "manifest.json",
            max_bytes=content_module._MAX_MANIFEST_BYTES,
            label="selection content blob manifest",
        )
        manifest = BlobManifest.from_bytes(manifest_bytes)
        if manifest.blob_ref != blob_ref:
            raise ContentCorrupt(
                "Selected blob manifest differs from its retained identity."
            )
        data_fd = content_module._open_immutable_regular_child(
            object_fd,
            "data",
            label="selection content blob payload",
        )
        data_before = os.fstat(data_fd)
        if data_before.st_size != manifest.size:
            raise ContentCorrupt(
                "Selected blob payload size differs from its canonical manifest."
            )
        try:
            yield manifest, data_fd
        finally:
            content_module._require_stable_managed_file(
                object_fd,
                "data",
                data_fd,
                data_before,
            )
    finally:
        if data_fd is not None:
            os.close(data_fd)
        os.close(object_fd)


def _page_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_TREE_PAGE_LIMIT
    ):
        raise ValueError(
            f"tree page limit must be between 1 and {MAX_TREE_PAGE_LIMIT}."
        )
    return value


def _byte_offset(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_OFFSET
    ):
        raise ValueError("byte offset must be a bounded nonnegative integer.")
    return value


def _byte_length(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_BYTE_READ_LENGTH
    ):
        raise ValueError(
            f"byte read length must be between 1 and {MAX_BYTE_READ_LENGTH}."
        )
    return value


def _selection_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("selection digest must be lowercase hexadecimal.")
    return value


def _bounded_cursor(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_CURSOR_BYTES
    ):
        raise ValueError("tree cursor must be a bounded canonical string.")
    return value


def _cursor_digest(
    index: int,
    *,
    selection_digest: str,
    snapshot_ref: SnapshotRef,
    entry_count: int,
) -> str:
    return request_digest(
        {
            "schema": "optpilot.selection-content-cursor.v1",
            "selection_digest": selection_digest,
            "tree_identity": request_digest(
                {"snapshot_ref": str(snapshot_ref), "entry_count": entry_count}
            ),
            "index": index,
        }
    )


def _encode_cursor(
    index: int,
    *,
    selection_digest: str,
    snapshot_ref: SnapshotRef,
    entry_count: int,
) -> str:
    digest = _cursor_digest(
        index,
        selection_digest=selection_digest,
        snapshot_ref=snapshot_ref,
        entry_count=entry_count,
    )
    return (
        f"{_CURSOR_PREFIX}.{index}."
        f"{digest}"
    )


def _decode_cursor(
    cursor: str | None,
    *,
    selection_digest: str,
    snapshot_ref: SnapshotRef,
    entry_count: int,
) -> int:
    if cursor is None:
        return 0
    cursor = _bounded_cursor(cursor)
    parts = cursor.split(".")
    if len(parts) != 3 or parts[0] != _CURSOR_PREFIX:
        raise ValueError("tree cursor is invalid for this selection.")
    raw_index, supplied_digest = parts[1], parts[2]
    if not raw_index.isascii() or not raw_index.isdigit():
        raise ValueError("tree cursor is invalid for this selection.")
    index = int(raw_index)
    if str(index) != raw_index or index <= 0 or index >= entry_count:
        raise ValueError("tree cursor is invalid for this selection.")
    expected = _cursor_digest(
        index,
        selection_digest=selection_digest,
        snapshot_ref=snapshot_ref,
        entry_count=entry_count,
    )
    if supplied_digest != expected:
        raise ValueError("tree cursor is invalid for this selection.")
    return index


__all__ = [
    "DEFAULT_BYTE_READ_LENGTH",
    "DEFAULT_TREE_PAGE_LIMIT",
    "MAX_BYTE_READ_LENGTH",
    "MAX_TREE_PAGE_LIMIT",
    "RealmSelectionContentService",
    "SelectionByteRead",
    "SelectionContentSummary",
    "SelectionTreeEntry",
    "SelectionTreePage",
]
