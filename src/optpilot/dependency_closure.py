"""Static run-time import closure checks for authored package components.

Package validation never runs a component's real dependency layer, and the
retained worker is deliberately not hermetic: it prepends the retained import
roots while the OptPilot interpreter's own site-packages stay importable so
that ``optpilot`` itself can be imported inside the worker.  A component that
imports a third-party package it never declared therefore works on the machine
that authored it and fails on every other machine, and no schema, source-path,
or callable-import check observes the difference.

This module closes that gap without executing authored code.  It parses the
retained Python sources reachable from a component's declared entry points,
follows the in-package import graph, and reports the top-level imports that
only the host interpreter can satisfy.  Because whole files are parsed, imports
deferred inside ``propose`` or an evaluator function are seen too.

The result is reported honestly rather than as a failure: a component may be
legitimately host-provisioned (an example that documents an extra dependency
group, say).  The point is that such a component must not look green.
"""

from __future__ import annotations

import ast
import re
import sys
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .locked_python_runtime_contract import (
    LockedPythonRuntimeError,
    parse_locked_python_wheel_lock,
    validate_locked_python_setup_declaration,
)


JsonDict = Dict[str, Any]

DEPENDENCY_DECLARED_CODE = "dependency_closure_declared"
DEPENDENCY_HOST_PROVISIONED_CODE = "dependency_host_provisioned"
DEPENDENCY_UNSCANNED_CODE = "dependency_closure_unscanned"

MAX_SCANNED_FILES = 2000
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_REPORTED_FILES = 8

# The closure the retained worker always leaves importable.  It is derived from
# installed metadata when available; this is the declaration it is derived from.
_FALLBACK_CORE_MODULES = frozenset(
    {
        "optpilot",
        "attr",
        "attrs",
        "jsonschema",
        "jsonschema_specifications",
        "referencing",
        "rpds",
        "yaml",
        "_yaml",
    }
)


@dataclass(frozen=True)
class UndeclaredImport:
    """One top-level module that only the host interpreter can provide."""

    module: str
    distributions: Tuple[str, ...]
    files: Tuple[str, ...]
    file_count: int

    def to_dict(self) -> JsonDict:
        return {
            "module": self.module,
            "distributions": list(self.distributions),
            "files": list(self.files),
            "file_count": self.file_count,
        }

    def describe(self) -> str:
        named = (
            f"{self.module} (distribution {', '.join(self.distributions)})"
            if self.distributions
            else self.module
        )
        where = ", ".join(self.files)
        if self.file_count > len(self.files):
            where = f"{where}, and {self.file_count - len(self.files)} more"
        return (
            f"{DEPENDENCY_HOST_PROVISIONED_CODE}: {named} is imported by {where} "
            "but no declared runtime provides it; it resolves only from the host "
            "interpreter and will be missing on other machines."
        )


@dataclass(frozen=True)
class DependencyClosureReport:
    """What a component's retained sources import, and who provides it."""

    code: str
    reason: str | None = None
    scanned_files: Tuple[str, ...] = ()
    unreadable_files: Tuple[str, ...] = ()
    locked_distributions: Tuple[str, ...] = ()
    undeclared: Tuple[UndeclaredImport, ...] = ()
    truncated: bool = False

    @property
    def declared(self) -> bool:
        return not self.undeclared

    def warnings(self) -> List[str]:
        return [item.describe() for item in self.undeclared]

    def to_dict(self) -> JsonDict:
        return {
            "code": self.code,
            "reason": self.reason,
            "declared": self.declared,
            "scanned_file_count": len(self.scanned_files),
            # The files actually read are part of the answer: a clean report is
            # only as trustworthy as the closure it covered.
            "scanned_files": list(self.scanned_files),
            "unreadable_files": list(self.unreadable_files),
            "locked_distributions": list(self.locked_distributions),
            "undeclared": [item.to_dict() for item in self.undeclared],
            "truncated": self.truncated,
        }


