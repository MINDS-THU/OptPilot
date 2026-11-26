"""Ledger-first coordination for retained parameter and sealed-file runs.

The Realm ledger is the authority; :class:`RunController` is a deterministic
in-process cache.  This module keeps their admission order explicit:

``pure preflight -> canonical Realm commit -> controller apply``.

Parameter candidates retain the original no-content fast path.  File candidates
enter only as generation-bound drafts, are sealed under one provisional owner
change, and become canonical only in the atomic proposal/admission commit.
"""

from __future__ import annotations

import copy
import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

from .realm._validation import freeze_json
from .realm.content import AllowedTreeSource
from .realm.ledger import RealmLedger
from .realm.method_exchange_records import (
    RunMethodExchangePreparationRecord,
    RunMethodProposalCompletionReceipt,
)
from .realm.run_control_records import RunSubmissionControlReceipt
from .realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
    RunAdmissionReceipt,
    RunCreateReceipt,
    RunFinishReceipt,
)
from .realm.refs import SnapshotRef, request_digest
from .realm.service import RealmContentService
from .retained_file_candidates import (
    CANDIDATE_SEAL_LIMITS,
    FileCandidateDraft,
    sealed_file_candidate_declaration_digest,
    sealed_file_candidate_spec,
    validate_portable_candidate_metadata,
    validate_sealed_file_candidate_spec,
)
from .realm.owners import OwnerMembership
from .realm.run_snapshot import RunLedgerSnapshot
from .run_control_manifest import build_run_controller
from .run_controller import (
    AcceptedLogicalTrial,
    CandidateNormalizer,
    LogicalTrialIdFactory,
    LogicalTrialRestoreState,
    MethodProtocolError,
    PreparedProposal,
    RunController,
    RunControllerRestoreState,
)


_CANONICAL_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "format",
        "spec",
        "lineage",
        "generator",
        "validation",
        "materialization",
    }
)


