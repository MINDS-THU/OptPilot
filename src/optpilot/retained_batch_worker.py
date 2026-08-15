"""Retained, replay-safe batch-method worker.

This module is the process-runtime slice for ``optpilot.method.batch.v1``,
covering both Python batch methods (an imported callable) and command batch
methods (one authored command executed per proposal exchange).  It deliberately
does not accept a study file, run directory, or evidence store.  Semantic
initialization comes from one complete typed, path-free run definition;
the worker derives the method contract and legacy constructor context from it.
Filesystem authority is limited to explicitly bound logical scopes below one
provider-owned projection root.  Proposal evidence is carried by each request.

The engine is independent of transport.  The provider-private transport uses
canonical length-prefixed JSON over a Unix-domain socket because supervised
workers have no protocol-bearing stdin/stdout.  Proposal and observation
responses remain replayable until the controller explicitly acknowledges them;
the cache never silently evicts a state-changing exchange.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import json
import math
import os
import random
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, TextIO

from .method_protocol_limits import (
    MAX_BATCH_EXCHANGE_ITEMS,
    MAX_DURABLE_METHOD_BYTES,
    RETAINED_COMMAND_METHOD_INTERPRETERS,
)
from .run_execution_profile import method_exchange_timeout_seconds
from .retained_file_candidates import (
    CANDIDATE_STAGING_QUOTA,
    FileCandidateDraft,
    FileCandidateDraftSelection,
    FileCandidateStagingBinding,
    candidate_staging_exchange_coordinate,
    durable_worker_response_digest,
    file_candidate_declaration_digest,
    file_candidate_draft_token,
    portable_selection,
)
from .realm._validation import thaw_json
from .realm.refs import canonical_json_bytes, request_digest
from .realm.run_closure import ScopePath
from .realm.run_definition import METHOD_CONTRACT_SCHEMA, RunDefinitionManifest
from .retained_study_compiler import (
    METHOD_CONTEXT_SCOPE,
    package_projection_contract_features,
)
from .spec import StudySpec


BATCH_PROTOCOL = "optpilot.method.batch.v1"
BATCH_REQUEST_SCHEMA = "optpilot.retained-python-batch-request.v2"
BATCH_RESPONSE_SCHEMA = "optpilot.retained-python-batch-response.v1"
BATCH_WORKER_INIT_SCHEMA = "optpilot.retained-python-batch-worker-init.v3"

MAX_BATCH_FRAME_BYTES = 8 * 1024 * 1024
MAX_BATCH_INIT_BYTES = 2 * 1024 * 1024
MAX_BATCH_DURABLE_RESPONSE_BYTES = MAX_DURABLE_METHOD_BYTES
MAX_BATCH_JSON_DEPTH = 64
MAX_BATCH_JSON_NODES = 100_000
MAX_UNIX_SOCKET_PATH_BYTES = 100

_HEADER = struct.Struct("!I")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@~-]*$")
_LOWER_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_COMMAND_INPUT_PLACEHOLDER = "{input_file}"
_COMMAND_OUTPUT_PLACEHOLDER = "{output_file}"
_MAX_COMMAND_STDERR_ECHO_CHARS = 64 * 1024
_CALL_CONTEXT_LOCK = threading.RLock()
_EXCHANGE_CHAIN_DOMAIN = b"optpilot/retained-batch-exchange-chain/v1\0"
INITIAL_BATCH_EXCHANGE_CHAIN = hashlib.sha256(_EXCHANGE_CHAIN_DOMAIN).hexdigest()


class RetainedBatchWorkerConfigurationError(ValueError):
    """The retained contract and operational projection cannot be bound."""


class _RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise RetainedBatchWorkerConfigurationError(f"{label} must be a mapping.")
    actual = set(value)
    if actual != expected:
        raise RetainedBatchWorkerConfigurationError(
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}."
        )


def _safe_id(value: Any, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string.")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be a path-free ASCII token.") from error
    if len(encoded) > max_bytes or _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a path-free ASCII token.")
    return value


def _portable_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RetainedBatchWorkerConfigurationError(
            f"{label} must be a portable relative path."
        )
    if value == ".":
        return value
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise RetainedBatchWorkerConfigurationError(
            f"{label} must be a portable relative path."
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(component in {"", ".", ".."} for component in path.parts)
        or str(path) != value
    ):
        raise RetainedBatchWorkerConfigurationError(
            f"{label} must be a portable relative path."
        )
    return value


def _absolute_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RetainedBatchWorkerConfigurationError(
            f"{label} must be an absolute operational path."
        )
    path = Path(value)
    if not path.is_absolute():
        raise RetainedBatchWorkerConfigurationError(
            f"{label} must be an absolute operational path."
        )
    return value


def _unix_socket_path(value: Any) -> str:
    path = _absolute_path(value, "socket path")
    try:
        encoded = os.fsencode(path)
    except UnicodeEncodeError as error:
        raise RetainedBatchWorkerConfigurationError(
            "worker socket path is not representable by the filesystem."
        ) from error
    if len(encoded) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise RetainedBatchWorkerConfigurationError(
            "worker socket path exceeds the portable Unix-socket length bound."
        )
    return path


def _validate_json(value: Any, label: str) -> bytes:
    """Validate bounded JSON without recursively trusting hostile structure."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_BATCH_JSON_NODES:
            raise ValueError(f"{label} exceeds the JSON node limit.")
        if depth > MAX_BATCH_JSON_DEPTH:
            raise ValueError(f"{label} exceeds the JSON nesting limit.")
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} contains a non-finite number.")
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} contains a non-string object key.")
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        raise ValueError(f"{label} contains a non-JSON value.")
    return canonical_json_bytes(thaw_json(value))


def _json_copy(value: Any, label: str) -> Any:
    return json.loads(_validate_json(value, label).decode("utf-8"))


def _response_digest(encoded: bytes) -> str:
    """Return the durable response digest, excluding live draft authority."""

    try:
        value = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("worker response bytes are invalid.") from error
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != encoded:
        raise ValueError("worker response bytes are not canonical.")
    return durable_worker_response_digest(value)


def retained_batch_exchange_chain_digest(
    previous_chain: str,
    *,
    exchange_id: str,
    exchange_sequence: int,
    request_digest_value: str,
    response_digest: str,
) -> str:
    """Advance the constant-size proof of one contiguous callback prefix."""

    if not isinstance(previous_chain, str) or _LOWER_DIGEST_RE.fullmatch(
        previous_chain
    ) is None:
        raise ValueError("previous exchange chain digest is invalid.")
    exchange = _safe_id(exchange_id, "exchange id")
    if (
        isinstance(exchange_sequence, bool)
        or not isinstance(exchange_sequence, int)
        or exchange_sequence <= 0
    ):
        raise ValueError("exchange sequence must be a positive integer.")
    for value, label in (
        (request_digest_value, "request digest"),
        (response_digest, "response digest"),
    ):
        if not isinstance(value, str) or _LOWER_DIGEST_RE.fullmatch(value) is None:
            raise ValueError(f"{label} is invalid.")
    record = canonical_json_bytes(
        {
            "exchange_id": exchange,
            "exchange_sequence": exchange_sequence,
            "request_digest": request_digest_value,
            "response_digest": response_digest,
        }
    )
    return hashlib.sha256(
        _EXCHANGE_CHAIN_DOMAIN + bytes.fromhex(previous_chain) + record
    ).hexdigest()


