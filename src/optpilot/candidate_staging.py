"""Helpers for staging generated file-candidate bundles before sealing."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence

from .retained_file_candidates import portable_candidate_id


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


class CandidateBundleStager:
    """Stage generated files and return a provisional file-candidate manifest.

    Methods can use this helper when they produce files outside the trial
    workspace. The helper copies those files into one method-call staging inbox
    and returns a draft manifest.  The runner later seals the bundle; this
    helper never writes to or names the immutable candidate store.
    """

    def __init__(
        self,
        root_dir: str | Path,
    ):
        self.root_dir = Path(root_dir).resolve()

    def stage_directory(
        self,
        source_dir: str | Path,
        *,
        candidate_id: str,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_input = Path(source_dir)
        if source_input.is_symlink():
            raise NotADirectoryError(f"Candidate source directory must not be a symlink: {source_dir}")
        source_root = source_input.resolve()
        if not source_root.is_dir():
            raise NotADirectoryError(f"Candidate source is not a directory: {source_dir}")
        entries = tuple(
            source
            for source in sorted(source_root.rglob("*"))
            if not _is_excluded(source, source_root)
        )
        for source in entries:
            if source.is_symlink():
                raise ValueError(
                    f"Candidate directory must not contain symlinks: {source}"
                )
            if not source.is_file() and not source.is_dir():
                raise ValueError(
                    f"Candidate directory contains a special file: {source}"
                )
        mappings = [
            CandidateFileMapping(
                source=source,
                path=source.relative_to(source_root).as_posix(),
            )
            for source in entries
            if source.is_file()
        ]
        if not mappings:
            raise ValueError(f"Candidate directory contains no staged files: {source_dir}")
        candidate = self.stage_files(
            mappings,
            candidate_id=candidate_id,
            lineage=lineage,
            generator=generator,
        )
        files_root = Path(candidate["spec"]["bundleRef"])
        try:
            for source in entries:
                if source.is_dir():
                    relative = source.relative_to(source_root)
                    (files_root / relative).mkdir(parents=True, exist_ok=True)
        except BaseException:
            shutil.rmtree(files_root.parent, ignore_errors=True)
            raise
        return candidate

    def stage_file(
        self,
        source: str | Path,
        *,
        path: str | None = None,
        candidate_id: str,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_path = Path(source)
        if source_path.is_symlink():
            raise FileNotFoundError(f"Candidate source must not be a symlink: {source}")
        source_path = source_path.resolve()
        return self.stage_files(
            [CandidateFileMapping(source=source_path, path=path or source_path.name)],
            candidate_id=candidate_id,
            lineage=lineage,
            generator=generator,
        )

    def stage_files(
        self,
        files: Sequence[CandidateFileMapping | JsonDict | tuple[Any, Any]],
        *,
        candidate_id: str,
        lineage: Optional[JsonDict] = None,
        generator: Optional[JsonDict] = None,
    ) -> JsonDict:
        mappings = [_coerce_mapping(item) for item in files]
        if not mappings:
            raise ValueError("At least one candidate file is required.")

        candidate_id = portable_candidate_id(candidate_id)
        # Candidate ids are user-controlled semantic identities, never storage
        # coordinates.  A generated key prevents traversal and keeps a later
        # content-addressed cutover from depending on public ids as paths.
        storage_key = f"bundle-{uuid.uuid4().hex}"
        candidate_root = self.root_dir / storage_key
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
                source_input = mapping.source
                if source_input.is_symlink():
                    raise FileNotFoundError(f"Candidate source must not be a symlink: {source_input}")
                source = source_input.resolve()
                if not source.exists() or not source.is_file():
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
        candidate = {
            "candidate_id": candidate_id,
            "format": "files",
            "spec": spec,
            "lineage": dict(lineage or {"parents": []}),
            "generator": dict(generator or {"strategy": "staged_file_candidate"}),
        }
        return candidate

    def _content_ref(self, path: Path) -> str:
        return str(path.resolve())


def stage_candidate_directory(
    source_dir: str | Path,
    candidate_staging_dir: str | Path,
    *,
    candidate_id: str,
    lineage: Optional[JsonDict] = None,
    generator: Optional[JsonDict] = None,
) -> JsonDict:
    return CandidateBundleStager(candidate_staging_dir).stage_directory(
        source_dir,
        candidate_id=candidate_id,
        lineage=lineage,
        generator=generator,
    )


def stage_candidate_files(
    files: Sequence[CandidateFileMapping | JsonDict | tuple[Any, Any]],
    candidate_staging_dir: str | Path,
    *,
    candidate_id: str,
    lineage: Optional[JsonDict] = None,
    generator: Optional[JsonDict] = None,
) -> JsonDict:
    return CandidateBundleStager(candidate_staging_dir).stage_files(
        files,
        candidate_id=candidate_id,
        lineage=lineage,
        generator=generator,
    )


def stage_candidate_file(
    source: str | Path,
    candidate_staging_dir: str | Path,
    *,
    candidate_id: str,
    path: str | None = None,
    lineage: Optional[JsonDict] = None,
    generator: Optional[JsonDict] = None,
) -> JsonDict:
    return CandidateBundleStager(candidate_staging_dir).stage_file(
        source,
        path=path,
        candidate_id=candidate_id,
        lineage=lineage,
        generator=generator,
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


__all__ = [
    "CandidateBundleStager",
    "CandidateFileMapping",
    "stage_candidate_directory",
    "stage_candidate_file",
    "stage_candidate_files",
]