@dataclass
class RetainedRunAuthority:
    """Mutable local cursor over one canonical Realm run controller term.

    A brand-new run may start from its creation receipt.  Every restart must
    hydrate from the current canonical snapshot so an old idempotent receipt
    cannot rewind the disposable cursor and relaunch already-adopted work.
    """

    ledger: RealmLedger
    actor_principal_id: str
    controller: RunController
    candidate_contract: Mapping[str, Any]
    candidate_normalizer: CandidateNormalizer
    normalizer_version: str
    logical_trial_id_factory: LogicalTrialIdFactory | None
    run_id: str
    owner_id: str
    run_revision: int
    owner_revision: int
    controller_lease_id: str
    controller_holder_id: str
    controller_fencing_token: int

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "candidate_contract" and name in self.__dict__:
            raise AttributeError(
                "candidate_contract is immutable for the lifetime of an authority."
            )
        super().__setattr__(name, value)

    @classmethod
    def from_create_receipt(
        cls,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        receipt: RunCreateReceipt,
        candidate_normalizer: CandidateNormalizer,
        normalizer_version: str,
        logical_trial_id_factory: LogicalTrialIdFactory | None = None,
    ) -> "RetainedRunAuthority":
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(receipt, RunCreateReceipt):
            raise TypeError("receipt must be a RunCreateReceipt.")
        lease = receipt.controller_lease
        if (
            receipt.run.controller_lease_id != lease.lease_id
            or receipt.run.controller_holder_id != lease.holder_id
            or receipt.run.controller_fencing_token != lease.fencing_token
            or receipt.run.current_revision != receipt.revision.revision
            or receipt.revision.owner_revision != 0
        ):
            raise ValueError("Run creation receipt has inconsistent authority facts.")
        head = ledger.read_run_snapshot(
            actor_principal_id=actor_principal_id,
            run_id=receipt.run.run_id,
        )
        if (
            head.run.current_revision != 0
            or head.revision != receipt.revision
            or head.run.owner_id != receipt.run.owner_id
            or head.run.controller_lease_id != lease.lease_id
            or head.run.controller_holder_id != lease.holder_id
            or head.run.controller_fencing_token != lease.fencing_token
        ):
            raise ValueError(
                "Run creation receipt is no longer the canonical run head; "
                "hydrate the authority instead."
            )
        return cls._from_snapshot(
            ledger=ledger,
            actor_principal_id=actor_principal_id,
            snapshot=head,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
            logical_trial_id_factory=logical_trial_id_factory,
        )

    @classmethod
    def hydrate(
        cls,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        run_id: str,
        candidate_normalizer: CandidateNormalizer,
        normalizer_version: str,
        logical_trial_id_factory: LogicalTrialIdFactory | None = None,
    ) -> "RetainedRunAuthority":
        """Rebuild the disposable controller and authority cursor from Realm.

        The normalizer implementation is resolved by the caller and must match
        the immutable version named by the run-control manifest.  Neither a
        mutable study config nor a prior in-process receipt fills in missing
        semantics during recovery.
        """

        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        snapshot = ledger.read_run_snapshot(
            actor_principal_id=actor_principal_id,
            run_id=run_id,
        )
        return cls._from_snapshot(
            ledger=ledger,
            actor_principal_id=actor_principal_id,
            snapshot=snapshot,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
            logical_trial_id_factory=logical_trial_id_factory,
        )

    @classmethod
    def _from_snapshot(
        cls,
        *,
        ledger: RealmLedger,
        actor_principal_id: str,
        snapshot: RunLedgerSnapshot,
        candidate_normalizer: CandidateNormalizer,
        normalizer_version: str,
        logical_trial_id_factory: LogicalTrialIdFactory | None,
    ) -> "RetainedRunAuthority":
        candidate_contract = _retained_candidate_contract(snapshot)
        controller = build_run_controller(
            snapshot.control.manifest,
            candidate_contract=candidate_contract,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
            logical_trial_id_factory=logical_trial_id_factory,
        )
        controller.restore_canonical_state(
            _controller_restore_state(snapshot, candidate_normalizer)
        )
        return cls(
            ledger=ledger,
            actor_principal_id=actor_principal_id,
            controller=controller,
            candidate_contract=candidate_contract,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
            logical_trial_id_factory=logical_trial_id_factory,
            run_id=snapshot.run.run_id,
            owner_id=snapshot.run.owner_id,
            run_revision=snapshot.revision.revision,
            owner_revision=snapshot.revision.owner_revision,
            controller_lease_id=snapshot.run.controller_lease_id,
            controller_holder_id=snapshot.run.controller_holder_id,
            controller_fencing_token=snapshot.run.controller_fencing_token,
        )

    def refresh_controller(self) -> RunLedgerSnapshot:
        """Rebuild the cache from the current head without changing authority.

        This is the common reconciliation path after attempt transactions and
        process recovery.  It deliberately refuses to adopt a different
        controller term; takeover must remain an explicit fenced operation.
        """

        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
        )
        if (
            snapshot.run.owner_id != self.owner_id
            or snapshot.run.controller_lease_id != self.controller_lease_id
            or snapshot.run.controller_holder_id != self.controller_holder_id
            or snapshot.run.controller_fencing_token
            != self.controller_fencing_token
            or snapshot.revision.revision < self.run_revision
            or snapshot.revision.owner_revision < self.owner_revision
        ):
            raise RuntimeError(
                "Canonical run head or controller term differs from this authority."
            )
        candidate_contract = _retained_candidate_contract(snapshot)
        rebuilt = build_run_controller(
            snapshot.control.manifest,
            candidate_contract=candidate_contract,
            candidate_normalizer=self.candidate_normalizer,
            normalizer_version=self.normalizer_version,
            logical_trial_id_factory=self.logical_trial_id_factory,
        )
        rebuilt.restore_canonical_state(
            _controller_restore_state(snapshot, self.candidate_normalizer)
        )
        if candidate_contract != self.candidate_contract:
            raise RuntimeError(
                "Canonical candidate contract changed for an existing run authority."
            )
        self.controller = rebuilt
        self.run_revision = snapshot.revision.revision
        self.owner_revision = snapshot.revision.owner_revision
        return snapshot

    def preflight(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        admission_id: str,
    ) -> PreparedProposal:
        """Prepare a deterministic admission without mutating either authority."""

        prepared = self.controller.preflight_proposal(
            candidates,
            admission_id=admission_id,
            expected_run_revision=self.run_revision,
        )
        for candidate_index, candidate in enumerate(prepared.candidates):
            candidate_format = self.candidate_contract.get("format")
            if candidate["format"] != candidate_format:
                raise MethodProtocolError(
                    "candidate_malformed",
                    "Candidate format differs from the retained environment contract.",
                    details={"candidate_index": candidate_index},
                )
            if set(candidate) != _CANONICAL_CANDIDATE_FIELDS:
                raise MethodProtocolError(
                    "candidate_malformed",
                    "Canonical candidates require exactly the standard "
                    "candidate fields.",
                    details={"candidate_index": candidate_index},
                )
            validation = _thaw_json(self.candidate_contract.get("validation", {}))
            materialization = _thaw_json(
                self.candidate_contract.get("materialization", {})
            )
            if (
                candidate["format"] != self.candidate_contract.get("format")
                or candidate["validation"] != validation
                or candidate["materialization"] != materialization
            ):
                raise MethodProtocolError(
                    "candidate_malformed",
                    "Candidate format, validation, or materialization differs "
                    "from the retained environment contract.",
                    details={"candidate_index": candidate_index},
                )
            if candidate_format == "files":
                try:
                    validate_sealed_file_candidate_spec(
                        candidate["spec"], self.candidate_contract
                    )
                except (TypeError, ValueError) as error:
                    raise MethodProtocolError(
                        "candidate_malformed",
                        f"Sealed file candidate is malformed: {error}",
                        details={"candidate_index": candidate_index},
                    ) from error
            try:
                validate_portable_candidate_metadata(
                    candidate["lineage"], "candidate lineage"
                )
                validate_portable_candidate_metadata(
                    candidate["generator"], "candidate generator"
                )
            except (TypeError, ValueError) as error:
                raise MethodProtocolError(
                    "candidate_malformed",
                    f"Candidate metadata is not portable: {error}",
                    details={"candidate_index": candidate_index},
                ) from error
        return prepared

    def commit_and_apply(
        self,
        prepared: PreparedProposal,
        *,
        change_ttl_seconds: float = 300,
    ) -> Tuple[AcceptedLogicalTrial, ...]:
        """Commit prepared facts, advance the cursor, then update the pure cache."""

        if not isinstance(prepared, PreparedProposal):
            raise TypeError("prepared must be a PreparedProposal.")
        if self.candidate_contract.get("format") != "parameters":
            raise ValueError(
                "Content-bearing candidates require the staged file proposal seam."
            )
        if prepared.expected_run_revision != self.run_revision:
            raise ValueError("Prepared proposal targets a different run revision.")
        if not prepared.candidates:
            raise ValueError(
                "Empty method completion is not part of the parameter admission slice."
            )
        plan = _parameter_admission_plan(prepared)
        operation_prefix = f"run/{self.run_id}/admission/{prepared.admission_id}"
        change = self.ledger.begin_owner_change(
            operation_id=f"{operation_prefix}/change",
            actor_principal_id=self.actor_principal_id,
            owner_id=self.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=change_ttl_seconds,
        )
        receipt = self.ledger.commit_run_candidate_admissions(
            operation_id=f"{operation_prefix}/commit",
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            change_id=change.change_id,
            plan=plan,
            content_bindings=(),
        )
        self._adopt_receipt_cursor(receipt)
        return self.controller.apply_admission(prepared, receipt)

    def admit(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        admission_id: str,
        change_ttl_seconds: float = 300,
    ) -> Tuple[AcceptedLogicalTrial, ...]:
        """Convenience wrapper retaining the explicit commit-before-apply order."""

        return self.commit_and_apply(
            self.preflight(candidates, admission_id=admission_id),
            change_ttl_seconds=change_ttl_seconds,
        )

    def complete_method_proposal(
        self,
        preparation: RunMethodExchangePreparationRecord,
        *,
        candidates: Sequence[Mapping[str, Any]],
        response_digest: str,
        change_ttl_seconds: float = 300,
    ) -> RunMethodProposalCompletionReceipt:
        """Atomically admit one prepared retained-method proposal.

        ``preparation.exchange_id`` is the stable admission identity.  It makes
        controller preflight, logical-trial allocation, the provisional owner
        change, and exact ledger replay converge on the same facts after a lost
        response.  ``response_digest`` is the digest of the complete validated
        worker response; transport/result validation remains the method-driver
        adapter's responsibility rather than a generic controller concern.

        The canonical proposal completion and admission commit in one ledger
        transaction.  Only after that receipt is verified does this disposable
        authority cursor advance and apply the prepared proposal to its cache.
        A restarted caller hydrates from the resulting snapshot instead of
        replaying this already-completed seam.
        """

        if not isinstance(preparation, RunMethodExchangePreparationRecord):
            raise TypeError(
                "preparation must be a RunMethodExchangePreparationRecord."
            )
        if preparation.kind != "proposal":
            raise ValueError("Method proposal admission requires a proposal preparation.")
        if preparation.run_id != self.run_id:
            raise ValueError("Method proposal preparation belongs to another run.")
        if self.candidate_contract.get("format") != "parameters":
            raise ValueError(
                "File candidates require the staged file proposal completion seam."
            )
        prepared = self.preflight(
            candidates,
            admission_id=preparation.exchange_id,
        )
        if (
            prepared.requested_width != preparation.exchange_input.requested_width
        ):
            raise ValueError(
                "Method proposal preparation differs from canonical preflight."
            )
        if not prepared.candidates:
            raise ValueError(
                "Empty method completion is not a parameter admission."
            )

        plan = _parameter_admission_plan(prepared)
        operation_prefix = (
            f"run/{self.run_id}/method-proposal/{preparation.exchange_id}"
        )
        change = self.ledger.begin_owner_change(
            operation_id=f"{operation_prefix}/change",
            actor_principal_id=self.actor_principal_id,
            owner_id=self.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=change_ttl_seconds,
        )
        receipt = self.ledger.complete_run_method_proposal_exchange(
            operation_id=f"{operation_prefix}/complete",
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            round_index=preparation.round_index,
            prepared_input_digest=preparation.input_digest,
            outcome="admitted",
            response_digest=response_digest,
            expected_run_revision=self.run_revision,
            expected_owner_revision=self.owner_revision,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            change_id=change.change_id,
            plan=plan,
            content_bindings=(),
        )
        admission = receipt.admission
        if admission is None:
            raise RuntimeError(
                "Canonical method proposal completion lacks its admission receipt."
            )
        self._adopt_receipt_cursor(admission)
        self.controller.apply_admission(prepared, admission)
        return receipt

    def complete_staged_file_method_proposal(
        self,
        preparation: RunMethodExchangePreparationRecord,
        *,
        candidates: Sequence[Mapping[str, Any] | FileCandidateDraft],
        response_digest: str,
        content_service: RealmContentService,
        store_id: str,
        source_resolver: Callable[[int, FileCandidateDraft], AllowedTreeSource],
        change_ttl_seconds: float = 300,
        heartbeat_interval_seconds: float | None = None,
    ) -> RunMethodProposalCompletionReceipt:
        """Seal and atomically admit one complete generation-bound file proposal."""

        if not isinstance(preparation, RunMethodExchangePreparationRecord):
            raise TypeError(
                "preparation must be a RunMethodExchangePreparationRecord."
            )
        if preparation.kind != "proposal" or preparation.run_id != self.run_id:
            raise ValueError("Method proposal preparation differs from this run.")
        if self.candidate_contract.get("format") != "files":
            raise ValueError("Staged file admission requires a file candidate contract.")
        if not isinstance(content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(store_id, str) or not store_id.strip() or "\x00" in store_id:
            raise ValueError("store_id must be non-empty text.")
        if not callable(source_resolver):
            raise TypeError("source_resolver must be callable.")
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise TypeError("file candidates must be a sequence.")
        ttl = _positive_finite(change_ttl_seconds, "change_ttl_seconds")
        interval = (
            ttl / 3.0
            if heartbeat_interval_seconds is None
            else _positive_finite(
                heartbeat_interval_seconds, "heartbeat_interval_seconds"
            )
        )
        if interval >= ttl:
            raise ValueError("heartbeat interval must be shorter than change TTL.")

        try:
            drafts = tuple(
                value
                if isinstance(value, FileCandidateDraft)
                else FileCandidateDraft.from_candidate(value)
                for value in candidates
            )
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise MethodProtocolError(
                "candidate_malformed",
                f"File-candidate proposal is malformed: {error}",
                details={},
            ) from error
        if not drafts:
            raise ValueError("Empty method completion has no file admission.")
        requested_width = preparation.exchange_input.requested_width
        if len(drafts) > requested_width:
            raise MethodProtocolError(
                "batch_overproduced",
                "Batch method returned more file candidates than requested.",
                details={
                    "requested_width": requested_width,
                    "returned_count": len(drafts),
                },
            )

        # A canonical completion can exist even though the caller lost the
        # commit response.  Recover it before consulting the now-advanced
        # controller cursor, touching staging, or beginning another owner
        # change.  The ledger compares both stable exchange-input and worker-
        # response digests and reconstructs the exact historical admission.
        recovered = self.ledger.read_run_method_proposal_completion_receipt(
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            exchange_id=preparation.exchange_id,
            expected_prepared_input_digest=preparation.input_digest,
            expected_response_digest=response_digest,
        )
        if recovered is not None:
            if recovered.admission is None or recovered.completion.outcome != "admitted":
                raise RuntimeError(
                    "Canonical file proposal recovery lacks its admission receipt."
                )
            self.refresh_controller()
            return recovered

        if (
            self.controller.next_proposal_width != requested_width
            or requested_width <= 0
        ):
            raise RuntimeError(
                "Method proposal preparation differs from the authority cursor."
            )
        proposal_ids: set[str] = set()
        accepted_ids = set(self.controller.accepted_candidate_ids)
        for index, draft in enumerate(drafts):
            if draft.candidate_id in proposal_ids or draft.candidate_id in accepted_ids:
                raise MethodProtocolError(
                    "duplicate_candidate_id",
                    "File candidate id is already present in this run or proposal.",
                    details={
                        "candidate_index": index,
                        "candidate_id": draft.candidate_id,
                    },
                )
            proposal_ids.add(draft.candidate_id)

        # Authenticate every token/controller/staging coordinate before the
        # first provisional owner mutation.  The same resolver is invoked
        # around every seal and once immediately before canonical commit.
        initial_sources = tuple(
            _resolved_file_source(source_resolver(index, draft))
            for index, draft in enumerate(drafts)
        )
        if len(set(initial_sources)) != len(initial_sources):
            raise MethodProtocolError(
                "candidate_malformed",
                "File candidates select duplicate frozen staging subtrees.",
                details={},
            )

        operation_prefix = (
            f"run/{self.run_id}/method-proposal/{preparation.exchange_id}"
        )
        # Capture is provisional and retry-scoped.  A failed invocation aborts
        # only its own change; a later invocation for the still-pending
        # exchange receives a fresh change/seal/hold namespace.  The canonical
        # proposal completion operation below remains exchange-stable.
        capture_prefix = f"{operation_prefix}/capture/{uuid.uuid4().hex}"
        change = self.ledger.begin_owner_change(
            operation_id=f"{capture_prefix}/change",
            actor_principal_id=self.actor_principal_id,
            owner_id=self.owner_id,
            expected_owner_revision=self.owner_revision,
            ttl_seconds=ttl,
        )
        heartbeat = _OwnerChangeHeartbeat(
            self.ledger,
            actor_principal_id=self.actor_principal_id,
            owner_id=self.owner_id,
            change_id=change.change_id,
            retention_lease_id=change.retention_lease_id,
            ttl_seconds=ttl,
            interval_seconds=interval,
            operation_coordinate=request_digest(
                {
                    "change_id": change.change_id,
                    "exchange_id": preparation.exchange_id,
                    "format": "optpilot.file-proposal-heartbeat.v1",
                    "session": uuid.uuid4().hex,
                }
            ),
        )
        heartbeat_active = False
        committed = False
        try:
            heartbeat.start()
            heartbeat_active = True
            capture = content_service.capture(
                actor_principal_id=self.actor_principal_id,
                change_id=change.change_id,
                store_id=store_id,
            )
            snapshots: list[SnapshotRef] = []
            canonical_candidates: list[dict[str, Any]] = []
            for ordinal, (draft, initial_source) in enumerate(
                zip(drafts, initial_sources)
            ):
                heartbeat.raise_if_failed()
                source = _resolved_file_source(source_resolver(ordinal, draft))
                if source != initial_source:
                    raise ValueError("File-candidate staging source changed before seal.")
                sealed = capture.seal_tree(
                    source=source,
                    limits=CANDIDATE_SEAL_LIMITS,
                    operation_id=f"{capture_prefix}/candidate/{ordinal:08d}/seal",
                )
                after = _resolved_file_source(source_resolver(ordinal, draft))
                if after != source:
                    raise ValueError("File-candidate staging source changed during seal.")
                if (
                    sealed_file_candidate_declaration_digest(draft, sealed.manifest)
                    != draft.declaration_digest
                ):
                    raise ValueError(
                        "Sealed file-candidate declaration differs from its draft token."
                    )
                spec = sealed_file_candidate_spec(
                    sealed.manifest, self.candidate_contract
                )
                snapshots.append(sealed.snapshot_ref)
                canonical_candidates.append(
                    {
                        "candidate_id": draft.candidate_id,
                        "format": "files",
                        "generator": _thaw_json(draft.generator),
                        "lineage": _thaw_json(draft.lineage),
                        "spec": spec,
                    }
                )
            prepared = self.preflight(
                canonical_candidates, admission_id=preparation.exchange_id
            )
            if prepared.requested_width != requested_width:
                raise RuntimeError(
                    "Sealed file proposal differs from canonical preparation width."
                )
            plan = _file_admission_plan(prepared, snapshots)
            bindings = tuple(
                sorted(
                    {
                        OwnerMembership(store_id, snapshot, RUN_CANDIDATE_ROLE)
                        for snapshot in snapshots
                    },
                    key=lambda item: (item.store_id, str(item.content_ref), item.role),
                )
            )
            held = self.ledger.hold_owner_content(
                operation_id=f"{capture_prefix}/hold",
                actor_principal_id=self.actor_principal_id,
                change_id=change.change_id,
                memberships=bindings,
            )
            if set(held) != set(bindings):
                raise RuntimeError("File-candidate provisional holds differ.")
            heartbeat.raise_if_failed()
            for ordinal, (draft, initial_source) in enumerate(
                zip(drafts, initial_sources)
            ):
                if (
                    _resolved_file_source(source_resolver(ordinal, draft))
                    != initial_source
                ):
                    raise ValueError(
                        "File-candidate staging authority changed before admission."
                    )
            heartbeat.stop()
            heartbeat_active = False
            receipt = self.ledger.complete_run_method_proposal_exchange(
                operation_id=f"{operation_prefix}/complete",
                actor_principal_id=self.actor_principal_id,
                run_id=self.run_id,
                round_index=preparation.round_index,
                prepared_input_digest=preparation.input_digest,
                outcome="admitted",
                response_digest=response_digest,
                expected_run_revision=self.run_revision,
                expected_owner_revision=self.owner_revision,
                controller_lease_id=self.controller_lease_id,
                controller_holder_id=self.controller_holder_id,
                controller_fencing_token=self.controller_fencing_token,
                change_id=change.change_id,
                plan=plan,
                content_bindings=bindings,
                rebase_file_candidate_owner_change=True,
            )
            committed = True
            admission = receipt.admission
            if admission is None:
                raise RuntimeError(
                    "Canonical file proposal completion lacks admission receipt."
                )
            self._adopt_receipt_cursor(admission)
            self.controller.apply_admission(prepared, admission)
            return receipt
        except BaseException:
            if heartbeat_active:
                heartbeat.stop(suppress_failure=True)
            if not committed:
                try:
                    self.ledger.abort_owner_change(
                        operation_id=f"{capture_prefix}/abort",
                        actor_principal_id=self.actor_principal_id,
                        change_id=change.change_id,
                    )
                except Exception:
                    pass
            raise

    def close_submissions(
        self,
        *,
        operation_id: str,
        stop_code: str,
    ) -> RunSubmissionControlReceipt:
        """Append one explicit canonical close and reconcile the local cache.

        Evidence-derived reasons such as ``max_trials``, ``max_failures``, and
        ``converged`` are intentionally rejected by the ledger. They are
        committed atomically by admission or attempt adoption instead.
        """

        receipt = self.ledger.close_run_submissions(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=self.run_revision,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            stop_code=stop_code,
        )
        self.refresh_controller()
        return receipt

    def escalate_stop(
        self,
        *,
        operation_id: str,
        stop_code: str,
    ) -> RunSubmissionControlReceipt:
        """Escalate the current soft drain to one canonical hard stop."""

        receipt = self.ledger.escalate_run_stop(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=self.run_revision,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
            stop_code=stop_code,
        )
        self.refresh_controller()
        return receipt

    def finish(self, *, operation_id: str) -> RunFinishReceipt:
        """Finalize a drained run using only the ledger-derived terminal pair."""

        receipt = self.ledger.finish_run(
            operation_id=operation_id,
            actor_principal_id=self.actor_principal_id,
            run_id=self.run_id,
            expected_run_revision=self.run_revision,
            controller_lease_id=self.controller_lease_id,
            controller_holder_id=self.controller_holder_id,
            controller_fencing_token=self.controller_fencing_token,
        )
        self.refresh_controller()
        return receipt

    def _adopt_receipt_cursor(self, receipt: RunAdmissionReceipt) -> None:
        if (
            receipt.run.run_id != self.run_id
            or receipt.run.owner_id != self.owner_id
            or receipt.revision.revision != self.run_revision + 1
            or receipt.revision.owner_revision < self.owner_revision
            or receipt.run.controller_lease_id != self.controller_lease_id
            or receipt.run.controller_holder_id != self.controller_holder_id
            or receipt.run.controller_fencing_token
            != self.controller_fencing_token
        ):
            raise RuntimeError("Canonical admission receipt does not match this authority.")
        # The ledger commit has already happened.  Advance the canonical cursor
        # before touching the rebuildable in-process controller cache.
        self.run_revision = receipt.revision.revision
        self.owner_revision = receipt.revision.owner_revision


def _parameter_admission_plan(prepared: PreparedProposal) -> RunAdmissionPlan:
    candidates = []
    logical_trials = []
    for candidate, logical_trial_id in zip(
        prepared.candidates, prepared.logical_trial_ids
    ):
        if candidate["format"] != "parameters":
            raise ValueError(
                "Parameter admission cannot contain content-bearing candidates."
            )
        candidates.append(
            CandidateAdmission(
                candidate_id=candidate["candidate_id"],
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec=candidate["spec"],
                ),
                lineage=candidate["lineage"],
                generator=candidate["generator"],
            )
        )
        logical_trials.append(
            LogicalTrialAdmission(
                logical_trial_id=logical_trial_id,
                candidate_id=candidate["candidate_id"],
                submission_metadata={
                    "admission_id": prepared.admission_id,
                    "prepared_proposal_digest": prepared.digest,
                },
            )
        )
    return RunAdmissionPlan(tuple(candidates), tuple(logical_trials))