def _projection_root(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RetainedBatchWorkerConfigurationError(
            "projection root must be an absolute directory."
        )
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RetainedBatchWorkerConfigurationError(
            "projection root cannot be inspected."
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RetainedBatchWorkerConfigurationError(
            "projection root must be a real directory."
        )
    return path.resolve(strict=True)


def _candidate_staging_root(binding: FileCandidateStagingBinding) -> Path:
    """Authenticate the provider-prepared staging namespace without creating it."""

    if not isinstance(binding, FileCandidateStagingBinding):
        raise TypeError("binding must be a FileCandidateStagingBinding.")
    root = Path(binding.root_path)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise RetainedBatchWorkerConfigurationError(
            "file-candidate staging root cannot be inspected."
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RetainedBatchWorkerConfigurationError(
            "file-candidate staging root must be a real directory."
        )
    resolved = root.resolve(strict=True)
    for name in ("active", "frozen"):
        child = resolved / name
        try:
            child_metadata = child.lstat()
        except OSError as error:
            raise RetainedBatchWorkerConfigurationError(
                "file-candidate staging namespace is incomplete."
            ) from error
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
            child_metadata.st_mode
        ):
            raise RetainedBatchWorkerConfigurationError(
                "file-candidate staging namespace is invalid."
            )
    return resolved


def _projection_entry(
    root: Path, relative_path: str, label: str, *, directory: bool
) -> Path:
    relative_path = _portable_path(relative_path, label)
    current = root
    if relative_path != ".":
        for component in relative_path.split("/"):
            current = current / component
            try:
                metadata = current.lstat()
            except OSError as error:
                raise RetainedBatchWorkerConfigurationError(
                    f"{label} cannot be inspected."
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise RetainedBatchWorkerConfigurationError(
                    f"{label} must not traverse a symbolic link."
                )
    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise RetainedBatchWorkerConfigurationError(
            f"{label} cannot be resolved."
        ) from error
    if resolved != root and root not in resolved.parents:
        raise RetainedBatchWorkerConfigurationError(
            f"{label} escapes the projection root."
        )
    if directory and not resolved.is_dir():
        raise RetainedBatchWorkerConfigurationError(f"{label} is not a directory.")
    if not directory and not resolved.is_file():
        raise RetainedBatchWorkerConfigurationError(f"{label} is not a file.")
    return resolved


def _projection_directory(root: Path, relative_path: str, label: str) -> Path:
    return _projection_entry(root, relative_path, label, directory=True)


def _projection_file(root: Path, relative_path: str, label: str) -> Path:
    return _projection_entry(root, relative_path, label, directory=False)


def _origin_inside_import_roots(origin: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = origin.resolve(strict=True)
    except OSError:
        return False
    for root in roots:
        if resolved != root and root not in resolved.parents:
            continue
        try:
            relative = resolved.relative_to(root)
        except ValueError:  # pragma: no cover - guarded above
            continue
        current = root
        safe = True
        for component in relative.parts:
            current = current / component
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    safe = False
                    break
            except OSError:
                safe = False
                break
        if safe:
            return True
    return False


@contextlib.contextmanager
def _method_call_context(
    import_roots: Sequence[Path], workdir: Path, stdout_target: TextIO
):
    """Temporarily establish exact worker-global Python and cwd state."""

    with _CALL_CONTEXT_LOCK:
        prior_path = list(sys.path)
        prior_cwd = Path.cwd()
        try:
            sys.path[:0] = [str(path) for path in import_roots]
            os.chdir(workdir)
            with contextlib.redirect_stdout(stdout_target):
                yield
        finally:
            os.chdir(prior_cwd)
            sys.path[:] = prior_path


class StaticEvidenceView:
    """One immutable request's method-visible evidence projection."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("evidence must be a mapping.")
        self._payload = _json_copy(payload, "method evidence")

    def decision_context(self) -> dict[str, Any]:
        return _json_copy(self._payload, "method evidence")


def _validated_method_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "compatibility",
        "config",
        "implementation",
        "runtime_requirements",
        "sandbox_spec",
        "schema",
        "settings",
    }
    _exact_keys(value, expected, "retained method contract")
    contract = _json_copy(value, "retained method contract")
    if contract["schema"] != METHOD_CONTRACT_SCHEMA:
        raise RetainedBatchWorkerConfigurationError(
            "retained method contract schema is unsupported."
        )
    for name in (
        "compatibility",
        "config",
        "implementation",
        "runtime_requirements",
        "sandbox_spec",
        "settings",
    ):
        if not isinstance(contract[name], dict):
            raise RetainedBatchWorkerConfigurationError(
                f"retained method contract {name} must be a mapping."
            )
    implementation = contract["implementation"]
    implementation_type = implementation.get("type")
    if (
        implementation.get("protocol") != BATCH_PROTOCOL
        or implementation_type not in {"python", "command"}
    ):
        raise RetainedBatchWorkerConfigurationError(
            "retained batch worker requires a Python or command "
            "optpilot.method.batch.v1 method."
        )
    if implementation_type == "python":
        _exact_keys(
            implementation,
            {"callable", "protocol", "type"},
            "retained method implementation",
        )
        callable_ref = implementation["callable"]
        if not isinstance(callable_ref, str):
            raise RetainedBatchWorkerConfigurationError(
                "retained method callable must use module:object form."
            )
        module_name, separator, attribute_path = callable_ref.partition(":")
        if (
            not separator
            or not module_name
            or not attribute_path
            or any(not item.isidentifier() for item in module_name.split("."))
            or any(not item.isidentifier() for item in attribute_path.split("."))
        ):
            raise RetainedBatchWorkerConfigurationError(
                "retained method callable must use module:object form."
            )
    else:
        _exact_keys(
            implementation,
            {"command", "protocol", "type"},
            "retained method implementation",
        )
        command = implementation["command"]
        if (
            isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise RetainedBatchWorkerConfigurationError(
                "retained method command must be a non-empty string list."
            )
        if command[0] not in RETAINED_COMMAND_METHOD_INTERPRETERS:
            raise RetainedBatchWorkerConfigurationError(
                "retained method command must start with the logical "
                "interpreter name python or python3."
            )
        for item in command:
            if item.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(item):
                raise RetainedBatchWorkerConfigurationError(
                    "retained method command must not contain absolute host paths."
                )
    formats = contract["compatibility"].get("formats", [])
    if (
        isinstance(formats, (str, bytes))
        or not isinstance(formats, Sequence)
        or not formats
        or any(value not in {"parameters", "files"} for value in formats)
    ):
        raise RetainedBatchWorkerConfigurationError(
            "retained batch worker candidate compatibility is unsupported."
        )
    if len(_validate_json(contract, "retained method contract")) > MAX_BATCH_INIT_BYTES:
        raise RetainedBatchWorkerConfigurationError(
            "retained method contract exceeds the worker initialization bound."
        )
    return contract


def _method_definition(method_id: str, contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "compatibility": _json_copy(contract["compatibility"], "compatibility"),
        "config": _json_copy(contract["config"], "method config"),
        "id": method_id,
        "implementation": _json_copy(
            contract["implementation"], "method implementation"
        ),
        "runtime": _json_copy(
            contract["runtime_requirements"], "runtime requirements"
        ),
        "sandboxSpec": _json_copy(contract["sandbox_spec"], "sandbox spec"),
        "settings": _json_copy(contract["settings"], "method settings"),
    }


def _scope_paths_from_runtime(definition: RunDefinitionManifest) -> tuple[ScopePath, ...]:
    settings = definition.prepared_method_runtime.runtime_settings
    if not isinstance(settings, Mapping) or set(settings) != {"import_roots", "schema"}:
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime does not contain strict logical Python settings."
        )
    if (
        settings.get("schema")
        != "optpilot.logical-python-process-runtime-settings.v1"
    ):
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime Python settings schema is unsupported."
        )
    values = settings.get("import_roots")
    if not isinstance(values, (list, tuple)) or not values:
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime import roots are absent."
        )
    try:
        roots = tuple(ScopePath.from_dict(value) for value in values)
    except Exception as error:
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime import roots are invalid."
        ) from error
    if len(set(roots)) != len(roots):
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime import roots contain duplicates."
        )
    return roots


def _scope_relative_path(scope_root: str, value: ScopePath) -> str:
    scope_root = _portable_path(scope_root, f"projection scope {value.scope}")
    if scope_root == ".":
        return value.relative_path
    if value.relative_path == ".":
        return scope_root
    return f"{scope_root}/{value.relative_path}"


def _resolve_method_projection(
    definition: RunDefinitionManifest,
    projection_root: Path,
    scope_roots: Mapping[str, str],
) -> tuple[tuple[Path, ...], Path, Path, Path | None, dict[str, str]]:
    if not isinstance(scope_roots, Mapping):
        raise RetainedBatchWorkerConfigurationError(
            "method projection scope_roots must be a mapping."
        )
    try:
        roots = {
            _safe_id(scope, "method projection scope"): _portable_path(
                value, f"projection scope {scope}"
            )
            for scope, value in scope_roots.items()
        }
    except ValueError as error:
        raise RetainedBatchWorkerConfigurationError(str(error)) from error
    import_paths = _scope_paths_from_runtime(definition)
    workdir_path = definition.prepared_method_runtime.workdir
    authored_path = definition.method_revision.authored_config
    if workdir_path is None:
        raise RetainedBatchWorkerConfigurationError(
            "prepared method runtime has no logical workdir."
        )
    required_scopes = {
        authored_path.scope,
        workdir_path.scope,
        *(value.scope for value in import_paths),
    }
    context_binding = _method_context_binding(definition)
    expected_scopes = set(required_scopes)
    if context_binding is not None:
        expected_scopes.add(context_binding[0])
    if set(roots) != expected_scopes:
        raise RetainedBatchWorkerConfigurationError(
            "method projection scopes differ from the retained runtime requirements."
        )
    resolved_imports = tuple(
        _projection_directory(
            projection_root,
            _scope_relative_path(roots[value.scope], value),
            f"method import root {index}",
        )
        for index, value in enumerate(import_paths)
    )
    if len(set(resolved_imports)) != len(resolved_imports):
        raise RetainedBatchWorkerConfigurationError(
            "method import roots resolve to duplicate directories."
        )
    workdir = _projection_directory(
        projection_root,
        _scope_relative_path(roots[workdir_path.scope], workdir_path),
        "method workdir",
    )
    authored_config = _projection_file(
        projection_root,
        _scope_relative_path(roots[authored_path.scope], authored_path),
        "method authored config",
    )
    method_context_root: Path | None = None
    if context_binding is not None:
        logical_scope, source_scope, source_path = context_binding
        expected_alias = _scope_relative_path(
            roots[source_scope], ScopePath(source_scope, source_path)
        )
        if roots[logical_scope] != expected_alias:
            raise RetainedBatchWorkerConfigurationError(
                "method-context scope alias differs from its retained source."
            )
        method_context_root = _projection_directory(
            projection_root,
            roots[logical_scope],
            "method-context root",
        )
    return resolved_imports, workdir, authored_config, method_context_root, roots


def _method_context_binding(
    definition: RunDefinitionManifest,
) -> tuple[str, str, str] | None:
    environment = definition.evaluation_closure.environment_revision
    contract = environment.projection_contract
    try:
        features = package_projection_contract_features(contract)
    except (TypeError, ValueError) as error:
        raise RetainedBatchWorkerConfigurationError(
            "retained method-context projection contract is unsupported."
        ) from error
    if "method_context" not in features:
        return None
    binding = contract.get("method_context")
    if not isinstance(binding, Mapping) or set(binding) != {
        "logical_scope",
        "source",
    }:
        raise RetainedBatchWorkerConfigurationError(
            "retained method-context projection contract is invalid."
        )
    source = binding.get("source")
    if not isinstance(source, Mapping) or set(source) != {"path", "scope"}:
        raise RetainedBatchWorkerConfigurationError(
            "retained method-context projection source is invalid."
        )
    logical_scope = binding.get("logical_scope")
    source_scope = source.get("scope")
    source_path = source.get("path")
    if logical_scope != METHOD_CONTEXT_SCOPE:
        raise RetainedBatchWorkerConfigurationError(
            "retained method-context logical scope is unsupported."
        )
    try:
        source_scope = _safe_id(source_scope, "method-context source scope")
        source_path = _portable_path(source_path, "method-context source path")
    except ValueError as error:
        raise RetainedBatchWorkerConfigurationError(str(error)) from error
    method_sources = tuple(
        layer
        for layer in definition.method_revision.source_layers
        if layer.scope == source_scope
        and layer.source_subpath == "."
        and layer.destination_subpath == "."
    )
    environment_sources = tuple(
        layer
        for layer in environment.source_layers
        if layer.scope == source_scope
        and layer.source_subpath == "."
        and layer.destination_subpath == "."
    )
    if (
        len(method_sources) != 1
        or len(environment_sources) != 1
        or method_sources[0].snapshot_ref != environment_sources[0].snapshot_ref
    ):
        raise RetainedBatchWorkerConfigurationError(
            "retained method context is not anchored to the shared package source."
        )
    return logical_scope, source_scope, source_path


def _hydrate_method_context(
    value: Mapping[str, Any], method_context_root: Path | None
) -> dict[str, Any]:
    result = _json_copy(value, "method context")
    instructions = result.get("instructions", [])
    references = result.get("references", [])
    if (
        isinstance(instructions, (str, bytes))
        or not isinstance(instructions, list)
        or isinstance(references, (str, bytes))
        or not isinstance(references, list)
    ):
        raise RetainedBatchWorkerConfigurationError(
            "retained method-context declaration is invalid."
        )
    if method_context_root is None:
        if instructions or references:
            raise RetainedBatchWorkerConfigurationError(
                "retained method-context projection is absent."
            )
        return result
    result["instructions"] = [
        str(
            _projection_file(
                method_context_root,
                _portable_path(path, f"method-context instruction {index}"),
                f"method-context instruction {index}",
            )
        )
        for index, path in enumerate(instructions)
    ]
    hydrated_references = []
    for index, raw_reference in enumerate(references):
        if not isinstance(raw_reference, Mapping):
            raise RetainedBatchWorkerConfigurationError(
                "retained method-context reference is invalid."
            )
        reference = dict(raw_reference)
        reference["path"] = str(
            _projection_file(
                method_context_root,
                _portable_path(
                    reference.get("path"),
                    f"method-context reference {index}",
                ),
                f"method-context reference {index}",
            )
        )
        hydrated_references.append(reference)
    result["references"] = hydrated_references
    return result


def _hydrate_method_study_state(
    value: Mapping[str, Any], method_context_root: Path | None
) -> dict[str, Any]:
    result = _json_copy(value, "study state")
    if method_context_root is None:
        return result
    candidate_context = result.get("candidate_context")
    if not isinstance(candidate_context, dict):
        raise RetainedBatchWorkerConfigurationError(
            "retained candidate context is invalid."
        )
    method_context = candidate_context.get("methodContext", {})
    if not isinstance(method_context, Mapping):
        raise RetainedBatchWorkerConfigurationError(
            "retained method context is invalid."
        )
    candidate_context["methodContext"] = _hydrate_method_context(
        method_context, method_context_root
    )
    return result


def _hydrate_method_study_spec(
    study_spec: StudySpec, method_context_root: Path | None
) -> StudySpec:
    raw = _json_copy(study_spec.raw, "retained study context")
    candidate_context = raw["candidate"]["context"]
    adapter_context = raw["environment"]["adapter"]["config"]["context"]
    hydrated = _hydrate_method_context(
        candidate_context.get("methodContext", {}), method_context_root
    )
    candidate_context["methodContext"] = _json_copy(
        hydrated, "hydrated candidate method context"
    )
    adapter_context["methodContext"] = _json_copy(
        hydrated, "hydrated environment method context"
    )
    return StudySpec(path=study_spec.path, raw=raw)


def _retained_study_spec(
    definition: RunDefinitionManifest,
    method_definition: Mapping[str, Any],
    authored_config: Path,
) -> StudySpec:
    """Reconstruct only semantics already cross-checked by the typed manifest."""

    environment_revision = definition.evaluation_closure.environment_revision
    environment_contract = environment_revision.evaluator_contract
    expected_environment_fields = {
        "access_policy",
        "adapter",
        "mutation_policy",
        "runtime_contract",
        "runtime_requirements",
        "schema",
    }
    if not isinstance(environment_contract, Mapping) or set(
        environment_contract
    ) != expected_environment_fields:
        raise RetainedBatchWorkerConfigurationError(
            "retained environment contract cannot form a method study context."
        )
    control = definition.run_control_manifest
    convergence = control.convergence
    raw = {
        "apiVersion": "optpilot/v1",
        "candidate": _json_copy(
            environment_revision.candidate_contract, "candidate contract"
        ),
        "config": "run_spec",
        "environment": {
            "accessPolicy": environment_contract["access_policy"],
            "adapter": _json_copy(environment_contract["adapter"], "environment adapter"),
            "environmentId": environment_revision.environment_id,
            "mutationPolicy": environment_contract["mutation_policy"],
            "runtime": _json_copy(
                environment_contract["runtime_requirements"],
                "environment runtime requirements",
            ),
            "runtimeContract": _json_copy(
                environment_contract["runtime_contract"],
                "environment runtime contract",
            ),
        },
        "evidence": _json_copy(definition.evidence_policy, "evidence policy"),
        "execution": _json_copy(definition.execution_policy, "execution policy"),
        "metadata": _json_copy(definition.metadata, "run metadata"),
        "method": _json_copy(method_definition, "method definition"),
        "objective": _json_copy(
            definition.evaluation_closure.evaluation_template.objective,
            "run objective",
        ),
        "reproducibility": _json_copy(
            definition.reproducibility_policy, "reproducibility policy"
        ),
        "stopping": {
            "convergenceRule": {
                "config": {
                    "minDelta": convergence.min_delta,
                    "patienceTrials": convergence.patience_trials,
                },
                "implementation": "builtin.no_improvement",
            },
            "maxFailures": control.max_failures,
            "maxTrials": control.max_trials,
        },
    }
    return StudySpec(path=authored_config, raw=_json_copy(raw, "retained study context"))


def _load_method(
    callable_ref: str,
    definition: Mapping[str, Any],
    study_spec: StudySpec,
    rng: random.Random,
    import_roots: Sequence[Path],
) -> Any:
    module_name, _, attribute_path = callable_ref.partition(":")
    expected_origin = _validate_method_module_before_import(
        module_name, import_roots
    )
    module = importlib.import_module(module_name)
    origin = getattr(module, "__file__", None)
    try:
        actual_origin = (
            None if not isinstance(origin, str) else Path(origin).resolve(strict=True)
        )
    except OSError:
        actual_origin = None
    if actual_origin != expected_origin:
        raise RetainedBatchWorkerConfigurationError(
            "retained method callable resolved outside its declared import roots."
        )
    target: Any = module
    for component in attribute_path.split("."):
        try:
            target = getattr(target, component)
        except AttributeError as error:
            raise RetainedBatchWorkerConfigurationError(
                "retained method callable object is absent."
            ) from error
    if not callable(target):
        raise RetainedBatchWorkerConfigurationError(
            "retained method callable object is not callable."
        )
    return target(_json_copy(definition, "method definition"), study_spec, rng)


def _validate_method_module_before_import(
    module_name: str, import_roots: Sequence[Path]
) -> Path:
    """Resolve the target through declared roots before any module code runs."""

    search_locations: Sequence[str] = [str(path) for path in import_roots]
    components = module_name.split(".")
    expected_origin: Path | None = None
    for index in range(len(components)):
        qualified = ".".join(components[: index + 1])
        try:
            spec = importlib.machinery.PathFinder.find_spec(
                qualified, search_locations
            )
        except (AttributeError, ImportError, OSError, ValueError):
            spec = None
        if spec is None:
            raise RetainedBatchWorkerConfigurationError(
                "retained method callable module is absent from its import roots."
            )
        raw_origin = spec.origin
        if raw_origin in {None, "namespace"}:
            expected_origin = None
        elif raw_origin in {"built-in", "frozen"} or not isinstance(raw_origin, str):
            raise RetainedBatchWorkerConfigurationError(
                "retained method callable module is not a projected source module."
            )
        else:
            origin = Path(raw_origin)
            if not _origin_inside_import_roots(origin, import_roots):
                raise RetainedBatchWorkerConfigurationError(
                    "retained method callable module escapes its import roots."
                )
            expected_origin = origin.resolve(strict=True)

        locations = spec.submodule_search_locations
        resolved_locations: list[str] = []
        if locations is not None:
            for value in locations:
                location = Path(value)
                if not _origin_inside_import_roots(location, import_roots):
                    raise RetainedBatchWorkerConfigurationError(
                        "retained method package escapes its import roots."
                    )
                resolved_locations.append(str(location.resolve(strict=True)))
        if index < len(components) - 1:
            if not resolved_locations:
                raise RetainedBatchWorkerConfigurationError(
                    "retained method callable parent is not a package."
                )
            search_locations = resolved_locations

        loaded = sys.modules.get(qualified)
        if loaded is not None:
            loaded_origin = getattr(loaded, "__file__", None)
            try:
                resolved_loaded = (
                    None
                    if not isinstance(loaded_origin, str)
                    else Path(loaded_origin).resolve(strict=True)
                )
            except OSError:
                resolved_loaded = None
            if expected_origin is not None and resolved_loaded != expected_origin:
                raise RetainedBatchWorkerConfigurationError(
                    "a conflicting method module was already loaded."
                )
            if expected_origin is None:
                loaded_paths = getattr(loaded, "__path__", ())
                try:
                    resolved_loaded_paths = {
                        str(Path(value).resolve(strict=True)) for value in loaded_paths
                    }
                except OSError as error:
                    raise RetainedBatchWorkerConfigurationError(
                        "a conflicting method package was already loaded."
                    ) from error
                if resolved_loaded_paths != set(resolved_locations):
                    raise RetainedBatchWorkerConfigurationError(
                        "a conflicting method package was already loaded."
                    )

    if expected_origin is None:
        raise RetainedBatchWorkerConfigurationError(
            "retained method callable must resolve to a projected source file."
        )
    return expected_origin


class _RetainedCommandBatchMethod:
    """Executes the authored command once per proposal exchange.

    The retained slice keeps command batch methods inside the same prepared
    process runtime as Python batch methods: the command head is a logical
    interpreter name mapped to the worker's exact interpreter, authored
    imports resolve through the retained import roots via ``PYTHONPATH``, and
    the working directory is the projected method config directory.  The
    exchange follows the documented command batch protocol — one JSON request
    on stdin unless the command names ``{input_file}``, one JSON response on
    stdout unless it names ``{output_file}``.  Observations are not forwarded
    to the command; each proposal request already carries the method-visible
    evidence projection.
    """

    def __init__(
        self,
        *,
        definition: Mapping[str, Any],
        study_spec: StudySpec,
        import_roots: Sequence[Path],
        workdir: Path,
        seed: int,
        stdout_target: TextIO,
    ) -> None:
        self._definition = _json_copy(definition, "method definition")
        self._command = [
            str(item) for item in self._definition["implementation"]["command"]
        ]
        self._candidate_context = _json_copy(
            study_spec.candidate.get("context", {}), "candidate context"
        )
        self._objective = _json_copy(study_spec.objective, "run objective")
        self._import_roots = tuple(import_roots)
        self._workdir = workdir
        self._seed = seed
        self._stdout_target = stdout_target
        self._timeout = method_exchange_timeout_seconds(
            self._definition.get("runtime", {})
        )

    def propose(
        self,
        n_candidates: int,
        study_state: Mapping[str, Any],
        evidence_view: "StaticEvidenceView",
        *,
        exchange_id: str,
        exchange_sequence: int,
    ) -> list[dict[str, Any]]:
        call_dir = Path(
            tempfile.mkdtemp(prefix="optpilot-method-exchange-")
        )
        try:
            input_path = call_dir / "request.json"
            output_path = call_dir / "response.json"
            request = self._exchange_request(
                n_candidates,
                study_state,
                evidence_view,
                exchange_id=exchange_id,
                exchange_sequence=exchange_sequence,
                call_dir=call_dir,
            )
            request_text = json.dumps(request, sort_keys=True)
            uses_input_file = any(
                _COMMAND_INPUT_PLACEHOLDER in item for item in self._command
            )
            uses_output_file = any(
                _COMMAND_OUTPUT_PLACEHOLDER in item for item in self._command
            )
            if uses_input_file:
                input_path.write_text(request_text, encoding="utf-8")
            argv = [sys.executable]
            for item in self._command[1:]:
                argv.append(
                    item.replace(_COMMAND_INPUT_PLACEHOLDER, str(input_path))
                    .replace(_COMMAND_OUTPUT_PLACEHOLDER, str(output_path))
                )
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(self._workdir),
                    env=self._exchange_env(),
                    input=None if uses_input_file else request_text,
                    text=True,
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                self._echo_command_stderr(error.stderr)
                raise RuntimeError(
                    f"method command timed out after {self._timeout:g} seconds."
                ) from None
            self._echo_command_stderr(completed.stderr)
            if uses_output_file:
                self._echo_command_stderr(completed.stdout)
            if completed.returncode != 0:
                raise RuntimeError(
                    "method command failed with exit code "
                    f"{completed.returncode}: {_text_tail(completed.stderr)}"
                )
            if uses_output_file:
                if not output_path.is_file():
                    raise RuntimeError(
                        "method command did not write its {output_file} response."
                    )
                response_text = output_path.read_text(encoding="utf-8")
            else:
                response_text = completed.stdout
            try:
                response = json.loads(response_text or "")
            except ValueError:
                raise RuntimeError(
                    "method command response is not valid JSON."
                ) from None
            if not isinstance(response, Mapping):
                raise RuntimeError(
                    "method command response must be a JSON object."
                )
            for event in response.get("method_events", []) or []:
                if isinstance(event, Mapping):
                    self._stdout_target.write(
                        json.dumps(dict(event), sort_keys=True) + "\n"
                    )
            candidates = response.get("candidates", [])
            if not isinstance(candidates, list):
                raise RuntimeError(
                    "method command response candidates must be a list."
                )
            return candidates
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)

    def _exchange_request(
        self,
        n_candidates: int,
        study_state: Mapping[str, Any],
        evidence_view: "StaticEvidenceView",
        *,
        exchange_id: str,
        exchange_sequence: int,
        call_dir: Path,
    ) -> dict[str, Any]:
        study_state = _json_copy(study_state, "study state")
        runtime_context = dict(study_state.get("runtime_context", {}))
        runtime_context["method_workspace"] = str(call_dir)
        settings = self._definition.get(
            "settings", self._definition.get("config", {})
        )
        return {
            "protocol": BATCH_PROTOCOL,
            "request_id": exchange_id,
            "exchange_sequence": exchange_sequence,
            "n_candidates": n_candidates,
            "candidate": _json_copy(
                self._candidate_context.get("candidate", {}), "candidate contract"
            ),
            "methodContext": _json_copy(
                self._candidate_context.get("methodContext", {}), "method context"
            ),
            "study_state": study_state,
            "objective": _json_copy(self._objective, "run objective"),
            "candidate_context": _json_copy(
                self._candidate_context, "candidate context"
            ),
            "evidence": evidence_view.decision_context(),
            "runtime_context": runtime_context,
            "settings": _json_copy(settings, "method settings"),
            "config": _json_copy(
                self._definition.get("config", {}), "method config"
            ),
            "seed": self._seed,
        }

    def _exchange_env(self) -> dict[str, str]:
        env = dict(os.environ)
        entries = [str(path) for path in self._import_roots]
        existing = env.get("PYTHONPATH")
        if existing:
            entries.append(existing)
        if entries:
            env["PYTHONPATH"] = os.pathsep.join(entries)
        return env

    def _echo_command_stderr(self, text: Any) -> None:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not isinstance(text, str) or not text:
            return
        try:
            self._stdout_target.write(_text_tail(text))
        except Exception:
            pass


def _text_tail(text: Any, limit: int = _MAX_COMMAND_STDERR_ECHO_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    return text if len(text) <= limit else text[-limit:]


def _create_candidate_exchange_inbox(
    staging_root: Path,
    *,
    run_id: str,
    exchange_id: str,
    exchange_sequence: int,
) -> tuple[Path, Path]:
    """Create the one worker-owned exchange parent and method-visible inbox."""

    coordinate = candidate_staging_exchange_coordinate(
        run_id=run_id,
        exchange_id=exchange_id,
        exchange_sequence=exchange_sequence,
    )
    exchange_root = staging_root / "active" / coordinate
    frozen_root = staging_root / "frozen" / coordinate
    if os.path.lexists(exchange_root) or os.path.lexists(frozen_root):
        raise RetainedBatchWorkerConfigurationError(
            "file-candidate staging exchange already exists."
        )
    try:
        os.mkdir(exchange_root, 0o700)
        inbox = exchange_root / "inbox"
        os.mkdir(inbox, 0o700)
    except BaseException:
        _remove_candidate_exchange(exchange_root)
        raise
    return exchange_root, inbox


@dataclass(frozen=True)
class _MethodFileCandidateDraft:
    candidate_id: str
    lineage: Mapping[str, Any]
    generator: Mapping[str, Any]
    source_root: Path
    directories: tuple[str, ...]
    files: tuple[Mapping[str, Any], ...]
    entry_count: int
    total_bytes: int
    declaration_digest: str


def _freeze_file_candidate_exchange(
    candidates: Sequence[Mapping[str, Any]],
    *,
    binding: FileCandidateStagingBinding,
    exchange_root: Path,
    inbox: Path,
    exchange_id: str,
    exchange_sequence: int,
    method_id: str,
) -> list[dict[str, Any]]:
    """Validate every draft, rehome it, then freeze the exchange by rename."""

    prepared_values: list[_MethodFileCandidateDraft] = []
    used_entries = 0
    used_bytes = 0
    for candidate in candidates:
        item = _inspect_method_file_candidate(
            candidate,
            inbox=inbox,
            method_id=method_id,
            max_entries=CANDIDATE_STAGING_QUOTA.max_entries - used_entries,
            max_total_bytes=CANDIDATE_STAGING_QUOTA.max_total_bytes - used_bytes,
        )
        prepared_values.append(item)
        used_entries += item.entry_count
        used_bytes += item.total_bytes
    prepared = tuple(prepared_values)
    source_names = [item.source_root.name for item in prepared]
    if len(set(source_names)) != len(source_names):
        raise ValueError("File candidates cannot select the same staging bundle.")
    try:
        actual_names = sorted(os.listdir(inbox), key=os.fsencode)
    except OSError as error:
        raise RetainedBatchWorkerConfigurationError(
            "file-candidate inbox cannot be inspected."
        ) from error
    if actual_names != sorted(source_names, key=os.fsencode):
        raise ValueError(
            "File-candidate inbox contains undeclared or missing bundles."
        )

    inbox_fd = os.open(
        inbox,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    exchange_fd = os.open(
        exchange_root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for ordinal, item in enumerate(prepared):
            os.rename(
                item.source_root.name,
                f"candidate-{ordinal:08d}",
                src_dir_fd=inbox_fd,
                dst_dir_fd=exchange_fd,
            )
        if os.listdir(inbox):
            raise RetainedBatchWorkerConfigurationError(
                "file-candidate inbox changed during rehome."
            )
        os.rmdir("inbox", dir_fd=exchange_fd)
    finally:
        os.close(exchange_fd)
        os.close(inbox_fd)

    active_fd = os.open(
        exchange_root.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    frozen_fd = os.open(
        exchange_root.parent.parent / "frozen",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.rename(
            exchange_root.name,
            exchange_root.name,
            src_dir_fd=active_fd,
            dst_dir_fd=frozen_fd,
        )
    finally:
        os.close(frozen_fd)
        os.close(active_fd)

    result = []
    for ordinal, item in enumerate(prepared):
        selection = f"candidate-{ordinal:08d}/files"
        token = file_candidate_draft_token(
            binding=binding,
            exchange_id=exchange_id,
            exchange_sequence=exchange_sequence,
            ordinal=ordinal,
            declaration_digest=item.declaration_digest,
        )
        result.append(
            FileCandidateDraft(
                candidate_id=item.candidate_id,
                draft=FileCandidateDraftSelection(token, selection),
                lineage=item.lineage,
                generator=item.generator,
            ).to_candidate()
        )
    return result


def _inspect_method_file_candidate(
    candidate: Mapping[str, Any],
    *,
    inbox: Path,
    method_id: str,
    max_entries: int = CANDIDATE_STAGING_QUOTA.max_entries,
    max_total_bytes: int = CANDIDATE_STAGING_QUOTA.max_total_bytes,
) -> _MethodFileCandidateDraft:
    if not isinstance(candidate, Mapping) or not {
        "candidate_id",
        "format",
        "spec",
    }.issubset(candidate):
        raise TypeError("Python batch file candidate is malformed.")
    if set(candidate) - {"candidate_id", "format", "generator", "lineage", "spec"}:
        raise TypeError("Python batch file candidate contains unsupported fields.")
    if candidate["format"] != "files":
        raise TypeError("Python batch method returned the wrong candidate format.")
    candidate_id = candidate["candidate_id"]
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or candidate_id != candidate_id.strip()
    ):
        raise TypeError("Python batch file candidate id is invalid.")
    lineage = _json_copy(
        candidate.get("lineage", {"parents": []}), "file candidate lineage"
    )
    generator = _json_copy(
        candidate.get(
            "generator", {"method_id": method_id, "strategy": "external"}
        ),
        "file candidate generator",
    )
    if not isinstance(lineage, dict) or not isinstance(generator, dict):
        raise TypeError("Python batch file candidate metadata is malformed.")

    spec = candidate["spec"]
    if not isinstance(spec, Mapping) or set(spec) != {"bundleRef", "files"}:
        raise TypeError(
            "Python batch file candidate spec must contain only bundleRef and files."
        )
    bundle_ref = spec["bundleRef"]
    file_entries = spec["files"]
    if not isinstance(bundle_ref, str) or not Path(bundle_ref).is_absolute():
        raise TypeError("Python batch file candidate bundleRef is invalid.")
    if (
        isinstance(file_entries, (str, bytes))
        or not isinstance(file_entries, Sequence)
        or not file_entries
    ):
        raise TypeError("Python batch file candidate files are invalid.")
    selected = Path(bundle_ref)
    candidate_root = selected.parent
    inbox = inbox.resolve(strict=True)
    if selected.name != "files" or candidate_root.parent != inbox:
        raise ValueError("File candidate bundle is outside its exchange inbox.")
    _real_staging_directory(candidate_root, "file-candidate bundle")
    selected = _real_staging_directory(selected, "file-candidate selection")
    declared_paths = set()
    for raw in file_entries:
        if not isinstance(raw, Mapping) or set(raw) != {
            "contentRef",
            "path",
            "sha256",
            "sizeBytes",
        }:
            raise TypeError("Python batch candidate file declaration is malformed.")
        path = portable_selection(raw["path"], "candidate file path")
        if path in declared_paths:
            raise ValueError("Python batch candidate file paths contain duplicates.")
        declared_paths.add(path)
        content_ref = raw["contentRef"]
        if not isinstance(content_ref, str) or not Path(content_ref).is_absolute():
            raise TypeError("Python batch candidate contentRef is invalid.")
        expected = selected.joinpath(*PurePosixPath(path).parts)
        if Path(content_ref) != expected:
            raise ValueError("Candidate file contentRef differs from its logical path.")
        expected_sha = raw["sha256"]
        expected_size = raw["sizeBytes"]
        if (
            not isinstance(expected_sha, str)
            or _LOWER_DIGEST_RE.fullmatch(expected_sha.lower()) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise TypeError("Python batch candidate file declaration is invalid.")

    directories, files, entry_count, total_bytes = _draft_tree_declaration(
        selected,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
    )
    if {item["path"] for item in files} != declared_paths:
        raise ValueError("Candidate file declarations differ from the staged tree.")
    declaration_digest = file_candidate_declaration_digest(
        candidate_id=candidate_id,
        lineage=lineage,
        generator=generator,
        directories=directories,
        files=files,
    )
    return _MethodFileCandidateDraft(
        candidate_id=candidate_id,
        lineage=lineage,
        generator=generator,
        source_root=candidate_root,
        directories=directories,
        files=files,
        entry_count=entry_count,
        total_bytes=total_bytes,
        declaration_digest=declaration_digest,
    )


def _real_staging_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} cannot be inspected.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a real directory.")
    return path.resolve(strict=True)


def _draft_tree_declaration(
    selected: Path,
    *,
    max_entries: int = CANDIDATE_STAGING_QUOTA.max_entries,
    max_total_bytes: int = CANDIDATE_STAGING_QUOTA.max_total_bytes,
) -> tuple[tuple[str, ...], tuple[Mapping[str, Any], ...], int, int]:
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 0:
        raise ValueError("File-candidate remaining entry quota is invalid.")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes < 0
    ):
        raise ValueError("File-candidate remaining byte quota is invalid.")
    directories: list[str] = []
    files: list[Mapping[str, Any]] = []
    entry_count = 0
    total_bytes = 0
    for current, raw_directories, raw_files in os.walk(
        selected, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        raw_directories.sort(key=os.fsencode)
        raw_files.sort(key=os.fsencode)
        for name in raw_directories:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Candidate tree contains a non-directory entry.")
            entry_count += 1
            if entry_count > max_entries:
                raise ValueError("File-candidate exchange exceeds its entry quota.")
            directories.append(path.relative_to(selected).as_posix())
        for name in raw_files:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Candidate tree contains a non-regular file.")
            entry_count += 1
            if entry_count > max_entries:
                raise ValueError("File-candidate exchange exceeds its entry quota.")
            if metadata.st_size > CANDIDATE_STAGING_QUOTA.max_file_bytes:
                raise ValueError("File candidate contains an oversized file.")
            total_bytes += metadata.st_size
            if total_bytes > max_total_bytes:
                raise ValueError("File-candidate exchange exceeds its byte quota.")
            digest = hashlib.sha256(b"optpilot/blob/v1\0")
            with path.open("rb") as handle:
                remaining = metadata.st_size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Candidate file changed during declaration.")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    raise ValueError("Candidate file changed during declaration.")
            after = path.lstat()
            if (
                after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
                or after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns
                or stat.S_IMODE(after.st_mode) != stat.S_IMODE(metadata.st_mode)
            ):
                raise ValueError("Candidate tree changed during declaration.")
            files.append(
                {
                    "executable": bool(metadata.st_mode & 0o111),
                    "path": path.relative_to(selected).as_posix(),
                    "sha256": digest.hexdigest(),
                    "sizeBytes": metadata.st_size,
                }
            )
    return (
        tuple(sorted(directories, key=lambda item: item.encode("utf-8"))),
        tuple(sorted(files, key=lambda item: item["path"].encode("utf-8"))),
        entry_count,
        total_bytes,
    )


def _remove_candidate_exchange(exchange_root: Path) -> None:
    try:
        if os.path.lexists(exchange_root):
            shutil.rmtree(exchange_root)
    except OSError:
        # The generation volume remains cleanup debt.  Do not replace the
        # bounded method/protocol failure with a provider path-bearing error.
        pass


@dataclass(frozen=True)
class _CachedExchange:
    exchange_id: str
    exchange_sequence: int
    request_digest: str
    response_digest: str
    response_bytes: bytes


@dataclass(frozen=True)
class _AcknowledgedExchange:
    """Constant-size receipt for the last cumulative watermark advance."""

    exchange_id: str
    exchange_sequence: int
    request_digest: str
    response_digest: str
    chain_digest: str


class RetainedPythonBatchEngine:
    """Long-lived, replay-safe engine for one retained Python batch method."""

    def __init__(
        self,
        *,
        run_definition: RunDefinitionManifest,
        projection_root: str | os.PathLike[str],
        scope_roots: Mapping[str, str],
        candidate_staging: FileCandidateStagingBinding | None = None,
        max_payload_bytes: int = MAX_BATCH_FRAME_BYTES,
        diagnostic: Callable[[Mapping[str, Any]], None] | None = None,
        user_stdout: TextIO | None = None,
    ) -> None:
        if not isinstance(run_definition, RunDefinitionManifest):
            raise TypeError("run_definition must be a RunDefinitionManifest.")
        if run_definition.prepared_method_runtime.runtime_kind != "process":
            raise RetainedBatchWorkerConfigurationError(
                "retained batch worker requires a prepared process runtime."
            )
        if (
            isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or not 1024 <= max_payload_bytes <= MAX_BATCH_FRAME_BYTES
        ):
            raise RetainedBatchWorkerConfigurationError(
                "worker payload bound is outside its supported range."
            )
        root = _projection_root(projection_root)
        (
            resolved_import_roots,
            resolved_workdir,
            authored_config,
            method_context_root,
            resolved_scopes,
        ) = _resolve_method_projection(run_definition, root, scope_roots)
        method_revision = run_definition.method_revision
        method_id = method_revision.method_id
        contract = _validated_method_contract(method_revision.method_contract)
        candidate_format = run_definition.evaluation_closure.environment_revision.candidate_contract.get(
            "format"
        )
        formats = contract["compatibility"].get("formats", ())
        if candidate_format not in {"parameters", "files"} or candidate_format not in formats:
            raise RetainedBatchWorkerConfigurationError(
                "retained method compatibility differs from the environment candidate format."
            )
        if candidate_format == "files":
            if not isinstance(candidate_staging, FileCandidateStagingBinding):
                raise RetainedBatchWorkerConfigurationError(
                    "file-candidate worker requires an exact staging binding."
                )
            candidate_staging_root = _candidate_staging_root(candidate_staging)
        else:
            if candidate_staging is not None:
                raise RetainedBatchWorkerConfigurationError(
                    "parameter worker cannot receive file-candidate staging."
                )
            candidate_staging_root = None
        definition = _method_definition(method_id, contract)
        stdout_target = user_stdout if user_stdout is not None else sys.stderr
        if not hasattr(stdout_target, "write"):
            raise TypeError("user_stdout must be a text stream or None.")
        if diagnostic is not None and not callable(diagnostic):
            raise TypeError("diagnostic must be callable or None.")

        self.method_id = method_id
        self.run_definition = run_definition
        self.run_definition_digest = run_definition.digest
        self.method_contract = contract
        self.projection_root = root
        self.scope_roots = MappingProxyType(dict(resolved_scopes))
        self.import_roots = resolved_import_roots
        self.workdir = resolved_workdir
        self.authored_config = authored_config
        self.method_context_root = method_context_root
        self.candidate_format = candidate_format
        self.candidate_staging = candidate_staging
        self.candidate_staging_root = candidate_staging_root
        self.max_payload_bytes = max_payload_bytes
        self._diagnostic = diagnostic
        self._stdout_target = stdout_target
        self._pending: _CachedExchange | None = None
        self._acknowledged_sequence = 0
        self._acknowledged_chain = INITIAL_BATCH_EXCHANGE_CHAIN
        self._last_acknowledged: _AcknowledgedExchange | None = None
        self._lock = threading.RLock()
        self._closed = False
        study_spec = _retained_study_spec(
            run_definition, definition, authored_config
        )
        study_spec = _hydrate_method_study_spec(
            study_spec, self.method_context_root
        )
        seed = run_definition.reproducibility_policy["seedPolicy"]["globalSeed"]
        if contract["implementation"]["type"] == "command":
            self._method: Any = _RetainedCommandBatchMethod(
                definition=definition,
                study_spec=study_spec,
                import_roots=self.import_roots,
                workdir=self.workdir,
                seed=seed,
                stdout_target=self._stdout_target,
            )
        else:
            try:
                with _method_call_context(
                    self.import_roots, self.workdir, self._stdout_target
                ):
                    self._method = _load_method(
                        str(contract["implementation"]["callable"]),
                        definition,
                        study_spec,
                        random.Random(seed),
                        self.import_roots,
                    )
            except RetainedBatchWorkerConfigurationError:
                raise
            except Exception as error:
                self._record_diagnostic("initialize", error)
                raise RetainedBatchWorkerConfigurationError(
                    "retained method initialization failed."
                ) from None
            if not callable(getattr(self._method, "propose", None)):
                raise RetainedBatchWorkerConfigurationError(
                    "retained Python batch method must implement propose."
                )

    @property
    def cached_exchange_count(self) -> int:
        with self._lock:
            return int(self._pending is not None)

    @property
    def acknowledged_exchange_count(self) -> int:
        with self._lock:
            return self._acknowledged_sequence

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one JSON request and return a path-free bounded response."""

        with self._lock:
            exchange_id: str | None = None
            try:
                encoded = _validate_json(request, "batch worker request")
                if len(encoded) > self.max_payload_bytes:
                    raise _RequestError(
                        "request_too_large", "The worker request exceeds its size bound."
                    )
                if not isinstance(request, Mapping):
                    raise _RequestError(
                        "invalid_request", "The worker request is malformed."
                    )
                if request["schema"] != BATCH_REQUEST_SCHEMA:
                    raise _RequestError(
                        "unsupported_schema", "The worker request schema is unsupported."
                    )
                try:
                    exchange_id = _safe_id(request["exchange_id"], "exchange id")
                except ValueError as error:
                    raise _RequestError(
                        "invalid_exchange_id", "The exchange id is invalid."
                    ) from error
                op = request["op"]
                payload = request["payload"]
                if op not in {"propose", "observe", "ack", "status", "shutdown"}:
                    raise _RequestError(
                        "unsupported_operation", "The worker operation is unsupported."
                    )
                expected = {"exchange_id", "op", "payload", "schema"}
                if op in {"propose", "observe"}:
                    expected.add("exchange_sequence")
                if set(request) != expected:
                    raise _RequestError(
                        "invalid_request", "The worker request is malformed."
                    )
                if not isinstance(payload, Mapping):
                    raise _RequestError(
                        "invalid_request", "The worker request payload is malformed."
                    )
                payload = _json_copy(payload, "batch worker request payload")
                if op in {"propose", "observe"}:
                    sequence = request["exchange_sequence"]
                    if (
                        isinstance(sequence, bool)
                        or not isinstance(sequence, int)
                        or sequence <= 0
                    ):
                        raise _RequestError(
                            "invalid_exchange_sequence",
                            "The exchange sequence is invalid.",
                        )
                    return self._handle_cached(
                        exchange_id, sequence, op, payload
                    )
                if op == "ack":
                    return self._handle_ack(exchange_id, payload)
                if op == "status":
                    return self._handle_status(exchange_id, payload)
                return self._handle_shutdown(exchange_id, payload)
            except _RequestError as error:
                return self._error_response(
                    exchange_id, error.code, error.public_message
                )
            except Exception as error:
                diagnostic_id = self._record_diagnostic("request", error)
                return self._error_response(
                    exchange_id,
                    "invalid_request",
                    "The worker request is malformed.",
                    diagnostic_id,
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callback = getattr(self._method, "close", None)
            if callable(callback):
                try:
                    with _method_call_context(
                        self.import_roots, self.workdir, self._stdout_target
                    ):
                        callback()
                except Exception as error:
                    self._record_diagnostic("close", error)

    def _handle_cached(
        self,
        exchange_id: str,
        exchange_sequence: int,
        op: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        digest = request_digest({"op": op, "payload": payload})
        if exchange_sequence <= self._acknowledged_sequence:
            return self._error_response(
                exchange_id,
                "exchange_acknowledged",
                "The exchange was already acknowledged.",
            )
        expected_sequence = self._acknowledged_sequence + 1
        if exchange_sequence != expected_sequence:
            return self._error_response(
                exchange_id,
                "exchange_out_of_order",
                "The exchange is not the next contiguous operation.",
            )
        cached = self._pending
        if cached is not None:
            if (
                cached.exchange_id != exchange_id
                or cached.exchange_sequence != exchange_sequence
                or cached.request_digest != digest
            ):
                return self._error_response(
                    exchange_id,
                    "exchange_conflict",
                    "A different exchange is already pending acknowledgement.",
                )
            return json.loads(cached.response_bytes.decode("utf-8"))
        if self._closed:
            return self._error_response(
                exchange_id, "worker_closed", "The batch worker is closed."
            )
        try:
            if op == "propose":
                result = self._propose(
                    payload,
                    exchange_id=exchange_id,
                    exchange_sequence=exchange_sequence,
                )
            else:
                result = self._observe(payload)
            response = self._success_response(exchange_id, result)
        except _RequestError as error:
            response = self._error_response(
                exchange_id, error.code, error.public_message
            )
        except Exception as error:
            # The diagnostic id is part of the canonical public response and
            # therefore part of its durable digest.  It must be derived from
            # stable exchange facts so rebuilding a fresh worker can reproduce
            # the exact response.  Exception details remain private in the
            # diagnostic sink and are deliberately absent from this identity.
            diagnostic_id = self._record_diagnostic(
                op,
                error,
                diagnostic_id=self._exchange_diagnostic_id(
                    exchange_id=exchange_id,
                    exchange_sequence=exchange_sequence,
                    operation=op,
                    request_digest_value=digest,
                    error_code="method_failed",
                ),
            )
            response = self._error_response(
                exchange_id,
                "method_failed",
                "The retained method operation failed.",
                diagnostic_id,
            )
        response_bytes = self._encode_response_or_error(exchange_id, response)
        self._pending = _CachedExchange(
            exchange_id,
            exchange_sequence,
            digest,
            _response_digest(response_bytes),
            response_bytes,
        )
        return json.loads(response_bytes.decode("utf-8"))

    def _propose(
        self,
        payload: Mapping[str, Any],
        *,
        exchange_id: str,
        exchange_sequence: int,
    ) -> dict[str, Any]:
        if set(payload) != {"evidence", "n_candidates", "study_state"}:
            raise _RequestError(
                "invalid_proposal", "The proposal request payload is malformed."
            )
        n_candidates = payload["n_candidates"]
        if (
            isinstance(n_candidates, bool)
            or not isinstance(n_candidates, int)
            or not 1 <= n_candidates <= MAX_BATCH_EXCHANGE_ITEMS
        ):
            raise _RequestError(
                "invalid_proposal", "The proposal width is invalid."
            )
        study_state = payload["study_state"]
        evidence = payload["evidence"]
        if not isinstance(study_state, Mapping) or not isinstance(evidence, Mapping):
            raise _RequestError(
                "invalid_proposal", "The proposal request payload is malformed."
            )
        evidence_view = StaticEvidenceView(evidence)
        hydrated_study_state = _hydrate_method_study_state(
            study_state, self.method_context_root
        )
        exchange_root: Path | None = None
        inbox: Path | None = None
        if self.candidate_format == "files":
            assert self.candidate_staging is not None
            assert self.candidate_staging_root is not None
            exchange_root, inbox = _create_candidate_exchange_inbox(
                self.candidate_staging_root,
                run_id=self.candidate_staging.run_id,
                exchange_id=exchange_id,
                exchange_sequence=exchange_sequence,
            )
            if "runtime_context" in hydrated_study_state:
                _remove_candidate_exchange(exchange_root)
                raise RetainedBatchWorkerConfigurationError(
                    "method study state already contains runtime_context."
                )
            hydrated_study_state["runtime_context"] = {
                "candidate_staging_dir": str(inbox),
            }
        callback = self._method.propose
        try:
            if isinstance(self._method, _RetainedCommandBatchMethod):
                candidates = self._method.propose(
                    n_candidates,
                    hydrated_study_state,
                    evidence_view,
                    exchange_id=exchange_id,
                    exchange_sequence=exchange_sequence,
                )
            else:
                with _method_call_context(
                    self.import_roots, self.workdir, self._stdout_target
                ):
                    parameters = inspect.signature(callback).parameters
                    if len(parameters) >= 3:
                        candidates = callback(
                            n_candidates,
                            hydrated_study_state,
                            evidence_view,
                        )
                    else:
                        candidates = callback(
                            n_candidates, hydrated_study_state
                        )
            if not isinstance(candidates, list) or any(
                not isinstance(candidate, Mapping) for candidate in candidates
            ):
                raise TypeError("batch propose must return a list of mappings.")
            if len(candidates) > n_candidates:
                raise _RequestError(
                    "batch_overproduced",
                    "The method returned more candidates than the requested width.",
                )
            if self.candidate_format == "parameters":
                if any(candidate.get("format", "parameters") != "parameters" for candidate in candidates):
                    raise TypeError(
                        "Python batch method returned the wrong candidate format."
                    )
                return {"candidates": _json_copy(candidates, "method candidates")}

            assert self.candidate_staging is not None
            assert exchange_root is not None and inbox is not None
            wire_candidates = _freeze_file_candidate_exchange(
                candidates,
                binding=self.candidate_staging,
                exchange_root=exchange_root,
                inbox=inbox,
                exchange_id=exchange_id,
                exchange_sequence=exchange_sequence,
                method_id=self.method_id,
            )
            exchange_root = None  # The subtree now lives under frozen/.
            return {"candidates": wire_candidates}
        except BaseException:
            if exchange_root is not None:
                _remove_candidate_exchange(exchange_root)
            raise

    def _observe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != {"observations"}:
            raise _RequestError(
                "invalid_observation", "The observation request payload is malformed."
            )
        observations = payload["observations"]
        if not isinstance(observations, list) or any(
            not isinstance(observation, Mapping) for observation in observations
        ):
            raise _RequestError(
                "invalid_observation", "The observations are malformed."
            )
        if not 1 <= len(observations) <= MAX_BATCH_EXCHANGE_ITEMS:
            raise _RequestError(
                "invalid_observation",
                "The observation batch width is invalid.",
            )
        callback = getattr(self._method, "observe", None)
        if callable(callback):
            with _method_call_context(
                self.import_roots, self.workdir, self._stdout_target
            ):
                callback(_json_copy(observations, "method observations"))
        return {"observation_count": len(observations)}

    def _handle_ack(
        self, exchange_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(payload) != {"exchange"} or not isinstance(
            payload.get("exchange"), Mapping
        ):
            return self._error_response(
                exchange_id, "invalid_ack", "The acknowledgement is malformed."
            )
        value = payload["exchange"]
        if set(value) != {
            "exchange_id",
            "exchange_sequence",
            "request_digest",
            "response_digest",
        }:
            return self._error_response(
                exchange_id, "invalid_ack", "The acknowledgement is malformed."
            )
        try:
            target = _safe_id(value["exchange_id"], "acknowledged exchange id")
            sequence = value["exchange_sequence"]
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= 0
            ):
                raise ValueError("invalid exchange sequence")
            request_digest_value = value["request_digest"]
            response_digest_value = value["response_digest"]
            if not isinstance(
                request_digest_value, str
            ) or _LOWER_DIGEST_RE.fullmatch(request_digest_value) is None:
                raise ValueError("invalid request digest")
            if not isinstance(
                response_digest_value, str
            ) or _LOWER_DIGEST_RE.fullmatch(response_digest_value) is None:
                raise ValueError("invalid response digest")
        except (KeyError, TypeError, ValueError):
            return self._error_response(
                exchange_id, "invalid_ack", "The acknowledgement is malformed."
            )

        claimed = _AcknowledgedExchange(
            target,
            sequence,
            request_digest_value,
            response_digest_value,
            retained_batch_exchange_chain_digest(
                self._acknowledged_chain,
                exchange_id=target,
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
                response_digest=response_digest_value,
            ),
        )
        last = self._last_acknowledged
        if sequence == self._acknowledged_sequence:
            if last is None or any(
                getattr(last, field) != getattr(claimed, field)
                for field in (
                    "exchange_id",
                    "exchange_sequence",
                    "request_digest",
                    "response_digest",
                )
            ):
                return self._error_response(
                    exchange_id,
                    "ack_conflict",
                    "The acknowledgement differs from the current watermark.",
                )
            return self._success_response(exchange_id, self._ack_result(last))
        if sequence != self._acknowledged_sequence + 1:
            return self._error_response(
                exchange_id,
                "ack_out_of_order",
                "The acknowledgement is not the next contiguous exchange.",
            )
        pending = self._pending
        if pending is None or (
            pending.exchange_id != target
            or pending.exchange_sequence != sequence
            or pending.request_digest != request_digest_value
            or pending.response_digest != response_digest_value
        ):
            return self._error_response(
                exchange_id,
                "ack_conflict",
                "The acknowledgement differs from the pending exchange.",
            )
        acknowledged = _AcknowledgedExchange(
            target,
            sequence,
            request_digest_value,
            response_digest_value,
            retained_batch_exchange_chain_digest(
                self._acknowledged_chain,
                exchange_id=target,
                exchange_sequence=sequence,
                request_digest_value=request_digest_value,
                response_digest=response_digest_value,
            ),
        )
        self._pending = None
        self._acknowledged_sequence = sequence
        self._acknowledged_chain = acknowledged.chain_digest
        self._last_acknowledged = acknowledged
        return self._success_response(
            exchange_id, self._ack_result(acknowledged)
        )

    @staticmethod
    def _ack_result(value: _AcknowledgedExchange) -> dict[str, Any]:
        return {
            "acknowledged_chain": value.chain_digest,
            "acknowledged_exchange": {
                "exchange_id": value.exchange_id,
                "exchange_sequence": value.exchange_sequence,
                "request_digest": value.request_digest,
                "response_digest": value.response_digest,
            },
            "acknowledged_sequence": value.exchange_sequence,
        }

    def _handle_status(
        self, exchange_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if payload:
            return self._error_response(
                exchange_id, "invalid_status", "The status request is malformed."
            )
        pending = self._pending
        return self._success_response(
            exchange_id,
            {
                "acknowledged_chain": self._acknowledged_chain,
                "acknowledged_sequence": self._acknowledged_sequence,
                "pending_exchange": (
                    None
                    if pending is None
                    else {
                        "exchange_id": pending.exchange_id,
                        "exchange_sequence": pending.exchange_sequence,
                        "request_digest": pending.request_digest,
                        "response_digest": pending.response_digest,
                    }
                ),
                "pending_response_bytes": (
                    0 if pending is None else len(pending.response_bytes)
                ),
            },
        )

    def _handle_shutdown(
        self, exchange_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if payload:
            return self._error_response(
                exchange_id, "invalid_shutdown", "The shutdown request is malformed."
            )
        self.close()
        return self._success_response(exchange_id, {"shutdown": True})

    def _success_response(
        self, exchange_id: str, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "exchange_id": exchange_id,
            "ok": True,
            "result": _json_copy(result, "worker result"),
            "schema": BATCH_RESPONSE_SCHEMA,
        }

    def _error_response(
        self,
        exchange_id: str | None,
        code: str,
        message: str,
        diagnostic_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "error": {
                "code": _safe_id(code, "worker error code", max_bytes=128),
                "diagnostic_id": diagnostic_id,
                "message": message[:256],
            },
            "exchange_id": exchange_id,
            "ok": False,
            "schema": BATCH_RESPONSE_SCHEMA,
        }

    def _encode_response_or_error(
        self, exchange_id: str, response: Mapping[str, Any]
    ) -> bytes:
        try:
            encoded = _validate_json(response, "batch worker response")
            if len(encoded) <= min(
                self.max_payload_bytes, MAX_BATCH_DURABLE_RESPONSE_BYTES
            ):
                return encoded
        except Exception as error:
            self._record_diagnostic("response", error)
        fallback = self._error_response(
            exchange_id,
            "response_too_large",
            "The retained method response exceeds its size bound.",
        )
        return canonical_json_bytes(fallback)

    def _exchange_diagnostic_id(
        self,
        *,
        exchange_id: str,
        exchange_sequence: int,
        operation: str,
        request_digest_value: str,
        error_code: str,
    ) -> str:
        """Return the stable private-log coordinate embedded in one response."""

        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "error_code": error_code,
                    "exchange_id": exchange_id,
                    "exchange_sequence": exchange_sequence,
                    "format": "optpilot.retained-batch-diagnostic-id.v1",
                    "operation": operation,
                    "request_digest": request_digest_value,
                    "run_definition_digest": self.run_definition_digest,
                }
            )
        ).hexdigest()[:32]

    def _record_diagnostic(
        self,
        operation: str,
        error: BaseException,
        *,
        diagnostic_id: str | None = None,
    ) -> str:
        if diagnostic_id is None:
            diagnostic_id = secrets.token_hex(16)
        elif (
            not isinstance(diagnostic_id, str)
            or len(diagnostic_id) != 32
            or any(character not in "0123456789abcdef" for character in diagnostic_id)
        ):
            raise ValueError("diagnostic_id must be 32 lowercase hexadecimal characters.")
        if self._diagnostic is None:
            return diagnostic_id
        event = {
            "diagnostic_id": diagnostic_id,
            "exception_type": type(error).__name__[:256],
            "message": str(error)[:16_384],
            "operation": operation[:128],
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )[-65_536:],
        }
        try:
            self._diagnostic(event)
        except Exception:
            pass
        return diagnostic_id


@dataclass(frozen=True)
class RetainedBatchWorkerInit:
    """Strict provider-private initialization record for the socket worker."""

    run_definition: RunDefinitionManifest
    projection_root: str
    scope_roots: Mapping[str, str]
    socket_path: str
    diagnostic_path: str
    candidate_staging: FileCandidateStagingBinding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_definition, RunDefinitionManifest):
            raise TypeError("run_definition must be a RunDefinitionManifest.")
        if self.run_definition.prepared_method_runtime.runtime_kind != "process":
            raise RetainedBatchWorkerConfigurationError(
                "retained batch worker requires a prepared process runtime."
            )
        _validated_method_contract(self.run_definition.method_revision.method_contract)
        if not isinstance(self.scope_roots, Mapping):
            raise RetainedBatchWorkerConfigurationError(
                "method projection scope_roots must be a mapping."
            )
        try:
            roots = {
                _safe_id(scope, "method projection scope"): _portable_path(
                    value, f"projection scope {scope}"
                )
                for scope, value in self.scope_roots.items()
            }
        except ValueError as error:
            raise RetainedBatchWorkerConfigurationError(str(error)) from error
        object.__setattr__(
            self,
            "scope_roots",
            MappingProxyType(
                dict(sorted(roots.items(), key=lambda item: item[0].encode("ascii")))
            ),
        )
        object.__setattr__(
            self, "projection_root", _absolute_path(self.projection_root, "projection root")
        )
        object.__setattr__(self, "socket_path", _unix_socket_path(self.socket_path))
        object.__setattr__(
            self,
            "diagnostic_path",
            _absolute_path(self.diagnostic_path, "diagnostic path"),
        )
        candidate_format = (
            self.run_definition.evaluation_closure.environment_revision.candidate_contract.get(
                "format"
            )
        )
        if candidate_format == "files":
            if not isinstance(self.candidate_staging, FileCandidateStagingBinding):
                raise RetainedBatchWorkerConfigurationError(
                    "file-candidate worker initialization lacks staging authority."
                )
        elif self.candidate_staging is not None:
            raise RetainedBatchWorkerConfigurationError(
                "non-file worker initialization cannot carry candidate staging."
            )
        if len(self.to_bytes()) > MAX_BATCH_INIT_BYTES:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization exceeds its size bound."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_staging": (
                None
                if self.candidate_staging is None
                else self.candidate_staging.to_dict()
            ),
            "diagnostic_path": self.diagnostic_path,
            "projection_root": self.projection_root,
            "run_definition": self.run_definition.to_dict(),
            "schema": BATCH_WORKER_INIT_SCHEMA,
            "scope_roots": dict(self.scope_roots),
            "socket_path": self.socket_path,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetainedBatchWorkerInit":
        _exact_keys(
            payload,
            {
                "candidate_staging",
                "diagnostic_path",
                "projection_root",
                "run_definition",
                "schema",
                "scope_roots",
                "socket_path",
            },
            "batch worker initialization",
        )
        if payload["schema"] != BATCH_WORKER_INIT_SCHEMA:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization schema is unsupported."
            )
        if not isinstance(payload["scope_roots"], dict):
            raise RetainedBatchWorkerConfigurationError(
                "batch worker scope roots must be a mapping."
            )
        try:
            definition = RunDefinitionManifest.from_dict(payload["run_definition"])
        except Exception as error:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker run definition is invalid."
            ) from error
        result = cls(
            run_definition=definition,
            projection_root=payload["projection_root"],
            scope_roots=payload["scope_roots"],
            socket_path=payload["socket_path"],
            diagnostic_path=payload["diagnostic_path"],
            candidate_staging=(
                None
                if payload["candidate_staging"] is None
                else FileCandidateStagingBinding.from_dict(
                    payload["candidate_staging"]
                )
            ),
        )
        if result.to_dict() != dict(payload):
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization is not canonical."
            )
        return result

    @classmethod
    def from_bytes(cls, payload: bytes) -> "RetainedBatchWorkerInit":
        if not isinstance(payload, bytes) or len(payload) > MAX_BATCH_INIT_BYTES:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization exceeds its size bound."
            )
        try:
            value = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization is not valid JSON."
            ) from error
        if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
            raise RetainedBatchWorkerConfigurationError(
                "batch worker initialization is not canonical JSON."
            )
        return cls.from_dict(value)


