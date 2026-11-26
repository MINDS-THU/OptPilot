"""Realm-native retention of declared outputs from one managed attempt.

The finalizer is deliberately smaller than the run adopter.  It reads only the
``trial`` scope of an already-realized :class:`ManagedProcessExecutionBinding`,
seals every declaration into one configured local content store, and places
the exact resulting roots under the attempt's existing capture change.  The
later run-adoption transaction remains the sole authority that turns those
provisional holds into canonical run artifacts.

No host path is accepted by this API or included in its result.  Host paths are
resolved internally from the managed binding and are used only as
descriptor-rooted capture roots.  A scheduler must authenticate worker
termination before calling this component because writable native paths are
not revocable capabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from ..attempts import (
    AttemptEnvelope,
    AttemptFinalization,
    CapturedArtifact,
    OutputDeclaration,
)
from ..runtime_binding import (
    TRIAL_SCOPE,
    LayeredVolumeScopeSource,
    PortableAttemptRuntimeSpec,
    VolumeScopeSource,
)
from .content import AllowedFileSource, AllowedTreeSource, LocalContentCapture
from .errors import RealmConflict, RealmError, RealmIntegrityError
from .ledger import RealmLedger
from .manifests import SealLimits, validate_portable_path
from .owners import OwnerMembership
from .process_execution_binder import ManagedProcessExecutionBinding
from .refs import request_digest
from .run_attempt_records import RUN_ARTIFACT_ROLE
from .service import RealmContentService


_CAPTURE_METADATA_FORMAT: Final = "optpilot.realm-attempt-artifact-capture.v1"
_MAX_DECLARATIONS: Final = 1_024
OPERATOR_JOB_OUTPUT_ROLE: Final = "operator-job-output"


class DeclaredOutputRuntimeBinding(Protocol):
    """Small shared binding surface needed to capture declared outputs."""

    @property
    def binding_id(self) -> str: ...

    @property
    def portable_spec(self) -> PortableAttemptRuntimeSpec: ...

    @property
    def scope_paths(self) -> Mapping[str, Path]: ...

    @property
    def workdir(self) -> Path: ...

    def validate(self) -> None: ...


class RealmAttemptFinalizationError(RealmError):
    """Path-free, retryable failure to capture one declared artifact.

    The original exception is intentionally not interpolated into the public
    message: low-level filesystem errors can contain provider-private roots.
    ``cause_type`` is diagnostic classification only and carries no host data.
    The capture change is left active so exact tree replay, ordinary
    content-addressed blob replay, or eventual change cleanup can reconcile it.
    """

    def __init__(
        self,
        code: str,
        *,
        declaration_id: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("finalization error code must be nonempty text.")
        if declaration_id is not None and (
            not isinstance(declaration_id, str) or not declaration_id
        ):
            raise ValueError("declaration_id must be nonempty text or None.")
        if cause_type is not None and (
            not isinstance(cause_type, str) or not cause_type
        ):
            raise ValueError("cause_type must be nonempty text or None.")
        self.code = code
        self.declaration_id = declaration_id
        self.cause_type = cause_type
        super().__init__(
            "A declared attempt artifact could not be captured safely."
            if declaration_id is not None
            else "The attempt artifact finalization request is invalid."
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "cause_type": self.cause_type,
            "code": self.code,
            "declaration_id": self.declaration_id,
            "message": str(self),
        }


@dataclass(frozen=True)
class _CaptureBudget:
    entries: int
    bytes: int

    def after(self, *, entries: int, size_bytes: int) -> "_CaptureBudget":
        if entries < 0 or size_bytes < 0:
            raise RealmIntegrityError("Captured artifact size facts are invalid.")
        if entries > self.entries or size_bytes > self.bytes:
            raise RealmConflict("Declared artifact capture exceeds its bounded budget.")
        return _CaptureBudget(
            entries=self.entries - entries,
            bytes=self.bytes - size_bytes,
        )


class RealmAttemptFinalizer:
    """Capture all outputs declared by one exact managed attempt envelope."""

    def __init__(
        self,
        ledger: RealmLedger,
        content_service: RealmContentService,
        *,
        actor_principal_id: str,
        store_id: str,
        seal_limits: SealLimits | None = None,
        max_declarations: int = _MAX_DECLARATIONS,
    ) -> None:
        if not isinstance(ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        # Both objects are Realm-internal facades.  Rejecting a split authority
        # here prevents bytes from being published under a different ledger
        # than the one receiving provisional owner holds.
        if getattr(content_service, "_ledger", None) is not ledger:
            raise ValueError("content service and finalizer must share one Realm ledger.")
        if not isinstance(actor_principal_id, str) or not actor_principal_id:
            raise ValueError("actor_principal_id must be nonempty text.")
        if not isinstance(store_id, str) or not store_id:
            raise ValueError("store_id must be nonempty text.")
        if isinstance(max_declarations, bool) or not isinstance(max_declarations, int):
            raise TypeError("max_declarations must be an integer.")
        if max_declarations <= 0 or max_declarations > _MAX_DECLARATIONS:
            raise ValueError(
                f"max_declarations must be between 1 and {_MAX_DECLARATIONS}."
            )
        limits = seal_limits or SealLimits()
        if not isinstance(limits, SealLimits):
            raise TypeError("seal_limits must be a SealLimits value or None.")
        self._ledger = ledger
        self._content_service = content_service
        self._actor_principal_id = actor_principal_id
        self._store_id = store_id
        self._seal_limits = limits
        self._max_declarations = max_declarations

    def finalize(
        self,
        *,
        envelope: AttemptEnvelope,
        binding: ManagedProcessExecutionBinding,
        change_id: str,
    ) -> AttemptFinalization:
        """Seal every declaration and return an adoption-ready finalization.

        Successful declarations are held one at a time with deterministic
        operation ids.  This is intentional: if a later declaration fails,
        earlier progress remains named and retryable under the same capture
        change instead of becoming unidentifiable staging debris.
        """

        canonical_envelope = self._canonical_envelope(envelope)
        if not isinstance(binding, ManagedProcessExecutionBinding):
            raise TypeError("binding must be a ManagedProcessExecutionBinding.")
        if not isinstance(change_id, str) or not change_id:
            raise ValueError("change_id must be nonempty text.")

        binding.validate()
        receipt = binding.receipt
        durable = receipt.binding
        attempt = receipt.attempt
        durable.validate_attempt(attempt)
        if (
            canonical_envelope.attempt_id != attempt.attempt_id
            or canonical_envelope.evaluation_spec_digest
            != attempt.evaluation_spec_digest
            or canonical_envelope.binding_id != durable.binding_id
            or canonical_envelope.evaluation_spec_digest
            != durable.portable_spec.evaluation_spec_digest
        ):
            raise RealmConflict(
                "Attempt envelope identity differs from the managed binding."
            )
        if change_id != attempt.capture_change_id:
            raise RealmConflict(
                "Artifact capture change differs from the managed attempt."
            )

        artifacts = self.capture_declared_outputs(
            envelope=canonical_envelope,
            binding=binding,
            change_id=change_id,
            membership_role=RUN_ARTIFACT_ROLE,
        )

        effective_code = self._effective_code(canonical_envelope)
        return AttemptFinalization(
            attempt_id=canonical_envelope.attempt_id,
            evaluation_spec_digest=canonical_envelope.evaluation_spec_digest,
            binding_id=canonical_envelope.binding_id,
            effective_outcome=canonical_envelope.outcome,
            effective_code=effective_code,
            captured_artifacts=artifacts,
            envelope=canonical_envelope,
        )

    def capture_declared_outputs(
        self,
        *,
        envelope: AttemptEnvelope,
        binding: DeclaredOutputRuntimeBinding,
        change_id: str,
        membership_role: str,
    ) -> tuple[CapturedArtifact, ...]:
        """Capture one exact envelope through the shared output-retention path.

        The caller owns the surrounding owner-change transaction. Canonical
        attempts use their prepared capture change; Operator Jobs use a change
        on the derived job owner and commit it before publishing the result.
        """

        canonical_envelope = self._canonical_envelope(envelope)
        if not isinstance(change_id, str) or not change_id:
            raise ValueError("change_id must be nonempty text.")
        if not isinstance(membership_role, str) or not membership_role:
            raise ValueError("membership_role must be nonempty text.")
        required = ("validate", "portable_spec", "scope_paths", "workdir", "binding_id")
        if any(not hasattr(binding, name) for name in required):
            raise TypeError("binding does not implement declared-output capture.")
        binding.validate()
        if (
            canonical_envelope.binding_id != binding.binding_id
            or canonical_envelope.evaluation_spec_digest
            != binding.portable_spec.evaluation_spec_digest
        ):
            raise RealmConflict(
                "Attempt envelope identity differs from the output capture binding."
            )
        declarations = canonical_envelope.output_declarations
        if len(declarations) > self._max_declarations:
            raise RealmAttemptFinalizationError("artifact_declaration_limit")

        trial_root, budget = self._trial_capture_root(binding)
        capture = self._content_service.capture(
            actor_principal_id=self._actor_principal_id,
            change_id=change_id,
            store_id=self._store_id,
        )
        artifacts: list[CapturedArtifact] = []
        for declaration in declarations:
            try:
                blob_staging_id: str | None = None
                validate_portable_path(declaration.path, limits=self._seal_limits)
                limits = self._remaining_limits(budget)
                if declaration.kind == "file":
                    if budget.entries < 1:
                        raise RealmConflict(
                            "Declared artifact capture exceeds its bounded budget."
                        )
                    sealed = capture.seal_blob(
                        source=AllowedFileSource(trial_root, declaration.path),
                        limits=limits,
                    )
                    content_ref = sealed.blob_ref
                    size_bytes = sealed.publication.logical_bytes
                    captured_entries = 1
                    blob_staging_id = sealed.publication.staging_id
                else:
                    sealed_tree = capture.seal_tree(
                        source=AllowedTreeSource(trial_root, declaration.path),
                        limits=limits,
                        operation_id=self._tree_capture_operation_id(
                            change_id=change_id,
                            envelope=canonical_envelope,
                            declaration=declaration,
                            membership_role=membership_role,
                        ),
                    )
                    content_ref = sealed_tree.snapshot_ref
                    size_bytes = sealed_tree.manifest.logical_bytes
                    captured_entries = len(sealed_tree.manifest.entries)

                budget = budget.after(
                    entries=captured_entries,
                    size_bytes=size_bytes,
                )
                membership = OwnerMembership(
                    self._store_id,
                    content_ref,
                    membership_role,
                )
                try:
                    self._ledger.hold_owner_content(
                        operation_id=self._hold_operation_id(
                            change_id=change_id,
                            envelope=canonical_envelope,
                            declaration=declaration,
                            membership_role=membership_role,
                        ),
                        actor_principal_id=self._actor_principal_id,
                        change_id=change_id,
                        memberships=(membership,),
                    )
                except Exception:
                    if blob_staging_id is not None:
                        self._rollback_blob_staging(
                            capture=capture,
                            change_id=change_id,
                            staging_id=blob_staging_id,
                            phase="failed owner hold",
                        )
                    raise
                if blob_staging_id is not None:
                    self._rollback_blob_staging(
                        capture=capture,
                        change_id=change_id,
                        staging_id=blob_staging_id,
                        phase="successful owner hold",
                    )
                artifacts.append(
                    CapturedArtifact(
                        declaration=declaration,
                        content_ref=str(content_ref),
                        size_bytes=size_bytes,
                        bindings=(
                            {
                                "content_ref": str(content_ref),
                                "store_id": self._store_id,
                            },
                        ),
                        visibility="operator",
                        metadata={
                            "capture_format": _CAPTURE_METADATA_FORMAT,
                            "content_kind": declaration.kind,
                            "declaration_digest": request_digest(
                                declaration.to_dict()
                            ),
                        },
                    )
                )
            except RealmAttemptFinalizationError:
                raise
            except Exception as error:
                raise RealmAttemptFinalizationError(
                    "artifact_capture_failed",
                    declaration_id=declaration.declaration_id,
                    cause_type=type(error).__name__,
                ) from None
        return tuple(artifacts)

    @staticmethod
    def _canonical_envelope(envelope: AttemptEnvelope) -> AttemptEnvelope:
        if not isinstance(envelope, AttemptEnvelope):
            raise TypeError("envelope must be an AttemptEnvelope.")
        try:
            canonical = AttemptEnvelope.from_dict(envelope.to_dict())
        except (KeyError, TypeError, ValueError):
            raise RealmIntegrityError("Attempt envelope is not canonical.") from None
        if canonical != envelope or canonical.digest != envelope.digest:
            raise RealmIntegrityError("Attempt envelope is not canonical.")
        return canonical

    def _trial_capture_root(
        self, binding: DeclaredOutputRuntimeBinding
    ) -> tuple[Path, _CaptureBudget]:
        spec = binding.portable_spec
        trial_scopes = tuple(item for item in spec.scopes if item.name == TRIAL_SCOPE)
        trial_requirements = tuple(
            item for item in spec.writable_volumes if item.name == TRIAL_SCOPE
        )
        if len(trial_scopes) != 1 or len(trial_requirements) != 1:
            raise RealmIntegrityError(
                "Managed attempt lacks one exact writable trial scope."
            )
        scope = trial_scopes[0]
        if (
            scope.access != "read-write"
            or not isinstance(
                scope.source,
                (VolumeScopeSource, LayeredVolumeScopeSource),
            )
            or scope.source.volume_name != TRIAL_SCOPE
        ):
            raise RealmIntegrityError("Managed trial scope semantics are invalid.")
        root = binding.scope_paths.get(TRIAL_SCOPE)
        if root is None or binding.workdir != root:
            raise RealmIntegrityError(
                "Managed attempt workdir differs from its trial scope root."
            )
        quota = trial_requirements[0].quota
        return root, _CaptureBudget(
            entries=min(self._seal_limits.max_entries, quota.max_entries),
            bytes=min(self._seal_limits.max_total_bytes, quota.max_total_bytes),
        )

    def _remaining_limits(self, budget: _CaptureBudget) -> SealLimits:
        if budget.entries <= 0 or budget.bytes <= 0:
            raise RealmConflict("Declared artifact capture exceeds its bounded budget.")
        return SealLimits(
            max_entries=budget.entries,
            max_depth=self._seal_limits.max_depth,
            max_total_bytes=budget.bytes,
            max_file_bytes=min(self._seal_limits.max_file_bytes, budget.bytes),
            max_path_bytes=self._seal_limits.max_path_bytes,
            max_component_bytes=self._seal_limits.max_component_bytes,
        )

    @staticmethod
    def _rollback_blob_staging(
        *,
        capture: LocalContentCapture,
        change_id: str,
        staging_id: str,
        phase: str,
    ) -> None:
        try:
            capture.authority.rollback_capture(
                change_id=change_id,
                staging_ids=(staging_id,),
            )
        except Exception:
            raise RealmIntegrityError(
                f"Blob {phase} left a staging hold that could not be rolled back."
            ) from None

    def _operation_coordinate(
        self,
        *,
        change_id: str,
        envelope: AttemptEnvelope,
        declaration: OutputDeclaration,
        membership_role: str,
    ) -> dict[str, object]:
        return {
            "attempt_id": envelope.attempt_id,
            "binding_id": envelope.binding_id,
            "change_id": change_id,
            "declaration": declaration.to_dict(),
            "envelope_digest": envelope.digest,
            "format": _CAPTURE_METADATA_FORMAT,
            "membership_role": membership_role,
            "store_id": self._store_id,
        }

    def _tree_capture_operation_id(
        self,
        *,
        change_id: str,
        envelope: AttemptEnvelope,
        declaration: OutputDeclaration,
        membership_role: str,
    ) -> str:
        digest = request_digest(
            {
                **self._operation_coordinate(
                    change_id=change_id,
                    envelope=envelope,
                    declaration=declaration,
                    membership_role=membership_role,
                ),
                "phase": "tree-capture",
            }
        )
        return f"run-attempt-artifact/tree/{digest}"

    def _hold_operation_id(
        self,
        *,
        change_id: str,
        envelope: AttemptEnvelope,
        declaration: OutputDeclaration,
        membership_role: str,
    ) -> str:
        digest = request_digest(
            {
                **self._operation_coordinate(
                    change_id=change_id,
                    envelope=envelope,
                    declaration=declaration,
                    membership_role=membership_role,
                ),
                # hold_owner_content authenticates the actor in its request;
                # distinct authorized recovery actors therefore need distinct
                # idempotency coordinates.
                "actor_principal_id": self._actor_principal_id,
                "phase": "owner-hold",
            }
        )
        return f"run-attempt-artifact/hold/{digest}"

    @staticmethod
    def _effective_code(envelope: AttemptEnvelope) -> str | None:
        if envelope.outcome == "success":
            return None
        candidate = envelope.error.get("code")
        if isinstance(candidate, str) and candidate:
            return candidate
        return f"attempt_{envelope.outcome}"


__all__ = [
    "DeclaredOutputRuntimeBinding",
    "OPERATOR_JOB_OUTPUT_ROLE",
    "RealmAttemptFinalizationError",
    "RealmAttemptFinalizer",
]
