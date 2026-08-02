"""Source-root discovery for authoring and catalog presentation.

Runtime source preparation deliberately does not live here. Canonical runs
retain one immutable package artifact and realize it through Realm projections;
Studio uses this module only to choose the smallest source tree that contains a
component config and its referenced Python package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List, Optional


def _choose_source_root(
    config_path: Path,
    refs: List[str],
    source_hints: List[Path] | None = None,
) -> Path:
    default_root = config_path.parent.resolve()
    candidates: List[Path] = []
    for hint in source_hints or []:
        resolved = hint.resolve()
        if _is_relative_to(config_path, resolved):
            candidates.append(resolved)
    for ref in refs:
        top_package_dir = _top_package_dir_for_ref(ref, config_path)
        if top_package_dir is None:
            continue
        if _is_relative_to(config_path, top_package_dir):
            candidates.append(top_package_dir)
    if not candidates:
        return default_root
    return sorted(
        {path.resolve() for path in candidates}, key=lambda path: len(path.parts)
    )[0]


def _top_package_dir_for_ref(ref: str, config_path: Path) -> Optional[Path]:
    if ref.startswith("builtin.") or ":" not in ref:
        return None
    module_name, _, _attr = ref.partition(":")
    if not module_name or module_name.startswith("python:"):
        return None
    parts = module_name.split(".")
    near_config = _top_package_dir_near_config(parts, config_path)
    if near_config is not None:
        return near_config
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    if spec is None or not spec.origin or spec.origin in {"built-in", "namespace"}:
        return None
    origin = Path(spec.origin).resolve()
    if len(parts) == 1:
        return origin.parent
    top_package_dir = origin.parents[len(parts) - 2]
    if not (top_package_dir / "__init__.py").exists():
        return None
    return top_package_dir


def _top_package_dir_near_config(
    parts: List[str], config_path: Path
) -> Optional[Path]:
    if not parts:
        return None
    for ancestor in [config_path.parent, *config_path.parents]:
        if ancestor.name != parts[0] or not (ancestor / "__init__.py").exists():
            continue
        if len(parts) == 1:
            return ancestor.resolve()
        module_file = ancestor / Path(*parts[1:]).with_suffix(".py")
        package_init = ancestor / Path(*parts[1:]) / "__init__.py"
        if module_file.exists() or package_init.exists():
            return ancestor.resolve()
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


__all__ = ["_choose_source_root"]
