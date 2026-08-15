"""Actor-bound service for durable local provider trust policy.

The service has no dependency on a concrete provider.  Runtime composition may
translate active heads into its provider-specific immutable trust value after
the Realm has authorized and validated the complete decision history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ._validation import required_text
from .config import prepare_private_directory
from .ledger import RealmLedger
from .provider_trust_records import (
    PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE,
    PROVIDER_TRUST_GATEWAY_CONTRACT,
    validate_provider_contract,
    PROVIDER_TRUST_POLICY_OWNER_ID,
    ProviderTrustDecision,
    ProviderTrustHead,
)
from .run_reader import register_current_local_principal


LOCAL_REALM_AUTHORITY_DIRECTORY = "authority"


class RealmProviderTrustPolicyService:
    """Bind provider-trust decisions to one server-observed Realm principal."""

    def __init__(
        self,
        ledger: RealmLedger,
        actor_principal_id: str,
        *,
        _owns_ledger: bool = False,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        self._ledger = ledger
        self._actor_principal_id = required_text(
            actor_principal_id,
            "provider trust service actor principal id",
        )
        self._owns_ledger = bool(_owns_ledger)
        self._closed = False

    @classmethod
    def open_local(cls, realm_root: Path) -> "RealmProviderTrustPolicyService":
        """Open only the local Realm authority needed by trust administration.

        This intentionally avoids constructing the full runtime, reconciling
        workers, or probing a container engine for a policy-only CLI command.
        The actor is derived from the current OS account and is never supplied
        by request data.
        """

        if not isinstance(realm_root, Path):
            raise TypeError("realm_root must be a Path.")
        if not realm_root.is_absolute():
            raise ValueError("realm_root must be an absolute path.")
        root = prepare_private_directory(realm_root)
        authority = prepare_private_directory(
            root / LOCAL_REALM_AUTHORITY_DIRECTORY
        )
        ledger: RealmLedger | None = None
        try:
            ledger = RealmLedger(authority / "realm.sqlite3")
            principal = register_current_local_principal(ledger)
            return cls(
                ledger,
                principal.principal_id,
                _owns_ledger=True,
            )
        except BaseException:
            if ledger is not None:
                ledger.close()
            raise

    @property
    def ledger(self) -> RealmLedger:
        self._require_open()
        return self._ledger

    @property
    def actor_principal_id(self) -> str:
        self._require_open()
        return self._actor_principal_id

    @property
    def owner_id(self) -> str:
        return PROVIDER_TRUST_POLICY_OWNER_ID

    @property
    def closed(self) -> bool:
        return self._closed

    def approve(
        self,
        *,
        operation_id: str,
        image_ref: str,
        python_executable: str = PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE,
        contract: str = PROVIDER_TRUST_GATEWAY_CONTRACT,
        reason: str = "",
    ) -> ProviderTrustDecision:
        self._require_open()
        return self._ledger.approve_provider_trust(
            operation_id=operation_id,
            actor_principal_id=self._actor_principal_id,
            image_ref=image_ref,
            python_executable=python_executable,
            contract=contract,
            reason=reason,
        )

    def revoke(
        self,
        *,
        operation_id: str,
        image_ref: str,
        python_executable: str = PROVIDER_TRUST_DEFAULT_PYTHON_EXECUTABLE,
        contract: str = PROVIDER_TRUST_GATEWAY_CONTRACT,
        reason: str = "",
    ) -> ProviderTrustDecision:
        self._require_open()
        return self._ledger.revoke_provider_trust(
            operation_id=operation_id,
            actor_principal_id=self._actor_principal_id,
            image_ref=image_ref,
            python_executable=python_executable,
            contract=contract,
            reason=reason,
        )

    def list_decisions(self) -> tuple[ProviderTrustDecision, ...]:
        self._require_open()
        return self._ledger.list_provider_trust_decisions(
            actor_principal_id=self._actor_principal_id
        )

    def list_heads(self) -> tuple[ProviderTrustHead, ...]:
        self._require_open()
        return self._ledger.list_provider_trust_heads(
            actor_principal_id=self._actor_principal_id
        )

    def list_active(
        self, *, contract: str = PROVIDER_TRUST_GATEWAY_CONTRACT
    ) -> tuple[ProviderTrustHead, ...]:
        """Approvals for one purpose only.

        Required rather than optional-and-unfiltered: an approval to host a
        preview window is not an approval to run a package's own code, and a
        caller that forgot to say which it meant would silently get both.
        """

        self._require_open()
        contract = validate_provider_contract(contract)
        return tuple(
            head
            for head in self._ledger.list_active_provider_trust(
                actor_principal_id=self._actor_principal_id
            )
            if head.decision.contract == contract
        )

    def read_active(
        self,
        *,
        image_ref: str,
        contract: str = PROVIDER_TRUST_GATEWAY_CONTRACT,
    ) -> ProviderTrustHead | None:
        self._require_open()
        contract = validate_provider_contract(contract)
        head = self._ledger.read_active_provider_trust(
            actor_principal_id=self._actor_principal_id,
            image_ref=image_ref,
        )
        if head is None or head.decision.contract != contract:
            return None
        return head

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_ledger:
            self._ledger.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Provider trust policy service is closed.")

    def __enter__(self) -> "RealmProviderTrustPolicyService":
        self._require_open()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__: Sequence[str] = (
    "LOCAL_REALM_AUTHORITY_DIRECTORY",
    "RealmProviderTrustPolicyService",
)
