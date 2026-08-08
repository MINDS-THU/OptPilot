"""Bounded, deterministic execution of generated DEVS simulator bundles.

The service in this module is deliberately synchronous.  An HTTP layer can
call :meth:`prepare` in a request worker, expose the returned queued record,
and call :meth:`run` in its existing background executor.  A second request
may call :meth:`stop` while ``run`` is blocked.

The default execution provider is a second, short-lived container dedicated to
one generated simulator. It has no network or credentials, a read-only root
and source tree, and explicit CPU, memory, PID, file-size, output, and lifetime
bounds. A local-process provider exists only behind an explicit trusted/test
opt-in; unavailable container infrastructure never causes an implicit fallback.
"""

from __future__ import annotations

import ast
import base64
import copy
import csv
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from default_tools.generated_execution import (
    ExecutionBoundaryError,
    GeneratedExecutionBoundary,
    PythonLaunch,
)
from default_tools.interface_output_action import (
    InterfaceOutputActionClient,
    OutputActionError,
    OutputActionExecutor,
    OutputActionResult,
)
from devs_tools.devs_construct_recon.tools.simulation.result_summary_contract import (
    MAX_DECLARED_METRICS,
    MAX_METRIC_DESCRIPTION_CHARS,
    METRIC_DIRECTIONS,
    TRACE_FILE,
    declared_metrics,
    declared_result_files,
)

from .interface_outputs import stable_tree_digest


SIMULATION_SCHEMA = "devs.simulation.v2"
SIMULATION_SCHEMA_V1 = "devs.simulation.v1"
ACCEPTED_SIMULATION_SCHEMAS = frozenset(
    {SIMULATION_SCHEMA_V1, SIMULATION_SCHEMA}
)
SIMULATION_MANIFEST = "simulation.json"
SIMULATION_ENTRYPOINT = "run.py"
PYTHON_RUNTIME_FIELD = "python_runtime"
XDEVS_VERSION = "3.0.0"
XDEVS_WHEEL_NAME = f"xdevs-{XDEVS_VERSION}-py3-none-any.whl"
XDEVS_RUNTIME_DIRECTORY = "runtime_dependencies"
XDEVS_REQUIREMENTS_LOCK = f"{XDEVS_RUNTIME_DIRECTORY}/requirements.lock"
XDEVS_WHEEL_PATH = f"{XDEVS_RUNTIME_DIRECTORY}/vendor/{XDEVS_WHEEL_NAME}"
XDEVS_LICENSE_PATH = (
    f"{XDEVS_RUNTIME_DIRECTORY}/licenses/xdevs-{XDEVS_VERSION}-LICENSE.txt"
)
XDEVS_NOTICE_PATH = f"{XDEVS_RUNTIME_DIRECTORY}/THIRD_PARTY_NOTICES.md"

_ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ARGUMENT_FLAG_RE = re.compile(r"^--[A-Za-z][A-Za-z0-9_-]{0,63}$")
_EXECUTION_ID_RE = re.compile(r"^exec_[a-f0-9]{32}$")
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "stopped"}
)
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_STRING_BYTES = 4096
_MAX_ARGUMENT_VECTOR_BYTES = 32 * 1024
_MAX_DESCRIPTION_BYTES = 4096
_MAX_LOCK_BYTES = 1024 * 1024
_PURE_PYTHON_WHEEL_RE = re.compile(
    r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.!+-]+-(?:py3|py2\.py3)-none-any\.whl$"
)
_PORTABILITY_LOCK = threading.RLock()
_XDEVS_WHEEL_CACHE: tuple[bytes, bytes] | None = None

_BEHAVIOR_TRACE_MAX_BYTES = 512 * 1024
_BEHAVIOR_SUMMARY_MAX_BYTES = 1024 * 1024
_BEHAVIOR_SOURCE_FILE_MAX_BYTES = 1024 * 1024
_BEHAVIOR_SOURCE_TOTAL_BYTES = 8 * 1024 * 1024
_BEHAVIOR_MAX_GRAPH_NODES = 512
_BEHAVIOR_MIN_ORIGIN_EVENTS = 3


class SimulationExecutionError(RuntimeError):
    """Base class for deterministic simulator execution errors."""


class SimulationBundleError(SimulationExecutionError):
    """The source bundle cannot be snapshotted safely."""


class SimulationManifestError(SimulationExecutionError):
    """``simulation.json`` or supplied argument values are invalid."""


class SimulationCapacityError(SimulationExecutionError):
    """Too many prepared or running executions exist."""


class ExecutionStateError(SimulationExecutionError):
    """An execution does not exist or cannot make the requested transition."""


@dataclass(frozen=True)
class BehaviorSmokeAssessment:
    """Conservative evidence from an execution's existing result artifacts.

    ``stalled`` is intentionally narrow: either a completed, lossless,
    positive-horizon multi-stage trace never progressed beyond simulation time
    zero, or repeated output from one component never reached a statically
    connected downstream component.  Every ambiguous case is ``inconclusive``
    or ``not_applicable`` so ordinary single-component and terminal-sink
    simulations are not rejected.
    """

    status: str
    message: str
    observed_components: tuple[str, ...] = ()
    expected_downstream_components: tuple[str, ...] = ()
    recorded_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResultFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    purpose: str
    status: str
    created_at: str
    bundle_digest: str
    snapshot_digest: str
    arguments: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    result_files: tuple[ResultFile, ...] = ()
    failure_kind: str | None = None
    message: str | None = None
    stop_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ArgumentSpec:
    name: str
    flag: str
    value_type: str
    required: bool
    has_default: bool
    default: Any
    minimum: int | float | None
    maximum: int | float | None
    choices: tuple[Any, ...]
    action: str


@dataclass(frozen=True)
class _Manifest:
    timeout_seconds: int
    arguments: tuple[_ArgumentSpec, ...]
    result_files: tuple[str, ...]
    requirements_lock: str
    schema_version: str = SIMULATION_SCHEMA_V1
    metrics: dict[str, Any] | None = None


@dataclass
class _PreparedExecution:
    record: ExecutionRecord
    job_dir: Path
    bundle_dir: Path
    results_dir: Path
    python_arguments: tuple[str, ...]
    timeout_seconds: int
    expected_results: tuple[str, ...]
    stop_event: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[bytes] | None = None
    launch: PythonLaunch | None = None


@dataclass
class _Capture:
    limit: int
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    truncated: bool = False

    def add(self, chunk: bytes) -> None:
        remaining = self.limit - self.size
        if remaining > 0:
            kept = chunk[:remaining]
            self.chunks.append(kept)
            self.size += len(kept)
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _SourceEntry:
    relative_path: str
    kind: str
    size: int
    mode: int
    modified_ns: int
    device: int
    inode: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_json_loads(text: str) -> Any:
    def pairs_hook(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SimulationManifestError(f"Duplicate JSON key: {key!r}.")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise SimulationManifestError(f"Non-finite JSON number is not allowed: {value}.")

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except SimulationManifestError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SimulationManifestError(f"Invalid {SIMULATION_MANIFEST}: {exc}") from exc


def _literal_scalar(node: ast.AST) -> Any:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    if value is None or type(value) in (str, bool, int, float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return None


def _module_from_entrypoint(entrypoint: Path) -> str | None:
    try:
        tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))
    except (OSError, UnicodeError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "SIM_MODULE" for target in targets):
            continue
        value_node = node.value
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            value = value_node.value
            if _MODULE_RE.fullmatch(value) and value.startswith("devs_project."):
                return value
    return None


def _argument_type_from_ast(call: ast.Call, keywords: Mapping[str, ast.AST]) -> str | None:
    type_node = keywords.get("type")
    if isinstance(type_node, ast.Name) and type_node.id in {"str", "int", "float", "bool"}:
        return {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}[type_node.id]
    action = _literal_scalar(keywords["action"]) if "action" in keywords else None
    if action in {"store_true", "store_false"}:
        return "boolean"
    if "default" in keywords:
        default = _literal_scalar(keywords["default"])
        if type(default) is bool:
            return "boolean"
        if type(default) is int:
            return "integer"
        if type(default) is float:
            return "number"
        if type(default) is str:
            return "string"
    return "string"


def _derive_arguments(bundle_root: Path) -> list[dict[str, Any]]:
    module = _module_from_entrypoint(bundle_root / SIMULATION_ENTRYPOINT)
    if not module:
        return []
    runner = bundle_root.joinpath(*module.split(".")).with_suffix(".py")
    try:
        resolved = runner.resolve(strict=True)
        resolved.relative_to(bundle_root.resolve(strict=True))
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return []
        tree = ast.parse(resolved.read_text(encoding="utf-8"), filename=str(resolved))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return []

    arguments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        flags = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and _ARGUMENT_FLAG_RE.fullmatch(arg.value)
        ]
        if not flags:
            continue
        flag = next((candidate for candidate in flags if candidate.startswith("--")), flags[0])
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        dest = _literal_scalar(keywords["dest"]) if "dest" in keywords else None
        name = dest if isinstance(dest, str) else flag[2:].replace("-", "_")
        if not _ARGUMENT_NAME_RE.fullmatch(name) or name in seen:
            continue
        value_type = _argument_type_from_ast(node, keywords)
        if value_type is None:
            continue
        item: dict[str, Any] = {
            "name": name,
            "flag": flag,
            "type": value_type,
            "required": bool(_literal_scalar(keywords["required"])) if "required" in keywords else False,
        }
        action = _literal_scalar(keywords["action"]) if "action" in keywords else None
        if action in {"store_true", "store_false"}:
            item["action"] = action
            if "default" not in keywords:
                item["default"] = action == "store_false"
        if "default" in keywords:
            default = _literal_scalar(keywords["default"])
            if default is not None:
                item["default"] = default
        if "help" in keywords:
            help_text = _literal_scalar(keywords["help"])
            if isinstance(help_text, str):
                item["description"] = help_text[:_MAX_DESCRIPTION_BYTES]
        if "choices" in keywords:
            try:
                choices = ast.literal_eval(keywords["choices"])
            except (ValueError, TypeError):
                choices = None
            if isinstance(choices, (list, tuple)) and len(choices) <= 128:
                if all(type(value) in (str, bool, int, float) for value in choices):
                    item["choices"] = list(choices)
        arguments.append(item)
        seen.add(name)
        if len(arguments) >= _MAX_ARGUMENTS:
            break
    return arguments


