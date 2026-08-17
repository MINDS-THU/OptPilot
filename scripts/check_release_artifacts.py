#!/usr/bin/env python3
"""Validate release metadata and distribution artifact boundaries.

The public ``optpilot`` distribution must remain a self-contained core package.
The source-only ``optpilot-studio`` distribution is checked when its artifact
directory is supplied explicitly.
"""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


#: Never distributable: working notes and untracked research trees. The
#: example packages used to sit in this list, which is why nobody could
#: install anything to run -- they are product, not scratch, and now ship.
RESEARCH_SCRATCH_PREFIXES = (
    "design/",
    "designs/",
    "resource/",
)

CORE_FORBIDDEN_SDIST_PREFIXES = (
    ".agents/",
    ".github/",
    ".optpilot-ui/",
    "docs/",
    "runs/",
    "scripts/",
    "site/",
    "studio/",
    "tests/",
    "workspace/",
    *RESEARCH_SCRATCH_PREFIXES,
)

CORE_FORBIDDEN_WHEEL_PREFIXES = (
    "optpilot/ui/",
    "optpilot/assistant_assets/",
    "optpilot/docs_assets/",
    "optpilot_studio/",
    *RESEARCH_SCRATCH_PREFIXES,
)

CORE_REQUIRED_WHEEL_ENTRIES = {
    "optpilot/__init__.py",
    "optpilot/__main__.py",
    "optpilot/cli.py",
    "optpilot/config.py",
    "optpilot/runner.py",
    "optpilot/realm/__init__.py",
}

CORE_REQUIRED_SDIST_ENTRIES = {
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
    "src/optpilot/__init__.py",
    "src/optpilot/cli.py",
    "src/optpilot/config.py",
    "src/optpilot/runner.py",
    "src/optpilot/realm/__init__.py",
}

STUDIO_REQUIRED_WHEEL_ENTRIES = {
    "optpilot_studio/__init__.py",
    "optpilot_studio/agent.py",
    "optpilot_studio/cli.py",
    "optpilot_studio/assistant_assets/prompts/system.md",
    "optpilot_studio/docs_assets/index.md",
    "optpilot_studio/docs_assets/operations.md",
    "optpilot_studio/ui/server.py",
    "optpilot_studio/ui/static/app.js",
    "optpilot_studio/ui/static/index.html",
    "optpilot_studio/ui/static/styles.css",
    "optpilot_studio/ui/workspace_runtime/Dockerfile",
}

STUDIO_REQUIRED_SDIST_ENTRIES = {
    "README.md",
    "pyproject.toml",
    "src/optpilot_studio/__init__.py",
    "src/optpilot_studio/agent.py",
    "src/optpilot_studio/cli.py",
    "src/optpilot_studio/assistant_assets/prompts/system.md",
    "src/optpilot_studio/docs_assets/index.md",
    "src/optpilot_studio/docs_assets/operations.md",
    "src/optpilot_studio/ui/server.py",
    "src/optpilot_studio/ui/static/app.js",
    "src/optpilot_studio/ui/static/index.html",
    "src/optpilot_studio/ui/static/styles.css",
    "src/optpilot_studio/ui/workspace_runtime/Dockerfile",
}

#: The same launch scripts as they appear INSIDE a distribution. The wheel
#: carries the example packages under optpilot_examples/, so an allowlisted
#: script has two legitimate names; the source distribution keeps catalog/.
#: Without this, a package that legitimately ships a launch script could
#: never be distributed at all.
def _distributed_executable_paths() -> set[str]:
    paths = set()
    for relative in ALLOWED_EXECUTABLE_PATHS:
        paths.add(relative)
        if relative.startswith("catalog/"):
            paths.add("optpilot_examples/" + relative[len("catalog/"):])
    return paths


