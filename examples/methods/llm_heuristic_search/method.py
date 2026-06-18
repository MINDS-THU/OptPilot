"""Run an upstream heuristic-search command and return its output as a file candidate.

This example wrapper is intentionally coarse-grained. It is for repositories
that already own their internal search loop and only need a thin OptPilot
boundary around one top-level run command plus one produced file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from optpilot.candidate_files import CandidateFileStore


JsonDict = Dict[str, Any]


class LLMHeuristicSearchMethod:
    def __init__(self, definition: JsonDict, study_spec, rng=None):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng

    def propose(self, n_candidates: int, study_state: JsonDict, evidence_view=None) -> List[JsonDict]:
        if n_candidates < 1:
            return []

        settings = dict(self.definition.get("settings", {}))
        if not settings.get("command"):
            raise ValueError("llm_heuristic_search requires settings.command.")

        runtime_context = dict(study_state.get("runtime_context", {}))
        candidate_store_dir = runtime_context.get("candidate_store_dir") or runtime_context.get("candidate_store")
        if not candidate_store_dir:
            raise ValueError("OptPilot runtime_context did not provide a candidate store directory.")

        method_workspace = Path(candidate_store_dir).parent / "llm_heuristic_search_workspace" / self.definition["id"]
        method_workspace.mkdir(parents=True, exist_ok=True)

        command = [str(item) for item in settings["command"]]
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in settings.get("env", {}).items()})
        if self.rng is not None and "seed" not in settings:
            env.setdefault("OPTPILOT_SEED", str(self.rng.randint(0, 2**31 - 1)))

        request_payload = self._build_request_payload(settings, study_state, runtime_context)
        request_path = method_workspace / "request.json"
        request_path.write_text(json.dumps(request_payload, indent=2, sort_keys=True), encoding="utf-8")

        stdout_path = method_workspace / "stdout.log"
        stderr_path = method_workspace / "stderr.log"
        generated_file = self._run_external_command(settings, command, env, method_workspace, request_path, stdout_path, stderr_path)

        store = CandidateFileStore(candidate_store_dir)
        candidate_path = str(settings.get("candidatePath") or self._default_candidate_path(study_state))
        candidate = store.store_file(
            generated_file,
            path=candidate_path,
            candidate_id=f"{self.definition['id']}-candidate",
            generator={
                "method_id": self.definition["id"],
                "strategy": "llm_heuristic_search_command",
                "upstream_repository": settings.get("upstreamRepository"),
            },
            metadata={
                "method_workspace": str(method_workspace),
                "request_path": str(request_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "upstream_repository": settings.get("upstreamRepository"),
                "generated_file": str(generated_file),
            },
        )
        return [candidate]

    def observe(self, observations: List[JsonDict]) -> None:
        return None

    def _build_request_payload(self, settings: JsonDict, study_state: JsonDict, runtime_context: JsonDict) -> JsonDict:
        return {
            "method_id": self.definition["id"],
            "settings": settings,
            "study_state": study_state,
            "runtime_context": runtime_context,
            "candidate_context": runtime_context.get("candidate_context", {}),
            "study_path": str(self.study_spec.path),
            "study_name": self.study_spec.name,
        }

    def _run_external_command(
        self,
        settings: JsonDict,
        command: List[str],
        env: JsonDict,
        method_workspace: Path,
        request_path: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> Path:
        repo_root = self._resolve_path(settings.get("repoRoot", "."))
        workdir = self._resolve_path(settings.get("workdir", settings.get("repoRoot", ".")))
        placeholders = {
            "{python}": sys.executable,
            "{method_workspace}": str(method_workspace),
            "{request_file}": str(request_path),
            "{repo_root}": str(repo_root),
        }
        formatted_command = [self._replace_placeholders(item, placeholders) for item in command]

        completed = subprocess.run(
            formatted_command,
            cwd=str(workdir),
            env=env,
            text=True,
            capture_output=True,
            timeout=int(settings.get("timeoutSeconds", 0) or 0) or None,
            check=False,
        )
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(
                f"LLM heuristic search command failed with exit code {completed.returncode}. See {stderr_path}."
            )

        generated_file_value = settings.get("generatedFile")
        if generated_file_value is None:
            raise ValueError("llm_heuristic_search requires settings.generatedFile.")
        return self._resolve_generated_file(generated_file_value, method_workspace, workdir, repo_root)

    def _default_candidate_path(self, study_state: JsonDict) -> str:
        candidate_context = dict(study_state.get("candidate_context", {}))
        candidate = dict(candidate_context.get("candidate", {}))
        files = dict(candidate.get("files", {}))
        editable = files.get("editable") or []
        if len(editable) == 1 and isinstance(editable[0], dict) and editable[0].get("path"):
            return str(editable[0]["path"])
        if len(editable) == 1 and isinstance(editable[0], str):
            return editable[0]
        entrypoint = candidate.get("spec", {}).get("entrypoint")
        if isinstance(entrypoint, str) and entrypoint:
            return entrypoint
        raise ValueError(
            "candidatePath is required when the environment does not expose exactly one editable file."
        )

    def _resolve_path(self, value: Any) -> Path:
        if value is None:
            raise ValueError("Required path setting is missing.")
        path = Path(str(value)).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (self._config_base_dir() / path).resolve()

    def _config_base_dir(self) -> Path:
        base_dir = self.definition.get("configBaseDir")
        if base_dir:
            return Path(str(base_dir)).expanduser().resolve()
        return self.study_spec.base_dir.resolve()

    def _resolve_generated_file(self, value: Any, method_workspace: Path, workdir: Path, repo_root: Path) -> Path:
        path = Path(str(value)).expanduser()
        if path.is_absolute():
            candidates = [path.resolve()]
        else:
            candidates = [
                (method_workspace / path).resolve(),
                (workdir / path).resolve(),
                (repo_root / path).resolve(),
            ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise FileNotFoundError(f"Configured generated file was not produced. Searched: {searched}")

    @staticmethod
    def _replace_placeholders(text: str, replacements: JsonDict) -> str:
        result = str(text)
        for old, new in replacements.items():
            result = result.replace(old, str(new))
        return result
