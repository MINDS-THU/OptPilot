"""Shared discovery helpers for OptPilot package folders."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from .config import AUTHORING_API_VERSION
from .package_settings import PACKAGE_SETTINGS_FILENAMES, package_identity


JsonDict = Dict[str, Any]

CATALOG_PACKAGE_DIRS = {"environments", "methods", "resources", "studies"}
OPT_CONFIGS = {"environment", "method", "resource", "study"}
UNIQUE_PACKAGE_CONFIGS = {"environment", "method", "resource"}
RESOURCE_MANIFEST_NAMES = [
    "optpilot.resource.yaml",
    "optpilot-resource.yaml",
    ".optpilot/resource.yaml",
    ".optpilot/interface.yaml",
]


@dataclass
class PackageEntry:
    config: str
    id: str
    path: Path
    package_id: str
    raw: JsonDict
    qualified_id: str
    synthesized: bool = False
    source_root: Optional[Path] = None

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "config": self.config,
            "id": self.id,
            "path": str(self.path),
            "package_id": self.package_id,
            "qualified_id": self.qualified_id,
            "synthesized": self.synthesized,
        }
        if self.source_root is not None:
            payload["source_root"] = str(self.source_root)
        return payload


@dataclass
class PackageIndex:
    package_root: Path
    package_id: str
    entries: List[PackageEntry] = field(default_factory=list)
    ignored_yaml: List[Path] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    #: Durable identity from the package's own settings file, when it has one.
    #: Absent for a folder that has never been published, which keeps working
    #: exactly as before.
    identity: Optional[str] = None

    def entries_by_config(self, config: str) -> List[PackageEntry]:
        return [entry for entry in self.entries if entry.config == config]

    def counts(self) -> JsonDict:
        counts: JsonDict = {config: len(self.entries_by_config(config)) for config in sorted(OPT_CONFIGS)}
        counts["ignored_yaml"] = len(self.ignored_yaml)
        return counts


def expand_package_roots(roots: Iterable[str | Path]) -> List[Path]:
    """Expand catalog roots into package roots using Studio-compatible rules."""

    expanded: List[Path] = []
    for root_value in roots:
        root = Path(root_value).expanduser().resolve()
        packages = package_roots(root)
        if packages and not looks_like_package(root):
            expanded.extend(packages)
        else:
            expanded.append(root)
    return _dedupe_paths(expanded)


def package_roots(catalog_root: str | Path) -> List[Path]:
    root = Path(catalog_root).expanduser().resolve()
    if looks_like_package(root):
        return [root]
    if not root.exists() or not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and looks_like_package(path))


def looks_like_package(path: str | Path) -> bool:
    root = Path(path)
    return any((root / name).exists() for name in CATALOG_PACKAGE_DIRS) or any(
        (root / name).is_file() for name in PACKAGE_SETTINGS_FILENAMES
    )


def index_package(package_root: str | Path) -> PackageIndex:
    # Preserve the caller's terminal path until it has been inspected with
    # lstat semantics.  Resolving first erases the fact that the package root
    # itself is a symlink and makes validation disagree with retained launch.
    lexical_root = Path(
        os.path.abspath(os.fspath(Path(package_root).expanduser()))
    )
    package_id = lexical_root.name
    result = PackageIndex(package_root=lexical_root, package_id=package_id)
    seen_paths: set[Path] = set()
    ids_by_package: Dict[tuple[str, str, str], Path] = {}

    try:
        root_info = os.lstat(lexical_root)
    except (OSError, ValueError) as error:
        result.errors.append(
            f"Package root does not exist or cannot be inspected: "
            f"{lexical_root}: {error}"
        )
        return result
    if stat.S_ISLNK(root_info.st_mode):
        result.errors.append(
            f"Package root must not be a symbolic link: {lexical_root}"
        )
        return result
    if not stat.S_ISDIR(root_info.st_mode):
        result.errors.append(
            f"Package root does not exist or is not a directory: {lexical_root}"
        )
        return result
    try:
        root = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        result.errors.append(f"Could not resolve package root {lexical_root}: {error}")
        return result
    result.package_root = root

    tree_error = _package_tree_symlink_error(root)
    if tree_error is not None:
        # Do not discover or parse a subset of an unsafe tree.  Retained launch
        # applies the same all-tree preflight before opening the Study.
        result.errors.append(tree_error)
        return result

    safe_yaml_paths: List[tuple[Path, Path]] = []
    unsafe_yaml_paths: set[Path] = set()
    for path in _iter_yaml_files(root):
        resolved, path_error = _confined_package_yaml_path(root, path)
        if path_error is not None:
            result.errors.append(path_error)
            unsafe_yaml_paths.add(path)
            continue
        assert resolved is not None
        safe_yaml_paths.append((path, resolved))

    settings_paths = {root / name for name in PACKAGE_SETTINGS_FILENAMES}
    if not any(path in unsafe_yaml_paths for path in settings_paths):
        try:
            result.identity = package_identity(root)
        except ValueError as error:
            # A malformed settings file is reported, never ignored: falling back to
            # the old path-derived anchor is what detaches a package from its
            # history, and it would happen silently.
            result.errors.append(str(error))

    for path, resolved in safe_yaml_paths:
        if resolved in seen_paths:
            continue
        if path.parent == root and path.name in PACKAGE_SETTINGS_FILENAMES:
            # Describes the package itself rather than anything in it, so it is
            # neither an entry nor a stray file.
            continue
        resource_source_root = _resource_manifest_source_root(root, path)
        if resource_source_root is not None:
            seen_paths.add(resolved)
            raw, read_error = _read_yaml_with_error(path)
            if read_error is not None:
                result.errors.append(
                    f"Could not parse expected resource config {path}: {read_error}"
                )
                continue
            if (
                raw.get("apiVersion") != AUTHORING_API_VERSION
                or raw.get("config") != "resource"
            ):
                result.errors.append(
                    f"Expected resource config at {path}, but its "
                    "apiVersion/config declaration is missing or unsupported."
                )
                continue
            entry = _entry_for_resource_dir(
                resource_source_root,
                raw,
                path,
                package_id=package_id,
            )
            _record_unique_id(result, ids_by_package, entry)
            result.entries.append(entry)
            continue
        seen_paths.add(resolved)
        raw, read_error = _read_yaml_with_error(path)
        expected_config = _expected_config_kind(root, path)
        if read_error is not None:
            if expected_config is not None:
                result.errors.append(
                    f"Could not parse expected {expected_config} config {path}: "
                    f"{read_error}"
                )
            else:
                result.ignored_yaml.append(path)
            continue
        config = raw.get("config")
        if raw.get("apiVersion") != AUTHORING_API_VERSION or config not in OPT_CONFIGS:
            if expected_config is not None:
                result.errors.append(
                    f"Expected {expected_config} config at {path}, but its "
                    "apiVersion/config declaration is missing or unsupported."
                )
            else:
                result.ignored_yaml.append(path)
            continue
        if expected_config is not None and config != expected_config:
            result.errors.append(
                f"Expected {expected_config} config at {path}, found {config!r}."
            )
            continue
        entry = _entry_for_config(path, raw, package_id=package_id)
        if config in UNIQUE_PACKAGE_CONFIGS:
            _record_unique_id(result, ids_by_package, entry)
        result.entries.append(entry)

    resources_root = root / "resources"
    if resources_root.is_symlink():
        result.errors.append(
            f"Package resource directory must not be a symbolic link: {resources_root}"
        )
    elif resources_root.exists() and resources_root.is_dir():
        resource_dirs: List[Path] = []
        for item in sorted(resources_root.iterdir()):
            if item.is_symlink():
                result.errors.append(
                    f"Package resource directory must not be a symbolic link: {item}"
                )
                continue
            if item.is_dir():
                resource_dirs.append(item)
        for resource_dir in resource_dirs:
            manifest_path, manifest = resource_manifest(resource_dir)
            if manifest_path and manifest_path.resolve() in seen_paths:
                continue
            entry = _entry_for_resource_dir(resource_dir, manifest, manifest_path, package_id=package_id)
            _record_unique_id(result, ids_by_package, entry)
            result.entries.append(entry)

    result.entries.sort(key=lambda item: (item.config, item.id, str(item.path)))
    result.ignored_yaml.sort()
    if not result.entries:
        result.errors.append(
            "Package contains no recognized environment, method, resource, or study configs."
        )
    return result


def qualified_id(package_id: str, kind: str, entry_id: str) -> str:
    package = package_id or "workspace"
    return f"{package}/{kind}/{entry_id}"


def resource_manifest(path: str | Path) -> tuple[Optional[Path], JsonDict]:
    root = Path(path)
    for name in RESOURCE_MANIFEST_NAMES:
        manifest_path = root / name
        # The package index reports the authoring error.  This lower-level
        # helper must still refuse to follow the link so its fallback discovery
        # pass cannot read bytes that the safe indexing pass rejected.
        if manifest_path.is_symlink():
            continue
        if not manifest_path.exists() or not manifest_path.is_file():
            continue
        raw = _read_yaml(manifest_path)
        if raw.get("apiVersion") == AUTHORING_API_VERSION and raw.get("config") == "resource":
            return manifest_path, raw
    return None, {}


def _resource_manifest_source_root(package_root: Path, path: Path) -> Optional[Path]:
    resources_root = package_root / "resources"
    try:
        relative = path.resolve().relative_to(resources_root.resolve())
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    resource_root = (resources_root / relative.parts[0]).resolve()
    for name in RESOURCE_MANIFEST_NAMES:
        if path.resolve() == (resource_root / name).resolve():
            return resource_root
    return None


def _entry_for_config(path: Path, raw: JsonDict, *, package_id: str) -> PackageEntry:
    config = str(raw["config"])
    entry_id = str(raw.get("id") or raw.get("name") or path.stem)
    return PackageEntry(
        config=config,
        id=entry_id,
        path=path.resolve(),
        package_id=package_id,
        raw=dict(raw),
        qualified_id=qualified_id(package_id, config, entry_id),
    )


def _entry_for_resource_dir(path: Path, manifest: JsonDict, manifest_path: Optional[Path], *, package_id: str) -> PackageEntry:
    raw: JsonDict
    if manifest:
        raw = dict(manifest)
    else:
        raw = {
            "apiVersion": AUTHORING_API_VERSION,
            "config": "resource",
            "id": _slug_text(path.name),
            "name": path.name,
        }
    entry_id = str(raw.get("id") or _slug_text(path.name))
    return PackageEntry(
        config="resource",
        id=entry_id,
        path=(manifest_path or path).resolve(),
        package_id=package_id,
        raw=raw,
        qualified_id=qualified_id(package_id, "resource", entry_id),
        synthesized=manifest_path is None,
        source_root=path.resolve(),
    )


def _record_unique_id(
    index: PackageIndex,
    seen: Dict[tuple[str, str, str], Path],
    entry: PackageEntry,
) -> None:
    key = (entry.package_id, entry.config, entry.id)
    previous = seen.get(key)
    if previous is not None and previous.resolve() != entry.path.resolve():
        index.errors.append(
            f"Duplicate catalog id {entry.id!r} for {entry.config!r} in package {entry.package_id!r}: "
            f"{previous} and {entry.path}"
        )
        return
    seen[key] = entry.path


def _iter_yaml_files(root: Path) -> Iterable[Path]:
    for pattern in ("*.yaml", "*.yml"):
        yield from sorted(root.rglob(pattern))


def _package_tree_symlink_error(root: Path) -> Optional[str]:
    """Return the first unsafe package-tree link before config discovery.

    ``Path.rglob`` deliberately does not descend through directory symlinks,
    which previously let a Study reference bytes that indexing never saw.  A
    tree walk with ``follow_symlinks=False`` exposes both directory and file
    links and matches the retained package preflight.
    """

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: os.fsencode(item.name))
        except OSError as error:
            return f"Could not inspect package directory {directory}: {error}"
        child_directories: List[Path] = []
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                linked = entry.stat(follow_symlinks=False)
            except OSError as error:
                return f"Could not inspect package entry {entry_path}: {error}"
            if stat.S_ISLNK(linked.st_mode):
                return (
                    "Package trees must not contain symbolic links: "
                    f"{entry_path}"
                )
            if stat.S_ISDIR(linked.st_mode):
                child_directories.append(entry_path)
        # Keep traversal and therefore the reported first error deterministic.
        pending.extend(reversed(child_directories))
    return None


def _confined_package_yaml_path(
    package_root: Path, path: Path
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve one package YAML only after rejecting every symlink hop.

    Package indexing is a read boundary: following even an in-package symlink
    makes the indexed bytes depend on an alias rather than the path that will
    later be captured.  An external target is worse because merely validating
    a package would read outside the package.  Check the terminal path and each
    relative ancestor with ``lstat`` semantics before resolving for the final
    containment check.
    """

    if path.is_symlink():
        return None, f"Package YAML config must not be a symbolic link: {path}"
    try:
        relative = path.relative_to(package_root)
    except ValueError:
        return None, f"Package YAML config is outside package root {package_root}: {path}"

    current = package_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return (
                None,
                "Package YAML config must not cross a symbolic-link directory "
                f"{current}: {path}",
            )

    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        return None, f"Could not resolve package YAML config {path}: {error}"
    try:
        resolved.relative_to(package_root)
    except ValueError:
        return (
            None,
            f"Package YAML config resolves outside package root {package_root}: {path}",
        )
    if not resolved.is_file():
        return None, f"Package YAML config is not a regular file: {path}"
    return resolved, None


