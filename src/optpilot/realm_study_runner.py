"""Single-authority launch composition for the bounded local Realm slice.

This module intentionally contains no evidence-store adapter and no run
directory.  It composes the already-separated capture, compilation, guarded
run creation, and retained-batch driver services while leaving each service as
the authority for its own lifecycle.  Reusing ``operation_id`` repeats the same
opaque owner/run identities, so response-loss replay cannot silently create a
second run.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Mapping
from pathlib import Path

from .realm._validation import required_text
from .realm.local_runtime import LocalRealmRuntime
from .realm.run_projection import RunSummaryProjection
from .run_execution_profile import (
    RunExecutionProfile,
    method_exchange_timeout_seconds,
)
from .spec import load_study_spec
from .study_launch_ids import local_study_operation_identities


_PROCESS_ENVIRONMENT_BINDING_KEY = secrets.token_bytes(32)


def _process_environment_binding_revision(
    environment: Mapping[str, str],
) -> str:
    """Return a process-local opaque identity without exposing input values."""

    digest = hmac.new(
        _PROCESS_ENVIRONMENT_BINDING_KEY,
        digestmod=hashlib.sha256,
    )
    items = list(environment.items())
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in items
    ):
        raise TypeError("method_environment names and values must be strings.")
    for name, value in sorted(items):
        for item in (name, value):
            encoded = item.encode("utf-8", errors="strict")
            digest.update(len(encoded).to_bytes(8, byteorder="big"))
            digest.update(encoded)
    return f"process-environment-{digest.hexdigest()}"


def new_local_study_operation_id() -> str:
    """Return one opaque id suitable for a fresh user-requested launch."""

    return f"local-study-run/{uuid.uuid4().hex}"


def local_study_run_id_for_operation(operation_id: str) -> str:
    """Derive the canonical run id for one local study operation.

    This is a pure presentation/coordination seam: it creates no run and opens
    no Realm.  A launcher and a monitor can therefore agree on the opaque run
    identity before execution starts, while the ledger remains the only
    authority that can create or mutate that run.
    """

    required_text(operation_id, "local study operation_id", max_bytes=512)
    return local_study_operation_identities(operation_id)["run_id"]


def run_local_realm_study(
    *,
    runtime: LocalRealmRuntime,
    package_root: Path,
    study_config_path: Path,
    operation_id: str,
    controller_ttl_seconds: float = 300.0,
    heartbeat_interval_seconds: float | None = None,
    attempt_ttl_seconds: float = 300.0,
    method_start_timeout: float = 10.0,
    method_request_timeout: float | None = None,
    method_environment: Mapping[str, str] | None = None,
) -> RunSummaryProjection:
    """Freeze and execute one supported local package through Realm only.

    The current retained compiler deliberately accepts only the bounded
    parameter-or-file/Python/process/batch slice. Unsupported studies fail
    during preparation; this function never falls back to the legacy
    directory runner or creates a second canonical writer.
    """

    if not isinstance(runtime, LocalRealmRuntime):
        raise TypeError("runtime must be a LocalRealmRuntime.")
    if runtime.closed:
        raise RuntimeError("Local Realm runtime is closed.")
    if not isinstance(package_root, Path):
        raise TypeError("package_root must be a Path.")
    if not isinstance(study_config_path, Path):
        raise TypeError("study_config_path must be a Path.")
    required_text(operation_id, "local study operation_id", max_bytes=512)
    # Preserve the launch boundary before reading Method semantics.  The
    # retained preparation service repeats the complete portable-path check;
    # this early containment check prevents a timeout lookup from changing the
    # established error for an out-of-package Study path.
    try:
        canonical_root = package_root.resolve(strict=True)
        canonical_study = study_config_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(
            "package_root and study_config_path must name existing paths."
        ) from error
    try:
        canonical_study.relative_to(canonical_root)
    except ValueError as error:
        raise ValueError(
            "study_config_path must be inside the explicit package_root."
        ) from error
    method_runtime = load_study_spec(str(study_config_path)).method.get(
        "runtime", {}
    )
    selected_method_request_timeout = method_exchange_timeout_seconds(
        method_runtime,
        override=method_request_timeout,
    )

    planned = runtime.study_launches.plan_local_package(
        operation_id=operation_id,
        package_root=package_root,
        study_config_path=study_config_path,
        process_environment_binding_revision=(
            _process_environment_binding_revision(method_environment)
            if method_environment
            else None
        ),
        execution_profile=RunExecutionProfile(
            controller_ttl_seconds=controller_ttl_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            attempt_ttl_seconds=attempt_ttl_seconds,
            method_start_timeout_seconds=method_start_timeout,
            method_request_timeout_seconds=selected_method_request_timeout,
        ),
    )
    completed = runtime.study_launches.execute(
        launch_id=planned.launch_id,
        method_environment=method_environment,
    )
    if completed.run_id is None:
        raise RuntimeError("Study launch completed without a canonical run handoff.")
    summary = runtime.run_reader.summary(run_id=completed.run_id)
    if not isinstance(summary, RunSummaryProjection):
        raise TypeError("Realm retained-batch driver returned an invalid summary.")
    if summary.run_id != completed.run_id:
        raise RuntimeError("Realm retained-batch driver returned another run.")
    return summary


__all__ = [
    "local_study_run_id_for_operation",
    "new_local_study_operation_id",
    "run_local_realm_study",
]
