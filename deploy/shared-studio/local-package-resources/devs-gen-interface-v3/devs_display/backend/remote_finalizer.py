from __future__ import annotations

import base64
import io
import json
import os
import stat
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 96 * 1024 * 1024


class RemoteFinalizerError(RuntimeError):
    pass


def _canonical_relative_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(str(value or "").strip())
    if (
        not str(path)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RemoteFinalizerError(f"{label} must be a canonical relative path")
    return path


def build_remote_finalizer_archive(
    *,
    bundle_root: Path,
    project_rel: str,
) -> bytes:
    resolved = bundle_root.resolve(strict=True)
    if bundle_root.is_symlink() or not resolved.is_dir():
        raise RemoteFinalizerError("Finalizer bundle must be a real directory")
    project_path = _canonical_relative_path(project_rel, "project_rel")
    excluded_directories = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "llm_calls",
        "node_modules",
    }
    buffer = io.BytesIO()
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for current_root, dirs, files in os.walk(resolved, followlinks=False):
            current = Path(current_root)
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in excluded_directories
                and not (current / name).is_symlink()
            )
            for filename in sorted(files):
                source = current / filename
                metadata = source.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    continue
                size = int(metadata.st_size)
                file_count += 1
                total_bytes += size
                if file_count > MAX_ARCHIVE_FILES:
                    raise RemoteFinalizerError("Finalizer bundle contains too many files")
                if size > MAX_ARCHIVE_FILE_BYTES:
                    raise RemoteFinalizerError(
                        f"Finalizer bundle file is too large: {filename}"
                    )
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    raise RemoteFinalizerError("Finalizer bundle is too large")
                relative = source.relative_to(resolved).as_posix()
                archive.write(source, (project_path / relative).as_posix())
    content = buffer.getvalue()
    if len(content) > MAX_ARCHIVE_TOTAL_BYTES:
        raise RemoteFinalizerError("Compressed finalizer bundle is too large")
    return content


def _bounded_error_body(error: urllib.error.HTTPError) -> str:
    try:
        payload = error.read(16_001)
    except OSError:
        return ""
    return payload[:16_000].decode("utf-8", errors="replace")


class RemoteCodexFinalizerClient:
    def __init__(self, *, endpoint: str, token: str, timeout_seconds: int) -> None:
        self.endpoint = endpoint.strip()
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        if not self.endpoint.startswith(("http://", "https://")):
            raise RemoteFinalizerError("Remote finalizer URL is invalid")
        if not self.token:
            raise RemoteFinalizerError("Remote finalizer token is missing")

    def run(
        self,
        *,
        review_id: str,
        project_rel: str,
        prompt: str,
        bundle_root: Path,
    ) -> dict[str, Any]:
        archive = build_remote_finalizer_archive(
            bundle_root=bundle_root,
            project_rel=project_rel,
        )
        body = json.dumps(
            {
                "review_id": review_id,
                "project_rel": project_rel,
                "prompt": prompt,
                "archive_base64": base64.b64encode(archive).decode("ascii"),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds + 30
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = _bounded_error_body(exc)
            raise RemoteFinalizerError(
                f"Remote automatic check returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RemoteFinalizerError(
                f"Remote automatic check is unavailable: {exc.reason}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RemoteFinalizerError("Remote automatic-check response is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteFinalizerError(
                "Remote automatic check returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("review_id") != review_id:
            raise RemoteFinalizerError("Remote automatic-check response is mismatched")
        self._apply_changes(bundle_root=bundle_root, payload=payload)
        return payload

    @staticmethod
    def _apply_changes(*, bundle_root: Path, payload: dict[str, Any]) -> None:
        root = bundle_root.resolve(strict=True)
        changed = payload.get("changed_files")
        deleted = payload.get("deleted_paths")
        if not isinstance(changed, list) or not isinstance(deleted, list):
            raise RemoteFinalizerError("Remote automatic-check changes are malformed")
        if len(changed) + len(deleted) > MAX_ARCHIVE_FILES:
            raise RemoteFinalizerError("Remote automatic check changed too many files")
        decoded: list[tuple[PurePosixPath, bytes]] = []
        total_bytes = 0
        for item in changed:
            if not isinstance(item, dict):
                raise RemoteFinalizerError("Remote automatic-check file is malformed")
            relative = _canonical_relative_path(str(item.get("path") or ""), "changed path")
            try:
                content = base64.b64decode(
                    str(item.get("content_base64") or ""), validate=True
                )
            except Exception as exc:
                raise RemoteFinalizerError(
                    "Remote automatic-check file content is invalid"
                ) from exc
            total_bytes += len(content)
            if len(content) > MAX_ARCHIVE_FILE_BYTES or total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                raise RemoteFinalizerError("Remote automatic-check changes are too large")
            decoded.append((relative, content))
        deletion_paths = [
            _canonical_relative_path(str(value), "deleted path") for value in deleted
        ]

        def checked_target(relative: PurePosixPath) -> Path:
            current = root
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise RemoteFinalizerError(
                        "Remote automatic-check path crosses a symlink"
                    )
            target = root.joinpath(*relative.parts)
            if target.is_symlink():
                raise RemoteFinalizerError(
                    "Remote automatic check cannot replace a symlink"
                )
            return target

        write_targets = [(checked_target(relative), content) for relative, content in decoded]
        delete_targets = [checked_target(relative) for relative in deletion_paths]
        for target, content in write_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.codex-{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        written = {target for target, _content in write_targets}
        for target in delete_targets:
            if target in written:
                continue
            if target.exists() and target.is_file():
                target.unlink()
