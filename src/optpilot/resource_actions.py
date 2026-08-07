"""Named, typed, headless Resource actions (F4).

A resource action is one registered operation of a Resource — a command with
typed inputs — runnable without launching the Resource's web interface.  The
authoring shape lives in ``resource.schema.json`` (``actions``); this module
compiles the declarations into typed specs and executes one action as a
bounded local subprocess.

Execution contract (what the authored command sees):

- ``OPTPILOT_RESOURCE_ACTION_INPUTS_FILE`` — path to a JSON file holding the
  validated input values (declared defaults applied).
- ``OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT`` — an existing writable directory
  the action should write its results into.
- Command tokens may name ``{inputs_file}``, ``{output_root}``, and
  ``{input:<key>}`` — the latter substitutes one validated scalar input value
  as a single argv token.  Placeholder references are checked at validation
  time against the action's declared inputs.
- ``grants.envFromHost`` / ``grants.secretsFromHost`` name host environment
  values passed through verbatim; a missing name fails before execution.
- When the action declares ``runtime.setup``, the steps run in the resource
  root before the command (Studio's prepared-runtime cache is not used by
  this local path; setup scripts should be idempotent).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from .method_protocol_limits import RETAINED_COMMAND_METHOD_INTERPRETERS
from .parameter_values import apply_parameter_defaults, validate_parameter_values
from .realm.run_closure import InterfaceRuntimeSpec


INPUTS_FILE_PLACEHOLDER = "{inputs_file}"
OUTPUT_ROOT_PLACEHOLDER = "{output_root}"
INPUT_PLACEHOLDER_PREFIX = "{input:"
INPUTS_FILE_ENV = "OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"
OUTPUT_ROOT_ENV = "OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"

MAX_RESOURCE_ACTIONS = 16
MAX_ACTION_TIMEOUT_SECONDS = 86_400
_MAX_CAPTURE_CHARS = 64 * 1024
_MAX_LISTED_OUTPUT_FILES = 256
_SCALAR_INPUT_TYPES = {"float", "int", "bool", "string", "categorical"}

_ACTION_ALLOWED_FIELDS = {
    "command",
    "cwd",
    "description",
    "env",
    "grants",
    "id",
    "inputs",
    "label",
    "runtime",
    "timeoutSeconds",
}
_ACTION_REQUIRED_FIELDS = {"command", "id", "label", "timeoutSeconds"}


@dataclass(frozen=True)
class ResourceActionSpec:
    """Complete typed declaration of one headless resource action."""

    action_id: str
    label: str
    description: str
    command: Tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    inputs: Mapping[str, Any]
    env_from_host: Tuple[str, ...]
    secrets_from_host: Tuple[str, ...]
    network: str
    runtime: Mapping[str, Any]
    timeout_seconds: int

    def input_placeholders(self) -> Tuple[str, ...]:
        """Return the declared input keys referenced by command tokens."""

        keys = []
        for token in self.command:
            for key in _input_placeholder_keys(token):
                if key not in keys:
                    keys.append(key)
        return tuple(keys)


def compile_resource_actions(
    resource: Mapping[str, Any],
    *,
    location: str = "<resource>",
) -> List[ResourceActionSpec]:
    """Compile and validate a resource config's ``actions`` declarations."""

    raw_actions = resource.get("actions")
    if raw_actions is None:
        return []
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError(f"{location} actions must be a non-empty list.")
    if len(raw_actions) > MAX_RESOURCE_ACTIONS:
        raise ValueError(
            f"{location} actions contains more than {MAX_RESOURCE_ACTIONS} entries."
        )
    actions = [
        _compile_resource_action(raw, f"{location} actions[{index}]")
        for index, raw in enumerate(raw_actions)
    ]
    if len({action.action_id for action in actions}) != len(actions):
        raise ValueError(f"{location} action ids must be unique.")
    return actions


