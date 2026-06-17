"""Helpers for storing generated code artifacts by reference."""

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
class CodeFileMapping:
    source: Path
    path: str


class CodeArtifactStore:
    """Copy generated code into a stable artifact folder and build a manifest.

    Methods remain user-owned, but many methods need the same mundane workflow:
    generate one or more files, store them durably, and return a code artifact
    manifest with ``contentRef`` and ``sha256`` fields. This helper owns that
    storage shape without owning the search algorithm.
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
        artifact_id: str | None = None,
        entrypoint: str | None = None,
        artifact_kind: str = "code_bundle",
        lineage: Optional[JsonDict] = None,
        generator_record: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_root = Path(source_dir).resolve()
        if not source_root.is_dir():
            raise NotADirectoryError(f"Code artifact source is not a directory: {source_dir}")
        mappings = []
        for source in sorted(source_root.rglob("*")):
            if not source.is_file():
                continue
            if _is_excluded(source, source_root):
                continue
            mappings.append(CodeFileMapping(source=source, path=source.relative_to(source_root).as_posix()))
        if not mappings:
            raise ValueError(f"Code artifact directory contains no storable files: {source_dir}")
        return self.store_files(
            mappings,
            artifact_id=artifact_id,
            entrypoint=entrypoint,
            artifact_kind=artifact_kind,
            lineage=lineage,
            generator_record=generator_record,
            metadata=metadata,
        )

    def store_file(
        self,
        source: str | Path,
        *,
        path: str | None = None,
        artifact_id: str | None = None,
        entrypoint: str | None = None,
        artifact_kind: str = "code_file",
        lineage: Optional[JsonDict] = None,
        generator_record: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        source_path = Path(source).resolve()
        return self.store_files(
            [CodeFileMapping(source=source_path, path=path or source_path.name)],
            artifact_id=artifact_id,
            entrypoint=entrypoint,
            artifact_kind=artifact_kind,
            lineage=lineage,
            generator_record=generator_record,
            metadata=metadata,
        )

    def store_files(
        self,
        files: Sequence[CodeFileMapping | JsonDict | tuple[Any, Any]],
        *,
        artifact_id: str | None = None,
        entrypoint: str | None = None,
        artifact_kind: str = "code_bundle",
        lineage: Optional[JsonDict] = None,
        generator_record: Optional[JsonDict] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        if artifact_kind not in {"code_file", "code_bundle", "code_module", "files"}:
            raise ValueError("artifact_kind must be code_file, code_bundle, code_module, or files.")
        mappings = [_coerce_mapping(item) for item in files]
        if not mappings:
            raise ValueError("At least one code file is required.")
        if artifact_kind == "code_file" and len(mappings) != 1:
            raise ValueError("code_file artifacts must contain exactly one file.")

        artifact_id = artifact_id or f"artifact-code-{uuid.uuid4().hex[:12]}"
        artifact_root = self.root_dir / artifact_id
        files_root = artifact_root / "files"
        if artifact_root.exists():
            raise FileExistsError(f"Code artifact already exists: {artifact_root}")
        files_root.mkdir(parents=True, exist_ok=False)

        entries = []
        seen_paths = set()
        try:
            for mapping in mappings:
                if not _is_safe_relative_path(mapping.path):
                    raise ValueError(f"Unsafe code artifact path: {mapping.path!r}")
                if mapping.path in seen_paths:
                    raise ValueError(f"Duplicate code artifact path: {mapping.path!r}")
                seen_paths.add(mapping.path)
                source = mapping.source.resolve()
                if not source.exists() or not source.is_file() or source.is_symlink():
                    raise FileNotFoundError(f"Code artifact source must be a regular file: {source}")
                destination = (files_root / mapping.path).resolve()
                if files_root.resolve() not in destination.parents:
                    raise ValueError(f"Code artifact destination escapes file root: {mapping.path!r}")
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
            shutil.rmtree(artifact_root, ignore_errors=True)
            raise

        if artifact_kind == "code_file":
            spec = dict(entries[0])
            if entrypoint:
                spec["entrypoint"] = entrypoint
        else:
            spec = {
                "bundleRef": self._content_ref(files_root),
                "files": entries,
            }
            if entrypoint:
                spec["entrypoint"] = entrypoint

        artifact = {
            "candidate_id": artifact_id,
            "artifact_id": artifact_id,
            "artifact_kind": artifact_kind,
            "spec": spec,
            "lineage": dict(lineage or {"parents": []}),
            "generator": dict(generator_record or {"strategy": "stored_code_artifact"}),
            "generator_record": dict(generator_record or {"strategy": "stored_code_artifact"}),
        }
        if metadata:
            artifact["metadata"] = dict(metadata)
        return artifact

    def _content_ref(self, path: Path) -> str:
        resolved = path.resolve()
        if self.content_ref_mode == "absolute":
            return str(resolved)
        assert self.content_ref_base is not None
        try:
            return resolved.relative_to(self.content_ref_base).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"Stored code path {resolved} is not under content_ref_base {self.content_ref_base}."
            ) from exc


def store_code_directory(
    source_dir: str | Path,
    artifact_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(artifact_store_dir, kwargs)
    return store.store_directory(source_dir, **kwargs)


def store_code_files(
    files: Sequence[CodeFileMapping | JsonDict | tuple[Any, Any]],
    artifact_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(artifact_store_dir, kwargs)
    return store.store_files(files, **kwargs)


def store_code_file(
    source: str | Path,
    artifact_store_dir: str | Path,
    **kwargs: Any,
) -> JsonDict:
    store = _store_from_kwargs(artifact_store_dir, kwargs)
    return store.store_file(source, **kwargs)


def _store_from_kwargs(artifact_store_dir: str | Path, kwargs: JsonDict) -> CodeArtifactStore:
    content_ref_mode = kwargs.pop("content_ref_mode", "absolute")
    content_ref_base = kwargs.pop("content_ref_base", None)
    return CodeArtifactStore(
        artifact_store_dir,
        content_ref_mode=content_ref_mode,
        content_ref_base=content_ref_base,
    )


def _coerce_mapping(item: CodeFileMapping | JsonDict | tuple[Any, Any]) -> CodeFileMapping:
    if isinstance(item, CodeFileMapping):
        return item
    if isinstance(item, dict):
        source = item.get("source")
        path = item.get("path")
    else:
        source, path = item
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Code file mapping path must be a non-empty string.")
    return CodeFileMapping(source=Path(source), path=path)


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
