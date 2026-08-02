"""Secure default locations for the OS-local OptPilot realm."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from .errors import RealmIntegrityError


REALM_ROOT_ENV = "OPTPILOT_REALM_ROOT"


def default_realm_root() -> Path:
    override = os.environ.get(REALM_ROOT_ENV)
    if override:
        # Keep the final component unresolved so ``prepare_private_directory``
        # can reject an override that aliases another location through a
        # symlink.  Resolving it here would erase that security-relevant fact.
        return Path(override).expanduser().absolute()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "OptPilot" / "realm").absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return (base / "OptPilot" / "realm").absolute()
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return (base / "optpilot" / "realm").absolute()


def prepare_private_directory(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if os.name == "nt":
        if path.exists() and path.is_symlink():
            raise RealmIntegrityError(f"Realm directory must not be a symlink: {path}.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve()
        if not resolved.is_dir():
            raise RealmIntegrityError(f"Realm path is not a directory: {resolved}.")
        return resolved

    if not path.name:
        raise RealmIntegrityError("Realm directory must not be a filesystem root.")
    # Parent placement is caller/OS policy; create it first, then create and
    # open the realm's final component relative to one fixed parent descriptor.
    # This prevents a terminal symlink swap between a path check and chmod.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise RealmIntegrityError(f"Could not safely open realm parent directory: {parent}.") from exc
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        try:
            directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise RealmIntegrityError(f"Could not safely open realm directory: {path}.") from exc
        try:
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise RealmIntegrityError(f"Realm path is not a directory: {path}.")
            os.fchmod(directory_fd, 0o700)
            os.fsync(directory_fd)
            mode = os.fstat(directory_fd).st_mode & 0o777
            if mode & 0o077:
                raise RealmIntegrityError(f"Realm directory permissions are not private: {oct(mode)}.")
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)
    return parent / path.name
