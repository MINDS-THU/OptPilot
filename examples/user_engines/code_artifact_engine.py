"""Example user-owned engine that returns stored code artifact manifests."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from optpilot.code_artifacts import CodeArtifactStore
from optpilot.provenance import PromptStore, build_generator_record, build_model_record


class CodeArtifactEngine:
    """Stores a configured source directory as a code artifact candidate.

    This is deliberately not an optimizer. It demonstrates how a user-owned
    code generation or search engine can use OptPilot's artifact helper while
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
        source_dir = self.study_spec.resolve_path(config["sourceDir"])
        runtime_context = study_state.get("runtime_context", {})
        artifact_store_dir = runtime_context.get("artifact_store_dir")
        if not artifact_store_dir:
            raise ValueError("CodeArtifactEngine requires runtime_context.artifact_store_dir.")

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
                metadata={"engine_id": self.definition["id"]},
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
                artifact_kind=config.get("artifactKind", "code_bundle"),
                lineage={"parents": list(config.get("parents", []))},
                generator_record=build_generator_record(
                    engine_id=self.definition["id"],
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
