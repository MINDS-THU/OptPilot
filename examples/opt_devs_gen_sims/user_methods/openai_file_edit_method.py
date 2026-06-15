"""User-owned OpenAI-backed file edit method for the SA simulator example."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from optpilot.code_artifacts import CodeArtifactStore, CodeFileMapping
from optpilot.provenance import PromptStore, build_generator_record, build_model_record


class OpenAIFileEditMethod:
    def __init__(self, definition: Dict[str, Any], study_spec, rng):
        self.definition = definition
        self.study_spec = study_spec
        self.rng = rng
        self.config = dict(definition.get("config", {}))
        self.candidate_context = dict(self.study_spec.primary_artifact.get("candidateContext", {}))
        self.target_files = _editable_paths_from_context(self.candidate_context)
        if not self.target_files:
            raise ValueError("OpenAIFileEditMethod requires candidate_context.files.editable.")
        self.source_dir = self._resolve_source_dir()
        self.source_files = self._resolve_source_files()
        self.primary_metric = str(self.study_spec.objective["primaryMetric"]["name"])
        self.primary_direction = str(self.study_spec.objective["primaryMetric"]["direction"])
        self.include_baseline = bool(self.config.get("includeBaselineCandidate", True))
        self._baseline_emitted = False
        self.observations: List[Dict[str, Any]] = []

    def propose(self, n_candidates: int, study_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        runtime_context = dict(study_state.get("runtime_context", {}))
        artifact_store_dir = runtime_context.get("artifact_store_dir")
        if not artifact_store_dir:
            raise ValueError("OpenAIFileEditMethod requires runtime_context.artifact_store_dir.")

        artifact_store = CodeArtifactStore(
            artifact_store_dir,
            content_ref_mode=runtime_context.get("artifact_content_ref_mode", "absolute"),
        )
        prompt_store = None
        if runtime_context.get("prompt_store_dir"):
            prompt_store = PromptStore(
                runtime_context["prompt_store_dir"],
                content_ref_mode=runtime_context.get("prompt_content_ref_mode", "absolute"),
            )

        candidates: List[Dict[str, Any]] = []
        remaining = int(n_candidates)

        if remaining > 0 and self.include_baseline and not self._baseline_emitted:
            candidates.append(self._build_baseline_candidate(artifact_store))
            self._baseline_emitted = True
            remaining -= 1

        for _ in range(remaining):
            prompt_messages = self._build_prompt_messages(study_state)
            prompt_record = (
                prompt_store.store_prompt(
                    messages=prompt_messages,
                    metadata={"method_id": self.definition["id"], "target_files": list(self.target_files)},
                )
                if prompt_store is not None
                else None
            )
            candidate_summary, candidate_files = self._request_edit(prompt_messages)
            candidates.append(self._store_candidate(artifact_store, candidate_summary, candidate_files, prompt_record))

        return candidates

    def observe(self, observations: List[Dict[str, Any]]) -> None:
        self.observations.extend(observations)

    def _build_baseline_candidate(self, artifact_store: CodeArtifactStore) -> Dict[str, Any]:
        mappings = [
            CodeFileMapping(source=self._source_file_for(relative_path), path=relative_path)
            for relative_path in self.target_files
        ]
        return artifact_store.store_files(
            mappings,
            artifact_id=f"sa-baseline-{uuid.uuid4().hex[:12]}",
            artifact_kind="code_bundle",
            lineage={"parents": [], "source": "baseline_source_tree"},
            generator_record={
                "method_id": self.definition["id"],
                "strategy": "baseline_source_snapshot",
                "owned_by": "user",
            },
            metadata={"summary": "Baseline candidate copied from the upstream SA simulator."},
        )

    def _build_prompt_messages(self, study_state: Dict[str, Any]) -> List[Dict[str, str]]:
        prompt_parts = [
            "You are proposing the next candidate for an OptPilot study.",
            f"Primary metric: {self.primary_metric} ({self.primary_direction}).",
            f"Completed trials so far: {int(study_state.get('completed_trials', 0))}.",
            "Allowed target files:",
            *[f"- {path}" for path in self.target_files],
            "Current source files:",
            self._render_source_snapshot(),
            "Recent observations:",
            self._render_recent_observations(),
            "Return JSON only.",
        ]

        messages = [{"role": "system", "content": self._load_system_prompt()}]
        messages.append({"role": "user", "content": "\n\n".join(prompt_parts)})
        return messages

    def _load_system_prompt(self) -> str:
        exposure = self.candidate_context.get("exposure", {})
        instructions = exposure.get("instructions", []) or []
        if instructions:
            return Path(str(instructions[0])).read_text(encoding="utf-8")
        return str(self.config.get("systemPrompt", "Return JSON with full updated file contents."))

    def _render_source_snapshot(self) -> str:
        blocks = []
        for relative_path in self.target_files:
            source_text = self._source_file_for(relative_path).read_text(encoding="utf-8")
            blocks.append(f"FILE: {relative_path}\n```python\n{source_text}\n```")
        return "\n\n".join(blocks)

    def _render_recent_observations(self) -> str:
        if not self.observations:
            return "No prior observations yet."
        lines = []
        for observation in self.observations[-3:]:
            metric_values = dict(observation.get("metric_values", {}))
            lines.append(
                json.dumps(
                    {
                        "trial_id": observation.get("trial_id"),
                        "artifact_id": observation.get("artifact_id"),
                        "status": observation.get("status"),
                        "metric_values": metric_values,
                        "event_summary": observation.get("event_summary", {}),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        best = self._best_observation()
        if best is not None:
            lines.append(
                "Best observation so far:\n"
                + json.dumps(
                    {
                        "trial_id": best.get("trial_id"),
                        "artifact_id": best.get("artifact_id"),
                        "status": best.get("status"),
                        "metric_values": best.get("metric_values", {}),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return "\n\n".join(lines)

    def _best_observation(self) -> Dict[str, Any] | None:
        best = None
        for observation in self.observations:
            if observation.get("status") != "success":
                continue
            metric_values = observation.get("metric_values", {})
            if self.primary_metric not in metric_values:
                continue
            if best is None or _is_better(
                float(metric_values[self.primary_metric]),
                float(best["metric_values"][self.primary_metric]),
                self.primary_direction,
            ):
                best = observation
        return best

    def _request_edit(self, messages: List[Dict[str, str]]) -> Tuple[str, Dict[str, str]]:
        api_key_env_var = str(self.config.get("apiKeyEnvVar", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env_var) or self._read_api_key_from_dotenv(api_key_env_var)
        if not api_key:
            raise ValueError(
                f"OpenAIFileEditMethod requires {api_key_env_var} in the environment or a .env file."
            )

        payload = {
            "model": self.config.get("model", "gpt-4.1-mini"),
            "temperature": float(self.config.get("temperature", 0.2)),
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        max_tokens = self.config.get("maxTokens")
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)

        request = urllib.request.Request(
            self.config.get("apiBase", "https://api.openai.com/v1/chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(self.config.get("requestTimeoutSeconds", 120))) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI request failed with HTTP {exc.code}: {detail}") from exc

        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        files_payload = parsed.get("files", [])
        edited_files = {
            str(item["path"]): str(item["content"])
            for item in files_payload
            if isinstance(item, dict) and item.get("path") and item.get("content")
        }
        return str(parsed.get("summary", "LLM edited SA simulator files.")), edited_files

    def _read_api_key_from_dotenv(self, env_var_name: str) -> str | None:
        for dotenv_path in self._candidate_dotenv_paths():
            value = _read_dotenv_value(dotenv_path, env_var_name)
            if value:
                return value
        return None

    def _candidate_dotenv_paths(self) -> List[Path]:
        candidates: List[Path] = []
        seen: set[Path] = set()
        search_roots = [self.study_spec.base_dir, *self.study_spec.base_dir.parents, Path.cwd()]
        for root in search_roots:
            dotenv_path = root / ".env"
            resolved = dotenv_path.resolve()
            if resolved in seen or not dotenv_path.is_file():
                continue
            seen.add(resolved)
            candidates.append(dotenv_path)
        return candidates

    def _store_candidate(
        self,
        artifact_store: CodeArtifactStore,
        summary: str,
        edited_files: Dict[str, str],
        prompt_record: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            mappings: List[CodeFileMapping] = []
            for relative_path in self.target_files:
                source_path = self._source_file_for(relative_path)
                destination = tmp_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(edited_files.get(relative_path, source_path.read_text(encoding="utf-8")), encoding="utf-8")
                mappings.append(CodeFileMapping(source=destination, path=relative_path))

            best = self._best_observation()
            model_record = build_model_record(
                provider=str(self.config.get("provider", "openai")),
                model=str(self.config.get("model", "gpt-4.1-mini")),
                parameters={
                    "temperature": float(self.config.get("temperature", 0.2)),
                    "max_tokens": int(self.config.get("maxTokens", 0) or 0),
                },
            )
            return artifact_store.store_files(
                mappings,
                artifact_id=f"sa-llm-{uuid.uuid4().hex[:12]}",
                artifact_kind="code_bundle",
                lineage={"parents": [best["artifact_id"]] if best else []},
                generator_record=build_generator_record(
                    method_id=self.definition["id"],
                    strategy="openai_file_edit",
                    prompt_record=prompt_record,
                    model_record=model_record,
                    extra={
                        "owned_by": "user",
                        "summary": summary,
                        "edited_paths": sorted(edited_files),
                    },
                ),
                metadata={"summary": summary},
            )

    def _resolve_source_dir(self) -> Path | None:
        files = self.candidate_context.get("files", {})
        root = str(files.get("root", "."))
        for entry in self.candidate_context.get("workspace", {}).get("copy", []) or []:
            if str(entry.get("to", ".")) == root:
                source_dir = Path(str(entry["from"])).resolve()
                if source_dir.is_dir():
                    return source_dir
        return None

    def _resolve_source_files(self) -> Dict[str, Path]:
        source_files: Dict[str, Path] = {}
        workspace_copy = self.candidate_context.get("workspace", {}).get("copy", []) or []
        for relative_path in self.target_files:
            source = None
            if self.source_dir is not None:
                source = self.source_dir / relative_path
            else:
                source = _source_for_workspace_file(relative_path, workspace_copy)
            if source is None or not source.exists():
                raise FileNotFoundError(
                    f"OpenAIFileEditMethod could not resolve source for editable file {relative_path!r}."
                )
            source_files[relative_path] = source
        return source_files

    def _source_file_for(self, relative_path: str) -> Path:
        return self.source_files[relative_path]


def _is_better(candidate: float, incumbent: float, direction: str) -> bool:
    if direction == "minimize":
        return candidate < incumbent
    return candidate > incumbent


def _read_dotenv_value(path: Path, env_var_name: str) -> str | None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != env_var_name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            return value[1:-1]
        return value
    return None


def _editable_paths_from_context(candidate_context: Dict[str, Any]) -> List[str]:
    files = candidate_context.get("files", {})
    editable = files.get("editable", []) or []
    paths = [str(item["path"]) for item in editable if isinstance(item, dict) and item.get("path")]
    if paths:
        return paths
    return [str(path) for path in files.get("required", []) or []]


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