def scan_dependency_closure(
    *,
    package_root: str | Path,
    import_roots: Sequence[str | Path],
    entry_modules: Sequence[str] = (),
    seed_files: Sequence[str | Path] = (),
    provided_modules: Iterable[str] = (),
    locked_distributions: Sequence[str] = (),
) -> DependencyClosureReport:
    """Report run-time imports that no declared dependency layer provides.

    ``entry_modules`` are dotted module names (the module half of an authored
    ``module:object`` reference).  ``seed_files`` are additional in-package
    Python files that the component starts, such as a script named by a command
    entrypoint.  Only sources reachable from those seeds are read, so payload
    trees that are copied into a trial workspace rather than imported by the
    component are correctly left alone.
    """

    root = Path(package_root).expanduser().resolve()
    roots: List[Path] = []
    for value in import_roots:
        resolved = Path(value).expanduser().resolve()
        if not _is_inside(resolved, root):
            return DependencyClosureReport(
                code=DEPENDENCY_UNSCANNED_CODE,
                reason=(
                    "A declared Python import root is outside the package, so the "
                    "retained import closure cannot be determined from package "
                    "source alone."
                ),
            )
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    if not roots:
        return DependencyClosureReport(
            code=DEPENDENCY_UNSCANNED_CODE,
            reason="The component declares no Python import roots.",
        )

    pending: List[Path] = []
    for module in entry_modules:
        files, _owning_root = _resolve_absolute(module.split("."), roots)
        pending.extend(files)
    for value in seed_files:
        seed = Path(value).expanduser().resolve()
        if seed.is_file() and _is_inside(seed, root):
            pending.append(seed)
    if not pending:
        return DependencyClosureReport(
            code=DEPENDENCY_UNSCANNED_CODE,
            reason=(
                "No retained Python source was found for the component's declared "
                "entry points."
            ),
        )

    provided = frozenset(provided_modules) | _core_provided_modules()
    stdlib = _stdlib_modules()
    scanned: Dict[Path, None] = {}
    unreadable: List[Path] = []
    undeclared: Dict[str, List[Path]] = {}
    truncated = False

    while pending:
        path = pending.pop()
        if path in scanned:
            continue
        if len(scanned) >= MAX_SCANNED_FILES:
            truncated = True
            break
        scanned[path] = None
        tree = _parse_source(path)
        if tree is None:
            unreadable.append(path)
            continue
        for reference in _iter_imports(tree):
            files, unresolved = _resolve_reference(reference, path, roots)
            pending.extend(item for item in files if item not in scanned)
            if unresolved is None or unresolved in stdlib or unresolved in provided:
                continue
            seen = undeclared.setdefault(unresolved, [])
            if path not in seen:
                seen.append(path)

    findings = tuple(
        UndeclaredImport(
            module=module,
            distributions=_host_distributions(module),
            files=tuple(
                _package_relative(item, root) for item in sorted(files)[:MAX_REPORTED_FILES]
            ),
            file_count=len(files),
        )
        for module, files in sorted(undeclared.items())
    )
    return DependencyClosureReport(
        code=(
            DEPENDENCY_HOST_PROVISIONED_CODE
            if findings
            else DEPENDENCY_DECLARED_CODE
        ),
        reason=_closure_reason(findings, truncated=truncated),
        scanned_files=tuple(sorted(_package_relative(item, root) for item in scanned)),
        unreadable_files=tuple(
            sorted(_package_relative(item, root) for item in unreadable)
        ),
        locked_distributions=tuple(locked_distributions),
        undeclared=findings,
        truncated=truncated,
    )


@dataclass(frozen=True)
class LockedRuntimeProvision:
    """What a component's declared locked Python layer makes importable."""

    modules: frozenset[str] = frozenset()
    distributions: Tuple[str, ...] = ()
    unreadable: Tuple[str, ...] = ()


