"""Example user-owned method that returns stored file candidate manifests."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from optpilot.candidate_files import CandidateFileStore
from optpilot.provenance import PromptStore, build_generator_record, build_model_record


class FileCandidateMethod:
    """Stores a configured source directory as a file candidate.

    This is deliberately not an optimizer. It demonstrates how a user-owned
    code generation or search method can use OptPilot's candidate helper while
    still returning only a manifest to the core runner.
    """

    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self._cursor = 0
        self.observed: List[Dict[str, Any]] = []

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        config = self.definition.get("config", {})
        runtime_context = study_state.get("runtime_context", {})
        candidate_context = study_state.get("candidate_context") or runtime_context.get("candidate_context", {})
        source_dir = self._resolve_source_dir(candidate_context)
        candidate_store_dir = runtime_context.get("candidate_store_dir")
        if not candidate_store_dir:
            raise ValueError("FileCandidateMethod requires runtime_context.candidate_store_dir.")

        candidate_store = CandidateFileStore(
            candidate_store_dir,
            content_ref_mode=runtime_context.get("candidate_content_ref_mode", "absolute"),
        )
        prompt_record = None
        if runtime_context.get("prompt_store_dir") and config.get("promptMessages"):
            prompt_store = PromptStore(
                runtime_context["prompt_store_dir"],
                content_ref_mode=runtime_context.get("prompt_content_ref_mode", "absolute"),
            )
            prompt_record = prompt_store.store_prompt(
                messages=list(config["promptMessages"]),
                metadata={"method_id": self.definition["id"]},
            )
        model_record = None
        if config.get("model"):
            model_record = build_model_record(
                provider=config.get("provider", "unspecified"),
                model=config["model"],
                parameters=dict(config.get("modelParameters", {})),
            )
        candidates = []
        for _ in range(n_candidates):
            candidate_id = f"file-candidate-{uuid.uuid4().hex[:12]}"
            candidate = candidate_store.store_directory(
                source_dir,
                candidate_id=candidate_id,
                entrypoint=config.get("entrypoint"),
                lineage={"parents": list(config.get("parents", []))},
                generator=build_generator_record(
                    method_id=self.definition["id"],
                    strategy="stored_directory_example",
                    prompt_record=prompt_record,
                    model_record=model_record,
                    extra={"owned_by": "user", "cursor": self._cursor},
                ),
            )
            candidates.append(candidate)
            self._cursor += 1
        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        self.observed.extend(observations)

    def _resolve_source_dir(self, candidate_context: Dict[str, Any]) -> Path:
        files = candidate_context.get("files", {})
        root = str(files.get("root", "."))
        for entry in candidate_context.get("workspace", {}).get("copy", []) or []:
            if str(entry.get("to", ".")) == root:
                source = Path(str(entry.get("from", ""))).resolve()
                if source.is_dir():
                    return source
        raise ValueError("FileCandidateMethod requires a trialWorkspace entry matching candidate.materialize.root.")
