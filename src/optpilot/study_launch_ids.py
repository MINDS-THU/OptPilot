"""Deterministic logical identities for one retained local study launch."""

from __future__ import annotations

from .realm._validation import required_text
from .realm.refs import request_digest


LOCAL_STUDY_OPERATION_SCHEMA = "optpilot.local-realm-study-run.v1"


def local_study_operation_identities(operation_id: str) -> dict[str, str]:
    """Derive path-free owner, job, controller, and run coordinates.

    These are logical replay coordinates only.  Provider bindings, processes,
    ports, and host paths are deliberately absent.
    """

    required_text(operation_id, "local study operation_id", max_bytes=512)
    digest = request_digest(
        {"schema": LOCAL_STUDY_OPERATION_SCHEMA, "operation_id": operation_id}
    )
    coordinate = digest[:32]
    return {
        "source_owner_id": f"study-source-{coordinate}",
        "definition_owner_id": f"study-definition-{coordinate}",
        "controller_holder_id": f"study-controller-bootstrap-{coordinate}",
        "run_id": f"run-{coordinate}",
        "run_owner_id": f"run-owner-{coordinate}",
    }


__all__ = ["LOCAL_STUDY_OPERATION_SCHEMA", "local_study_operation_identities"]
