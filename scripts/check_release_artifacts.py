#!/usr/bin/env python3
"""Check release metadata and core distribution artifact boundaries."""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_ARTIFACT_PREFIXES = (
    ".agents/",
    ".github/",
    ".optpilot-ui/",
    "catalog/",
    "designs/",
    "docs/",
    "resource/",
    "runs/",
    "scripts/",
    "site/",
    "studio/",
    "tests/",
    "workspace/",
)

FORBIDDEN_WHEEL_PREFIXES = (
    "optpilot_studio/",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", nargs="?", default="dist", help="Directory containing built artifacts")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    errors.extend(_check_versions(root))
    errors.extend(_check_artifacts(Path(args.dist_dir)))
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Release artifact checks passed.")
    return 0


def _check_versions(root: Path) -> list[str]:
    core_version = _first_match(root / "pyproject.toml", r'^version = "([^"]+)"')
    studio_version = _first_match(root / "studio" / "pyproject.toml", r'^version = "([^"]+)"')
    studio_pin = _first_match(root / "studio" / "pyproject.toml", r'"optpilot==([^"]+)"')
    core_init = _first_match(root / "src" / "optpilot" / "__init__.py", r'^__version__ = "([^"]+)"')
    studio_init = _first_match(root / "studio" / "src" / "optpilot_studio" / "__init__.py", r'^__version__ = "([^"]+)"')
    versions = {
        "core pyproject": core_version,
        "studio pyproject": studio_version,
        "studio dependency pin": studio_pin,
        "core __init__": core_init,
        "studio __init__": studio_init,
    }
    missing = [name for name, value in versions.items() if not value]
    if missing:
        return [f"Missing version declaration(s): {', '.join(missing)}"]
    unique = sorted(set(versions.values()))
    if len(unique) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        return [f"Version declarations are not synchronized: {details}"]
    return []


def _check_artifacts(dist_dir: Path) -> list[str]:
    errors: list[str] = []
    wheels = sorted(dist_dir.glob("optpilot-*.whl"))
    sdists = sorted(dist_dir.glob("optpilot-*.tar.gz"))
    if not wheels:
        errors.append(f"No optpilot wheel found in {dist_dir}")
    if not sdists:
        errors.append(f"No optpilot sdist found in {dist_dir}")
    for wheel in wheels:
        errors.extend(_check_wheel(wheel))
    for sdist in sdists:
        errors.extend(_check_sdist(sdist))
    return errors


def _check_wheel(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            normalized = name.lstrip("/")
            if normalized.startswith(FORBIDDEN_WHEEL_PREFIXES):
                errors.append(f"{path.name} contains forbidden wheel entry: {normalized}")
    return errors


def _check_sdist(path: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            normalized = _strip_sdist_root(member.name)
            if normalized.startswith(FORBIDDEN_ARTIFACT_PREFIXES):
                errors.append(f"{path.name} contains forbidden sdist entry: {normalized}")
    return errors


def _strip_sdist_root(name: str) -> str:
    parts = name.lstrip("/").split("/", 1)
    return parts[1] if len(parts) == 2 else ""


def _first_match(path: Path, pattern: str) -> str:
    regex = re.compile(pattern, re.MULTILINE)
    match = regex.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


if __name__ == "__main__":
    raise SystemExit(main())