def _compile_resource_action(raw: Any, location: str) -> ResourceActionSpec:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{location} must be an object.")
    actual = set(raw)
    if not actual <= _ACTION_ALLOWED_FIELDS or not _ACTION_REQUIRED_FIELDS <= actual:
        raise ValueError(
            f"{location} fields differ; "
            f"missing={sorted(_ACTION_REQUIRED_FIELDS - actual)!r}, "
            f"extra={sorted(actual - _ACTION_ALLOWED_FIELDS)!r}."
        )
    action_id = raw["id"]
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{location}.id must be a non-empty string.")
    label = raw["label"]
    if not isinstance(label, str) or not label:
        raise ValueError(f"{location}.label must be a non-empty string.")
    command = raw["command"]
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise ValueError(f"{location}.command must be a non-empty string list.")
    cwd = raw.get("cwd", ".")
    if (
        not isinstance(cwd, str)
        or not cwd
        or cwd.startswith(("/", "\\"))
        or (len(cwd) >= 3 and cwd[1] == ":" and cwd[2] in {"/", "\\"})
        or ".." in cwd.split("/")
    ):
        raise ValueError(
            f"{location}.cwd must be a portable directory relative to the resource root."
        )
    env = raw.get("env", {})
    if not isinstance(env, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise ValueError(f"{location}.env must map string names to string values.")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, Mapping):
        raise ValueError(f"{location}.inputs must be an object.")
    grants = raw.get("grants", {})
    if not isinstance(grants, Mapping) or not set(grants) <= {
        "envFromHost",
        "network",
        "secretsFromHost",
    }:
        raise ValueError(
            f"{location}.grants may contain only envFromHost, network, and secretsFromHost."
        )
    env_from_host = _string_list(grants.get("envFromHost", []), f"{location}.grants.envFromHost")
    secrets_from_host = _string_list(
        grants.get("secretsFromHost", []), f"{location}.grants.secretsFromHost"
    )
    network = grants.get("network", "disabled")
    if network not in {"disabled", "enabled"}:
        raise ValueError(f"{location}.grants.network must be 'disabled' or 'enabled'.")
    runtime = raw.get("runtime", {})
    if runtime not in (None, {}):
        try:
            compiled_runtime = InterfaceRuntimeSpec.from_authoring_dict(runtime)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{location}.runtime is invalid: {error}") from error
        if compiled_runtime.sandbox == "container":
            raise ValueError(
                f"{location}.runtime.sandbox 'container' is not executable by the "
                "local resource-action path; declare a process runtime."
            )
    timeout_seconds = raw["timeoutSeconds"]
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= MAX_ACTION_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"{location}.timeoutSeconds must be an integer between 1 and "
            f"{MAX_ACTION_TIMEOUT_SECONDS}."
        )

    spec = ResourceActionSpec(
        action_id=action_id,
        label=label,
        description=str(raw.get("description", "")),
        command=tuple(command),
        cwd=cwd,
        env=dict(env),
        inputs=dict(inputs),
        env_from_host=env_from_host,
        secrets_from_host=secrets_from_host,
        network=network,
        runtime=dict(runtime or {}),
        timeout_seconds=timeout_seconds,
    )
    _validate_command_placeholders(spec, location)
    return spec