def _file_admission_plan(
    prepared: PreparedProposal, snapshots: Sequence[SnapshotRef]
) -> RunAdmissionPlan:
    if len(prepared.candidates) != len(snapshots):
        raise ValueError("File-candidate snapshots differ from prepared candidates.")
    candidates = []
    logical_trials = []
    for candidate, logical_trial_id, snapshot in zip(
        prepared.candidates, prepared.logical_trial_ids, snapshots
    ):
        if candidate["format"] != "files" or not isinstance(snapshot, SnapshotRef):
            raise ValueError("File admission requires one tree snapshot per candidate.")
        candidates.append(
            CandidateAdmission(
                candidate_id=candidate["candidate_id"],
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="files",
                    spec=candidate["spec"],
                    content_refs=(snapshot,),
                ),
                lineage=candidate["lineage"],
                generator=candidate["generator"],
            )
        )
        logical_trials.append(
            LogicalTrialAdmission(
                logical_trial_id=logical_trial_id,
                candidate_id=candidate["candidate_id"],
                submission_metadata={
                    "admission_id": prepared.admission_id,
                    "prepared_proposal_digest": prepared.digest,
                },
            )
        )
    return RunAdmissionPlan(tuple(candidates), tuple(logical_trials))


def _resolved_file_source(value: Any) -> AllowedTreeSource:
    if not isinstance(value, AllowedTreeSource):
        raise TypeError("source_resolver must return an AllowedTreeSource.")
    if not value.allowed_root.is_absolute():
        raise ValueError("file-candidate allowed root must be absolute.")
    return value