def locked_runtime_modules(
    setup: Any, *, config_dir: str | Path, package_root: str | Path
) -> LockedRuntimeProvision:
    """Return the modules and distributions a component's locked layer provides.

    The vendored wheels are the truth here, so their own top-level names are
    read out of the archives.  Nothing is installed and no index is contacted.
    A lock entry that cannot be read is reported rather than assumed empty:
    guessing would turn a broken lock into a false accusation against the
    component's imports.
    """

    try:
        declaration = validate_locked_python_setup_declaration(setup)
    except LockedPythonRuntimeError:
        return LockedRuntimeProvision()
    root = Path(package_root).expanduser().resolve()
    step = declaration["steps"][0]
    cwd = _package_path(step["cwd"], Path(config_dir), root)
    if cwd is None:
        return LockedRuntimeProvision(unreadable=(str(step["cwd"]),))
    modules: set[str] = set()
    distributions: set[str] = set()
    unreadable: set[str] = set()
    for requirement in step["requirements"]:
        lock_path = _package_path(requirement, cwd, root)
        if lock_path is None or not lock_path.is_file():
            unreadable.add(str(requirement))
            continue
        try:
            locked = parse_locked_python_wheel_lock(lock_path.read_bytes())
        except (LockedPythonRuntimeError, OSError):
            unreadable.add(str(requirement))
            continue
        for wheel_value, _digest in locked:
            distribution = _wheel_distribution(wheel_value)
            if distribution:
                distributions.add(distribution)
            wheel_path = _package_path(wheel_value, lock_path.parent, root)
            if wheel_path is None or not wheel_path.is_file():
                unreadable.add(str(wheel_value))
            modules.update(_wheel_modules(wheel_path, distribution))
    return LockedRuntimeProvision(
        modules=frozenset(modules),
        distributions=tuple(sorted(distributions)),
        unreadable=tuple(sorted(unreadable)),
    )


@dataclass(frozen=True)
class _ImportReference:
    level: int
    module: str
    names: Tuple[str, ...]
    is_from: bool


def _iter_imports(tree: ast.AST) -> Iterable[_ImportReference]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield _ImportReference(
                    level=0, module=alias.name, names=(), is_from=False
                )
        elif isinstance(node, ast.ImportFrom):
            yield _ImportReference(
                level=node.level or 0,
                module=node.module or "",
                names=tuple(alias.name for alias in node.names),
                is_from=True,
            )


def _resolve_reference(
    reference: _ImportReference, path: Path, roots: Sequence[Path]
) -> Tuple[List[Path], str | None]:
    """Return in-package files to scan and any unresolved top-level module."""

    if reference.level:
        base = path.parent
        for _ in range(reference.level - 1):
            base = base.parent
        if not any(_is_inside(base, root) for root in roots):
            # A relative import that walks out of the retained roots is a
            # broken import, not a third-party dependency signal.
            return [], None
        parts = reference.module.split(".") if reference.module else []
        files, _root = _resolve_absolute(parts, [base]) if parts else ([], base)
        target = base.joinpath(*parts)
        for name in reference.names:
            files.extend(_submodule_files(target, name))
        return files, None

    parts = [part for part in reference.module.split(".") if part]
    if not parts:
        return [], None
    files, owning_root = _resolve_absolute(parts, roots)
    if owning_root is None:
        return files, parts[0]
    if reference.is_from:
        target = owning_root.joinpath(*parts)
        for name in reference.names:
            files.extend(_submodule_files(target, name))
    return files, None


def _resolve_absolute(
    parts: Sequence[str], roots: Sequence[Path]
) -> Tuple[List[Path], Path | None]:
    """Walk a dotted name across roots, collecting the source files it runs.

    Roots are searched in declared order and the first one that owns the
    top-level name wins, which is how the interpreter resolves the same import
    once the retained roots are on ``sys.path``.
    """

    files: List[Path] = []
    if not parts:
        return files, None
    for root in roots:
        current = root
        resolved = False
        for index, part in enumerate(parts):
            package_dir = current / part
            package_init = package_dir / "__init__.py"
            module_file = current / f"{part}.py"
            if package_init.is_file():
                files.append(package_init)
                resolved = resolved or index == 0
                current = package_dir
                continue
            if module_file.is_file():
                files.append(module_file)
                resolved = resolved or index == 0
                break
            if package_dir.is_dir():
                # A namespace package contributes no source of its own.
                resolved = resolved or index == 0
                current = package_dir
                continue
            break
        if resolved:
            return files, root
    return files, None


