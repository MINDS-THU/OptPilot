"""Filesystem and subprocess safety helpers for DEVS generator tools.

These helpers intentionally reject lexical traversal and symlinks instead of
silently normalizing them.  The tools operate on agent-authored paths, so a
path should say exactly which file below the declared working root it means.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping, Optional


class UnsafePathError(PermissionError):
    """Raised when a caller-provided path is not safe for a tool operation."""


RESERVED_PATH_PARTS = frozenset({".devs_display_sessions"})


def _relative_user_path(value: object, *, allow_root: bool) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise UnsafePathError("Path must be a relative string.")

    raw = os.fspath(value)
    if not isinstance(raw, str) or "\x00" in raw:
        raise UnsafePathError("Path must be a valid relative string.")
    if raw == "":
        if allow_root:
            return Path(".")
        raise UnsafePathError("An empty path is not allowed.")

    relative = Path(raw)
    if relative.is_absolute():
        raise UnsafePathError("Absolute paths are not allowed.")
    if any(part == ".." for part in relative.parts):
        raise UnsafePathError("Parent-directory traversal is not allowed.")
    if any(part in RESERVED_PATH_PARTS for part in relative.parts):
        raise UnsafePathError("Backend-owned session metadata is not accessible to tools.")
    return relative


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        if part in ("", "."):
            continue
        current = current / part
        # is_symlink() also recognizes a broken symlink, unlike exists().
        if current.is_symlink():
            raise UnsafePathError("Symbolic links are not allowed in tool paths.")


def resolve_confined_path(
    root: object,
    user_path: object,
    *,
    allow_root: bool = False,
    must_exist: bool = False,
    expected: Optional[str] = None,
) -> Path:
    """Resolve ``user_path`` below ``root`` without following user symlinks.

    ``expected`` may be ``"file"``, ``"directory"``, or ``None``. Existing
    special files (devices, sockets, and FIFOs) are always rejected.
    """

    root_path = Path(root).resolve(strict=True)
    if not root_path.is_dir():
        raise UnsafePathError("The configured working root is not a directory.")

    relative = _relative_user_path(user_path, allow_root=allow_root)
    _reject_symlink_components(root_path, relative)
    candidate = root_path / relative
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise UnsafePathError("Access outside the working directory is not allowed.") from exc

    if not allow_root and resolved == root_path:
        raise UnsafePathError("The working directory itself is not a valid file path.")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(os.fspath(user_path))

    if candidate.exists():
        mode = candidate.stat(follow_symlinks=False).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UnsafePathError("Special files are not allowed in tool paths.")
        if expected == "file" and not stat.S_ISREG(mode):
            raise UnsafePathError("The selected path is not a regular file.")
        if expected == "directory" and not stat.S_ISDIR(mode):
            raise UnsafePathError("The selected path is not a directory.")
    elif expected == "directory" and must_exist:
        raise FileNotFoundError(os.fspath(user_path))

    return resolved


def validate_regular_tree(root: object) -> None:
    """Reject symlinks and special files anywhere inside an input tree."""

    tree_root = Path(root)
    if tree_root.is_symlink() or not tree_root.is_dir():
        raise UnsafePathError("The selected project is not a regular directory.")

    for current, directories, filenames in os.walk(tree_root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                raise UnsafePathError(
                    f"Symbolic link '{child.relative_to(tree_root)}' is not allowed in a project."
                )
            mode = child.stat(follow_symlinks=False).st_mode
            if not stat.S_ISDIR(mode):
                raise UnsafePathError(
                    f"Special path '{child.relative_to(tree_root)}' is not allowed in a project."
                )
        for name in filenames:
            child = current_path / name
            if child.is_symlink():
                raise UnsafePathError(
                    f"Symbolic link '{child.relative_to(tree_root)}' is not allowed in a project."
                )
            mode = child.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise UnsafePathError(
                    f"Special file '{child.relative_to(tree_root)}' is not allowed in a project."
                )


def safe_subprocess_environment(
    source: Optional[Mapping[str, str]] = None,
    *,
    home: Optional[object] = None,
) -> dict[str, str]:
    """Return a small, credential-free environment for generated Python.

    Generated simulators do not need the interface's model-provider keys or
    OptPilot launch-control capabilities.  Python itself is invoked by an
    absolute path, so retaining a minimal PATH is only for compatible child
    tooling that a simulator may legitimately invoke.
    """

    inherited = os.environ if source is None else source
    environment: dict[str, str] = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("PATH", "LANG", "LC_ALL", "TZ"):
        value = inherited.get(name)
        if value:
            environment[name] = value
    if home is not None:
        private_home = os.fspath(home)
        environment.update(
            {
                "HOME": private_home,
                "TMPDIR": private_home,
                "XDG_CACHE_HOME": os.path.join(private_home, ".cache"),
                "XDG_CONFIG_HOME": os.path.join(private_home, ".config"),
            }
        )
    return environment
