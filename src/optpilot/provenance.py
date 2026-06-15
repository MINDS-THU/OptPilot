"""Helpers for prompt and model provenance records."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import utc_now_iso


JsonDict = Dict[str, Any]


class PromptStore:
    """Store LLM prompt payloads by reference and hash."""

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

    def store_prompt(
        self,
        *,
        messages: List[JsonDict],
        prompt_record_id: str | None = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list.")
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise TypeError(f"messages[{index}] must be an object.")
            if not isinstance(message.get("role"), str) or not isinstance(message.get("content"), str):
                raise TypeError(f"messages[{index}] must define string role and content.")

        prompt_record_id = prompt_record_id or f"prompt-{uuid.uuid4().hex[:12]}"
        prompt_dir = self.root_dir / prompt_record_id
        if prompt_dir.exists():
            raise FileExistsError(f"Prompt record already exists: {prompt_dir}")
        prompt_dir.mkdir(parents=True, exist_ok=False)
        payload = {
            "prompt_record_id": prompt_record_id,
            "messages": [dict(message) for message in messages],
            "metadata": dict(metadata or {}),
            "created_at": utc_now_iso(),
        }
        prompt_path = prompt_dir / "prompt.json"
        _write_json(prompt_path, payload)
        return {
            "prompt_record_id": prompt_record_id,
            "contentRef": self._content_ref(prompt_path),
            "sha256": _sha256_file(prompt_path),
            "sizeBytes": prompt_path.stat().st_size,
            "message_count": len(messages),
            "created_at": payload["created_at"],
            "metadata": dict(metadata or {}),
        }

    def _content_ref(self, path: Path) -> str:
        resolved = path.resolve()
        if self.content_ref_mode == "absolute":
            return str(resolved)
        assert self.content_ref_base is not None
        try:
            return resolved.relative_to(self.content_ref_base).as_posix()
        except ValueError as exc:
            raise ValueError(f"Prompt path {resolved} is not under content_ref_base {self.content_ref_base}.") from exc


def build_model_record(
    *,
    provider: str,
    model: str,
    parameters: Optional[JsonDict] = None,
    invocation_id: str | None = None,
    metadata: Optional[JsonDict] = None,
) -> JsonDict:
    if not provider or not model:
        raise ValueError("provider and model are required.")
    return {
        "provider": provider,
        "model": model,
        "parameters": dict(parameters or {}),
        "invocation_id": invocation_id,
        "metadata": dict(metadata or {}),
        "recorded_at": utc_now_iso(),
    }


def build_generator_record(
    *,
    method_id: str,
    strategy: str,
    prompt_record: Optional[JsonDict] = None,
    model_record: Optional[JsonDict] = None,
    extra: Optional[JsonDict] = None,
) -> JsonDict:
    record = {
        "method_id": method_id,
        "strategy": strategy,
    }
    if prompt_record is not None:
        record["prompt_record_id"] = prompt_record.get("prompt_record_id")
        record["prompt_record"] = dict(prompt_record)
    if model_record is not None:
        record["model_record"] = dict(model_record)
    if extra:
        record.update(dict(extra))
    return record


def _write_json(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