def _transport_error(code: str, message: str) -> bytes:
    return canonical_json_bytes(
        {
            "error": {
                "code": code,
                "diagnostic_id": None,
                "message": message,
            },
            "exchange_id": None,
            "ok": False,
            "schema": BATCH_RESPONSE_SCHEMA,
        }
    )


def _receive_exact(connection: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ConnectionError("socket frame ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_frame(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(_HEADER.pack(len(payload)) + payload)


class UnixBatchWorkerServer:
    """Sequential provider-private Unix-socket transport for one engine.

    Closing the listener deliberately leaves its pathname in place.  The
    supervising process provider records the ready endpoint's exact identity
    and is the sole authority that may unlink and durably reconcile it.
    """

    def __init__(
        self,
        engine: RetainedPythonBatchEngine,
        socket_path: str | os.PathLike[str],
        *,
        connection_timeout: float = 5.0,
    ) -> None:
        if not isinstance(engine, RetainedPythonBatchEngine):
            raise TypeError("engine must be a RetainedPythonBatchEngine.")
        path = Path(_unix_socket_path(os.fspath(socket_path)))
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as error:
            raise RetainedBatchWorkerConfigurationError(
                "worker socket parent cannot be resolved."
            ) from error
        if not parent.is_dir() or path.exists() or path.is_symlink():
            raise RetainedBatchWorkerConfigurationError(
                "worker socket path must be absent below an existing directory."
            )
        if (
            isinstance(connection_timeout, bool)
            or not isinstance(connection_timeout, (int, float))
            or not math.isfinite(float(connection_timeout))
            or not 0.01 <= float(connection_timeout) <= 300.0
        ):
            raise RetainedBatchWorkerConfigurationError(
                "worker connection timeout is outside its supported bound."
            )
        self.engine = engine
        self.socket_path = parent / path.name
        self.connection_timeout = float(connection_timeout)
        self.ready = threading.Event()
        self.startup_error: BaseException | None = None
        self._stop = False

    def serve_forever(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                listener.bind(str(self.socket_path))
            except BaseException as error:
                self.startup_error = error
                raise
            os.chmod(self.socket_path, 0o600)
            listener.listen(8)
            self.ready.set()
            while not self._stop:
                connection, _ = listener.accept()
                with connection:
                    self.serve_connection(connection)
        finally:
            listener.close()
            self.engine.close()
            self.ready.set()

    def serve_connection(self, connection: socket.socket) -> None:
        """Serve one already-accepted socket until EOF, error, or shutdown."""

        if not isinstance(connection, socket.socket):
            raise TypeError("connection must be a socket.")
        connection.settimeout(self.connection_timeout)
        while not self._stop:
            try:
                header = _receive_exact(connection, _HEADER.size)
                if header is None:
                    return
                (size,) = _HEADER.unpack(header)
                if size > self.engine.max_payload_bytes:
                    _send_frame(
                        connection,
                        _transport_error(
                            "frame_too_large", "The protocol frame exceeds its size bound."
                        ),
                    )
                    return
                payload = _receive_exact(connection, size)
                if payload is None:
                    return
                request = self._decode_request(payload)
            except (ConnectionError, OSError):
                return
            except _RequestError as error:
                try:
                    _send_frame(
                        connection,
                        _transport_error(error.code, error.public_message),
                    )
                except OSError:
                    return
                continue
            except Exception:
                try:
                    _send_frame(
                        connection,
                        _transport_error(
                            "malformed_frame",
                            "The protocol frame contains invalid JSON.",
                        ),
                    )
                except OSError:
                    return
                continue
            response = self.engine.handle(request)
            encoded = canonical_json_bytes(response)
            try:
                _send_frame(connection, encoded)
            except OSError:
                return
            if (
                request.get("op") == "shutdown"
                and response.get("ok") is True
                and response.get("result", {}).get("shutdown") is True
            ):
                self._stop = True
                return

    def _decode_request(self, payload: bytes) -> dict[str, Any]:
        return _decode_protocol_frame(payload)


def _decode_protocol_frame(payload: bytes) -> dict[str, Any]:
    """One decoding for every transport, so the frame grammar cannot fork."""

    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise _RequestError(
            "malformed_frame", "The protocol frame is not valid JSON."
        ) from error
    try:
        _validate_json(value, "protocol frame")
    except (TypeError, ValueError, RecursionError) as error:
        raise _RequestError(
            "malformed_frame", "The protocol frame contains invalid JSON."
        ) from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise _RequestError(
            "noncanonical_frame", "The protocol frame is not canonical JSON."
        )
    return value


def _read_exact_stream(stream: BinaryIO, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ConnectionError("stream frame ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class StdioBatchWorkerServer:
    """Serve the frame protocol over a stream that stays open.

    Used when the worker runs inside a container: the container's own standard
    input and output are the stream, because a Unix socket cannot cross the
    container boundary on every platform OptPilot runs on, and the design says
    the method's container answers on the same stream it receives on.

    The protocol must own that stream exclusively, and authored code cannot be
    trusted to stay off it: redirecting Python's stdout object only reroutes
    Python-level prints, while a subprocess or a compiled extension still
    reaches the underlying descriptor. So :meth:`claim_stdio` takes the
    descriptors at the operating-system level -- before any authored code has
    been imported -- and after it runs, anything written to descriptor 1 lands
    in the error stream and anything read from descriptor 0 sees end-of-file
    instead of eating protocol bytes.
    """

    def __init__(
        self,
        engine: RetainedPythonBatchEngine,
        protocol_input: BinaryIO,
        protocol_output: BinaryIO,
    ) -> None:
        if not isinstance(engine, RetainedPythonBatchEngine):
            raise TypeError("engine must be a RetainedPythonBatchEngine.")
        self.engine = engine
        self._input = protocol_input
        self._output = protocol_output
        self._stop = False

    @staticmethod
    def claim_stdio() -> tuple[BinaryIO, BinaryIO]:
        """Take exclusive ownership of the process's standard streams.

        Must run before authored code is imported. Descriptor 1 is repointed at
        the error stream so an authored print -- from Python, a subprocess, or a
        compiled extension -- is captured as diagnostics rather than corrupting
        a frame. Descriptor 0 is repointed at an empty stream so authored code
        that reads input gets end-of-file rather than the next frame.
        """

        protocol_in = os.fdopen(os.dup(0), "rb", buffering=0)
        protocol_out = os.fdopen(os.dup(1), "wb", buffering=0)
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(devnull, 0)
        finally:
            os.close(devnull)
        os.dup2(2, 1)
        return protocol_in, protocol_out

    def _send(self, payload: bytes) -> None:
        self._output.write(_HEADER.pack(len(payload)) + payload)
        self._output.flush()

    def serve_forever(self) -> None:
        try:
            while not self._stop:
                try:
                    header = _read_exact_stream(self._input, _HEADER.size)
                except (ConnectionError, OSError):
                    return
                if header is None:
                    # End-of-file: the side holding the stream has gone away,
                    # and a worker with nobody to answer has nothing to do.
                    return
                (size,) = _HEADER.unpack(header)
                if size > self.engine.max_payload_bytes:
                    self._send(
                        _transport_error(
                            "frame_too_large",
                            "The protocol frame exceeds its size bound.",
                        )
                    )
                    return
                try:
                    payload = _read_exact_stream(self._input, size)
                except (ConnectionError, OSError):
                    return
                if payload is None:
                    return
                try:
                    request = _decode_protocol_frame(payload)
                except _RequestError as error:
                    try:
                        self._send(
                            _transport_error(error.code, error.public_message)
                        )
                    except OSError:
                        return
                    continue
                response = self.engine.handle(request)
                try:
                    self._send(canonical_json_bytes(response))
                except OSError:
                    return
                if (
                    request.get("op") == "shutdown"
                    and response.get("ok") is True
                    and response.get("result", {}).get("shutdown") is True
                ):
                    self._stop = True
                    return
        finally:
            self.engine.close()


def unix_batch_worker_request(
    socket_path: str | os.PathLike[str],
    request: Mapping[str, Any],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Issue one canonical request over a fresh provider-private connection."""

    encoded = _validate_json(request, "batch worker request")
    if len(encoded) > MAX_BATCH_FRAME_BYTES:
        raise ValueError("batch worker request exceeds the frame size bound.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        _send_frame(connection, encoded)
        header = _receive_exact(connection, _HEADER.size)
        if header is None:
            raise ConnectionError("batch worker closed without a response")
        (size,) = _HEADER.unpack(header)
        if size > MAX_BATCH_FRAME_BYTES:
            raise ValueError("batch worker response exceeds the frame size bound.")
        payload = _receive_exact(connection, size)
        if payload is None:
            raise ConnectionError("batch worker response ended early")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("batch worker returned invalid JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("batch worker returned noncanonical JSON")
    return value


def _open_private_diagnostic(path: Path) -> TextIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise RetainedBatchWorkerConfigurationError(
            "worker diagnostic target is not a regular file."
        )
    return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)


def _main(argv: Sequence[str]) -> int:
    stdio = False
    if argv and argv[0] == "--stdio":
        stdio = True
        argv = argv[1:]
    if len(argv) != 1:
        raise SystemExit(
            "Usage: python -m optpilot.retained_batch_worker [--stdio] INIT_JSON"
        )
    if stdio:
        # Before anything else: once authored code has been imported it is too
        # late to keep it off the protocol stream.
        protocol_in, protocol_out = StdioBatchWorkerServer.claim_stdio()
    init_path = Path(argv[0])
    if not init_path.is_absolute() or init_path.is_symlink():
        raise SystemExit("Worker initialization path must be a real absolute file.")
    try:
        initialization = RetainedBatchWorkerInit.from_bytes(init_path.read_bytes())
    except (OSError, RetainedBatchWorkerConfigurationError) as error:
        raise SystemExit("Worker initialization is invalid.") from error
    diagnostic_path = Path(initialization.diagnostic_path)
    try:
        diagnostic_parent = diagnostic_path.parent.resolve(strict=True)
    except OSError as error:
        raise SystemExit("Worker diagnostic parent is invalid.") from error
    if not diagnostic_parent.is_dir():
        raise SystemExit("Worker diagnostic parent is invalid.")
    diagnostic_path = diagnostic_parent / diagnostic_path.name
    with _open_private_diagnostic(diagnostic_path) as diagnostic_stream:
        def record(event: Mapping[str, Any]) -> None:
            diagnostic_stream.write(
                canonical_json_bytes(dict(event)).decode("utf-8") + "\n"
            )

        engine = RetainedPythonBatchEngine(
            run_definition=initialization.run_definition,
            projection_root=initialization.projection_root,
            scope_roots=initialization.scope_roots,
            candidate_staging=initialization.candidate_staging,
            diagnostic=record,
            user_stdout=diagnostic_stream,
        )
        if stdio:
            StdioBatchWorkerServer(engine, protocol_in, protocol_out).serve_forever()
        else:
            UnixBatchWorkerServer(engine, initialization.socket_path).serve_forever()
    return 0


def main() -> int:
    return _main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BATCH_PROTOCOL",
    "BATCH_REQUEST_SCHEMA",
    "BATCH_RESPONSE_SCHEMA",
    "BATCH_WORKER_INIT_SCHEMA",
    "INITIAL_BATCH_EXCHANGE_CHAIN",
    "MAX_BATCH_EXCHANGE_ITEMS",
    "MAX_BATCH_DURABLE_RESPONSE_BYTES",
    "MAX_BATCH_FRAME_BYTES",
    "MAX_UNIX_SOCKET_PATH_BYTES",
    "RetainedBatchWorkerConfigurationError",
    "RetainedBatchWorkerInit",
    "RetainedPythonBatchEngine",
    "StaticEvidenceView",
    "StdioBatchWorkerServer",
    "UnixBatchWorkerServer",
    "retained_batch_exchange_chain_digest",
    "unix_batch_worker_request",
]
