"""Shared discovery helpers for OptPilot package folders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from .config import AUTHORING_API_VERSION


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
    return any((root / name).exists() for name in CATALOG_PACKAGE_DIRS)


def index_package(package_root: str | Path) -> PackageIndex:
    root = Path(package_root).expanduser().resolve()
    package_id = root.name
    result = PackageIndex(package_root=root, package_id=package_id)
    seen_paths: set[Path] = set()
    ids_by_package: Dict[tuple[str, str, str], Path] = {}

    if not root.exists() or not root.is_dir():
        result.errors.append(f"Package root does not exist or is not a directory: {root}")
        return result

    for path in _iter_yaml_files(root):
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        raw = _read_yaml(path)
        config = raw.get("config")
        if raw.get("apiVersion") != AUTHORING_API_VERSION or config not in OPT_CONFIGS:
            result.ignored_yaml.append(path)
            continue
        entry = _entry_for_config(path, raw, package_id=package_id)
        if config in UNIQUE_PACKAGE_CONFIGS:
            _record_unique_id(result, ids_by_package, entry)
        result.entries.append(entry)

    resources_root = root / "resources"
    if resources_root.exists() and resources_root.is_dir():
        for resource_dir in sorted(item for item in resources_root.iterdir() if item.is_dir()):
            manifest_path, manifest = resource_manifest(resource_dir)
            if manifest_path and manifest_path.resolve() in seen_paths:
                continue
            entry = _entry_for_resource_dir(resource_dir, manifest, manifest_path, package_id=package_id)
            _record_unique_id(result, ids_by_package, entry)
            result.entries.append(entry)

    result.entries.sort(key=lambda item: (item.config, item.id, str(item.path)))
    result.ignored_yaml.sort()
    return result


def qualified_id(package_id: str, kind: str, entry_id: str) -> str:
    package = package_id or "workspace"
    return f"{package}/{kind}/{entry_id}"


def resource_manifest(path: str | Path) -> tuple[Optional[Path], JsonDict]:
    root = Path(path)
    for name in RESOURCE_MANIFEST_NAMES:
        manifest_path = root / name
        if not manifest_path.exists() or not manifest_path.is_file():
            continue
        raw = _read_yaml(manifest_path)
        if raw.get("apiVersion") == AUTHORING_API_VERSION and raw.get("config") == "resource":
            return manifest_path, raw
    return None, {}


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


def _read_yaml(path: Path) -> JsonDict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


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