def _read_yaml(path: Path) -> JsonDict:
    raw, _error = _read_yaml_with_error(path)
    return raw


def _read_yaml_with_error(path: Path) -> tuple[JsonDict, Optional[str]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as error:
        return {}, f"{type(error).__name__}: {error}"
    if not isinstance(raw, dict):
        return {}, "the YAML document must be an object"
    return raw, None


def _expected_config_kind(package_root: Path, path: Path) -> Optional[str]:
    """Return the config kind implied by a conventional component path.

    Packages may legitimately carry domain YAML, including inside vendored
    source trees.  Only filenames that are part of OptPilot's documented
    layout are treated as configs so a syntax error there cannot disappear as
    an ignored data file.
    """

    try:
        relative = path.resolve().relative_to(package_root.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    filename = path.name.lower()
    if parts[0] == "environments" and len(parts) >= 3:
        return "environment" if filename.startswith("environment") else None
    if parts[0] == "methods" and len(parts) >= 3:
        return "method" if filename.startswith("method") else None
    if parts[0] == "studies" and len(parts) >= 2:
        return "study"
    if parts[0] == "resources" and len(parts) >= 3:
        resource_relative = "/".join(parts[2:])
        if resource_relative in RESOURCE_MANIFEST_NAMES:
            return "resource"
    return None


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _slug_text(value: str) -> str:
    text = value.strip().lower()
    chars = [char if char.isalnum() else "-" for char in text]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug or "resource"