def _submodule_files(target: Path, name: str) -> List[Path]:
    """Files for ``from package import name`` when name is itself a module."""

    if not name or name == "*":
        return []
    module_file = target / f"{name}.py"
    package_init = target / name / "__init__.py"
    return [item for item in (module_file, package_init) if item.is_file()]


def _parse_source(path: Path) -> ast.AST | None:
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, RecursionError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _closure_reason(
    findings: Sequence[UndeclaredImport], *, truncated: bool
) -> str | None:
    truncation = (
        " The scan stopped at the supported file limit, so the closure may be "
        "incomplete."
        if truncated
        else ""
    )
    if not findings:
        return truncation.strip() or None
    modules = ", ".join(item.module for item in findings)
    return (
        f"These run-time imports resolve only from the host interpreter: {modules}. "
        "Vendor them into the component's locked runtime (runtime.setup with a "
        "hash-locked wheel lock), retain them as package source, or document the "
        "component as host-provisioned." + truncation
    )


def _package_relative(path: Path, package_root: Path) -> str:
    try:
        return path.relative_to(package_root).as_posix()
    except ValueError:
        return path.as_posix()


def _package_path(value: str, base: Path, package_root: Path) -> Path | None:
    candidate = Path(str(value))
    if candidate.is_absolute() or "\x00" in str(value):
        return None
    resolved = (base / candidate).resolve()
    return resolved if _is_inside(resolved, package_root) else None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _wheel_distribution(wheel_value: str) -> str:
    name = Path(wheel_value).name
    if not name.endswith(".whl"):
        return ""
    return _normalize_distribution(name.split("-")[0])


def _wheel_modules(path: Path | None, distribution: str) -> frozenset[str]:
    fallback = frozenset({distribution.replace("-", "_")}) if distribution else frozenset()
    if path is None or not path.is_file():
        return fallback
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            top_level = next(
                (name for name in names if name.endswith(".dist-info/top_level.txt")),
                None,
            )
            if top_level is not None:
                declared = {
                    line.strip()
                    for line in archive.read(top_level).decode("utf-8", "replace").splitlines()
                    if line.strip()
                }
                if declared:
                    return frozenset(declared)
            modules: set[str] = set()
            for name in names:
                head, separator, _rest = name.partition("/")
                if not head or head.endswith((".dist-info", ".data")):
                    continue
                if separator:
                    modules.add(head)
                elif head.endswith((".py", ".so", ".pyd")):
                    modules.add(Path(head).stem.split(".")[0])
            return frozenset(modules) or fallback
    except (OSError, ValueError, zipfile.BadZipFile):
        return fallback


@lru_cache(maxsize=1)
def _stdlib_modules() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names) | frozenset(sys.builtin_module_names)


@lru_cache(maxsize=1)
def _distribution_index() -> Mapping[str, Sequence[str]]:
    """Host module-to-distribution map, or empty when metadata is unavailable."""

    try:
        return importlib_metadata.packages_distributions()
    except Exception:  # pragma: no cover - metadata backends vary by installer
        return {}


@lru_cache(maxsize=1)
def _core_provided_modules() -> frozenset[str]:
    """Modules the OptPilot interpreter itself always makes importable."""

    index = _distribution_index()
    if not index:
        return _FALLBACK_CORE_MODULES
    distributions = _requirement_closure("optpilot")
    modules = {"optpilot"}
    for module, owners in index.items():
        if any(_normalize_distribution(owner) in distributions for owner in owners):
            modules.add(module)
    if modules == {"optpilot"}:
        return _FALLBACK_CORE_MODULES
    return frozenset(modules)


def _requirement_closure(distribution: str) -> frozenset[str]:
    seen: set[str] = set()
    pending = [distribution]
    while pending:
        current = _normalize_distribution(pending.pop())
        if not current or current in seen:
            continue
        seen.add(current)
        try:
            requirements = importlib_metadata.requires(current) or []
        except importlib_metadata.PackageNotFoundError:
            continue
        for requirement in requirements:
            dependency = _requirement_distribution(requirement)
            if dependency:
                pending.append(dependency)
    return frozenset(seen)


