"""Shared helpers for Docker/Podman-compatible container runtimes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_container_image(
    *,
    executable: str,
    image: str,
    build: Dict[str, Any],
    base_dir: Path,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a container image from a local context and return build metadata."""

    if not build:
        return {}
    command = container_build_command(
        executable=executable,
        image=image,
        build=build,
        base_dir=base_dir,
    )
    completed = subprocess.run(
        command,
        cwd=str(resolve_build_context(build, base_dir)),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    metadata = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise RuntimeError(f"Container image build failed with exit code {completed.returncode}: {completed.stderr.strip()}")
    return metadata


def container_build_command(
    *,
    executable: str,
    image: str,
    build: Dict[str, Any],
    base_dir: Path,
) -> List[str]:
    context = resolve_build_context(build, base_dir)
    command = [str(executable), "build", "-t", str(build.get("tag") or image)]
    dockerfile = build.get("dockerfile")
    if dockerfile:
        command.extend(["-f", str(resolve_build_path(dockerfile, context))])
    if build.get("target"):
        command.extend(["--target", str(build["target"])])
    if build.get("platform"):
        command.extend(["--platform", str(build["platform"])])
    if build.get("pull"):
        command.append("--pull")
    if build.get("noCache") or build.get("no_cache"):
        command.append("--no-cache")
    for key, value in (build.get("args") or {}).items():
        command.extend(["--build-arg", f"{key}={value}"])
    command.extend([str(item) for item in build.get("extraArgs", []) or []])
    command.append(str(context))
    return command


def resolve_build_context(build: Dict[str, Any], base_dir: Path) -> Path:
    value = build.get("context", ".")
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def resolve_build_path(value: Any, context: Path) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (context / path).resolve()


def network_args(policy: str) -> List[str]:
    normalized = str(policy or "disabled").lower()
    if normalized in {"disabled", "none", "off"}:
        return ["--network", "none"]
    if normalized == "host":
        return ["--network", "host"]
    if normalized in {"enabled", "default", "bridge"}:
        return []
    raise ValueError(f"Unsupported container network policy: {policy!r}")


def dedupe_mounts(mounts: List[tuple[Path, str]]) -> List[tuple[Path, str]]:
    by_path: Dict[Path, str] = {}
    for path, mode in mounts:
        resolved = path.resolve()
        if not resolved.exists():
            continue
        existing = by_path.get(resolved)
        if existing == "rw" or mode == existing:
            continue
        by_path[resolved] = "rw" if mode == "rw" else "ro"
    return [(path, mode) for path, mode in sorted(by_path.items(), key=lambda item: str(item[0]))]


def container_pythonpath() -> str:
    cwd = Path.cwd().resolve()
    entries = [str(cwd / "src"), str(cwd)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    return os.pathsep.join(entries)
