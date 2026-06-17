"""Helpers for storing generated file candidates by reference."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence


JsonDict = Dict[str, Any]

DEFAULT_EXCLUDE_NAMES = {
    ".DS_Store",
    ".git",
    "__pycache__",
}
DEFAULT_EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
}


@dataclass(frozen=True)
class CandidateFileMapping:
    source: Path
    path: str


class CandidateFileStore:
    """Store generated files and return a file-candidate manifest.

    Methods can use this helper when they produce files outside the trial
    workspace. The helper copies those files into the run's candidate store and
    returns a manifest with stable ``contentRef`` and ``sha256`` fields.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        content_ref_mode: str = "absolute",
        content_ref_base: str | Path | None = None,
    ):
        self.root_dir = Path(root_dir).resolve()
        self.content_ref_mode = content_ref_mode
        self.content_ref_base = Path(content_ref_base).resolve() if content_ref_base else None
        if content_ref_mode not in {"absolute", "relative"}:
            raise ValueError("content_ref_mode must be 'absolute' or 'relative'.")
        if content_ref_mode == "relative" and self.content_ref_base is None:
            raise ValueError("content_ref_base is required when content_ref_mode='relative'.")

    def store_directory(
        self,
        source_dir: str | Path,
        *,
        candidate_id: str | None = None,
        entrypoint: str | None = None,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_root = Path(source_dir).resolve()
        if not source_root.is_dir():
            raise NotADirectoryError(f"Candidate source is not a directory: {source_dir}")
        mappings = [
            CandidateFileMapping(source=source, path=source.relative_to(source_root).as_posix())
            for source in sorted(source_root.rglob("*"))
            if source.is_file() and not _is_excluded(source, source_root)
        ]
        if not mappings:
            raise ValueError(f"Candidate directory contains no storable files: {source_dir}")
        return self.store_files(
            mappings,
            candidate_id=candidate_id,
            entrypoint=entrypoint,
            lineage=lineage,
            generator=generator,
            metadata=metadata,
        )

    def store_file(
        self,
        source: str | Path,
        *,
        path: str | None = None,
        candidate_id: str | None = None,
        entrypoint: str | None = None,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_path = Path(source).resolve()
        return self.store_files(
            [CandidateFileMapping(source=source_path, path=path or source_path.name)],
            candidate_id=candidate_id,
            entrypoint=entrypoint,
            lineage=lineage,
            generator=generator,
            metadata=metadata,
        )

    def store_files(
        self,
        files: Sequence[CandidateFileMapping | JsonDict | tuple[Any, Any]],
        *,
        candidate_id: str | None = None,
        entrypoint: str | None = None,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        mappings = [_coerce_mapping(item) for item in files]
        if not mappings:
            raise ValueError("At least one candidate file is required.")

        candidate_id = candidate_id or f"candidate-files-{uuid.uuid4().hex[:12]}"
        candidate_root = self.root_dir / candidate_id
        files_root = candidate_root / "files"
        if candidate_root.exists():
            raise FileExistsError(f"Candidate already exists: {candidate_root}")
        files_root.mkdir(parents=True, exist_ok=False)

        entries: List[JsonDict] = []
        seen_paths = set()
        try:
            for mapping in mappings:
                if not _is_safe_relative_path(mapping.path):
                    raise ValueError(f"Unsafe candidate file path: {mapping.path!r}")
                if mapping.path in seen_paths:
                    raise ValueError(f"Duplicate candidate file path: {mapping.path!r}")
                seen_paths.add(mapping.path)
                source = mapping.source.resolve()
                if not source.exists() or not source.is_file() or source.is_symlink():
                    raise FileNotFoundError(f"Candidate source must be a regular file: {source}")
                destination = (files_root / mapping.path).resolve()
                if files_root.resolve() not in destination.parents:
                    raise ValueError(f"Candidate destination escapes file root: {mapping.path!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                entries.append(
                    {
                        "path": mapping.path,
                        "contentRef": self._content_ref(destination),
                        "sha256": _sha256_file(destination),
                        "sizeBytes": destination.stat().st_size,
                    }
                )
        except Exception:
            shutil.rmtree(candidate_root, ignore_errors=True)
            raise

        spec: JsonDict = {
            "bundleRef": self._content_ref(files_root),
            "files": entries,
        }
        if entrypoint:
            spec["entrypoint"] = entrypoint

        candidate = {
            "candidate_id": candidate_id,
            "format": "files",
            "spec": spec,
            "lineage": dict(lineage or {"parents": []}),
            "generator": dict(generator or {"strategy": "stored_file_candidate"}),
        }
        if metadata:
            candidate["metadata"] = dict(metadata)
        return candidate

    def _content_ref(self, path: Path) -> str:
        resolved = path.resolve()
        if self.content_ref_mode == "absolute":
            return str(resolved)
        assert self.content_ref_base is not None
        try:
            return resolved.relative_to(self.content_ref_base).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"Stored candidate path {resolved} is not under content_ref_base {self.content_ref_base}."
            ) from exc


def store_candidate_directory(
    source_dir: str | Path,
    candidate_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(candidate_store_dir, kwargs)
    return store.store_directory(source_dir, **kwargs)


def store_candidate_files(
    files: Sequence[CandidateFileMapping | JsonDict | tuple[Any, Any]],
    candidate_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(candidate_store_dir, kwargs)
    return store.store_files(files, **kwargs)


def store_candidate_file(
    source: str | Path,
    candidate_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(candidate_store_dir, kwargs)
    return store.store_file(source, **kwargs)


def _store_from_kwargs(candidate_store_dir: str | Path, kwargs: JsonDict) -> CandidateFileStore:
    content_ref_mode = kwargs.pop("content_ref_mode", "absolute")
    content_ref_base = kwargs.pop("content_ref_base", None)
    return CandidateFileStore(
        candidate_store_dir,
        content_ref_mode=content_ref_mode,
        content_ref_base=content_ref_base,
    )


def _coerce_mapping(item: CandidateFileMapping | JsonDict | tuple[Any, Any]) -> CandidateFileMapping:
    if isinstance(item, CandidateFileMapping):
        return item
    if isinstance(item, dict):
        source = item.get("source")
        path = item.get("path")
    else:
        source, path = item
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Candidate file mapping path must be a non-empty string.")
    return CandidateFileMapping(source=Path(source), path=path)


def _is_excluded(path: Path, source_root: Path) -> bool:
    rel_parts = path.relative_to(source_root).parts
    if any(part in DEFAULT_EXCLUDE_NAMES for part in rel_parts):
        return True
    return path.suffix in DEFAULT_EXCLUDE_SUFFIXES


def _is_safe_relative_path(value: str) -> bool:
    if "\\" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute():
        return False
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
