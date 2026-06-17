"""Example user-owned method that returns stored code artifact manifests."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from optpilot.code_artifacts import CodeArtifactStore
from optpilot.provenance import PromptStore, build_generator_record, build_model_record


class CodeArtifactMethod:
    """Stores a configured source directory as a code artifact candidate.

    This is deliberately not an optimizer. It demonstrates how a user-owned
    code generation or search method can use OptPilot's artifact helper while
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
        artifact_store_dir = runtime_context.get("artifact_store_dir")
        if not artifact_store_dir:
            raise ValueError("CodeArtifactMethod requires runtime_context.artifact_store_dir.")

        artifact_store = CodeArtifactStore(
            artifact_store_dir,
            content_ref_mode=runtime_context.get("artifact_content_ref_mode", "absolute"),
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
        artifacts = []
        for _ in range(n_candidates):
            artifact_id = f"code-artifact-{uuid.uuid4().hex[:12]}"
            artifact = artifact_store.store_directory(
                source_dir,
                artifact_id=artifact_id,
                entrypoint=config.get("entrypoint"),
                artifact_kind=config.get("artifactKind", "files"),
                lineage={"parents": list(config.get("parents", []))},
                generator_record=build_generator_record(
                    method_id=self.definition["id"],
                    strategy="stored_directory_example",
                    prompt_record=prompt_record,
                    model_record=model_record,
                    extra={"owned_by": "user", "cursor": self._cursor},
                ),
            )
            artifacts.append(artifact)
            self._cursor += 1
        return artifacts

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
        raise ValueError("CodeArtifactMethod requires a trialWorkspace entry matching candidate.materialize.root.")
