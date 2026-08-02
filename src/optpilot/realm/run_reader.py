"""Actor-bound local read service for canonical Realm runs.

The Studio or another local server constructs one :class:`LocalRealmContext`
at startup.  Request handlers use its bound read service and therefore never
accept a principal id, database path, run-root path, or workspace path from an
HTTP request.  Run ids remain canonical Realm identities rather than encoded
filesystem locations.
"""

from __future__ import annotations

import getpass
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .ledger import PrincipalRecord, RealmLedger
from .refs import canonical_json_bytes
from .run_catalog import (
    AuthorizedRunPage,
    RUN_CATALOG_DEFAULT_PAGE_SIZE,
)
from .run_projection import RunSummaryProjection
from .run_timeline import (
    RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    RunTimelinePage,
)
from .run_workbench import (
    RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    RunWorkbenchReadModel,
)
from .run_views import (
    BorrowedRunView,
    RealmRunViewService,
    RunViewRef,
    RunViewSelectionResult,
    RunWorkbenchHead,
)
from .run_candidate_comparison import RunCandidateComparisonProjection
from .selection_service import (
    RealmSelectionActionService,
    SelectionKeepResult,
    SelectionOpenResult,
)
from .selections import SelectionRef


LOCAL_REALM_PRINCIPAL_KIND = "local-os-user-v1"


def _current_os_user_identity() -> dict[str, str]:
    """Return a server-observed OS account identity without a host path."""

    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is not None:
        return {"kind": "posix-effective-uid", "value": str(get_effective_uid())}
    username = getpass.getuser()
    if not isinstance(username, str) or not username.strip():
        raise RuntimeError("Could not determine the local OS user identity.")
    return {"kind": "os-account-name", "value": username.strip()}


def _local_principal_digest(ledger: RealmLedger) -> str:
    identity = {
        "schema": "optpilot.local-realm-principal.v1",
        "realm_id": ledger.realm_id,
        "os_user": _current_os_user_identity(),
    }
    return hashlib.sha256(
        b"optpilot/local-realm-principal/v1\0" + canonical_json_bytes(identity)
    ).hexdigest()


def register_current_local_principal(ledger: RealmLedger) -> PrincipalRecord:
    """Register and return the principal derived from the observed OS user.

    This is an application-composition primitive.  Request handlers must use
    the already-bound service graph and must never substitute a request value
    for this server-observed identity.
    """

    if not isinstance(ledger, RealmLedger):
        raise TypeError("ledger must be a RealmLedger.")
    digest = _local_principal_digest(ledger)
    return ledger.register_principal(
        operation_id=f"bootstrap/local-os-user/{digest}",
        principal_id=f"local-user:sha256:{digest}",
        kind=LOCAL_REALM_PRINCIPAL_KIND,
    )