def _string_list(value: Any, location: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{location} must be a list of non-empty strings.")
    return tuple(value)


def _input_placeholder_keys(token: str) -> List[str]:
    keys = []
    start = 0
    while True:
        begin = token.find(INPUT_PLACEHOLDER_PREFIX, start)
        if begin < 0:
            return keys
        end = token.find("}", begin)
        if end < 0:
            raise ValueError(
                f"command token {token!r} has an unterminated {{input:...}} placeholder."
            )
        keys.append(token[begin + len(INPUT_PLACEHOLDER_PREFIX) : end])
        start = end + 1


def _validate_command_placeholders(spec: ResourceActionSpec, location: str) -> None:
    for token in spec.command:
        try:
            keys = _input_placeholder_keys(token)
        except ValueError as error:
            raise ValueError(f"{location}.{error}") from error
        for key in keys:
            declaration = spec.inputs.get(key)
            if not isinstance(declaration, Mapping):
                raise ValueError(
                    f"{location}.command references undeclared input {key!r}."
                )
            if declaration.get("valueType") not in _SCALAR_INPUT_TYPES:
                raise ValueError(
                    f"{location}.command input {key!r} must be a scalar value type "
                    f"({sorted(_SCALAR_INPUT_TYPES)}); pass structured values "
                    "through the inputs file instead."
                )


def find_resource_action(
    actions: List[ResourceActionSpec], action_id: str
) -> ResourceActionSpec:
    for action in actions:
        if action.action_id == action_id:
            return action
    known = sorted(action.action_id for action in actions)
    raise ValueError(
        f"Resource declares no action {action_id!r}; declared actions: {known}."
    )


def run_resource_action(
    resource_config_path: str | Path,
    action_id: str,
    *,
    input_values: Mapping[str, Any] | None = None,
    output_root: str | Path,
    run_setup: bool = True,
    host_env: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Execute one declared resource action headlessly and return a summary.

    ``host_env`` defaults to ``os.environ`` and is the source for
    ``grants.envFromHost`` / ``grants.secretsFromHost`` values.
    """

    from .config import validate_authoring_config  # local import: config imports us
    import yaml

    resource_path = Path(resource_config_path).expanduser().resolve()
    validation = validate_authoring_config(resource_path)
    if not validation.get("valid"):
        raise ValueError(
            f"{resource_path} is not a valid resource config: "
            + "; ".join(validation.get("errors", []))
        )
    with resource_path.open("r", encoding="utf-8") as handle:
        resource = yaml.safe_load(handle) or {}
    if resource.get("config") != "resource":
        raise ValueError(f"{resource_path} is not a resource config.")
    resource_root = resource_path.parent
    actions = compile_resource_actions(resource, location=str(resource_path))
    if not actions:
        raise ValueError(f"{resource_path} declares no actions.")
    action = find_resource_action(actions, action_id)

    errors = validate_parameter_values(
        input_values, action.inputs, location="inputs"
    )
    if errors:
        raise ValueError(
            "Action input values are invalid: " + "; ".join(errors)
        )
    bound_inputs = apply_parameter_defaults(input_values, action.inputs)

    environment = dict(host_env if host_env is not None else os.environ)
    missing = [
        name
        for name in (*action.env_from_host, *action.secrets_from_host)
        if name not in environment
    ]
    if missing:
        raise ValueError(
            "Action requires host environment values that are not set: "
            + ", ".join(sorted(missing))
        )

    output_path = Path(output_root).expanduser().resolve()
    if output_path.exists():
        if not output_path.is_dir():
            raise ValueError(f"Output root {output_path} is not a directory.")
        if any(output_path.iterdir()):
            raise ValueError(
                f"Output root {output_path} is not empty; choose a fresh directory."
            )
    else:
        output_path.mkdir(parents=True)

    setup_summary: Dict[str, Any] | None = None
    runtime_setup = (
        action.runtime.get("setup") if isinstance(action.runtime, Mapping) else None
    )
    if runtime_setup and run_setup:
        from .setup import run_process_setup

        setup_summary = run_process_setup(dict(runtime_setup), resource_root)

    work_dir = Path(tempfile.mkdtemp(prefix="optpilot-resource-action-"))
    started = time.monotonic()
    try:
        inputs_file = work_dir / "inputs.json"
        inputs_file.write_text(
            json.dumps(bound_inputs, indent=2, sort_keys=True), encoding="utf-8"
        )
        argv = [
            _substitute_token(token, inputs_file, output_path, bound_inputs)
            for token in action.command
        ]
        # Mirror the retained command-method contract (F3): a python/python3
        # head means "the interpreter running optpilot", so actions work
        # without a python on PATH and see the same user-provisioned
        # dependencies as the rest of the host installation.
        if argv and argv[0] in RETAINED_COMMAND_METHOD_INTERPRETERS:
            import sys

            argv[0] = sys.executable
        run_env = {
            **_minimal_action_env(environment),
            **dict(action.env),
            **{
                name: environment[name]
                for name in (*action.env_from_host, *action.secrets_from_host)
            },
            INPUTS_FILE_ENV: str(inputs_file),
            OUTPUT_ROOT_ENV: str(output_path),
        }
        cwd = (resource_root / action.cwd).resolve()
        if not cwd.is_dir():
            raise ValueError(f"Action cwd {cwd} is not a directory.")
        timed_out = False
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=run_env,
                text=True,
                capture_output=True,
                timeout=action.timeout_seconds,
                check=False,
            )
            returncode: int | None = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = None
            stdout = _expired_text(error.stdout)
            stderr = _expired_text(error.stderr)
        duration = time.monotonic() - started
        ok = returncode == 0 and not timed_out
        result: Dict[str, Any] = {
            "ok": ok,
            "resource_id": str(resource.get("id", "")),
            "action_id": action.action_id,
            "label": action.label,
            "command": argv,
            "cwd": str(cwd),
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_seconds": round(duration, 3),
            "inputs": bound_inputs,
            "output_root": str(output_path),
            "outputs": _list_output_files(output_path),
            "stdout_tail": stdout[-_MAX_CAPTURE_CHARS:],
            "stderr_tail": stderr[-_MAX_CAPTURE_CHARS:],
        }
        if timed_out:
            result["error"] = (
                f"Action timed out after {action.timeout_seconds} seconds."
            )
        if setup_summary is not None:
            result["setup"] = {"ran": bool(setup_summary.get("ran"))}
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _substitute_token(
    token: str,
    inputs_file: Path,
    output_root: Path,
    bound_inputs: Mapping[str, Any],
) -> str:
    result = token.replace(INPUTS_FILE_PLACEHOLDER, str(inputs_file)).replace(
        OUTPUT_ROOT_PLACEHOLDER, str(output_root)
    )
    for key in _input_placeholder_keys(result):
        value = bound_inputs.get(key)
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        result = result.replace(f"{INPUT_PLACEHOLDER_PREFIX}{key}}}", rendered)
    return result


_MINIMAL_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "SYSTEMROOT",
    "SystemRoot",
)


def _minimal_action_env(host_env: Mapping[str, str]) -> Dict[str, str]:
    # The same key selection as setup.minimal_host_env, applied to an explicit
    # host environment so callers (and tests) can pin the source mapping.
    return {key: host_env[key] for key in _MINIMAL_ENV_KEYS if key in host_env}


def _expired_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _list_output_files(output_root: Path) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    for path in sorted(output_root.rglob("*")):
        if not path.is_file():
            continue
        files.append(
            {
                "path": str(path.relative_to(output_root)),
                "size": path.stat().st_size,
            }
        )
        if len(files) >= _MAX_LISTED_OUTPUT_FILES:
            files.append({"path": "...", "size": None})
            break
    return files
