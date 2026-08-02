"""Actor-bound resolution of immutable candidate inspection targets."""

from __future__ import annotations

from dataclasses import dataclass

from .inspection import ResolvedCandidateInspectionTarget
from .ledger import PrincipalRecord, RealmLedger
from .selections import SelectionRef


@dataclass(frozen=True)
class RealmInspectionTargetService:
    """Resolve inspection semantics without creating a workspace or process."""

    _ledger: RealmLedger
    _principal: PrincipalRecord

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    def resolve_candidate(
        self, *, selection: SelectionRef
    ) -> ResolvedCandidateInspectionTarget:
        if not isinstance(selection, SelectionRef):
            raise TypeError("selection must be a SelectionRef.")
        return self._ledger.resolve_candidate_inspection_target(
            actor_principal_id=self.principal_id,
            selection=selection,
        )


__all__ = ["RealmInspectionTargetService"]
