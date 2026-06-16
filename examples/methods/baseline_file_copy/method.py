"""Baseline method for file-candidate environments.

This method emits the unmodified source files declared by the environment's
candidate contract. It is useful as a sanity check before trying stronger
methods such as LLM file editors.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from optpilot.code_artifacts import CodeArtifactStore, CodeFileMapping


class BaselineFileCopyMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.candidate_context = dict(study_spec.primary_artifact.get("candidateContext", {}))
        self.target_files = _editable_paths_from_context(self.candidate_context)
        if not self.target_files:
            raise ValueError("BaselineFileCopyMethod requires files.editable or files.required candidate context.")
        self.source_dir = _resolve_source_dir(self.candidate_context)
        self.source_files = _resolve_source_files(self.target_files, self.candidate_context, self.source_dir)
        self._emitted = False

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._emitted or n_candidates <= 0:
            return []
        runtime_context = dict(study_state.get("runtime_context", {}))
        artifact_store_dir = runtime_context.get("artifact_store_dir")
        if not artifact_store_dir:
            raise ValueError("BaselineFileCopyMethod requires runtime_context.artifact_store_dir.")
        artifact_store = CodeArtifactStore(
            artifact_store_dir,
            content_ref_mode=runtime_context.get("artifact_content_ref_mode", "absolute"),
        )
        self._emitted = True
        return [
            artifact_store.store_files(
                [CodeFileMapping(source=self.source_files[path], path=path) for path in self.target_files],
                artifact_id=f"baseline-{uuid.uuid4().hex[:12]}",
                artifact_kind=str(self.study_spec.primary_artifact["kind"]),
                lineage={"parents": [], "source": "baseline_source_tree"},
                generator_record={
                    "method_id": self.definition["id"],
                    "strategy": "baseline_file_copy",
                    "owned_by": "example",
                },
                metadata={"summary": "Unmodified source files declared by the environment."},
            )
        ]

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        return None


def _editable_paths_from_context(candidate_context: Dict[str, Any]) -> List[str]:
    files = candidate_context.get("files", {})
    editable = files.get("editable", []) or []
    paths = [str(item["path"]) for item in editable if isinstance(item, dict) and item.get("path")]
    if paths:
        return paths
    return [str(path) for path in files.get("required", []) or []]


def _resolve_source_dir(candidate_context: Dict[str, Any]) -> Path | None:
    files = candidate_context.get("files", {})
    root = str(files.get("root", "."))
    for entry in candidate_context.get("workspace", {}).get("copy", []) or []:
        if str(entry.get("to", ".")) == root:
            source_dir = Path(str(entry["from"])).resolve()
            if source_dir.is_dir():
                return source_dir
    return None


def _resolve_source_files(
    target_files: List[str],
    candidate_context: Dict[str, Any],
    source_dir: Path | None,
) -> Dict[str, Path]:
    copy_entries = candidate_context.get("workspace", {}).get("copy", []) or []
    source_files: Dict[str, Path] = {}
    for relative_path in target_files:
        source = source_dir / relative_path if source_dir is not None else _source_for_workspace_file(relative_path, copy_entries)
        if source is None or not source.exists():
            raise FileNotFoundError(f"Could not resolve source for editable file {relative_path!r}.")
        source_files[relative_path] = source
    return source_files


def _source_for_workspace_file(relative_path: str, copy_entries: List[Dict[str, Any]]) -> Path | None:
    for entry in copy_entries:
        source = Path(str(entry.get("from", ""))).resolve()
        destination = str(entry.get("to", ""))
        if destination == relative_path and source.is_file():
            return source
        if source.is_dir():
            try:
                rel = Path(relative_path).relative_to(destination)
            except ValueError:
                continue
            return source / rel
    return None
