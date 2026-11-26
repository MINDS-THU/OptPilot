"""Actor-bound Open/Keep actions over immutable selection references.

The service intentionally has no source-specific actions.  A Workbench or
other presentation layer first mints one immutable :class:`SelectionRef`; all
subsequent content actions consume only that reference.  The principal is
bound when the service is composed so request callers cannot substitute an
authority identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RealmIntegrityError
from .ledger import PrincipalRecord, RealmLedger
from .selections import (
    ReadOnlySelectionView,
    SelectionEligibility,
    SelectionRef,
)
from .workspaces import WorkspaceCommitReceipt


@dataclass(frozen=True)
class SelectionOpenResult:
    selection: SelectionRef
    eligibility: SelectionEligibility
    view: ReadOnlySelectionView | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.view is not None):
            raise ValueError("open result view differs from its eligibility.")
        if self.view is not None and self.view.selection != self.selection:
            raise ValueError("open result view refers to another selection.")


@dataclass(frozen=True)
class SelectionKeepResult:
    selection: SelectionRef
    eligibility: SelectionEligibility
    workspace: WorkspaceCommitReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        if not isinstance(self.eligibility, SelectionEligibility):
            raise TypeError("eligibility must be a SelectionEligibility.")
        if self.eligibility.eligible != (self.workspace is not None):
            raise ValueError("keep result workspace differs from its eligibility.")


@dataclass(frozen=True)
class RealmSelectionActionService:
    """Open or keep domain content through one actor-bound selection model.

    ``Open`` creates only a non-authorizing read-only descriptor. ``Keep``
    transactionally adds an independent workspace owner for the same verified
    tree.  Neither operation captures or copies source bytes.
    """

    _ledger: RealmLedger
    _principal: PrincipalRecord

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")

    @property
    def principal_id(self) -> str:
        """The composition-time authority; action calls cannot replace it."""

        return self._principal.principal_id

    def open_read_only(
        self,
        *,
        selection: SelectionRef,
    ) -> SelectionOpenResult:
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        # Deliberately resolve on every Open.  The descriptor is neither a
        # durable object nor an authorization capability, and no projection is
        # created merely to answer whether the selection can be opened.
        resolution = self._ledger.resolve_selection_for_read_projection(
            actor_principal_id=self.principal_id,
            selection=selection,
        )
        if resolution.selection != selection:
            raise RealmIntegrityError(
                "Resolved selection differs from the requested selection."
            )
        view = (
            ReadOnlySelectionView.build(resolution)
            if resolution.eligibility.eligible
            else None
        )
        return SelectionOpenResult(selection, resolution.eligibility, view)

    def keep_as_editable_workspace(
        self,
        *,
        operation_id: str,
        selection: SelectionRef,
        title: str,
        workspace_id: str | None = None,
        owner_id: str | None = None,
    ) -> SelectionKeepResult:
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        # Resolution, retention validation, independent-owner creation, and
        # exact replay all remain one atomic ledger operation.
        receipt = self._ledger.keep_selection_as_workspace(
            operation_id=operation_id,
            actor_principal_id=self.principal_id,
            selection=selection,
            title=title,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if receipt.selection != selection:
            raise RealmIntegrityError(
                "Kept selection differs from the requested selection."
            )
        return SelectionKeepResult(
            receipt.selection, receipt.eligibility, receipt.workspace
        )


__all__ = [
    "RealmSelectionActionService",
    "SelectionKeepResult",
    "SelectionOpenResult",
]
