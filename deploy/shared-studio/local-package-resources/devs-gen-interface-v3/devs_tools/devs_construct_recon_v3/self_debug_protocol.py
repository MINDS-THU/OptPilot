"""Small, opt-in primitives for bounded post-generation debugging.

This module deliberately does not run during normal generation.  It defines a
strict action boundary that a later repair loop can opt into without turning a
runtime probe into an acceptance gate.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .utils import extract_json


class DebugRunRequest(BaseModel):
    argv: list[str] = Field(default_factory=list, max_length=32)
    stdin: str = Field(default="", max_length=32 * 1024)
    timeout_seconds: int = Field(default=15, ge=1, le=30)


class SelfDebugAction(BaseModel):
    """One turn: read files, replace one complete file, or finish."""

    action: Literal["read_files", "replace_file", "finish"]
    paths: list[str] = Field(default_factory=list, max_length=8)
    path: str = ""
    content: str = Field(default="", max_length=512 * 1024)
    run: DebugRunRequest | None = None
    summary: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "SelfDebugAction":
        if self.action == "read_files":
            if not self.paths or self.path or self.content or self.run is not None:
                raise ValueError("read_files requires only a non-empty paths list")
        elif self.action == "replace_file":
            if not self.path or not self.content or self.paths:
                raise ValueError(
                    "replace_file requires one path and one complete file content"
                )
        elif self.paths or self.path or self.content or self.run is not None:
            raise ValueError("finish accepts only an optional summary")
        return self


def parse_self_debug_action(raw_output: str) -> SelfDebugAction:
    """Parse the exact schema; intentionally perform no key aliasing."""
    return SelfDebugAction.model_validate(extract_json(raw_output))


class SelfDebugWorkspace:
    """Execute validated actions inside one generated project directory."""

    def __init__(self, project_root: str | Path, max_read_chars: int = 160_000):
        self.project_root = Path(project_root).resolve()
        self.max_read_chars = max_read_chars

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.project_root / relative_path).resolve()
        if candidate != self.project_root and self.project_root not in candidate.parents:
            raise ValueError(f"Path escapes generated project: {relative_path}")
        return candidate

    def execute(
        self,
        action: SelfDebugAction,
        *,
        run_callback: Callable[[DebugRunRequest], str] | None = None,
    ) -> dict:
        if action.action == "finish":
            return {"action": "finish", "summary": action.summary}

        if action.action == "read_files":
            files: dict[str, str] = {}
            remaining = self.max_read_chars
            for relative in action.paths:
                path = self._resolve(relative)
                if not path.is_file():
                    raise FileNotFoundError(relative)
                text = path.read_text(encoding="utf-8")
                if len(text) > remaining:
                    text = text[:remaining] + "\n...[bounded read truncated]"
                files[relative] = text
                remaining -= min(len(text), remaining)
                if remaining == 0:
                    break
            return {"action": "read_files", "files": files}

        path = self._resolve(action.path)
        if path.suffix != ".py":
            raise ValueError("Only generated Python files may be replaced")
        if action.run is not None and run_callback is None:
            raise ValueError("A run was requested but no run callback was supplied")
        compile(action.content, action.path, "exec")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(action.content, encoding="utf-8")
        result = {"action": "replace_file", "path": action.path}
        if action.run is not None:
            assert run_callback is not None
            result["run"] = run_callback(action.run)
        return result