ALLOWED_EXECUTABLE_PATHS = {
    "catalog/devs_gallery/resources/devs-gen-interface/_optpilot_launch_interface.sh",
    "catalog/or_solving/methods/coopa_solver/launch_console.sh",
    "catalog/devs_gallery/resources/devs-gen-interface/_start_backend.sh",
    "catalog/devs_gallery/resources/devs-gen-interface/_start_frontend.sh",
    "scripts/smoke_test.sh",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default="dist",
        help="Directory containing the core wheel and sdist",
    )
    parser.add_argument(
        "--studio-dist-dir",
        help="Optional directory containing the source-only Studio wheel and sdist",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    version, version_errors = _check_versions(root)
    errors = list(version_errors)
    errors.extend(_check_source_modes(root))
    if version:
        errors.extend(_check_core_artifacts(Path(args.dist_dir), root, version))
        if args.studio_dist_dir:
            errors.extend(
                _check_studio_artifacts(Path(args.studio_dist_dir), version)
            )
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    checked = "core and Studio" if args.studio_dist_dir else "core"
    print(f"Release artifact checks passed ({checked}).")
    return 0


def _check_versions(root: Path) -> tuple[str, list[str]]:
    core_version = _first_match(root / "pyproject.toml", r'^version = "([^"]+)"')
    studio_version = _first_match(
        root / "studio" / "pyproject.toml", r'^version = "([^"]+)"'
    )
    studio_pin = _first_match(
        root / "studio" / "pyproject.toml", r'"optpilot==([^"]+)"'
    )
    core_init = _first_match(
        root / "src" / "optpilot" / "__init__.py",
        r'^__version__ = "([^"]+)"',
    )
    studio_init = _first_match(
        root / "studio" / "src" / "optpilot_studio" / "__init__.py",
        r'^__version__ = "([^"]+)"',
    )
    versions = {
        "core pyproject": core_version,
        "studio pyproject": studio_version,
        "studio dependency pin": studio_pin,
        "core __init__": core_init,
        "studio __init__": studio_init,
    }
    missing = [name for name, value in versions.items() if not value]
    if missing:
        return "", [f"Missing version declaration(s): {', '.join(missing)}"]
    unique = sorted(set(versions.values()))
    if len(unique) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        return "", [f"Version declarations are not synchronized: {details}"]
    return core_version, []


def _check_core_artifacts(dist_dir: Path, root: Path, version: str) -> list[str]:
    wheel, sdist, errors = _artifact_pair(dist_dir, "optpilot", version)
    expected_wheel = set(CORE_REQUIRED_WHEEL_ENTRIES)
    expected_sdist = set(CORE_REQUIRED_SDIST_ENTRIES)
    for source_root, wheel_prefix, sdist_prefix, suffix in (
        (
            root / "src" / "optpilot" / "schemas",
            "optpilot/schemas",
            "src/optpilot/schemas",
            ".json",
        ),
        (
            root / "src" / "optpilot" / "realm" / "migrations",
            "optpilot/realm/migrations",
            "src/optpilot/realm/migrations",
            ".sql",
        ),
    ):
        for source in source_root.rglob(f"*{suffix}"):
            relative = source.relative_to(source_root).as_posix()
            expected_wheel.add(f"{wheel_prefix}/{relative}")
            expected_sdist.add(f"{sdist_prefix}/{relative}")
    if wheel:
        errors.extend(
            _check_wheel(
                wheel,
                required=expected_wheel,
                forbidden_prefixes=CORE_FORBIDDEN_WHEEL_PREFIXES,
                metadata_name="optpilot",
                version=version,
                required_entry_points=("optpilot = optpilot.cli:main",),
            )
        )
    if sdist:
        errors.extend(
            _check_sdist(
                sdist,
                required=expected_sdist,
                forbidden_prefixes=CORE_FORBIDDEN_SDIST_PREFIXES,
            )
        )
    return errors


def _check_studio_artifacts(dist_dir: Path, version: str) -> list[str]:
    wheel, sdist, errors = _artifact_pair(dist_dir, "optpilot_studio", version)
    if wheel:
        errors.extend(
            _check_wheel(
                wheel,
                required=STUDIO_REQUIRED_WHEEL_ENTRIES,
                forbidden_prefixes=("optpilot/", *RESEARCH_SCRATCH_PREFIXES),
                metadata_name="optpilot_studio",
                version=version,
                required_entry_points=(
                    "optpilot-studio = optpilot_studio.ui.server:main",
                    "ui = optpilot_studio.cli:add_ui_subcommand",
                ),
                required_metadata=(f"Requires-Dist: optpilot=={version}",),
            )
        )
    if sdist:
        errors.extend(
            _check_sdist(
                sdist,
                required=STUDIO_REQUIRED_SDIST_ENTRIES,
                forbidden_prefixes=("tests/", *RESEARCH_SCRATCH_PREFIXES),
            )
        )
    return errors


def _artifact_pair(
    dist_dir: Path,
    distribution: str,
    version: str,
) -> tuple[Path | None, Path | None, list[str]]:
    errors: list[str] = []
    expected_wheel = dist_dir / f"{distribution}-{version}-py3-none-any.whl"
    expected_sdist = dist_dir / f"{distribution}-{version}.tar.gz"
    candidates = [
        *dist_dir.glob(f"{distribution}-*.whl"),
        *dist_dir.glob(f"{distribution}-*.tar.gz"),
    ]
    expected = {expected_wheel, expected_sdist}
    unexpected = sorted(path.name for path in candidates if path not in expected)
    if unexpected:
        errors.append(
            f"{dist_dir} contains stale or unexpectedly tagged {distribution} "
            f"artifact(s): {', '.join(unexpected)}"
        )
    if not expected_wheel.is_file():
        errors.append(f"Missing expected wheel: {expected_wheel}")
    if not expected_sdist.is_file():
        errors.append(f"Missing expected sdist: {expected_sdist}")
    return (
        expected_wheel if expected_wheel.is_file() else None,
        expected_sdist if expected_sdist.is_file() else None,
        errors,
    )


def _check_wheel(
    path: Path,
    *,
    required: set[str],
    forbidden_prefixes: tuple[str, ...],
    metadata_name: str,
    version: str,
    required_entry_points: tuple[str, ...],
    required_metadata: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        errors.extend(_missing_entries(path, required, names))
        errors.extend(_archive_hygiene_errors(path, names, forbidden_prefixes))
        allowed_executables = _distributed_executable_paths()
        for member in members:
            mode = member.external_attr >> 16
            if (
                stat.S_ISREG(mode)
                and mode & 0o111
                and member.filename not in allowed_executables
            ):
                errors.append(
                    f"{path.name} contains unexpectedly executable file: "
                    f"{member.filename}"
                )
        dist_info = f"{metadata_name}-{version}.dist-info"
        metadata_path = f"{dist_info}/METADATA"
        entry_points_path = f"{dist_info}/entry_points.txt"
        if metadata_path not in names:
            errors.append(f"{path.name} is missing {metadata_path}")
        else:
            metadata = archive.read(metadata_path).decode("utf-8")
            for line in (
                f"Version: {version}",
                *required_metadata,
            ):
                if line not in metadata:
                    errors.append(f"{path.name} metadata is missing: {line}")
        if entry_points_path not in names:
            errors.append(f"{path.name} is missing {entry_points_path}")
        else:
            entry_points = archive.read(entry_points_path).decode("utf-8")
            for line in required_entry_points:
                if line not in entry_points:
                    errors.append(f"{path.name} entry points are missing: {line}")
    return errors


def _check_sdist(
    path: Path,
    *,
    required: set[str],
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = {_strip_sdist_root(member.name) for member in members}
        allowed_executables = _distributed_executable_paths()
        for member in members:
            normalized = _strip_sdist_root(member.name)
            if (
                member.isfile()
                and member.mode & 0o111
                and normalized not in allowed_executables
            ):
                errors.append(
                    f"{path.name} contains unexpectedly executable file: {normalized}"
                )
    errors.extend(_missing_entries(path, required, names))
    errors.extend(_archive_hygiene_errors(path, names, forbidden_prefixes))
    return errors


def _missing_entries(path: Path, required: set[str], actual: set[str]) -> list[str]:
    return [
        f"{path.name} is missing required entry: {name}"
        for name in sorted(required - actual)
    ]


def _archive_hygiene_errors(
    path: Path,
    names: set[str],
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    for name in sorted(names):
        normalized = name.lstrip("/")
        parts = normalized.split("/")
        if normalized.startswith(forbidden_prefixes):
            errors.append(f"{path.name} contains forbidden entry: {normalized}")
        if (
            "__pycache__" in parts
            or normalized.endswith((".pyc", ".pyo", ".DS_Store"))
            or normalized.startswith((".pytest_cache/", ".optpilot-ui/"))
        ):
            errors.append(f"{path.name} contains generated/local entry: {normalized}")
    return errors


def _strip_sdist_root(name: str) -> str:
    parts = name.lstrip("/").split("/", 1)
    return parts[1] if len(parts) == 2 else ""


def _first_match(path: Path, pattern: str) -> str:
    regex = re.compile(pattern, re.MULTILINE)
    match = regex.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def _check_source_modes(root: Path) -> list[str]:
    """Check the mode git records, not the mode this filesystem reports.

    What ships and what another machine checks out is git's recorded mode.
    The working tree's own bits can differ for reasons that have nothing to do
    with the repository -- a network or sync-managed folder may report
    everything as executable -- and reading those produced a wall of failures
    about files git considers perfectly ordinary.
    """

    try:
        output = subprocess.check_output(
            ["git", "ls-files", "-sz"],
            cwd=root,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"Could not inspect tracked source modes: {exc}"]
    errors: list[str] = []
    recorded: dict[str, str] = {}
    for value in output.split(b"\0"):
        if not value:
            continue
        try:
            # "<mode> <object> <stage>\t<path>"
            meta, relative = value.decode("utf-8").split("\t", 1)
            mode = meta.split(" ", 1)[0]
        except ValueError:
            continue
        recorded[relative] = mode
    for relative, mode in sorted(recorded.items()):
        if mode == "120000":  # a symlink records its own mode; not our concern
            continue
        executable = mode == "100755"
        expected = relative in ALLOWED_EXECUTABLE_PATHS
        if executable and not expected:
            errors.append(f"Tracked non-script file is executable: {relative}")
        elif expected and not executable:
            errors.append(f"Tracked launch script is not executable: {relative}")
    missing = sorted(ALLOWED_EXECUTABLE_PATHS - set(recorded))
    for relative in missing:
        errors.append(f"Expected tracked launch script is missing: {relative}")
    return errors


if __name__ == "__main__":
    raise SystemExit(main())