@dataclass(frozen=True)
class RealmRunReadService:
    """Read-only Realm run API bound once to a trusted server principal."""

    _ledger: RealmLedger
    _principal: PrincipalRecord

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")

    @property
    def principal_id(self) -> str:
        """The server-bound principal; request methods cannot replace it."""

        return self._principal.principal_id

    def list_runs(
        self,
        *,
        page_token: str | None = None,
        limit: int = RUN_CATALOG_DEFAULT_PAGE_SIZE,
    ) -> AuthorizedRunPage:
        return self._ledger.list_runs(
            actor_principal_id=self.principal_id,
            page_token=page_token,
            limit=limit,
        )

    def summary(self, *, run_id: str) -> RunSummaryProjection:
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=run_id,
        )
        return RunSummaryProjection.from_snapshot(snapshot)

    def workbench_page(
        self,
        *,
        run_id: str,
        kind: str,
        page_token: str | None = None,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        # Reading a fresh snapshot on every request is intentional: the
        # Workbench token is exact-head scoped, so an old token fails closed
        # after the canonical head advances instead of mixing two revisions.
        snapshot = self._ledger.read_run_snapshot(
            actor_principal_id=self.principal_id,
            run_id=run_id,
        )
        summary = RunSummaryProjection.from_snapshot(snapshot)
        model = RunWorkbenchReadModel.from_snapshot(snapshot, summary=summary)
        return model.page(kind, page_token=page_token, limit=limit)

    def timeline_page(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
        expected_head_sequence: int,
        after_sequence: int = 0,
        limit: int = RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    ) -> RunTimelinePage:
        return self._ledger.read_run_timeline_page(
            actor_principal_id=self.principal_id,
            run_id=run_id,
            expected_run_revision=expected_run_revision,
            expected_head_sequence=expected_head_sequence,
            after_sequence=after_sequence,
            limit=limit,
        )


class LocalRealmContext:
    """Server-owned local Realm lifecycle and OS-user principal bootstrap."""

    def __init__(self) -> None:
        raise TypeError("Use LocalRealmContext.open().")

    @classmethod
    def _from_bound_principal(
        cls,
        *,
        ledger: RealmLedger,
        principal: PrincipalRecord,
        owns_ledger: bool,
    ) -> "LocalRealmContext":
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")
        if not isinstance(owns_ledger, bool):
            raise TypeError("owns_ledger must be a boolean.")
        context = object.__new__(cls)
        context._ledger = ledger
        context._principal = principal
        context._owns_ledger = owns_ledger
        context._closed = False
        # Keep the raw bound service private so every operation obtained from
        # this lifecycle context passes through ``_require_open``.
        context._runs = RealmRunReadService(ledger, principal)
        context._run_views = RealmRunViewService(ledger, principal)
        context._selection_actions = RealmSelectionActionService(
            ledger, principal
        )
        return context

    @classmethod
    def open(cls, *, ledger: RealmLedger | None = None) -> "LocalRealmContext":
        """Open default Realm authority and bind the observed local OS user.

        ``ledger`` is an application-composition/test seam, not request input.
        Production callers omit it and receive the secure default RealmLedger
        configuration.  No principal value is accepted from the caller.
        """

        owns_ledger = ledger is None
        selected_ledger = RealmLedger() if ledger is None else ledger
        if not isinstance(selected_ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger or None.")
        try:
            principal = register_current_local_principal(selected_ledger)
            return cls._from_bound_principal(
                ledger=selected_ledger,
                principal=principal,
                owns_ledger=owns_ledger,
            )
        except BaseException:
            if owns_ledger:
                selected_ledger.close()
            raise

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    def list_runs(
        self,
        *,
        page_token: str | None = None,
        limit: int = RUN_CATALOG_DEFAULT_PAGE_SIZE,
    ) -> AuthorizedRunPage:
        self._require_open()
        return self._runs.list_runs(page_token=page_token, limit=limit)

    def summary(self, *, run_id: str) -> RunSummaryProjection:
        self._require_open()
        return self._runs.summary(run_id=run_id)

    def workbench_page(
        self,
        *,
        run_id: str,
        kind: str,
        page_token: str | None = None,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        self._require_open()
        return self._runs.workbench_page(
            run_id=run_id,
            kind=kind,
            page_token=page_token,
            limit=limit,
        )

    def timeline_page(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
        expected_head_sequence: int,
        after_sequence: int = 0,
        limit: int = RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    ) -> RunTimelinePage:
        self._require_open()
        return self._runs.timeline_page(
            run_id=run_id,
            expected_run_revision=expected_run_revision,
            expected_head_sequence=expected_head_sequence,
            after_sequence=after_sequence,
            limit=limit,
        )

    def open_run_view(self, *, run_id: str) -> BorrowedRunView:
        self._require_open()
        return self._run_views.open(run_id=run_id)

    def refresh_run_view(self, *, ref: RunViewRef) -> BorrowedRunView:
        self._require_open()
        return self._run_views.refresh(ref=ref)

    def run_view_workbench_head(self, *, ref: RunViewRef) -> RunWorkbenchHead:
        self._require_open()
        return self._run_views.workbench_head(ref=ref)

    def run_view_workbench_page(
        self,
        *,
        ref: RunViewRef,
        kind: str,
        page_token: str | None = None,
        limit: int = RUN_WORKBENCH_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        self._require_open()
        return self._run_views.workbench_page(
            ref=ref,
            kind=kind,
            page_token=page_token,
            limit=limit,
        )

    def run_view_timeline_page(
        self,
        *,
        ref: RunViewRef,
        expected_run_revision: int,
        expected_head_sequence: int,
        after_sequence: int = 0,
        limit: int = RUN_TIMELINE_DEFAULT_PAGE_SIZE,
    ) -> RunTimelinePage:
        self._require_open()
        return self._run_views.timeline_page(
            ref=ref,
            expected_run_revision=expected_run_revision,
            expected_head_sequence=expected_head_sequence,
            after_sequence=after_sequence,
            limit=limit,
        )

    def mint_run_view_selection(
        self,
        *,
        ref: RunViewRef,
        presentation_selection: Mapping[str, Any],
    ) -> RunViewSelectionResult:
        self._require_open()
        return self._run_views.mint_selection(
            ref=ref,
            presentation_selection=presentation_selection,
        )

    def compare_run_view_candidates(
        self,
        *,
        ref: RunViewRef,
        baseline_presentation_selection: Mapping[str, Any],
        comparison_presentation_selection: Mapping[str, Any],
    ) -> RunCandidateComparisonProjection:
        self._require_open()
        return self._run_views.compare_candidates(
            ref=ref,
            baseline_presentation_selection=baseline_presentation_selection,
            comparison_presentation_selection=comparison_presentation_selection,
        )

    def open_selection_read_only(
        self, *, selection: SelectionRef
    ) -> SelectionOpenResult:
        """Open one immutable selection without creating durable state."""

        self._require_open()
        return self._selection_actions.open_read_only(selection=selection)

    def keep_selection_as_workspace(
        self,
        *,
        operation_id: str,
        selection: SelectionRef,
        title: str,
        workspace_id: str | None = None,
        owner_id: str | None = None,
    ) -> SelectionKeepResult:
        """Keep one selection through the atomic no-copy owner derivation."""

        self._require_open()
        return self._selection_actions.keep_as_editable_workspace(
            operation_id=operation_id,
            selection=selection,
            title=title,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )

    def detach_run_view(self, *, ref: RunViewRef) -> None:
        self._require_open()
        self._run_views.detach(ref=ref)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Local Realm context is closed.")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_ledger:
            self._ledger.close()

    def __enter__(self) -> "LocalRealmContext":
        self._require_open()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


__all__ = [
    "LOCAL_REALM_PRINCIPAL_KIND",
    "LocalRealmContext",
    "RealmRunReadService",
    "register_current_local_principal",
]
