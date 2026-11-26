"""Fail-closed execution boundary for generated Python code.

Generated simulators are untrusted input.  The default boundary therefore runs
Python in a separate Docker/Podman container with no network, no inherited
environment, a read-only root filesystem, and explicit resource limits.

The process backend exists only for unit tests and explicitly trusted local
development.  It requires a second opt-in flag and is never selected as a
fallback when a container engine or image is unavailable.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


DEFAULT_EXECUTION_IMAGE = "optpilot/workspace-dev:latest"
DEFAULT_CONTAINER_MEMORY_MIB = 512
DEFAULT_CONTAINER_CPUS = 1.0
DEFAULT_CONTAINER_PIDS = 64
DEFAULT_CONTAINER_TMP_MIB = 64
DEFAULT_CONTAINER_FILE_SIZE_MIB = 32
XDEVS_VERSION = "3.0.0"

_MODE_ENV = "DEVS_GENERATED_EXECUTION_MODE"
_ENGINE_ENV = "DEVS_GENERATED_EXECUTION_ENGINE"
_IMAGE_ENV = "DEVS_GENERATED_EXECUTION_IMAGE"
_TRUSTED_PROCESS_ENV = "DEVS_GENERATED_EXECUTION_TRUSTED_LOCAL"
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "OPTPILOT_SIMULATION_RESULTS_DIR",
        "DEVS_SIMULATION_RESULTS_DIR",
    }
)
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,511}$")
_ENGINE_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_PURE_WHEEL_RE = re.compile(
    r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.!+-]+-(?:py3|py2\.py3)-none-any\.whl$"
)


class ExecutionBoundaryError(RuntimeError):
    """Generated code cannot be launched through the configured boundary."""


@dataclass(frozen=True)
class PythonLaunch:
    """A fully resolved, credential-free Python launch contract."""

    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    mode: str
    container_name: str | None = None


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_environment(home: Path, temporary: Path) -> dict[str, str]:
    return {
        "PATH": str(Path(sys.executable).resolve().parent),
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _container_client_environment() -> dict[str, str]:
    """Environment for the trusted Docker/Podman CLI, never for user code.

    Docker Desktop commonly stores the selected context below ``HOME`` and
    Podman commonly exposes its rootless socket through ``XDG_RUNTIME_DIR``.
    Keeping those narrowly scoped connection settings lets the trusted client
    reach the local daemon without inheriting arbitrary application secrets.
    None of these values are forwarded with ``docker run --env``.
    """

    result = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR", "CONTAINER_HOST"):
        value = os.environ.get(name)
        if value:
            result[name] = value
    return result


def _regular_directory(path: Path, *, label: str) -> Path:
    try:
        supplied_metadata = path.lstat()
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise ExecutionBoundaryError(f"{label} is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(supplied_metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ExecutionBoundaryError(f"{label} must be a regular directory.")
    return resolved


def _container_path(relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExecutionBoundaryError("Python runtime paths must be canonical and relative.")
    return "/workspace/" + path.as_posix()


def _workspace_pythonpath(workspace: Path) -> tuple[str, ...]:
    """Return only validated offline Python paths bundled with the workspace."""

    result: list[str] = []
    vendor = workspace / "runtime_dependencies" / "vendor"
    if vendor.is_dir() and not vendor.is_symlink():
        for wheel in sorted(vendor.iterdir(), key=lambda item: item.name):
            try:
                metadata = wheel.lstat()
            except OSError as exc:
                raise ExecutionBoundaryError(
                    f"Cannot inspect bundled Python dependency {wheel.name!r}: {exc}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ExecutionBoundaryError("Bundled Python dependencies must be regular files.")
            if not _PURE_WHEEL_RE.fullmatch(wheel.name):
                continue
            result.append(_container_path(wheel.relative_to(workspace).as_posix()))
    package_root = workspace / "runtime_dependencies" / "python"
    if package_root.is_dir() and not package_root.is_symlink():
        result.append(_container_path(package_root.relative_to(workspace).as_posix()))
    return tuple(result)


def stage_installed_xdevs_package(workspace: str | Path) -> Path:
    """Copy the trusted installed xDEVS package into a temporary workspace.

    This is used by the generation agent's scratch executions.  Student-facing
    generated bundles already carry the deterministic pure-Python wheel.  No
    package manager or network access is used in either path.
    """

    root = Path(workspace).resolve(strict=True)
    try:
        distribution = importlib.metadata.distribution("xdevs")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ExecutionBoundaryError("The trusted interface runtime is missing xdevs 3.0.0.") from exc
    if distribution.version != XDEVS_VERSION:
        raise ExecutionBoundaryError(
            f"The trusted interface runtime must provide xdevs {XDEVS_VERSION}; "
            f"found {distribution.version}."
        )
    spec = importlib.util.find_spec("xdevs")
    if spec is None or spec.origin is None:
        raise ExecutionBoundaryError("The trusted xdevs package cannot be located.")
    source = Path(spec.origin).resolve(strict=True).parent
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(source_metadata.st_mode):
        raise ExecutionBoundaryError("The trusted xdevs package is not a regular directory.")
    destination = root / "runtime_dependencies" / "python" / "xdevs"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        shutil.rmtree(destination)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name == "__pycache__" or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)
    for directory, directory_names, file_names in os.walk(destination, followlinks=False):
        for name in (*directory_names, *file_names):
            candidate = Path(directory) / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                shutil.rmtree(destination, ignore_errors=True)
                raise ExecutionBoundaryError("The trusted xdevs package contains a symbolic link.")
    return destination


class GeneratedExecutionBoundary:
    """Build and clean up isolated Python execution launches."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        allow_trusted_process: bool = False,
        engine: str | None = None,
        image: str | None = None,
        python_executable: str | Path = sys.executable,
        memory_mib: int = DEFAULT_CONTAINER_MEMORY_MIB,
        cpus: float = DEFAULT_CONTAINER_CPUS,
        pids_limit: int = DEFAULT_CONTAINER_PIDS,
        tmp_mib: int = DEFAULT_CONTAINER_TMP_MIB,
        file_size_mib: int = DEFAULT_CONTAINER_FILE_SIZE_MIB,
    ) -> None:
        selected_mode = (mode or os.environ.get(_MODE_ENV) or "container").strip().lower()
        if selected_mode not in {"container", "process"}:
            raise ExecutionBoundaryError(
                f"{_MODE_ENV} must be 'container' or the explicit trusted-local 'process' mode."
            )
        trusted_process = allow_trusted_process or _enabled(os.environ.get(_TRUSTED_PROCESS_ENV))
        if selected_mode == "process" and not trusted_process:
            raise ExecutionBoundaryError(
                "Process execution is disabled. It may only be enabled explicitly for "
                "unit tests or a trusted single-user local deployment."
            )
        if type(memory_mib) is not int or not 64 <= memory_mib <= 8192:
            raise ValueError("memory_mib must be an integer from 64 to 8192.")
        if type(pids_limit) is not int or not 8 <= pids_limit <= 1024:
            raise ValueError("pids_limit must be an integer from 8 to 1024.")
        if type(tmp_mib) is not int or not 8 <= tmp_mib <= 1024:
            raise ValueError("tmp_mib must be an integer from 8 to 1024.")
        if type(file_size_mib) is not int or not 1 <= file_size_mib <= 1024:
            raise ValueError("file_size_mib must be an integer from 1 to 1024.")
        if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0.1 <= float(cpus) <= 16:
            raise ValueError("cpus must be between 0.1 and 16.")

        self.mode = selected_mode
        self.allow_trusted_process = trusted_process
        self.python_executable = Path(python_executable).resolve(strict=True)
        self.memory_mib = memory_mib
        self.cpus = float(cpus)
        self.pids_limit = pids_limit
        self.tmp_mib = tmp_mib
        self.file_size_mib = file_size_mib

        requested_engine = (engine or os.environ.get(_ENGINE_ENV) or "").strip()
        if requested_engine and not _ENGINE_RE.fullmatch(requested_engine):
            raise ExecutionBoundaryError(f"{_ENGINE_ENV} must name docker or podman.")
        self.engine = self._find_engine(requested_engine) if selected_mode == "container" else None
        selected_image = (image or os.environ.get(_IMAGE_ENV) or DEFAULT_EXECUTION_IMAGE).strip()
        if not _IMAGE_RE.fullmatch(selected_image):
            raise ExecutionBoundaryError(f"{_IMAGE_ENV} is not a valid trusted image reference.")
        self.image = selected_image

    @staticmethod
    def _find_engine(requested: str) -> Path | None:
        names = (requested,) if requested else ("docker", "podman")
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
        return None

    def build_python_launch(
        self,
        workspace: str | Path,
        python_arguments: Sequence[str],
        *,
        results_directory: str | Path | None = None,
        home_directory: str | Path | None = None,
        temporary_directory: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        stdin_open: bool = False,
    ) -> PythonLaunch:
        root = _regular_directory(Path(workspace), label="Generated-code workspace")
        arguments = tuple(str(item) for item in python_arguments)
        if not arguments or any("\x00" in item for item in arguments):
            raise ExecutionBoundaryError("Python execution arguments are invalid.")
        extra = dict(environment or {})
        unsupported = sorted(set(extra) - _SAFE_ENVIRONMENT_NAMES)
        if unsupported:
            raise ExecutionBoundaryError(
                "Generated execution attempted to pass unsupported environment names: "
                + ", ".join(unsupported)
            )
        if any("\x00" in str(value) for value in extra.values()):
            raise ExecutionBoundaryError("Generated execution environment contains invalid text.")

        result_root = None
        if results_directory is not None:
            result_root = _regular_directory(Path(results_directory), label="Simulation result directory")

        if self.mode == "process":
            if not self.allow_trusted_process:
                raise ExecutionBoundaryError("Trusted process execution was not enabled.")
            home = Path(home_directory or root).resolve()
            temporary = Path(temporary_directory or root).resolve()
            process_environment = _safe_environment(home, temporary)
            python_paths = []
            for item in _workspace_pythonpath(root):
                relative = item.removeprefix("/workspace/")
                python_paths.append(str(root.joinpath(*PurePosixPath(relative).parts)))
            if python_paths:
                process_environment["PYTHONPATH"] = os.pathsep.join(python_paths)
            if result_root is not None:
                process_environment["OPTPILOT_SIMULATION_RESULTS_DIR"] = str(result_root)
                process_environment["DEVS_SIMULATION_RESULTS_DIR"] = str(result_root)
            process_environment.update({key: str(value) for key, value in extra.items()})
            return PythonLaunch(
                argv=(str(self.python_executable), *arguments),
                cwd=root,
                environment=process_environment,
                mode="process",
            )

        if self.engine is None:
            raise ExecutionBoundaryError(
                "Generated-code execution requires Docker or Podman, but neither executable "
                "is available. No process fallback was used."
            )
        container_name = f"optpilot-devs-{uuid.uuid4().hex}"
        container_environment = {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        python_paths = _workspace_pythonpath(root)
        if python_paths:
            container_environment["PYTHONPATH"] = ":".join(python_paths)
        if result_root is not None:
            container_environment["OPTPILOT_SIMULATION_RESULTS_DIR"] = "/results"
            container_environment["DEVS_SIMULATION_RESULTS_DIR"] = "/results"
        container_environment.update({key: str(value) for key, value in extra.items()})

        uid = os.getuid() if hasattr(os, "getuid") else 65534
        gid = os.getgid() if hasattr(os, "getgid") else 65534
        argv = [
            str(self.engine),
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            f"{self.memory_mib}m",
            "--memory-swap",
            f"{self.memory_mib}m",
            "--cpus",
            f"{self.cpus:g}",
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            f"nproc={self.pids_limit}:{self.pids_limit}",
            "--ulimit",
            f"fsize={self.file_size_mib * 1024 * 1024}:{self.file_size_mib * 1024 * 1024}",
            "--ipc",
            "none",
            "--user",
            f"{uid}:{gid}",
            "--workdir",
            "/workspace",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={self.tmp_mib}m,mode=1777",
            "--volume",
            f"{root}:/workspace:ro",
        ]
        if stdin_open:
            argv.append("--interactive")
        if result_root is not None:
            argv.extend(("--volume", f"{result_root}:/results:rw"))
        for name, value in sorted(container_environment.items()):
            argv.extend(("--env", f"{name}={value}"))
        argv.extend(("--entrypoint", "python", self.image, *arguments))

        return PythonLaunch(
            argv=tuple(argv),
            cwd=root,
            environment=_container_client_environment(),
            mode="container",
            container_name=container_name,
        )

    def force_remove(self, launch: PythonLaunch | None) -> None:
        """Best-effort cleanup for a container whose CLI process was interrupted."""

        if (
            launch is None
            or launch.mode != "container"
            or not launch.container_name
            or self.engine is None
        ):
            return
        try:
            subprocess.run(
                (str(self.engine), "rm", "--force", launch.container_name),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=launch.environment,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
