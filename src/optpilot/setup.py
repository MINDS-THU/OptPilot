"""Process setup helpers for editable component copies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


JsonDict = Dict[str, Any]
ProgressCallback = Callable[[str, str], None]


def run_process_setup(
    setup: JsonDict | None,
    root: Path,
    *,
    progress: Optional[ProgressCallback] = None,
) -> JsonDict:
    """Run a public ``runtime.setup`` or ``interface.setup`` block in ``root``.

    Setup is intentionally process-only. Container images should already contain
    their dependencies or build them through the container build process.
    """

    if not setup:
        return {"ran": False, "steps": []}
    root = root.resolve()
    steps = list(setup.get("steps") or [])
    timeout = int(setup.get("timeoutSeconds", 600) or 600)
    base_env = setup_env(setup)
    completed_steps = []
    for index, step in enumerate(steps):
        commands = setup_commands_for_step(step, root)
        step_results = []
        for command in commands:
            cwd = setup_cwd(step, root)
            env = dict(base_env)
            env.update({str(key): str(value) for key, value in (step.get("env") or {}).items()})
            title = f"Setup step {index + 1}: {step.get('uses')}"
            detail = " ".join(command)
            if progress:
                progress(title, detail)
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            step_result = {
                "command": command,
                "cwd": str(cwd),
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
            step_results.append(step_result)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Setup step {index + 1} failed with exit code {completed.returncode}: "
                    f"{completed.stderr.strip() or completed.stdout.strip()}"
                )
        completed_steps.append({"uses": step.get("uses"), "commands": step_results})
    return {"ran": True, "steps": completed_steps}


def prepared_runtime_from_setup(setup: JsonDict | None, root: Path) -> JsonDict:
    """Return PATH/Python hints produced by typed setup steps."""

    if not setup:
        return {}
    root = root.resolve()
    path_entries: List[str] = []
    python_path_entries: List[str] = []
    python_executable: Optional[str] = None
    for step in setup.get("steps") or []:
        cwd = setup_cwd(step, root)
        kind = str(step.get("uses") or "")
        if kind == "uv":
            venv_path = cwd / ".venv"
            path_entries.append(str(_venv_bin(venv_path)))
            python_path_entries.extend(str(path) for path in _venv_python_paths(venv_path))
            python_executable = str(_venv_python(venv_path))
        elif kind == "python-venv":
            venv_path = _safe_child(cwd, str(step.get("venv") or ".venv"))
            path_entries.append(str(_venv_bin(venv_path)))
            python_path_entries.extend(str(path) for path in _venv_python_paths(venv_path))
            python_executable = str(_venv_python(venv_path))
        elif kind == "npm":
            path_entries.append(str(cwd / "node_modules" / ".bin"))
    if not path_entries and not python_executable:
        return {}
    prepared: JsonDict = {"pathPrepend": list(reversed(path_entries))}
    if python_path_entries:
        prepared["pythonPathPrepend"] = list(dict.fromkeys(reversed(python_path_entries)))
    if python_executable:
        prepared["pythonExecutable"] = python_executable
    return prepared


def apply_prepared_env(env: Dict[str, str], prepared_env: JsonDict | None) -> Dict[str, str]:
    """Apply prepared runtime env without hiding the host PATH/PYTHONPATH."""

    result = dict(env)
    for key, value in (prepared_env or {}).items():
        key = str(key)
        value = str(value)
        if key in {"PATH", "PYTHONPATH"} and result.get(key):
            result[key] = _join_unique_paths([*value.split(os.pathsep), *result[key].split(os.pathsep)])
        else:
            result[key] = value
    return result


def setup_env(setup: JsonDict) -> Dict[str, str]:
    env = _minimal_host_env()
    for key in setup.get("envFromHost", []) or []:
        key = str(key)
        if key in os.environ:
            env[key] = os.environ[key]
    env.update({str(key): str(value) for key, value in (setup.get("env") or {}).items()})
    return env


def _minimal_host_env() -> Dict[str, str]:
    keys = [
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SystemRoot",
    ]
    return {key: os.environ[key] for key in keys if key in os.environ}


def setup_commands_for_step(step: JsonDict, root: Path) -> List[List[str]]:
    kind = str(step.get("uses") or "")
    cwd = setup_cwd(step, root)
    if kind == "uv":
        command = ["uv", "sync"]
        for extra in step.get("extras", []) or []:
            command.extend(["--extra", str(extra)])
        for group in step.get("groups", []) or []:
            command.extend(["--group", str(group)])
        if bool(step.get("frozen")):
            command.append("--frozen")
        return [command]
    if kind == "python-venv":
        venv = str(step.get("venv") or ".venv")
        python = str(step.get("python") or sys.executable)
        venv_path = _safe_child(cwd, venv)
        commands = [[python, "-m", "venv", str(venv_path)]]
        pip = _venv_pip(venv_path)
        requirements = list(step.get("requirements", []) or [])
        if not requirements and (cwd / "requirements.txt").exists():
            requirements = ["requirements.txt"]
        for requirement in requirements:
            commands.append([str(pip), "install", "-r", str(_safe_child(cwd, requirement))])
        if bool(step.get("installProject")):
            commands.append([str(pip), "install", "-e", "."])
        return commands
    if kind == "npm":
        return [["npm", str(step.get("install") or "ci")]]
    if kind == "command":
        command = [str(item) for item in step.get("command", []) or []]
        if command:
            return [command]
    raise ValueError(f"Unsupported setup step: {kind!r}")


def setup_cwd(step: JsonDict, root: Path) -> Path:
    cwd = step.get("cwd") or "."
    return _safe_child(root, str(cwd))


def _safe_child(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Setup path must stay inside the editable copy: {value}") from exc
    return resolved


def _venv_bin(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts"
    return venv_path / "bin"


def _venv_python(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def _venv_pip(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "pip"
    return venv_path / "bin" / "pip"


def _venv_python_paths(venv_path: Path) -> List[Path]:
    python = _venv_python(venv_path)
    if python.exists():
        paths = _inspect_site_packages(python)
        if paths:
            return paths
    if sys.platform == "win32":
        return [venv_path / "Lib" / "site-packages"]
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return [venv_path / "lib" / version / "site-packages"]


def _inspect_site_packages(python: Path) -> List[Path]:
    code = (
        "import json, site, sysconfig; "
        "paths = []; "
        "paths.extend(site.getsitepackages() if hasattr(site, 'getsitepackages') else []); "
        "purelib = sysconfig.get_path('purelib'); "
        "platlib = sysconfig.get_path('platlib'); "
        "paths.extend([purelib, platlib]); "
        "print(json.dumps([p for p in dict.fromkeys(paths) if p]))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    try:
        import json

        payload = json.loads(completed.stdout)
    except Exception:
        return []
    return [Path(str(path)) for path in payload if path]


def _join_unique_paths(paths: List[str]) -> str:
    return os.pathsep.join(dict.fromkeys(path for path in paths if path))