def _derive_result_files(bundle_root: Path) -> list[str]:
    """Recognize the generated runner's explicit result contract statically.

    The manifest must not promise a file merely because a conventional name
    exists.  The summary contract requires its known writer and reachable call;
    the event-trace contract requires the known generated helper to be attached
    after Coordinator construction and before initialization.  Existing or
    user-authored runners advertise only the contracts they satisfy.  No
    generated module is imported or executed here.
    """

    module = _module_from_entrypoint(bundle_root / SIMULATION_ENTRYPOINT)
    if not module:
        return []
    runner = bundle_root.joinpath(*module.split(".")).with_suffix(".py")
    try:
        resolved = runner.resolve(strict=True)
        resolved.relative_to(bundle_root.resolve(strict=True))
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return []
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return []

    return list(declared_result_files(source, filename=str(resolved)))


def _runner_source(bundle_root: Path) -> tuple[str, str] | None:
    """Read the runner module named by run.py, bounded by the same checks."""

    module = _module_from_entrypoint(bundle_root / SIMULATION_ENTRYPOINT)
    if not module:
        return None
    runner = bundle_root.joinpath(*module.split(".")).with_suffix(".py")
    try:
        resolved = runner.resolve(strict=True)
        resolved.relative_to(bundle_root.resolve(strict=True))
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        return resolved.read_text(encoding="utf-8"), str(resolved)
    except (OSError, UnicodeError, ValueError):
        return None


def _derive_metrics(bundle_root: Path) -> dict[str, Any] | None:
    """Build the optional v2 metrics block from static runner declarations.

    Names come from the runner's own summary contract (an explicit
    ``OPTPILOT_METRICS`` literal, or the literal keys the runner passes to
    ``write_simulation_summary``). The objective is the first declared metric
    that carries a direction. Nothing is imported or executed.
    """

    read = _runner_source(bundle_root)
    if read is None:
        return None
    source, filename = read
    declarations = declared_metrics(source, filename=filename)
    if not declarations:
        return None
    block: dict[str, Any] = {
        "keys": [entry["name"] for entry in declarations]
    }
    descriptions = {
        entry["name"]: entry["description"]
        for entry in declarations
        if entry.get("description")
    }
    if descriptions:
        block["descriptions"] = descriptions
    for entry in declarations:
        if entry.get("direction"):
            block["objective"] = {
                "metric": entry["name"],
                "direction": entry["direction"],
            }
            break
    return block


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return digest.rstrip(b"=").decode("ascii")


def _wheel_entry(path: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(path)
    entry.create_system = 3
    entry.date_time = (1980, 1, 1, 0, 0, 0)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    return entry


def _xdevs_notice_payload() -> bytes:
    return (
        "# Third-party software\n\n"
        f"This generated simulator includes xDEVS {XDEVS_VERSION} as an unmodified "
        "pure-Python wheel so it can be prepared offline. xDEVS is licensed under "
        "the GNU General Public License, version 3. The wheel contains its Python "
        "source and complete license at "
        f"`xdevs-{XDEVS_VERSION}.dist-info/LICENSE.txt`. xDEVS project: "
        "https://github.com/iscar-ucm/xdevs.py\n"
    ).encode("utf-8")


def _installed_xdevs_wheel() -> tuple[bytes, bytes]:
    """Build one deterministic pure-Python wheel from installed xDEVS source.

    The interface runtime already installs xDEVS to validate generated models.
    Repacking that exact installed distribution keeps generated bundles
    offline-runnable without contacting a package index, while preserving its
    Python source, metadata, and complete GPLv3 license text.
    """

    global _XDEVS_WHEEL_CACHE
    with _PORTABILITY_LOCK:
        if _XDEVS_WHEEL_CACHE is not None:
            return _XDEVS_WHEEL_CACHE
        try:
            distribution = importlib.metadata.distribution("xdevs")
        except importlib.metadata.PackageNotFoundError as exc:
            raise SimulationBundleError(
                "The DEVS Generator runtime is missing xdevs 3.0.0."
            ) from exc
        if distribution.version != XDEVS_VERSION:
            raise SimulationBundleError(
                f"The DEVS Generator requires xdevs {XDEVS_VERSION}, but "
                f"{distribution.version} is installed."
            )
        files = distribution.files
        if files is None:
            raise SimulationBundleError("The installed xdevs distribution has no file inventory.")
        dist_info = f"xdevs-{XDEVS_VERSION}.dist-info"
        payloads: dict[str, bytes] = {}
        for package_path in files:
            relative = PurePosixPath(str(package_path))
            if (
                relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.parts[0] not in {"xdevs", dist_info}
                or relative.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}
                or "__pycache__" in relative.parts
                or relative.suffix in {".pyc", ".pyo"}
            ):
                continue
            candidate = Path(distribution.locate_file(package_path))
            try:
                metadata = candidate.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    continue
                payload = candidate.read_bytes()
            except OSError as exc:
                raise SimulationBundleError(
                    f"Cannot read installed xdevs source file {relative.as_posix()!r}: {exc}"
                ) from exc
            payloads[relative.as_posix()] = payload
        required = {
            "xdevs/__init__.py",
            "xdevs/models.py",
            "xdevs/sim.py",
            f"{dist_info}/METADATA",
            f"{dist_info}/WHEEL",
            f"{dist_info}/LICENSE.txt",
        }
        missing = sorted(required - set(payloads))
        if missing:
            raise SimulationBundleError(
                "The installed xdevs distribution is incomplete: " + ", ".join(missing)
            )
        wheel_metadata = payloads[f"{dist_info}/WHEEL"].decode(
            "utf-8", errors="strict"
        )
        if "Root-Is-Purelib: true" not in wheel_metadata or "Tag: py3-none-any" not in wheel_metadata:
            raise SimulationBundleError(
                "The installed xdevs distribution is not the expected pure-Python wheel."
            )
        payloads[f"{dist_info}/THIRD_PARTY_NOTICES.md"] = _xdevs_notice_payload()
        record_path = f"{dist_info}/RECORD"
        record_buffer = io.StringIO(newline="")
        writer = csv.writer(record_buffer, lineterminator="\n")
        for relative in sorted(payloads):
            payload = payloads[relative]
            writer.writerow((relative, f"sha256={_record_digest(payload)}", len(payload)))
        writer.writerow((record_path, "", ""))
        payloads[record_path] = record_buffer.getvalue().encode("utf-8")

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            for relative in sorted(payloads):
                archive.writestr(_wheel_entry(relative), payloads[relative])
        wheel = output.getvalue()
        license_payload = payloads[f"{dist_info}/LICENSE.txt"]
        _XDEVS_WHEEL_CACHE = (wheel, license_payload)
        return _XDEVS_WHEEL_CACHE