def _requirement_distribution(requirement: str) -> str:
    """Name the distribution a core requirement pulls in, ignoring extras.

    Requirements gated on an extra are not part of the guaranteed closure, so a
    component may not rely on them being importable.
    """

    text, _, marker = str(requirement).partition(";")
    if "extra" in marker:
        return ""
    name = text.split("[")[0].strip()
    match = re.match(r"^[A-Za-z0-9._-]+", name)
    return _normalize_distribution(match.group(0)) if match else ""


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip()).lower()


def _host_distributions(module: str) -> Tuple[str, ...]:
    """Best-effort name of the distribution that would provide a module.

    This is a naming aid only (``yaml`` comes from ``PyYAML``); it is read from
    the host and never decides whether an import counts as declared.
    """

    return tuple(sorted(_distribution_index().get(module, ()) or ()))


def component_dependency_closure(
    *,
    config_kind: str,
    raw: Mapping[str, Any],
    config_path: str | Path,
    import_roots: Sequence[str | Path],
    package_root: str | Path,
) -> DependencyClosureReport:
    """Scan one authored environment or method config for undeclared imports."""

    if config_kind not in {"environment", "method"}:
        return DependencyClosureReport(
            code=DEPENDENCY_UNSCANNED_CODE,
            reason="Only environments and methods declare retained Python entry points.",
        )
    block_name = "evaluator" if config_kind == "environment" else "entrypoint"
    block = raw.get(block_name) if isinstance(raw.get(block_name), Mapping) else {}
    keys = ("python", "adapter") if config_kind == "environment" else ("python",)
    entry_modules = [
        str(block[key]).partition(":")[0]
        for key in keys
        if isinstance(block.get(key), str) and block.get(key)
    ]
    config_dir = Path(config_path).parent
    root = Path(package_root).expanduser().resolve()
    seeds = [
        token
        for token in block.get("command", []) or []
        if isinstance(token, str) and token.endswith(".py")
    ]
    if config_kind == "environment":
        # A ``python_module`` reference is imported out of the environment's own
        # retained tree at run time, so its imports are the environment's.  A
        # ``candidate_template`` is payload copied into a trial workspace and
        # belongs to the candidate contract instead, so it is left alone.
        method_context = (
            raw.get("methodContext")
            if isinstance(raw.get("methodContext"), Mapping)
            else {}
        )
        seeds.extend(
            reference["path"]
            for reference in method_context.get("references", []) or []
            if isinstance(reference, Mapping)
            and reference.get("type") == "python_module"
            and isinstance(reference.get("path"), str)
            and reference["path"].endswith(".py")
        )
    seed_files = [
        path
        for path in (_package_path(value, config_dir, root) for value in seeds)
        if path is not None
    ]
    runtime = raw.get("runtime") if isinstance(raw.get("runtime"), Mapping) else {}
    locked = locked_runtime_modules(
        runtime.get("setup"), config_dir=config_dir, package_root=package_root
    )
    if locked.unreadable:
        # The declared layer's contents are unknown, so its imports cannot be
        # attributed either way. Preparation rejects the lock separately.
        return DependencyClosureReport(
            code=DEPENDENCY_UNSCANNED_CODE,
            reason=(
                "The component's declared dependency lock could not be read, so "
                "what its prepared layer provides is unknown: "
                f"{', '.join(locked.unreadable)}."
            ),
            locked_distributions=locked.distributions,
        )
    return scan_dependency_closure(
        package_root=package_root,
        import_roots=import_roots,
        entry_modules=entry_modules,
        seed_files=seed_files,
        provided_modules=locked.modules,
        locked_distributions=locked.distributions,
    )


__all__ = [
    "DEPENDENCY_DECLARED_CODE",
    "DEPENDENCY_HOST_PROVISIONED_CODE",
    "DEPENDENCY_UNSCANNED_CODE",
    "DependencyClosureReport",
    "LockedRuntimeProvision",
    "UndeclaredImport",
    "component_dependency_closure",
    "locked_runtime_modules",
    "scan_dependency_closure",
]
