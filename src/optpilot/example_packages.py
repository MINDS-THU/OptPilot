"""The example packages OptPilot ships, and how they become the person's own.

A fresh install used to open onto an empty catalog: the examples lived in the
repository and were excluded from every distribution, so anyone who installed
OptPilot rather than cloning it had nothing to browse and nothing to run.

They now travel inside the install, and this module copies them out on first
use into an ordinary folder the person owns. That copy is the point. Left
inside the installed software they would be read-only, replaced by the next
upgrade, and a second class of package with different rules -- exactly the
split that made registering one impossible before. Copied out, an example is
the same kind of folder as anything the person writes: editable, registerable,
movable, and deletable.

Each shipped package carries a fixed identity in its settings file, so a copy
keeps its lineage. Re-copying a package the person has edited is therefore
never right, and never happens: a folder that already exists is left exactly
as it is.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

__all__ = [
    "ExamplePackageInstallation",
    "installed_example_packages",
    "shipped_example_packages",
    "shipped_examples_root",
    "install_example_packages",
]

#: Directories that make a folder a package rather than an ordinary folder.
_PACKAGE_MARKERS = ("environments", "methods", "resources", "studies")

#: Never copied out: build leavings and version-control noise that mean
#: nothing to the person receiving the package.
_SKIP_NAMES = frozenset({"__pycache__", ".git", ".DS_Store", ".pytest_cache"})


@dataclass(frozen=True)
class ExamplePackageInstallation:
    """What one call to :func:`install_example_packages` did."""

    #: Packages copied out on this call, newest-first by name.
    installed: tuple[str, ...]
    #: Packages left untouched because the person already has them. Never
    #: overwritten: their copy may contain edits worth more than the original.
    kept: tuple[str, ...]
    #: Where the copies live.
    root: Path


def shipped_examples_root() -> Path | None:
    """The examples inside this installation, or None when absent.

    Absent is a legitimate state, not a fault: a checkout running from source
    reads them from the repository instead, and a deliberately minimal build
    may omit them.
    """

    try:
        import optpilot_examples
    except Exception:
        return None
    location = getattr(optpilot_examples, "__file__", None)
    if not location:
        return None
    root = Path(location).parent
    if not root.is_dir():
        return None
    if _is_source_checkout(root):
        # Running from a checkout of OptPilot itself: these ARE the
        # repository's catalog folder, which is already found where it sits.
        # Copying them out would produce a second copy of every package, and
        # a catalog holding two of everything refuses to load at all.
        return None
    return root


def _is_source_checkout(examples_root: Path) -> bool:
    """Whether this location is OptPilot's own repository rather than an install.

    In a checkout, an editable install resolves the examples to the
    repository's ``catalog`` directory, whose parent is the project itself.
    An installed copy sits in the interpreter's packages directory, where no
    project file is a sibling.
    """

    parent = examples_root.parent
    return (parent / "pyproject.toml").is_file() and (
        (parent / "src" / "optpilot").is_dir() or (parent / ".git").exists()
    )


def _package_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and path.name not in _SKIP_NAMES
        and not path.name.startswith(".")
        and any((path / marker).exists() for marker in _PACKAGE_MARKERS)
    )


def shipped_example_packages() -> list[Path]:
    """Every example package inside this installation."""

    root = shipped_examples_root()
    return _package_dirs(root) if root is not None else []


def installed_example_packages(packages_root: Path) -> list[Path]:
    """Every package folder the person already has."""

    return _package_dirs(Path(packages_root))


def _copy_package(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _SKIP_NAMES or name.endswith((".pyc", ".pyo"))
        }

    # Copy to a temporary name and move it into place, so an interrupted copy
    # never leaves a half-package that later looks installed.
    staging = destination.parent / f".{destination.name}.incoming"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging, ignore=ignore, symlinks=False)
    staging.replace(destination)


def install_example_packages(
    packages_root: Path,
    *,
    only: Iterable[str] | None = None,
) -> ExamplePackageInstallation:
    """Copy the shipped examples into ``packages_root``, keeping what is there.

    Safe to call on every start: a package the person already has is left
    alone, including one they have edited or deliberately emptied.
    """

    root = Path(packages_root)
    wanted = None if only is None else {str(name) for name in only}
    installed: list[str] = []
    kept: list[str] = []
    shipped = shipped_example_packages()
    if shipped:
        root.mkdir(parents=True, exist_ok=True)
    for source in shipped:
        if wanted is not None and source.name not in wanted:
            continue
        destination = root / source.name
        if destination.exists():
            kept.append(source.name)
            continue
        _copy_package(source, destination)
        installed.append(source.name)
    return ExamplePackageInstallation(
        installed=tuple(installed), kept=tuple(kept), root=root
    )