def _atomic_write(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise SimulationBundleError(
            f"Generated dependency parent is not a regular directory: {path.parent.name}."
        )
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SimulationBundleError(f"Generated dependency path is not a regular file: {path.name}.")
        if path.read_bytes() == payload:
            return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _package_xdevs_runtime(bundle_root: Path) -> None:
    wheel, license_payload = _installed_xdevs_wheel()
    wheel_digest = hashlib.sha256(wheel).hexdigest()
    notice = _xdevs_notice_payload()
    lock = (
        f"vendor/{XDEVS_WHEEL_NAME} --hash=sha256:{wheel_digest}\n"
    ).encode("utf-8")
    with _PORTABILITY_LOCK:
        runtime_root = bundle_root / XDEVS_RUNTIME_DIRECTORY
        directories = (runtime_root, runtime_root / "vendor", runtime_root / "licenses")
        for directory in directories:
            if directory.exists() or directory.is_symlink():
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise SimulationBundleError(
                        f"{directory.relative_to(bundle_root).as_posix()} must be a regular directory."
                    )
            else:
                directory.mkdir(mode=0o700)
        _atomic_write(bundle_root / XDEVS_WHEEL_PATH, wheel)
        _atomic_write(bundle_root / XDEVS_LICENSE_PATH, license_payload)
        _atomic_write(bundle_root / XDEVS_NOTICE_PATH, notice)
        _atomic_write(bundle_root / XDEVS_REQUIREMENTS_LOCK, lock)


def _portable_runtime_declaration() -> dict[str, str]:
    return {"requirements_lock": XDEVS_REQUIREMENTS_LOCK}


def _atomic_write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise SimulationManifestError(f"{SIMULATION_MANIFEST} exceeds 64 KiB.")
    _atomic_write(path, payload)


def ensure_simulation_manifest(bundle_root: str | Path) -> Path:
    """Create a conservative manifest for an existing generated bundle.

    Only Python AST is inspected.  No generated module is imported or run.  If
    the outer ``run.py`` names a constant ``SIM_MODULE``, simple argparse
    declarations in that module are exposed.  Anything dynamic is ignored and
    therefore cannot become an executable argument accidentally.
    """

    root = Path(bundle_root).resolve(strict=True)
    if not root.is_dir():
        raise SimulationBundleError("Simulator bundle root must be a directory.")
    entrypoint = root / SIMULATION_ENTRYPOINT
    try:
        entry_metadata = entrypoint.lstat()
    except FileNotFoundError as exc:
        raise SimulationBundleError(f"Simulator bundle is missing {SIMULATION_ENTRYPOINT}.") from exc
    if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISREG(entry_metadata.st_mode):
        raise SimulationBundleError(f"{SIMULATION_ENTRYPOINT} must be a regular file, not a link.")

    manifest_path = root / SIMULATION_MANIFEST
    with _PORTABILITY_LOCK:
        if manifest_path.exists() or manifest_path.is_symlink():
            metadata = manifest_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SimulationManifestError(f"{SIMULATION_MANIFEST} must be a regular file.")
            try:
                manifest = _strict_json_loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                raise SimulationManifestError(
                    f"Cannot read {SIMULATION_MANIFEST}: {exc}"
                ) from exc
            _load_manifest(
                manifest_path,
                maximum_timeout_seconds=24 * 60 * 60,
                validate_runtime_files=False,
            )
            if not isinstance(manifest, dict):  # guarded by _load_manifest
                raise SimulationManifestError(
                    f"{SIMULATION_MANIFEST} must contain a JSON object."
                )
            derived_results = _derive_result_files(root)
            if TRACE_FILE in derived_results:
                # The exact generated trace attachment is a static provenance
                # marker for a Generator-owned runner. Repairs may replace that
                # runner while leaving its earlier manifest in place, so add any
                # newly satisfied standard contracts without disturbing custom
                # result declarations.
                existing_results = manifest.get("result_files", [])
                for result in derived_results:
                    if result not in existing_results:
                        existing_results.append(result)
                manifest["result_files"] = existing_results
            if "metrics" not in manifest:
                # A repaired or regenerated runner may newly declare its
                # metric names; adopt them (and the v2 grammar that carries
                # them) without touching an authored metrics declaration.
                derived_metrics = _derive_metrics(root)
                if derived_metrics is not None:
                    manifest["metrics"] = derived_metrics
                    manifest["schema_version"] = SIMULATION_SCHEMA
        else:
            manifest = {
                "schema_version": SIMULATION_SCHEMA,
                "entrypoint": SIMULATION_ENTRYPOINT,
                "timeout_seconds": 30,
                "arguments": _derive_arguments(root),
                "result_files": _derive_result_files(root),
            }
            derived_metrics = _derive_metrics(root)
            if derived_metrics is not None:
                manifest["metrics"] = derived_metrics
        declared_runtime = manifest.get(PYTHON_RUNTIME_FIELD)
        expected_runtime = _portable_runtime_declaration()
        if declared_runtime not in (None, expected_runtime):
            raise SimulationManifestError(
                f"{PYTHON_RUNTIME_FIELD} must declare the generated "
                f"{XDEVS_REQUIREMENTS_LOCK} lock."
            )
        _package_xdevs_runtime(root)
        manifest[PYTHON_RUNTIME_FIELD] = expected_runtime
        _atomic_write_manifest(manifest_path, manifest)
        _load_manifest(
            manifest_path,
            maximum_timeout_seconds=24 * 60 * 60,
        )
    return manifest_path


def simulation_metadata(
    bundle_root: str | Path,
    *,
    maximum_timeout_seconds: int = 24 * 60 * 60,
) -> dict[str, Any]:
    """Return validated, JSON-serializable metadata for a Run form.

    The API-facing name is ``parameters`` even though the portable on-disk
    manifest calls them ``arguments``.  Callers therefore do not need to use
    this module's private parser or expose unvalidated manifest content.
    """

    path = ensure_simulation_manifest(bundle_root)
    parsed = _load_manifest(path, maximum_timeout_seconds=maximum_timeout_seconds)
    document = _strict_json_loads(path.read_text(encoding="utf-8"))
    parameters = copy.deepcopy(document.get("arguments", []))
    metadata = {
        "schema_version": parsed.schema_version,
        "entrypoint": SIMULATION_ENTRYPOINT,
        "timeout_seconds": parsed.timeout_seconds,
        "parameters": parameters,
        "result_files": list(parsed.result_files),
        PYTHON_RUNTIME_FIELD: {"requirements_lock": parsed.requirements_lock},
    }
    if parsed.metrics is not None:
        metadata["metrics"] = copy.deepcopy(parsed.metrics)
    return metadata


def _read_behavior_text(path: Path, maximum_bytes: int) -> str:
    """Read one already-retained regular file through a small fixed bound."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"Behavior evidence is unavailable: {path.name}.") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > maximum_bytes
    ):
        raise ValueError(f"Behavior evidence is not a bounded regular file: {path.name}.")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Behavior evidence is not readable UTF-8: {path.name}.") from exc


def _behavior_source_files(bundle_root: Path) -> dict[str, str]:
    """Load only source/structure files needed by the local graph parser."""

    json_names = {
        "approved_structure_plan.json",
        "derived_plan_artifact.json",
        "global_plan.json",
        "plan_artifact.json",
        "system_model_info.json",
        "system_registry.json",
        "system_registry_v1_post_build.json",
    }
    files: dict[str, str] = {}
    total = 0
    for current_root, dirs, names in os.walk(bundle_root, followlinks=False):
        current = Path(current_root)
        dirs[:] = [
            name
            for name in dirs
            if name not in {"__pycache__", "runtime_dependencies"}
            and not (current / name).is_symlink()
        ]
        for name in names:
            if not (name.endswith(".py") or name in json_names):
                continue
            path = current / name
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _BEHAVIOR_SOURCE_FILE_MAX_BYTES
            ):
                continue
            total += metadata.st_size
            if total > _BEHAVIOR_SOURCE_TOTAL_BYTES:
                raise ValueError("Generated source is too large for a behavior check.")
            try:
                files[path.relative_to(bundle_root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeError):
                continue
    return files


def _local_behavior_graph(
    bundle_root: Path,
) -> tuple[set[str], dict[str, set[str]], bool]:
    """Return output-capable atomic paths and local coupling reachability.

    This deliberately calls only the deterministic parser.  If the generated
    Python uses structure the parser cannot account for, ``complete`` is false
    and the behavior gate becomes inconclusive instead of asking for a repair.
    """

    from .graph_parser import (
        detect_root_model,
        infer_model_info,
        local_parse_covers_visible_structure,
        local_parse_xdevs_structure,
        ports_for_meta,
        resolve_model_path,
    )

    files = _behavior_source_files(bundle_root)
    model_info = infer_model_info(files)
    root_class = detect_root_model(model_info)
    if not root_class:
        return set(), {}, False

    output_atomics: set[str] = set()
    edges: dict[str, set[str]] = {}
    complete = True
    visited: set[tuple[str, str]] = set()

    def add_edge(source: str, target: str) -> None:
        if source != target:
            edges.setdefault(source, set()).add(target)

    def visit(class_name: str, instance_path: str, depth: int) -> None:
        nonlocal complete
        if depth > 16 or len(visited) >= _BEHAVIOR_MAX_GRAPH_NODES:
            complete = False
            return
        key = (class_name, instance_path)
        if key in visited:
            return
        visited.add(key)
        meta = model_info.get(class_name)
        if not isinstance(meta, dict):
            complete = False
            return
        if meta.get("model_type") == "atomic":
            if ports_for_meta(meta).get("outputs"):
                output_atomics.add(instance_path)
            return

        source_path = resolve_model_path(files, str(meta.get("path") or ""))
        source = files.get(source_path, "")
        parsed = local_parse_xdevs_structure(class_name, source) if source else None
        visible_structure = bool(
            source
            and (
                "self.add_component(" in source
                or "self.add_coupling(" in source
            )
        )
        if not parsed:
            if visible_structure:
                complete = False
            return
        if not local_parse_covers_visible_structure(class_name, source, parsed):
            complete = False
            return

        children: dict[str, str] = {}
        parsed_components = parsed.get("components", [])
        for component in parsed_components:
            if not isinstance(component, dict):
                complete = False
                continue
            child_name = str(component.get("name") or "")
            child_class = str(component.get("className") or "")
            if not child_name or not child_class:
                complete = False
                continue
            children[child_name] = child_class
            visit(child_class, f"{instance_path}.{child_name}", depth + 1)

        # Some generated coupled models retrieve a child from
        # ``self.components[index]`` before wiring it.  The graph parser keeps
        # that local alias as the coupling endpoint, so resolve the bounded,
        # literal-index form against the already parsed component order.
        endpoint_aliases: dict[str, str] = {}
        for match in re.finditer(
            r"^\s*(\w+)\s*=\s*self\.components\[(\d{1,3})\]\s*(?:#.*)?$",
            source,
            re.MULTILINE,
        ):
            index = int(match.group(2))
            if index >= len(parsed_components):
                continue
            component = parsed_components[index]
            if isinstance(component, dict) and component.get("name"):
                endpoint_aliases[match.group(1)] = str(component["name"])

        for coupling in parsed.get("couplings", []):
            if not isinstance(coupling, dict):
                complete = False
                continue
            source_model = str(coupling.get("source_model") or "")
            target_model = str(coupling.get("target_model") or "")
            source_model = endpoint_aliases.get(source_model, source_model)
            target_model = endpoint_aliases.get(target_model, target_model)
            if not source_model or not target_model:
                complete = False
                continue
            source_node = (
                instance_path
                if source_model == "self"
                else f"{instance_path}.{source_model}"
            )
            target_node = (
                instance_path
                if target_model == "self"
                else f"{instance_path}.{target_model}"
            )
            if source_model != "self" and source_model not in children:
                complete = False
                continue
            if target_model != "self" and target_model not in children:
                complete = False
                continue
            add_edge(source_node, target_node)

    visit(root_class, root_class, 0)
    return output_atomics, edges, complete


def _match_trace_component(
    component: str,
    atomic_paths: set[str],
) -> str | None:
    """Match runtime instance names while allowing a different root label."""

    if component in atomic_paths:
        return component
    trace_parts = tuple(part for part in component.split(".") if part)
    if not trace_parts:
        return None
    candidates = [
        path
        for path in atomic_paths
        if tuple(path.split(".")[1:]) == trace_parts[1:]
    ]
    if len(candidates) == 1:
        return candidates[0]
    leaf_candidates = [
        path for path in atomic_paths if path.rsplit(".", 1)[-1] == trace_parts[-1]
    ]
    return leaf_candidates[0] if len(leaf_candidates) == 1 else None


def _bounded_behavior_component_list(components: set[str]) -> str:
    """Format a few structure-owned names for user and repair diagnostics."""

    ordered = sorted(components)
    shown = [name[:96] for name in ordered[:3]]
    description = ", ".join(shown)
    if len(ordered) > len(shown):
        description += f" (+{len(ordered) - len(shown)} more)"
    return description[:360]


def assess_behavior_smoke(
    bundle_root: str | Path,
    result_root: str | Path,
) -> BehaviorSmokeAssessment:
    """Assess likely main-flow progress without another model call or run.

    The check reuses ``summary.json`` and ``event_trace.jsonl`` from the exact
    smoke execution.  It is not a domain-correctness oracle.  It only detects
    the strong, generic deadlock signal where repeated upstream emissions never
    produce an event from any statically connected, output-capable downstream
    atomic component.
    """

    bundle = Path(bundle_root).resolve()
    results = Path(result_root).resolve()
    summary_path = results / "summary.json"
    trace_path = results / TRACE_FILE
    if not summary_path.is_file() or not trace_path.is_file():
        return BehaviorSmokeAssessment(
            "inconclusive",
            "Behavior evidence was not declared by this simulator.",
        )

    try:
        summary = json.loads(
            _read_behavior_text(summary_path, _BEHAVIOR_SUMMARY_MAX_BYTES)
        )
        trace_lines = _read_behavior_text(
            trace_path, _BEHAVIOR_TRACE_MAX_BYTES
        ).splitlines()
    except (ValueError, json.JSONDecodeError):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "Behavior evidence could not be read safely.",
        )
    run_summary = summary.get("run") if isinstance(summary, dict) else None
    simulated_time = (
        run_summary.get("simulated_time")
        if isinstance(run_summary, dict)
        else None
    )
    if (
        not isinstance(run_summary, dict)
        or run_summary.get("completed") is not True
        or type(simulated_time) not in (int, float)
        or not math.isfinite(float(simulated_time))
        or float(simulated_time) <= 0
    ):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The result does not declare a completed positive-time scenario.",
        )

    events_by_component: dict[str, int] = {}
    observation_times: list[float] = []
    observation_rows = 0
    recorded_state_rows = 0
    observation_timing_complete = True
    footer: dict[str, Any] | None = None
    try:
        for raw_line in trace_lines:
            if not raw_line.strip():
                continue
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                raise ValueError("Trace row must be an object.")
            record_type = row.get("record_type")
            if record_type in {"event", "state"}:
                observation_rows += 1
                raw_time = row.get("simulation_time", row.get("time"))
                if (
                    type(raw_time) in (int, float)
                    and math.isfinite(float(raw_time))
                    and float(raw_time) >= 0
                ):
                    observation_times.append(float(raw_time))
                else:
                    observation_timing_complete = False
            if record_type == "event":
                component = row.get("component")
                if isinstance(component, str) and component:
                    events_by_component[component] = (
                        events_by_component.get(component, 0) + 1
                    )
            elif record_type == "state":
                recorded_state_rows += 1
            elif record_type == "summary":
                footer = row
    except (ValueError, json.JSONDecodeError):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The event trace is incomplete or malformed.",
        )
    if not isinstance(footer, dict):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The event trace did not finish with a summary.",
        )
    recorded_events = footer.get("recorded_events")
    recorded_states = footer.get("recorded_states")
    if (
        type(recorded_events) is not int
        or recorded_events < 0
        or footer.get("dropped_events") != 0
        or sum(events_by_component.values()) != recorded_events
    ):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The bounded event trace is incomplete, so behavior was not inferred.",
            tuple(sorted(events_by_component)),
            recorded_events=(
                recorded_events if type(recorded_events) is int else 0
            ),
        )
    if not events_by_component:
        return BehaviorSmokeAssessment(
            "not_applicable",
            "This scenario produced no traceable output events.",
        )

    try:
        output_atomics, edges, graph_complete = _local_behavior_graph(bundle)
    except (OSError, RuntimeError, ValueError):
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The generated structure could not be checked deterministically.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )
    if graph_complete and len(output_atomics) <= 1:
        return BehaviorSmokeAssessment(
            "not_applicable",
            "The model has no multi-stage output path to validate.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )
    active: set[str] = set()
    counts_by_node: dict[str, int] = {}
    for component, count in events_by_component.items():
        matched = _match_trace_component(component, output_atomics)
        if matched is not None:
            active.add(matched)
            counts_by_node[matched] = counts_by_node.get(matched, 0) + count
    if not active:
        return BehaviorSmokeAssessment(
            "inconclusive",
            "Trace component names could not be matched to the generated structure.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )

    expected_downstream: set[str] = set()
    origin_event_count = 0
    downstream_observed = False
    for origin in active:
        reachable: set[str] = set()
        pending = list(edges.get(origin, ()))
        while pending and len(reachable) <= _BEHAVIOR_MAX_GRAPH_NODES:
            node = pending.pop()
            if node in reachable:
                continue
            reachable.add(node)
            pending.extend(edges.get(node, ()))
        downstream = (reachable & output_atomics) - {origin}
        if downstream:
            expected_downstream.update(downstream)
            origin_event_count += counts_by_node.get(origin, 0)
            if downstream & active:
                downstream_observed = True

    complete_timing_evidence = (
        observation_timing_complete
        and observation_rows == len(observation_times)
        and type(recorded_states) is int
        and recorded_states >= 0
        and recorded_state_rows == recorded_states
        and footer.get("dropped_states") == 0
        and footer.get("dropped_records", 0) == 0
        and footer.get("truncated") is not True
    )
    if (
        len(output_atomics) > 1
        and complete_timing_evidence
        and observation_times
        and all(simulation_time == 0.0 for simulation_time in observation_times)
    ):
        return BehaviorSmokeAssessment(
            "stalled",
            (
                "The simulation exited successfully for a positive-time "
                "scenario, but every recorded event and state observation "
                "occurred at simulation time 0. The model became quiescent "
                "before the scenario began. Ensure an autonomous source "
                "schedules its first event, or have the runner inject the "
                "declared deterministic startup input."
            ),
            tuple(sorted(events_by_component)),
            tuple(sorted(expected_downstream)),
            recorded_events,
        )

    if not graph_complete:
        return BehaviorSmokeAssessment(
            "inconclusive",
            "The generated structure uses dynamic topology, so behavior was not inferred.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )

    if len(output_atomics) <= 1:
        return BehaviorSmokeAssessment(
            "not_applicable",
            "The model has no multi-stage output path to validate.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )

    if not expected_downstream:
        return BehaviorSmokeAssessment(
            "not_applicable",
            "Observed output leads only to terminal sinks or model boundaries.",
            tuple(sorted(events_by_component)),
            recorded_events=recorded_events,
        )
    if downstream_observed or origin_event_count < _BEHAVIOR_MIN_ORIGIN_EVENTS:
        return BehaviorSmokeAssessment(
            "passed",
            "The existing smoke trace reached a connected downstream component.",
            tuple(sorted(events_by_component)),
            tuple(sorted(expected_downstream)),
            recorded_events,
        )
    return BehaviorSmokeAssessment(
        "stalled",
        (
            "The simulation exited successfully, but repeated upstream events "
            "never produced output from a connected downstream component. "
            "The expected main behavior may be stalled. Observed upstream: "
            f"{_bounded_behavior_component_list(active)}. Expected downstream: "
            f"{_bounded_behavior_component_list(expected_downstream)}."
        ),
        tuple(sorted(events_by_component)),
        tuple(sorted(expected_downstream)),
        recorded_events,
    )


def _validate_scalar(value: Any, spec: _ArgumentSpec) -> Any:
    if spec.value_type == "string":
        if type(value) is not str:
            raise SimulationManifestError(f"Argument {spec.name!r} must be a string.")
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_ARGUMENT_STRING_BYTES or any(ord(character) < 32 for character in value):
            raise SimulationManifestError(f"Argument {spec.name!r} contains unsupported text.")
    elif spec.value_type == "integer":
        if type(value) is not int:
            raise SimulationManifestError(f"Argument {spec.name!r} must be an integer.")
    elif spec.value_type == "number":
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise SimulationManifestError(f"Argument {spec.name!r} must be a finite number.")
    elif spec.value_type == "boolean":
        if type(value) is not bool:
            raise SimulationManifestError(f"Argument {spec.name!r} must be a boolean.")
    else:  # pragma: no cover - guarded while loading the manifest
        raise SimulationManifestError(f"Unsupported argument type {spec.value_type!r}.")

    if spec.minimum is not None and value < spec.minimum:
        raise SimulationManifestError(f"Argument {spec.name!r} is below its minimum.")
    if spec.maximum is not None and value > spec.maximum:
        raise SimulationManifestError(f"Argument {spec.name!r} is above its maximum.")
    if spec.choices and value not in spec.choices:
        raise SimulationManifestError(f"Argument {spec.name!r} is not one of its allowed choices.")
    return value


def _load_manifest(
    path: Path,
    *,
    maximum_timeout_seconds: int,
    validate_runtime_files: bool = True,
) -> _Manifest:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SimulationManifestError(f"{SIMULATION_MANIFEST} must be a regular file.")
        if metadata.st_size > _MAX_MANIFEST_BYTES:
            raise SimulationManifestError(f"{SIMULATION_MANIFEST} exceeds 64 KiB.")
        document = _strict_json_loads(path.read_text(encoding="utf-8"))
    except SimulationManifestError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SimulationManifestError(f"Cannot read {SIMULATION_MANIFEST}: {exc}") from exc
    if not isinstance(document, dict):
        raise SimulationManifestError(f"{SIMULATION_MANIFEST} must contain a JSON object.")
    allowed_top = {
        "schema_version",
        "entrypoint",
        "timeout_seconds",
        "arguments",
        "result_files",
        "metrics",
        PYTHON_RUNTIME_FIELD,
    }
    unknown = set(document) - allowed_top
    if unknown:
        raise SimulationManifestError(f"Unknown manifest fields: {', '.join(sorted(unknown))}.")
    schema_version = document.get("schema_version")
    if schema_version not in ACCEPTED_SIMULATION_SCHEMAS:
        raise SimulationManifestError(
            "schema_version must be one of "
            + ", ".join(sorted(ACCEPTED_SIMULATION_SCHEMAS))
            + "."
        )
    manifest_metrics = _validated_manifest_metrics(document, schema_version)
    if document.get("entrypoint") != SIMULATION_ENTRYPOINT:
        raise SimulationManifestError(f"entrypoint must be exactly {SIMULATION_ENTRYPOINT!r}.")
    raw_runtime = document.get(PYTHON_RUNTIME_FIELD)
    if raw_runtime is None and not validate_runtime_files:
        requirements_lock = ""
    else:
        if not isinstance(raw_runtime, dict) or set(raw_runtime) != {"requirements_lock"}:
            raise SimulationManifestError(
                f"{PYTHON_RUNTIME_FIELD} must contain only requirements_lock."
            )
        requirements_lock = _safe_relative_path(
            raw_runtime.get("requirements_lock"), label="Python requirements lock"
        )
    timeout = document.get("timeout_seconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= maximum_timeout_seconds:
        raise SimulationManifestError(
            f"timeout_seconds must be an integer from 1 to {maximum_timeout_seconds}."
        )
    raw_arguments = document.get("arguments", [])
    if not isinstance(raw_arguments, list) or len(raw_arguments) > _MAX_ARGUMENTS:
        raise SimulationManifestError(f"arguments must be an array of at most {_MAX_ARGUMENTS} items.")
    arguments: list[_ArgumentSpec] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_arguments):
        if not isinstance(raw, dict):
            raise SimulationManifestError(f"arguments[{index}] must be an object.")
        allowed = {
            "name", "flag", "type", "required", "default", "minimum", "maximum",
            "choices", "action", "description", "label",
        }
        unknown_argument = set(raw) - allowed
        if unknown_argument:
            raise SimulationManifestError(
                f"Unknown fields in arguments[{index}]: {', '.join(sorted(unknown_argument))}."
            )
        name = raw.get("name")
        if not isinstance(name, str) or not _ARGUMENT_NAME_RE.fullmatch(name) or name in seen_names:
            raise SimulationManifestError(f"arguments[{index}].name is invalid or duplicated.")
        flag = raw.get("flag", f"--{name}")
        if not isinstance(flag, str) or not _ARGUMENT_FLAG_RE.fullmatch(flag):
            raise SimulationManifestError(f"arguments[{index}].flag is invalid.")
        value_type = raw.get("type")
        if value_type not in {"string", "integer", "number", "boolean"}:
            raise SimulationManifestError(f"arguments[{index}].type is unsupported.")
        required = raw.get("required", False)
        if type(required) is not bool:
            raise SimulationManifestError(f"arguments[{index}].required must be a boolean.")
        action = raw.get("action", "value")
        if action not in {"value", "store_true", "store_false"}:
            raise SimulationManifestError(f"arguments[{index}].action is unsupported.")
        if action != "value" and value_type != "boolean":
            raise SimulationManifestError(f"arguments[{index}] flag actions require boolean type.")
        for text_key in ("description", "label"):
            text_value = raw.get(text_key)
            if text_value is not None and (
                not isinstance(text_value, str)
                or len(text_value.encode("utf-8")) > _MAX_DESCRIPTION_BYTES
            ):
                raise SimulationManifestError(f"arguments[{index}].{text_key} is invalid.")
        minimum = raw.get("minimum")
        maximum = raw.get("maximum")
        if minimum is not None and (type(minimum) not in (int, float) or not math.isfinite(float(minimum))):
            raise SimulationManifestError(f"arguments[{index}].minimum must be finite.")
        if maximum is not None and (type(maximum) not in (int, float) or not math.isfinite(float(maximum))):
            raise SimulationManifestError(f"arguments[{index}].maximum must be finite.")
        if value_type not in {"integer", "number"} and (minimum is not None or maximum is not None):
            raise SimulationManifestError(f"arguments[{index}] bounds require a numeric type.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise SimulationManifestError(f"arguments[{index}] minimum exceeds maximum.")
        choices_raw = raw.get("choices", [])
        if not isinstance(choices_raw, list) or len(choices_raw) > 128:
            raise SimulationManifestError(f"arguments[{index}].choices is invalid.")
        partial = _ArgumentSpec(
            name=name,
            flag=flag,
            value_type=value_type,
            required=required,
            has_default="default" in raw,
            default=raw.get("default"),
            minimum=minimum,
            maximum=maximum,
            choices=(),
            action=action,
        )
        choices = tuple(_validate_scalar(choice, partial) for choice in choices_raw)
        spec = replace(partial, choices=choices)
        if spec.has_default:
            _validate_scalar(spec.default, spec)
        if required and spec.has_default:
            raise SimulationManifestError(f"arguments[{index}] cannot be required and have a default.")
        arguments.append(spec)
        seen_names.add(name)

    raw_results = document.get("result_files", [])
    if not isinstance(raw_results, list) or len(raw_results) > 128:
        raise SimulationManifestError("result_files must be an array of at most 128 paths.")
    result_files: list[str] = []
    for raw_result in raw_results:
        result_files.append(_safe_relative_path(raw_result, label="result file"))
    if len(set(result_files)) != len(result_files):
        raise SimulationManifestError("result_files contains duplicate paths.")
    if validate_runtime_files:
        _validate_requirements_lock(path.parent, requirements_lock)
    return _Manifest(
        timeout,
        tuple(arguments),
        tuple(result_files),
        requirements_lock,
        schema_version=schema_version,
        metrics=manifest_metrics,
    )


_METRIC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _validated_manifest_metrics(
    document: dict[str, Any], schema_version: str
) -> dict[str, Any] | None:
    """Validate the optional v2 metrics block strictly and boundedly."""

    raw = document.get("metrics")
    if raw is None:
        return None
    if schema_version == SIMULATION_SCHEMA_V1:
        raise SimulationManifestError(
            f"metrics requires schema_version {SIMULATION_SCHEMA!r}."
        )
    if not isinstance(raw, dict) or not set(raw) <= {
        "keys",
        "objective",
        "descriptions",
    }:
        raise SimulationManifestError(
            "metrics must contain only keys, objective, and descriptions."
        )
    keys = raw.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or len(keys) > MAX_DECLARED_METRICS
        or any(
            not isinstance(key, str) or not _METRIC_KEY_RE.fullmatch(key)
            for key in keys
        )
        or len(set(keys)) != len(keys)
    ):
        raise SimulationManifestError(
            "metrics.keys must be a non-empty array of unique metric names "
            f"(at most {MAX_DECLARED_METRICS})."
        )
    validated: dict[str, Any] = {"keys": list(keys)}
    objective = raw.get("objective")
    if objective is not None:
        if (
            not isinstance(objective, dict)
            or set(objective) != {"metric", "direction"}
            or objective.get("metric") not in keys
            or objective.get("direction") not in METRIC_DIRECTIONS
        ):
            raise SimulationManifestError(
                "metrics.objective must name a declared metric with direction "
                + " or ".join(METRIC_DIRECTIONS)
                + "."
            )
        validated["objective"] = dict(objective)
    descriptions = raw.get("descriptions")
    if descriptions is not None:
        if (
            not isinstance(descriptions, dict)
            or not set(descriptions) <= set(keys)
            or any(
                not isinstance(text, str)
                or not text
                or len(text) > MAX_METRIC_DESCRIPTION_CHARS
                for text in descriptions.values()
            )
        ):
            raise SimulationManifestError(
                "metrics.descriptions must map declared metric names to "
                f"non-empty strings of at most {MAX_METRIC_DESCRIPTION_CHARS} "
                "characters."
            )
        validated["descriptions"] = dict(descriptions)
    return validated


def _render_arguments(manifest: _Manifest, supplied: Mapping[str, Any] | None) -> tuple[dict[str, Any], tuple[str, ...]]:
    supplied = {} if supplied is None else supplied
    if not isinstance(supplied, Mapping) or any(not isinstance(key, str) for key in supplied):
        raise SimulationManifestError("Simulation arguments must be a JSON object.")
    known = {argument.name for argument in manifest.arguments}
    unknown = set(supplied) - known
    if unknown:
        raise SimulationManifestError(f"Unknown simulation arguments: {', '.join(sorted(unknown))}.")
    resolved: dict[str, Any] = {}
    argv: list[str] = []
    for spec in manifest.arguments:
        if spec.name in supplied:
            value = _validate_scalar(supplied[spec.name], spec)
        elif spec.has_default:
            value = spec.default
        elif spec.required:
            raise SimulationManifestError(f"Required simulation argument {spec.name!r} is missing.")
        else:
            continue
        resolved[spec.name] = value
        if spec.action == "store_true":
            if value:
                argv.append(spec.flag)
        elif spec.action == "store_false":
            if not value:
                argv.append(spec.flag)
        else:
            if type(value) is bool:
                rendered = "true" if value else "false"
            elif type(value) is float:
                rendered = format(value, ".17g")
            else:
                rendered = str(value)
            argv.extend((spec.flag, rendered))
    if sum(len(part.encode("utf-8")) + 1 for part in argv) > _MAX_ARGUMENT_VECTOR_BYTES:
        raise SimulationManifestError("Rendered simulation arguments exceed 32 KiB.")
    return resolved, tuple(argv)


def _safe_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SimulationManifestError(f"Invalid {label} path.")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise SimulationManifestError(f"Invalid {label} path: {value!r}.")
    if len(value.encode("utf-8")) > 1024:
        raise SimulationManifestError(f"{label.title()} path is too long.")
    return value


def _validate_requirements_lock(bundle_root: Path, relative_lock: str) -> None:
    lock_path = bundle_root.joinpath(*PurePosixPath(relative_lock).parts)
    try:
        lock_metadata = lock_path.lstat()
    except FileNotFoundError as exc:
        raise SimulationManifestError(
            f"Declared Python requirements lock is missing: {relative_lock}."
        ) from exc
    if (
        stat.S_ISLNK(lock_metadata.st_mode)
        or not stat.S_ISREG(lock_metadata.st_mode)
        or lock_metadata.st_size > _MAX_LOCK_BYTES
    ):
        raise SimulationManifestError(
            "Declared Python requirements lock must be a regular UTF-8 file no larger than 1 MiB."
        )
    try:
        text = lock_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise SimulationManifestError(
            f"Cannot read declared Python requirements lock: {exc}"
        ) from exc
    wheel_count = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line or line.endswith("\\"):
            raise SimulationManifestError(
                f"Python requirements lock line {line_number} uses unsupported syntax."
            )
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as exc:
            raise SimulationManifestError(
                f"Python requirements lock line {line_number} is invalid."
            ) from exc
        if len(tokens) != 2 or not tokens[1].startswith("--hash=sha256:"):
            raise SimulationManifestError(
                f"Python requirements lock line {line_number} must contain one wheel and one sha256 hash."
            )
        wheel_relative = _safe_relative_path(tokens[0], label="locked wheel")
        expected_digest = tokens[1].removeprefix("--hash=sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise SimulationManifestError(
                f"Python requirements lock line {line_number} has an invalid sha256 hash."
            )
        if not _PURE_PYTHON_WHEEL_RE.fullmatch(PurePosixPath(wheel_relative).name):
            raise SimulationManifestError(
                f"Locked dependency is not a supported pure-Python wheel: {wheel_relative}."
            )
        wheel_path = lock_path.parent.joinpath(*PurePosixPath(wheel_relative).parts)
        try:
            wheel_metadata = wheel_path.lstat()
        except FileNotFoundError as exc:
            raise SimulationManifestError(
                f"Locked Python wheel is missing: {wheel_relative}."
            ) from exc
        if stat.S_ISLNK(wheel_metadata.st_mode) or not stat.S_ISREG(
            wheel_metadata.st_mode
        ):
            raise SimulationManifestError(
                f"Locked Python wheel must be a regular file: {wheel_relative}."
            )
        try:
            actual_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise SimulationManifestError(
                f"Cannot read locked Python wheel {wheel_relative}: {exc}"
            ) from exc
        if actual_digest != expected_digest:
            raise SimulationManifestError(
                f"Locked Python wheel hash does not match: {wheel_relative}."
            )
        wheel_count += 1
        if wheel_count > 256:
            raise SimulationManifestError(
                "Python requirements lock contains more than 256 wheels."
            )
    if wheel_count == 0:
        raise SimulationManifestError("Python requirements lock contains no wheels.")


def _inventory_tree(root: Path, *, max_entries: int, max_bytes: int, max_file_bytes: int) -> tuple[_SourceEntry, ...]:
    entries: list[_SourceEntry] = []
    total_bytes = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > 64:
            raise SimulationBundleError("Simulator bundle exceeds the maximum directory depth.")
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SimulationBundleError(f"Cannot inspect simulator bundle: {exc}") from exc
        for child in children:
            relative = Path(child.path).relative_to(root).as_posix()
            if len(relative.encode("utf-8")) > 4096:
                raise SimulationBundleError("Simulator bundle contains an excessively long path.")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SimulationBundleError(f"Cannot inspect {relative!r}: {exc}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SimulationBundleError(f"Simulator bundle may not contain symbolic links: {relative!r}.")
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    _SourceEntry(
                        relative,
                        "directory",
                        metadata.st_size,
                        metadata.st_mode,
                        metadata.st_mtime_ns,
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                )
                stack.append((Path(child.path), depth + 1))
            elif not stat.S_ISREG(metadata.st_mode):
                raise SimulationBundleError(f"Simulator bundle contains a special file: {relative!r}.")
            else:
                if metadata.st_size > max_file_bytes:
                    raise SimulationBundleError(f"Simulator file exceeds the per-file limit: {relative!r}.")
                total_bytes += metadata.st_size
                if total_bytes > max_bytes:
                    raise SimulationBundleError("Simulator bundle exceeds the total byte limit.")
                entries.append(
                    _SourceEntry(
                        relative,
                        "file",
                        metadata.st_size,
                        metadata.st_mode,
                        metadata.st_mtime_ns,
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                )
            if len(entries) > max_entries:
                raise SimulationBundleError("Simulator bundle exceeds the file-count limit.")
    return tuple(sorted(entries, key=lambda entry: entry.relative_path))


def _copy_snapshot(source: Path, destination: Path, *, max_entries: int, max_bytes: int, max_file_bytes: int) -> str:
    before = _inventory_tree(
        source,
        max_entries=max_entries,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )
    digest = hashlib.sha256()
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for entry in before:
        source_path = source.joinpath(*PurePosixPath(entry.relative_path).parts)
        destination_path = destination.joinpath(*PurePosixPath(entry.relative_path).parts)
        if entry.kind == "directory":
            metadata = source_path.lstat()
            expected = (entry.size, entry.mode, entry.modified_ns, entry.device, entry.inode)
            actual = (metadata.st_size, metadata.st_mode, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino)
            if actual != expected or not stat.S_ISDIR(metadata.st_mode):
                raise SimulationBundleError(f"Simulator directory changed while snapshotting: {entry.relative_path!r}.")
            destination_path.mkdir(parents=True, exist_ok=False, mode=0o700)
            os.chmod(destination_path, 0o700)
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(source_path, read_flags)
        try:
            opened = os.fstat(descriptor)
            identity = (opened.st_size, opened.st_mode, opened.st_mtime_ns, opened.st_dev, opened.st_ino)
            expected = (entry.size, entry.mode, entry.modified_ns, entry.device, entry.inode)
            if identity != expected or not stat.S_ISREG(opened.st_mode):
                raise SimulationBundleError(f"Simulator file changed while snapshotting: {entry.relative_path!r}.")
            file_digest = hashlib.sha256()
            with open(destination_path, "xb", buffering=0) as target:
                os.fchmod(target.fileno(), 0o700 if entry.mode & 0o111 else 0o600)
                while True:
                    chunk = os.read(descriptor, 128 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    file_digest.update(chunk)
            after_open = os.fstat(descriptor)
            if (
                after_open.st_size,
                after_open.st_mode,
                after_open.st_mtime_ns,
                after_open.st_dev,
                after_open.st_ino,
            ) != expected:
                raise SimulationBundleError(f"Simulator file changed while snapshotting: {entry.relative_path!r}.")
        finally:
            os.close(descriptor)
        digest.update(entry.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
    after = _inventory_tree(
        source,
        max_entries=max_entries,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )
    if before != after:
        raise SimulationBundleError("Simulator bundle changed while snapshotting.")
    return digest.hexdigest()


class SimulationExecutionService:
    """Prepare, run, stop, and inspect bounded simulator executions."""

    def __init__(
        self,
        storage_root: str | Path,
        python_executable: str | Path = sys.executable,
        max_concurrency: int = 2,
        *,
        allowed_bundle_root: str | Path | None = None,
        max_pending: int = 16,
        maximum_timeout_seconds: int = 300,
        queue_timeout_seconds: int = 60,
        stop_grace_seconds: float = 1.0,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 1024 * 1024,
        max_bundle_entries: int = 20_000,
        max_bundle_bytes: int = 512 * 1024 * 1024,
        max_bundle_file_bytes: int = 128 * 1024 * 1024,
        max_result_entries: int = 512,
        max_result_bytes: int = 64 * 1024 * 1024,
        max_result_file_bytes: int = 32 * 1024 * 1024,
        max_retained_executions: int = 64,
        execution_mode: str | None = None,
        allow_trusted_process: bool = False,
        container_engine: str | None = None,
        container_image: str | None = None,
        container_memory_mib: int = 512,
        container_cpus: float = 1.0,
        container_pids_limit: int = 64,
        output_action_executor: OutputActionExecutor | None = None,
    ):
        self.bundle_root = (
            Path(allowed_bundle_root).resolve(strict=True)
            if allowed_bundle_root is not None
            else None
        )
        if self.bundle_root is not None and not self.bundle_root.is_dir():
            raise ValueError("allowed_bundle_root must be a directory.")
        self.execution_root = Path(storage_root).resolve()
        self.execution_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.python_executable = Path(python_executable).resolve(strict=True)
        if not self.python_executable.is_file():
            raise ValueError("python_executable must be a file.")
        self.execution_boundary = GeneratedExecutionBoundary(
            mode=execution_mode,
            allow_trusted_process=allow_trusted_process,
            engine=container_engine,
            image=container_image,
            python_executable=self.python_executable,
            memory_mib=container_memory_mib,
            cpus=container_cpus,
            pids_limit=container_pids_limit,
            file_size_mib=max(1, max_result_file_bytes // (1024 * 1024)),
        )
        self.output_action_executor = (
            output_action_executor
            if output_action_executor is not None
            else InterfaceOutputActionClient.from_environment()
        )
        for name, value in {
            "max_concurrency": max_concurrency,
            "max_pending": max_pending,
            "maximum_timeout_seconds": maximum_timeout_seconds,
            "queue_timeout_seconds": queue_timeout_seconds,
            "max_stdout_bytes": max_stdout_bytes,
            "max_stderr_bytes": max_stderr_bytes,
            "max_bundle_entries": max_bundle_entries,
            "max_bundle_bytes": max_bundle_bytes,
            "max_bundle_file_bytes": max_bundle_file_bytes,
            "max_result_entries": max_result_entries,
            "max_result_bytes": max_result_bytes,
            "max_result_file_bytes": max_result_file_bytes,
            "max_retained_executions": max_retained_executions,
        }.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if stop_grace_seconds < 0:
            raise ValueError("stop_grace_seconds must not be negative.")
        self.max_pending = max_pending
        self.maximum_timeout_seconds = maximum_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self.stop_grace_seconds = stop_grace_seconds
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_bundle_entries = max_bundle_entries
        self.max_bundle_bytes = max_bundle_bytes
        self.max_bundle_file_bytes = max_bundle_file_bytes
        self.max_result_entries = max_result_entries
        self.max_result_bytes = max_result_bytes
        self.max_result_file_bytes = max_result_file_bytes
        self.max_retained_executions = max_retained_executions
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.RLock()
        self._executions: dict[str, _PreparedExecution] = {}
        with self._lock:
            self._prune_storage_unlocked(reserve=0)

    def prepare(
        self,
        bundle_path: str | Path,
        arguments: Mapping[str, Any] | None = None,
        *,
        purpose: str = "interactive",
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        if not _PURPOSE_RE.fullmatch(purpose):
            raise ValueError("purpose must be a short lowercase identifier.")
        source = self._resolve_bundle(bundle_path)
        ensure_simulation_manifest(source)
        execution_id = execution_id or f"exec_{uuid.uuid4().hex}"
        if not _EXECUTION_ID_RE.fullmatch(execution_id):
            raise ValueError("execution_id must use the exec_<32 lowercase hex> form.")
        with self._lock:
            self._prune_storage_unlocked(reserve=1)
            pending = sum(
                execution.record.status not in _TERMINAL_STATUSES
                for execution in self._executions.values()
            )
            if pending >= self.max_pending:
                raise SimulationCapacityError("Too many simulator executions are queued or running.")
            if execution_id in self._executions or (self.execution_root / execution_id).exists():
                raise ExecutionStateError(f"Execution {execution_id!r} already exists.")
            job_dir = self.execution_root / execution_id
            bundle_dir = job_dir / "bundle"
            results_dir = job_dir / "results"
            bundle_dir.mkdir(parents=True, mode=0o700)
            results_dir.mkdir(mode=0o700)
        try:
            try:
                source_digest_before = stable_tree_digest(source)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SimulationBundleError(
                    f"Cannot digest simulator bundle before snapshotting: {exc}"
                ) from exc
            _copy_snapshot(
                source,
                bundle_dir,
                max_entries=self.max_bundle_entries,
                max_bytes=self.max_bundle_bytes,
                max_file_bytes=self.max_bundle_file_bytes,
            )
            try:
                source_digest_after = stable_tree_digest(source)
                snapshot_digest = stable_tree_digest(bundle_dir)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SimulationBundleError(
                    f"Cannot digest simulator bundle after snapshotting: {exc}"
                ) from exc
            if source_digest_before != source_digest_after or snapshot_digest != source_digest_before:
                raise SimulationBundleError(
                    "Simulator bundle changed or was not copied exactly while snapshotting."
                )
            manifest = _load_manifest(
                bundle_dir / SIMULATION_MANIFEST,
                maximum_timeout_seconds=self.maximum_timeout_seconds,
            )
            resolved_arguments, rendered_arguments = _render_arguments(manifest, arguments)
            if not (bundle_dir / SIMULATION_ENTRYPOINT).is_file():
                raise SimulationBundleError(f"Snapshot is missing {SIMULATION_ENTRYPOINT}.")
            record = ExecutionRecord(
                execution_id=execution_id,
                purpose=purpose,
                status="queued",
                created_at=_utc_now(),
                bundle_digest=snapshot_digest,
                snapshot_digest=snapshot_digest,
                arguments=resolved_arguments,
            )
            prepared = _PreparedExecution(
                record=record,
                job_dir=job_dir,
                bundle_dir=bundle_dir,
                results_dir=results_dir,
                python_arguments=("-u", SIMULATION_ENTRYPOINT, *rendered_arguments),
                timeout_seconds=manifest.timeout_seconds,
                expected_results=manifest.result_files,
            )
            with self._lock:
                self._executions[execution_id] = prepared
                self._persist(prepared)
            return copy.deepcopy(record.to_dict())
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def execute(
        self,
        bundle_path: str | Path,
        arguments: Mapping[str, Any] | None = None,
        *,
        purpose: str = "interactive",
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        queued = self.prepare(
            bundle_path,
            arguments,
            purpose=purpose,
            execution_id=execution_id,
        )
        return self.run(queued["execution_id"])

    def run(self, execution_id: str) -> dict[str, Any]:
        prepared = self._get_prepared(execution_id)
        with self._lock:
            if prepared.record.status != "queued":
                raise ExecutionStateError(
                    f"Execution {execution_id!r} is {prepared.record.status}, not queued."
                )
        queue_deadline = time.monotonic() + self.queue_timeout_seconds
        acquired = False
        while not acquired:
            if prepared.stop_event.is_set():
                return self._finish_without_process(prepared, "stopped", "Execution was stopped while queued.")
            remaining = queue_deadline - time.monotonic()
            if remaining <= 0:
                return self._finish_without_process(
                    prepared,
                    "failed",
                    "Execution could not start before the queue timeout.",
                    failure_kind="capacity_timeout",
                )
            acquired = self._slots.acquire(timeout=min(0.1, remaining))
        try:
            if prepared.stop_event.is_set():
                return self._finish_without_process(prepared, "stopped", "Execution was stopped while queued.")
            return self._run_process(prepared)
        finally:
            self._slots.release()

    def stop(self, execution_id: str) -> bool:
        prepared = self._get_prepared(execution_id)
        with self._lock:
            if prepared.record.status in _TERMINAL_STATUSES:
                return False
            prepared.stop_event.set()
            prepared.record = replace(prepared.record, stop_requested=True)
            process = prepared.process
            launch = prepared.launch
            self._persist(prepared)
        self.execution_boundary.force_remove(launch)
        if process is not None:
            self._signal_process_group(process, signal.SIGTERM)
        return True

    def get_record(self, execution_id: str) -> dict[str, Any]:
        prepared = self._get_prepared(execution_id)
        with self._lock:
            return copy.deepcopy(prepared.record.to_dict())

    def list_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(item.record.to_dict()) for item in self._executions.values()]

    def _resolve_bundle(self, bundle_path: str | Path) -> Path:
        candidate = Path(bundle_path)
        if not candidate.is_absolute():
            if self.bundle_root is None:
                raise SimulationBundleError(
                    "Relative simulator paths require allowed_bundle_root."
                )
            candidate = self.bundle_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            if self.bundle_root is not None:
                resolved.relative_to(self.bundle_root)
        except (OSError, ValueError) as exc:
            raise SimulationBundleError(
                "Simulator bundle must exist below the configured bundle root."
            ) from exc
        if not resolved.is_dir():
            raise SimulationBundleError("Simulator bundle must be a directory.")
        return resolved

    def _get_prepared(self, execution_id: str) -> _PreparedExecution:
        with self._lock:
            try:
                return self._executions[execution_id]
            except KeyError as exc:
                raise ExecutionStateError(f"Unknown execution {execution_id!r}.") from exc

    def _run_process(self, prepared: _PreparedExecution) -> dict[str, Any]:
        if self.output_action_executor is not None:
            return self._run_output_action(prepared)

        start_monotonic = time.monotonic()
        home_dir = prepared.job_dir / "home"
        temp_dir = prepared.job_dir / "tmp"
        home_dir.mkdir(mode=0o700, exist_ok=True)
        temp_dir.mkdir(mode=0o700, exist_ok=True)
        stdout_capture = _Capture(self.max_stdout_bytes)
        stderr_capture = _Capture(self.max_stderr_bytes)
        overflow_event = threading.Event()
        failure_kind: str | None = None
        message: str | None = None
        status = "failed"
        process: subprocess.Popen[bytes] | None = None
        with self._lock:
            prepared.record = replace(prepared.record, status="running", started_at=_utc_now())
            self._persist(prepared)
        try:
            launch = self.execution_boundary.build_python_launch(
                prepared.bundle_dir,
                prepared.python_arguments,
                results_directory=prepared.results_dir,
                home_directory=home_dir,
                temporary_directory=temp_dir,
            )
            with self._lock:
                prepared.launch = launch
            process = subprocess.Popen(
                launch.argv,
                cwd=launch.cwd,
                env=launch.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            with self._lock:
                prepared.process = process
            readers = [
                threading.Thread(
                    target=self._read_stream,
                    args=(process.stdout, stdout_capture, overflow_event),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._read_stream,
                    args=(process.stderr, stderr_capture, overflow_event),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            result_check_at = start_monotonic
            while process.poll() is None:
                now = time.monotonic()
                if prepared.stop_event.is_set():
                    status = "stopped"
                    message = "Execution was stopped."
                    self._terminate(process)
                    break
                if overflow_event.is_set():
                    failure_kind = "output_limit"
                    message = "Execution exceeded the stdout or stderr limit."
                    self._terminate(process)
                    break
                if now - start_monotonic >= prepared.timeout_seconds:
                    status = "timed_out"
                    failure_kind = "timeout"
                    message = f"Execution timed out after {prepared.timeout_seconds} seconds."
                    self._terminate(process)
                    break
                if now >= result_check_at:
                    result_check_at = now + 0.2
                    violation = self._result_limit_violation(prepared.results_dir)
                    if violation:
                        failure_kind = "result_limit"
                        message = violation
                        self._terminate(process)
                        break
                time.sleep(0.02)
            # A runner can finish while leaving ordinary child processes alive.
            # They share this process group, so stop them before closing pipes
            # and retaining result files.
            self._signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=max(self.stop_grace_seconds, 0.1) + 1.0)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process, signal.SIGKILL)
                process.wait(timeout=2.0)
            self._signal_process_group(process, signal.SIGKILL)
            for reader in readers:
                reader.join(timeout=1.0)
            if prepared.stop_event.is_set():
                status = "stopped"
                message = message or "Execution was stopped."
            elif status == "timed_out":
                pass
            elif stdout_capture.truncated or stderr_capture.truncated:
                status = "failed"
                failure_kind = "output_limit"
                message = "Execution exceeded the stdout or stderr limit."
            elif failure_kind:
                status = "failed"
            elif process.returncode == 0:
                status = "succeeded"
            else:
                status = "failed"
                failure_kind = "nonzero_exit"
                message = f"Simulation exited with code {process.returncode}."
        except ExecutionBoundaryError as exc:
            failure_kind = "execution_boundary"
            message = str(exc)
            status = "failed"
        except OSError as exc:
            failure_kind = "launch_error"
            message = f"Simulation could not start: {exc}"
            status = "failed"
        except Exception as exc:
            failure_kind = "execution_error"
            message = f"Simulation execution failed while being supervised: {exc}"
            status = "failed"
        finally:
            self.execution_boundary.force_remove(prepared.launch)
            if process is not None:
                self._signal_process_group(process, signal.SIGTERM)
                if process.poll() is None:
                    try:
                        process.wait(timeout=max(self.stop_grace_seconds, 0.1))
                    except subprocess.TimeoutExpired:
                        self._signal_process_group(process, signal.SIGKILL)
                self._signal_process_group(process, signal.SIGKILL)
            with self._lock:
                prepared.process = None
                prepared.launch = None

        result_files: tuple[ResultFile, ...] = ()
        if failure_kind != "result_limit":
            try:
                result_files = self._collect_result_files(prepared.results_dir)
                available = {item.path for item in result_files}
                missing = [path for path in prepared.expected_results if path not in available]
                if status == "succeeded" and missing:
                    status = "failed"
                    failure_kind = "missing_result"
                    message = f"Simulation did not create required result files: {', '.join(missing)}."
            except SimulationExecutionError as exc:
                status = "failed"
                failure_kind = "result_limit"
                message = str(exc)

        finished = time.monotonic()
        with self._lock:
            prepared.record = replace(
                prepared.record,
                status=status,
                finished_at=_utc_now(),
                duration_seconds=round(finished - start_monotonic, 6),
                exit_code=process.returncode if process is not None else None,
                stdout=stdout_capture.text(),
                stderr=stderr_capture.text(),
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
                result_files=result_files,
                failure_kind=failure_kind,
                message=message,
                stop_requested=prepared.stop_event.is_set(),
            )
            self._persist(prepared)
            return copy.deepcopy(prepared.record.to_dict())

    def _run_output_action(self, prepared: _PreparedExecution) -> dict[str, Any]:
        """Run in a sibling of this interface's exact prepared runtime.

        Managed OptPilot launches expose a launch-local file broker.  The
        broker snapshots the safely staged source before running the
        Catalog-authored action.  A broker failure is reported as execution
        infrastructure failure; it never falls back to a process in the
        credential-bearing interface container.
        """

        assert self.output_action_executor is not None
        started = time.monotonic()
        with self._lock:
            prepared.record = replace(
                prepared.record,
                status="running",
                started_at=_utc_now(),
            )
            self._persist(prepared)

        result: OutputActionResult | None = None
        status = "failed"
        failure_kind: str | None = "execution_boundary"
        message: str | None = None
        try:
            result = self.output_action_executor.execute(
                source_directory=prepared.bundle_dir,
                arguments=prepared.python_arguments[2:],
                results_directory=prepared.results_dir,
                request_id=prepared.record.execution_id,
                timeout_seconds=prepared.timeout_seconds,
                response_timeout_seconds=max(
                    90.0, float(prepared.timeout_seconds) + 30.0
                ),
                should_cancel=prepared.stop_event.is_set,
            )
            status, failure_kind, message = self._translate_output_action_result(
                result
            )
        except OutputActionError as exc:
            message = (
                "OptPilot could not run this simulator in the isolated "
                f"interface runtime: {exc}"
            )
        except Exception as exc:
            message = (
                "Simulation execution failed while communicating with the "
                f"isolated interface runtime: {exc}"
            )

        result_files: tuple[ResultFile, ...] = ()
        if result is not None and failure_kind != "result_limit":
            try:
                result_files = self._collect_result_files(prepared.results_dir)
                available = {item.path for item in result_files}
                missing = [
                    path
                    for path in prepared.expected_results
                    if path not in available
                ]
                if status == "succeeded" and missing:
                    status = "failed"
                    failure_kind = "missing_result"
                    message = (
                        "Simulation did not create required result files: "
                        + ", ".join(missing)
                        + "."
                    )
            except SimulationExecutionError as exc:
                status = "failed"
                failure_kind = "result_limit"
                message = str(exc)

        duration = (
            result.duration_seconds
            if result is not None
            else max(0.0, time.monotonic() - started)
        )
        with self._lock:
            prepared.record = replace(
                prepared.record,
                status=status,
                finished_at=_utc_now(),
                duration_seconds=round(duration, 6),
                exit_code=result.exit_code if result is not None else None,
                stdout=result.stdout if result is not None else "",
                stderr=result.stderr if result is not None else "",
                stdout_truncated=(
                    result.stdout_truncated if result is not None else False
                ),
                stderr_truncated=(
                    result.stderr_truncated if result is not None else False
                ),
                result_files=result_files,
                failure_kind=failure_kind,
                message=message,
                stop_requested=prepared.stop_event.is_set(),
            )
            self._persist(prepared)
            return copy.deepcopy(prepared.record.to_dict())

    @staticmethod
    def _translate_output_action_result(
        result: OutputActionResult,
    ) -> tuple[str, str | None, str | None]:
        if result.status == "cancelled":
            return "stopped", None, "Execution was stopped."
        if result.status == "timed_out":
            return (
                "timed_out",
                "timeout",
                "Simulation timed out in the isolated interface runtime.",
            )
        if result.status == "infrastructure_failed":
            detail = (
                f" ({result.failure_code})" if result.failure_code else ""
            )
            return (
                "failed",
                "execution_boundary",
                "OptPilot could not establish the isolated simulation "
                f"runtime{detail}.",
            )
        if result.status == "rejected":
            detail = (
                f" ({result.failure_code})" if result.failure_code else ""
            )
            return (
                "failed",
                "execution_boundary",
                f"OptPilot rejected the simulation execution request{detail}.",
            )
        if result.stdout_truncated or result.stderr_truncated:
            return (
                "failed",
                "output_limit",
                "Execution exceeded the stdout or stderr limit.",
            )
        if result.status == "failed":
            return (
                "failed",
                result.failure_code or "nonzero_exit",
                (
                    f"Simulation exited with code {result.exit_code}."
                    if result.exit_code is not None
                    else "Simulation failed in the isolated interface runtime."
                ),
            )
        return "succeeded", None, None

    @staticmethod
    def _read_stream(stream: Any, capture: _Capture, overflow_event: threading.Event) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                capture.add(chunk)
                if capture.truncated:
                    overflow_event.set()
        finally:
            stream.close()

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        self._signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=self.stop_grace_seconds)
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
        try:
            os.killpg(process.pid, signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            if process.poll() is not None:
                return
            try:
                if signal_number == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

    def _result_limit_violation(self, root: Path) -> str | None:
        try:
            self._result_inventory(root, include_hashes=False)
        except SimulationExecutionError as exc:
            return str(exc)
        return None

    def _result_inventory(self, root: Path, *, include_hashes: bool) -> tuple[ResultFile, ...]:
        records: list[ResultFile] = []
        total = 0
        entry_count = 0

        def walk_error(error: OSError) -> None:
            if isinstance(error, FileNotFoundError):
                return
            raise SimulationExecutionError(
                f"Cannot inspect simulation results: {error}"
            ) from error

        for directory, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=walk_error,
        ):
            relative_directory = Path(directory).relative_to(root)
            if len(relative_directory.parts) > 64:
                raise SimulationExecutionError(
                    "Simulation results exceed the directory-depth limit."
                )
            directory_names.sort()
            file_names.sort()
            for directory_name in list(directory_names):
                path = Path(directory) / directory_name
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    directory_names.remove(directory_name)
                    continue
                except OSError as exc:
                    raise SimulationExecutionError(
                        f"Cannot inspect simulation result directory: {exc}"
                    ) from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise SimulationExecutionError("Simulation results may not contain symbolic links.")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SimulationExecutionError(
                        "Simulation results contain a special directory entry."
                    )
                entry_count += 1
                if entry_count > self.max_result_entries:
                    raise SimulationExecutionError(
                        "Simulation results exceed the entry-count limit."
                    )
            for file_name in file_names:
                path = Path(directory) / file_name
                relative = path.relative_to(root).as_posix()
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise SimulationExecutionError(
                        f"Cannot inspect simulation result file: {exc}"
                    ) from exc
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise SimulationExecutionError("Simulation results contain a link or special file.")
                if metadata.st_size > self.max_result_file_bytes:
                    raise SimulationExecutionError(f"Result file exceeds its size limit: {relative!r}.")
                total += metadata.st_size
                if total > self.max_result_bytes:
                    raise SimulationExecutionError("Simulation results exceed the total byte limit.")
                entry_count += 1
                if entry_count > self.max_result_entries:
                    raise SimulationExecutionError(
                        "Simulation results exceed the entry-count limit."
                    )
                digest = ""
                if include_hashes:
                    try:
                        digest = self._hash_regular_file(path, metadata)
                    except OSError as exc:
                        raise SimulationExecutionError(
                            f"Cannot retain simulation result {relative!r}: {exc}"
                        ) from exc
                records.append(ResultFile(relative, metadata.st_size, digest))
        return tuple(records)

    @staticmethod
    def _hash_regular_file(path: Path, expected: os.stat_result) -> str:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        digest = hashlib.sha256()
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
            ):
                raise SimulationExecutionError("A result file changed while it was being retained.")
            while True:
                chunk = os.read(descriptor, 128 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise SimulationExecutionError("A result file changed while it was being retained.")
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def _collect_result_files(self, root: Path) -> tuple[ResultFile, ...]:
        return self._result_inventory(root, include_hashes=True)

    def _finish_without_process(
        self,
        prepared: _PreparedExecution,
        status: str,
        message: str,
        *,
        failure_kind: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            prepared.record = replace(
                prepared.record,
                status=status,
                finished_at=_utc_now(),
                duration_seconds=0.0,
                failure_kind=failure_kind,
                message=message,
                stop_requested=prepared.stop_event.is_set(),
            )
            self._persist(prepared)
            return copy.deepcopy(prepared.record.to_dict())

    def _prune_storage_unlocked(self, *, reserve: int) -> None:
        """Keep execution evidence bounded during long interface sessions."""

        candidates: list[tuple[int, str, Path]] = []
        protected_count = 0
        try:
            children = list(self.execution_root.iterdir())
        except OSError:
            return
        for path in children:
            if not _EXECUTION_ID_RE.fullmatch(path.name):
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            prepared = self._executions.get(path.name)
            if (
                prepared is not None
                and prepared.record.status not in _TERMINAL_STATUSES
            ):
                protected_count += 1
                continue
            candidates.append((metadata.st_mtime_ns, path.name, path))
        target = max(
            self.max_retained_executions - reserve - protected_count,
            0,
        )
        candidates.sort()
        while len(candidates) > target:
            _, execution_id, path = candidates.pop(0)
            self._executions.pop(execution_id, None)
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _persist(prepared: _PreparedExecution) -> None:
        path = prepared.job_dir / "execution.json"
        payload = (json.dumps(prepared.record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".execution-", suffix=".json", dir=prepared.job_dir)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


__all__ = [
    "ExecutionRecord",
    "ExecutionStateError",
    "ResultFile",
    "SIMULATION_ENTRYPOINT",
    "SIMULATION_MANIFEST",
    "ACCEPTED_SIMULATION_SCHEMAS",
    "SIMULATION_SCHEMA",
    "SIMULATION_SCHEMA_V1",
    "SimulationBundleError",
    "SimulationCapacityError",
    "SimulationExecutionError",
    "SimulationExecutionService",
    "SimulationManifestError",
    "ensure_simulation_manifest",
    "simulation_metadata",
]
