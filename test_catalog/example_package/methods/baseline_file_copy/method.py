"""Baseline method for file-candidate environments.

This method emits the unmodified source files declared by the environment's
candidate contract. It is useful as a sanity check before trying stronger
methods such as LLM file editors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from optpilot.candidate_staging import CandidateBundleStager, CandidateFileMapping


class BaselineFileCopyMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.candidate_context = dict(study_spec.candidate.get("context", {}))
        self.target_files = _editable_paths_from_context(self.candidate_context)
        if not self.target_files:
            raise ValueError("BaselineFileCopyMethod requires files.editable or files.required candidate context.")
        self.source_files = _resolve_source_files(self.target_files, self.candidate_context)
        self._emitted = False

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._emitted or n_candidates <= 0:
            return []
        runtime_context = dict(study_state.get("runtime_context", {}))
        candidate_staging_dir = runtime_context.get("candidate_staging_dir")
        if not candidate_staging_dir:
            raise ValueError("BaselineFileCopyMethod requires runtime_context.candidate_staging_dir.")
        candidate_stager = CandidateBundleStager(candidate_staging_dir)
        self._emitted = True
        return [
            candidate_stager.stage_files(
                [CandidateFileMapping(source=self.source_files[path], path=path) for path in self.target_files],
                candidate_id=f"{self.definition['id']}-baseline",
                lineage={"parents": [], "source": "baseline_source_tree"},
                generator={
                    "method_id": self.definition["id"],
                    "strategy": "baseline_file_copy",
                    "owned_by": "example",
                    "summary": "Unmodified source files declared by the environment.",
                },
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


def _resolve_source_files(
    target_files: List[str],
    candidate_context: Dict[str, Any],
) -> Dict[str, Path]:
    method_context = candidate_context.get("methodContext", {})
    references = (
        method_context.get("references", [])
        if isinstance(method_context, dict)
        else []
    )
    templates = {
        str(reference.get("name")): Path(str(reference.get("path")))
        for reference in references
        if isinstance(reference, dict)
        and reference.get("type") == "candidate_template"
        and reference.get("name")
        and reference.get("path")
    }
    source_files: Dict[str, Path] = {}
    for relative_path in target_files:
        source = templates.get(relative_path)
        if source is None or not source.is_file():
            raise FileNotFoundError(
                "BaselineFileCopyMethod requires one candidate_template "
                f"methodContext reference named {relative_path!r}."
            )
        source_files[relative_path] = source
    return source_files
