"""Public single-authority study runner for OptPilot.

Authored studies are captured from one explicit package and executed through
the local Realm.  This module deliberately contains no run-directory writer,
resume mode, branch mode, or expanded-spec execution path.
"""

from __future__ import annotations

import copy
import math
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .models import Observation
from .realm.config import default_realm_root
from .realm.local_runtime import LocalRealmRuntime
from .realm.run_projection import RunSummaryProjection
from .realm_study_runner import (
    new_local_study_operation_id,
    run_local_realm_study,
)
from .spec import StudySpec


class StudyRunner:
    """Run one authored study through an owned local Realm service graph.

    ``package_root`` is always explicit because it is the complete source
    authority captured for the retained study definition.  ``realm_root`` may
    be omitted to use the private OS user-data location.  The operation id is
    generated once per runner so retrying the same runner replays the same
    retained operation instead of creating a second run.
    """

    def __init__(
        self,
        study_config_path: Path,
        *,
        package_root: Path,
        realm_root: Path | None = None,
        operation_id: str | None = None,
        method_request_timeout: float = 10.0,
        launch_inputs: Optional[Mapping[str, object]] = None,
    ) -> None:
        if not isinstance(study_config_path, Path):
            raise TypeError("study_config_path must be a Path.")
        if not isinstance(package_root, Path):
            raise TypeError("package_root must be a Path.")
        if realm_root is not None and not isinstance(realm_root, Path):
            raise TypeError("realm_root must be a Path or None.")
        if not study_config_path.is_absolute():
            raise ValueError("study_config_path must be absolute.")
        if not package_root.is_absolute():
            raise ValueError("package_root must be absolute.")
        if realm_root is not None and not realm_root.is_absolute():
            raise ValueError("realm_root must be absolute when provided.")

        self.study_config_path = study_config_path
        self.package_root = package_root
        # Do not resolve the final Realm component here.  Runtime opening must
        # still be able to reject a terminal symlink rather than having that
        # security-relevant fact erased by eager canonicalization.
        self.realm_root = default_realm_root() if realm_root is None else realm_root
        self.operation_id = (
            new_local_study_operation_id()
            if operation_id is None
            else operation_id
        )
        if (
            isinstance(method_request_timeout, bool)
            or not isinstance(method_request_timeout, (int, float))
            or not math.isfinite(float(method_request_timeout))
            or float(method_request_timeout) <= 0.0
        ):
            raise ValueError("method_request_timeout must be finite and positive.")
        self.method_request_timeout = float(method_request_timeout)
        if launch_inputs is not None and not isinstance(launch_inputs, Mapping):
            raise TypeError("launch_inputs must be a mapping or None.")
        self.launch_inputs = None if launch_inputs is None else dict(launch_inputs)

    def run(self) -> RunSummaryProjection:
        with LocalRealmRuntime.open(realm_root=self.realm_root) as runtime:
            return run_local_realm_study(
                runtime=runtime,
                package_root=self.package_root,
                study_config_path=self.study_config_path,
                operation_id=self.operation_id,
                method_request_timeout=self.method_request_timeout,
                # The public CLI boundary resolves exported host values for
                # this launch.  The retained service selects only names
                # declared by the Method; it never inherits this mapping.
                method_environment=os.environ,
                launch_inputs=self.launch_inputs,
            )


def run_study(
    spec_path: str,
    *,
    package_root: str,
    realm_root: Optional[str] = None,
    operation_id: Optional[str] = None,
    method_request_timeout: float = 10.0,
    launch_inputs: Optional[Mapping[str, object]] = None,
) -> RunSummaryProjection:
    """Run an authored study from one explicit package into the local Realm."""

    runner = StudyRunner(
        _absolute_text_path(spec_path, "spec_path"),
        package_root=_absolute_text_path(package_root, "package_root"),
        realm_root=(
            None
            if realm_root is None
            else _absolute_text_path(realm_root, "realm_root")
        ),
        operation_id=operation_id,
        method_request_timeout=method_request_timeout,
        launch_inputs=launch_inputs,
    )
    return runner.run()


def _absolute_text_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text.")
    # ``absolute`` normalizes a relative CLI input without dereferencing its
    # final component.  Source canonicalization and containment happen in the
    # retained package service; Realm-root symlink checks happen on open.
    return Path(value).expanduser().absolute()


def run_expanded_study_spec(
    spec_path: str,
    output_root: Optional[str] = None,
    resume_run_dir: Optional[str] = None,
    branch_from_run_dir: Optional[str] = None,
) -> None:
    """Reject the removed pre-Realm expanded-spec execution path."""

    del spec_path, output_root, resume_run_dir, branch_from_run_dir
    raise ValueError(
        "Expanded study execution was removed by the Realm cutover; run the "
        "authored study config with an explicit package_root."
    )


def _method_observation_payload(observation: Any) -> Dict[str, Any]:
    """Return the method-visible form of one observation.

    This compatibility helper remains shared with the controller integration
    tests while retained method projections take over its remaining callers.
    """

    payload = (
        observation.to_dict()
        if isinstance(observation, Observation)
        else copy.deepcopy(dict(observation))
    )
    event_summary = payload.get("event_summary")
    if not isinstance(event_summary, dict):
        return payload
    error = event_summary.get("error")
    if (
        not isinstance(error, dict)
        or error.get("code") != "unsupported_observation_status"
    ):
        return payload

    sanitized = dict(event_summary)
    sanitized["error"] = _sanitize_method_error(error)
    errors = event_summary.get("errors")
    if isinstance(errors, list):
        sanitized["errors"] = [_sanitize_method_error(item) for item in errors]
    payload["event_summary"] = sanitized
    return payload


def _sanitize_method_error(error: Any) -> Any:
    if (
        not isinstance(error, dict)
        or error.get("code") != "unsupported_observation_status"
    ):
        return copy.deepcopy(error)
    safe_error = {
        key: copy.deepcopy(value)
        for key, value in error.items()
        if key not in {"raw_status", "message"}
    }
    safe_error["message"] = (
        "Environment evaluator returned an unsupported observation status."
    )
    return safe_error


def _resolve_materialization_spec(study_spec: StudySpec) -> Dict[str, Any]:
    """Resolve the legacy worker's materializer declaration."""

    return dict(
        study_spec.candidate.get(
            "materialization",
            {
                "implementation": "builtin.parameter_to_config",
                "config": {},
            },
        )
    )


def _resolve_validation_spec(study_spec: StudySpec) -> Dict[str, Any]:
    """Resolve the legacy worker's validator declaration."""

    return dict(
        study_spec.candidate.get(
            "validation",
            {
                "implementation": "builtin.schema_validation",
                "config": {"enforceBounds": False},
            },
        )
    )


__all__ = ["StudyRunner", "run_study"]