class _OwnerChangeHeartbeat:
    """Small renewal loop for one potentially long multi-tree capture."""

    def __init__(
        self,
        ledger: RealmLedger,
        *,
        actor_principal_id: str,
        owner_id: str,
        change_id: str,
        retention_lease_id: str,
        ttl_seconds: float,
        interval_seconds: float,
        operation_coordinate: str,
    ) -> None:
        self._ledger = ledger
        self._actor = actor_principal_id
        self._change_id = change_id
        self._lease_id = retention_lease_id
        self._ttl = ttl_seconds
        self._interval = interval_seconds
        self._coordinate = operation_coordinate
        lease = ledger.validate_lease(
            actor_principal_id=actor_principal_id,
            lease_id=retention_lease_id,
            holder_id=actor_principal_id,
            fencing_token=1,
        )
        if (
            lease.owner_id != owner_id
            or lease.lease_kind != "owner-change-retention"
            or lease.audience != "realm-ledger"
            or lease.scope_key != f"owner-change:{change_id}"
            or lease.metadata.get("change_id") != change_id
        ):
            raise RuntimeError("Owner-change retention authority differs.")
        self._holder = lease.holder_id
        self._fence = lease.fencing_token
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._round = 0

    def start(self) -> None:
        self._heartbeat_once()
        thread = threading.Thread(
            target=self._background,
            name=f"optpilot-file-capture-heartbeat-{self._coordinate[:12]}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _heartbeat_once(self) -> None:
        with self._lock:
            self._round += 1
            round_number = self._round
        self._ledger.heartbeat_owner_change(
            operation_id=(
                "file-proposal-heartbeat/"
                f"{self._coordinate}/{round_number:016d}"
            ),
            actor_principal_id=self._actor,
            change_id=self._change_id,
            retention_lease_id=self._lease_id,
            holder_id=self._holder,
            fencing_token=self._fence,
            ttl_seconds=self._ttl,
        )

    def _background(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._heartbeat_once()
            except BaseException as error:
                with self._lock:
                    if self._failure is None:
                        self._failure = error
                self._stop.set()
                return

    def raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError("File-candidate capture heartbeat failed.") from failure

    def stop(self, *, suppress_failure: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if not suppress_failure:
            self.raise_if_failed()


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a positive finite number.")
    selected = float(value)
    if not math.isfinite(selected) or selected <= 0:
        raise ValueError(f"{label} must be a positive finite number.")
    return selected


def _controller_restore_state(
    snapshot: RunLedgerSnapshot,
    candidate_normalizer: CandidateNormalizer,
) -> RunControllerRestoreState:
    """Project typed Realm facts into the pure controller recovery contract."""

    if not isinstance(snapshot, RunLedgerSnapshot):
        raise TypeError("snapshot must be a RunLedgerSnapshot.")
    if not callable(candidate_normalizer):
        raise TypeError("candidate_normalizer must be callable.")

    candidate_by_key = {item.candidate_key: item for item in snapshot.candidates}
    contract = _retained_candidate_contract(snapshot)
    validation = _thaw_json(contract.get("validation", {}))
    materialization = _thaw_json(contract.get("materialization", {}))
    if not isinstance(validation, Mapping) or not isinstance(
        materialization, Mapping
    ):
        raise ValueError(
            "Environment validation and materialization contracts must be mappings."
        )
    normalized_by_key: dict[str, dict[str, Any]] = {}
    for candidate_key, record in candidate_by_key.items():
        admission = record.admission
        persisted = {
            "candidate_id": admission.candidate_id,
            "format": admission.envelope.candidate_format,
            "spec": _thaw_json(admission.envelope.spec),
            "lineage": _thaw_json(admission.lineage),
            "generator": _thaw_json(admission.generator),
            "validation": validation,
            "materialization": materialization,
        }
        if persisted["format"] != contract.get("format"):
            raise ValueError("Admitted candidate format differs from its contract.")
        if persisted["format"] == "files":
            if (
                len(admission.envelope.content_refs) != 1
                or not isinstance(admission.envelope.content_refs[0], SnapshotRef)
            ):
                raise ValueError(
                    "A retained file candidate requires exactly one tree snapshot."
                )
            validate_sealed_file_candidate_spec(persisted["spec"], contract)
        normalized_value = candidate_normalizer(copy.deepcopy(persisted))
        if not isinstance(normalized_value, Mapping):
            raise TypeError("candidate_normalizer must return a mapping.")
        normalized = _thaw_json(normalized_value)
        if (
            set(normalized) != _CANONICAL_CANDIDATE_FIELDS
            or normalized != persisted
        ):
            raise ValueError(
                "Resolved candidate normalizer changes immutable admitted facts "
                "or their canonical field set."
            )
        normalized_by_key[candidate_key] = normalized

    heads: dict[str, Any] = {}
    for transition in snapshot.logical_transitions:
        heads[transition.logical_trial_id] = transition
    attempts_by_trial: dict[str, list[Any]] = {
        item.admission.logical_trial_id: [] for item in snapshot.logical_trials
    }
    attempt_by_id = {}
    for attempt in snapshot.attempts:
        attempts_by_trial[attempt.logical_trial_id].append(attempt)
        attempt_by_id[attempt.attempt_id] = attempt
    observations_by_trial: dict[str, list[Any]] = {
        item.admission.logical_trial_id: [] for item in snapshot.logical_trials
    }
    observation_by_attempt = {}
    for observation in snapshot.observations:
        attempt = attempt_by_id[observation.attempt_id]
        observations_by_trial[attempt.logical_trial_id].append(observation)
        observation_by_attempt[observation.attempt_id] = observation

    restored_trials = []
    for trial in snapshot.logical_trials:
        trial_id = trial.admission.logical_trial_id
        head = heads[trial_id]
        attempts = attempts_by_trial[trial_id]
        observations = observations_by_trial[trial_id]
        terminal = head.to_state == "terminal"
        terminal_observation = (
            None
            if head.attempt_id is None
            else observation_by_attempt.get(head.attempt_id)
        )
        if head.attempt_id is not None:
            terminal_attempt = attempt_by_id.get(head.attempt_id)
            if terminal_attempt is None or terminal_attempt.logical_trial_id != trial_id:
                raise ValueError(
                    "Terminal logical transition refers to another attempt."
                )
        restored_trials.append(
            LogicalTrialRestoreState(
                logical_trial_id=trial_id,
                candidate=normalized_by_key[trial.candidate_key],
                state=head.to_state,
                outcome=head.outcome if terminal else None,
                code=head.code if terminal else None,
                terminal_sequence=head.sequence if terminal else None,
                attempt_count=len(attempts),
                observation_count=len(observations),
                metric_values=(
                    {}
                    if terminal_observation is None
                    else _thaw_json(terminal_observation.envelope.metric_values)
                ),
                completion_metadata={
                    "source": "realm-ledger",
                    "attempt_ids": [item.attempt_id for item in attempts],
                    "observation_ids": [
                        item.observation_id for item in observations
                    ],
                    "terminal_attempt_id": head.attempt_id,
                },
            )
        )

    submission = snapshot.control.current_submission
    return RunControllerRestoreState(
        run_status=snapshot.run.state,
        submission_state=submission.state,
        submission_stop_code=submission.stop_code,
        terminal_code=(
            None
            if snapshot.finalization is None
            else snapshot.finalization.code
        ),
        logical_trials=tuple(restored_trials),
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


def _retained_candidate_contract(
    snapshot: RunLedgerSnapshot,
) -> Mapping[str, Any]:
    """Return the immutable environment-owned bounded candidate contract."""

    contract = _thaw_json(
        snapshot.evaluation_closure.environment_revision.candidate_contract
    )
    if not isinstance(contract, Mapping):
        raise TypeError("Retained environment candidate contract must be a mapping.")
    if contract.get("format") not in {"parameters", "files"}:
        raise ValueError("Retained candidate contract format is unsupported.")
    frozen = freeze_json(contract, label="retained candidate contract")
    if not isinstance(frozen, Mapping):  # Defensive: mappings freeze to mappings.
        raise TypeError("Retained environment candidate contract must be a mapping.")
    return frozen


__all__ = ["RetainedRunAuthority"]
