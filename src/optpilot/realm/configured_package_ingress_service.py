"""Recoverable configured-package capture, validation, and publication."""

from __future__ import annotations

import time
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .catalog_publication import CatalogPackageHead
from .catalog_service import RealmCatalogPublicationService
from .configured_package_ingress import (
    CONFIGURED_PACKAGE_INGRESS_ARTIFACT_ROLE,
    ConfiguredPackageHeadChanged,
    ConfiguredPackageIngressAttempt,
    ConfiguredPackageIngressInProgress,
    ConfiguredPackageIngressOutcome,
    ConfiguredPackageIngressReceipt,
    ConfiguredPackageIngressRequest,
    ConfiguredPackageOwnershipConflict,
    ConfiguredPackageValidationResult,
    whole_tree_catalog_paths,
)
from .content import AllowedTreeSource, LocalContentStore
from .errors import ContentRejected, RealmConflict, RealmIntegrityError
from .ledger import RealmLedger
from .manifests import SealLimits
from .owners import OwnerMembership
from .projection import ProjectionSpec, TreeMapping
from .projection_service import ManagedReadOnlyProjection, RealmProjectionService
from .refs import request_digest
from .service import RealmContentService


ConfiguredPackageSourceResolver = Callable[[], AllowedTreeSource]
ConfiguredPackageValidator = Callable[[Path], ConfiguredPackageValidationResult]

CONFIGURED_PACKAGE_CAPTURE_LIMITS = SealLimits(
    max_entries=100_000,
    max_depth=64,
    max_total_bytes=20 * 1024**3,
    max_file_bytes=4 * 1024**3,
    max_path_bytes=4096,
    max_component_bytes=255,
)
_FOLLOWER_WAIT_SECONDS = 30.0
_MIN_ACTIVE_ATTEMPT_TTL_SECONDS = 1.0
_COMPLETION_HANDOFF_MARGIN_SECONDS = 5.0


class _IngressLivenessGuard:
    """Keep one fenced ingress attempt and its validation view live."""

    def __init__(
        self,
        *,
        ledger: RealmLedger,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        ttl_seconds: float,
    ) -> None:
        self._ledger = ledger
        self._request = request
        self._attempt = attempt
        self._requested_ttl_seconds = ttl_seconds
        # Sub-second caller slices are useful for crash tests and fast
        # takeover, but too narrow for fair scheduling while the leader is
        # demonstrably alive.  Active renewal uses a conservative lease slice;
        # an exceptional exit shortens it back to the requested takeover time.
        self._ttl_seconds = max(ttl_seconds, _MIN_ACTIVE_ATTEMPT_TTL_SECONDS)
        self._interval = min(self._ttl_seconds / 3, 30.0)
        self._incarnation = uuid.uuid4().hex
        self._counter = 0
        self._projection: ManagedReadOnlyProjection | None = None
        self._failure: BaseException | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active_ttl_seconds(self) -> float:
        return self._ttl_seconds

    def start(self) -> None:
        with self._lock:
            self._heartbeat_locked()
            self._thread = threading.Thread(
                target=self._run,
                name=f"configured-package-heartbeat-{self._incarnation}",
                daemon=True,
            )
            self._thread.start()

    def attach_projection(self, projection: ManagedReadOnlyProjection) -> None:
        if not isinstance(projection, ManagedReadOnlyProjection):
            raise TypeError("projection must be a ManagedReadOnlyProjection.")
        with self._lock:
            self.raise_if_failed()
            projection.heartbeat(
                operation_id=self._next_operation_id("projection"),
                ttl_seconds=min(self._ttl_seconds, 300),
            )
            self._projection = projection

    def detach_projection(self, projection: ManagedReadOnlyProjection) -> None:
        with self._lock:
            if self._projection is projection:
                self._projection = None

    def raise_if_failed(self) -> None:
        with self._lock:
            if self._failure is not None:
                raise self._failure

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def final_refresh_and_stop(self) -> None:
        """Quiesce the background loop, then hand off one fresh fence."""

        self.stop()
        with self._lock:
            if self._failure is not None:
                raise self._failure
            # Completion is one final ledger write after the renewal loop is
            # quiesced.  Its fence must outlive the ledger's own bounded lock
            # wait; otherwise an unrelated writer can make a live caller lose
            # leadership while SQLite is still legitimately waiting.
            self._heartbeat_locked(
                ttl_seconds=max(
                    self._ttl_seconds,
                    self._ledger.busy_timeout_ms / 1000
                    + _COMPLETION_HANDOFF_MARGIN_SECONDS,
                )
            )

    def release_after_failure(self) -> None:
        """Stop renewing and restore the caller's requested takeover delay."""

        self.stop()
        try:
            self._ledger.heartbeat_configured_package_ingress_attempt(
                operation_id=self._next_operation_id("failure-release"),
                request=self._request,
                attempt_id=self._attempt.attempt_id,
                worker_id=self._attempt.worker_id,
                worker_generation=self._attempt.worker_generation,
                ttl_seconds=self._requested_ttl_seconds,
            )
        except Exception:
            # A lost fence or a terminal transition already makes this guard
            # unable—and unnecessary—to influence takeover timing.
            pass

    def _next_operation_id(self, phase: str) -> str:
        self._counter += 1
        return (
            "configured-package-ingress/heartbeat/"
            f"{self._incarnation}/{phase}/{self._counter}"
        )

    def _heartbeat_locked(self, *, ttl_seconds: float | None = None) -> None:
        renewal_ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        self._ledger.heartbeat_configured_package_ingress_attempt(
            operation_id=self._next_operation_id("attempt"),
            request=self._request,
            attempt_id=self._attempt.attempt_id,
            worker_id=self._attempt.worker_id,
            worker_generation=self._attempt.worker_generation,
            ttl_seconds=renewal_ttl,
        )
        if self._projection is not None:
            self._projection.heartbeat(
                operation_id=self._next_operation_id("projection"),
                ttl_seconds=min(renewal_ttl, 300),
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._lock:
                    self._heartbeat_locked()
            except BaseException as error:
                with self._lock:
                    self._failure = error
                self._stop.set()
                return


def configured_package_capture_policy_digest(
    limits: SealLimits,
    *,
    excluded_directory_names: tuple[str, ...] = (),
) -> str:
    """Bind every trusted tree-selection rule used during package capture."""

    limits = _bounded_capture_limits(limits)
    excluded_directory_names = _validated_excluded_directory_names(
        excluded_directory_names
    )
    return request_digest(
        {
            "excluded_directory_names": list(excluded_directory_names),
            "limits": _seal_limits_dict(limits),
            "schema": "optpilot.configured-package-capture-policy.v2",
        }
    )


@dataclass(frozen=True)
class ConfiguredPackageIngressService:
    _ledger: RealmLedger
    _content_service: RealmContentService
    _projection_service: RealmProjectionService
    _catalog: RealmCatalogPublicationService
    _actor_principal_id: str
    _store: LocalContentStore

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(self._projection_service, RealmProjectionService):
            raise TypeError("projection_service must be a RealmProjectionService.")
        if not isinstance(self._catalog, RealmCatalogPublicationService):
            raise TypeError("catalog must be a RealmCatalogPublicationService.")
        if (
            not isinstance(self._actor_principal_id, str)
            or not self._actor_principal_id
        ):
            raise ValueError("actor_principal_id is required.")
        if (
            self._content_service._ledger is not self._ledger
            or self._projection_service.ledger is not self._ledger
            or self._catalog._ledger is not self._ledger
        ):
            raise ValueError(
                "configured package ingress services must share one Realm ledger."
            )
        if self._catalog._content_service is not self._content_service:
            raise ValueError(
                "configured package ingress catalog must share its content service."
            )
        if self._catalog.principal_id != self._actor_principal_id:
            raise ValueError(
                "configured package ingress actor must match its catalog principal."
            )
        if not isinstance(self._store, LocalContentStore):
            raise TypeError("store must be a LocalContentStore.")
        if (
            self._content_service._local_stores.get(self._store.store_id)
            is not self._store
            or self._projection_service._stores.get(self._store.store_id)
            is not self._store
            or self._catalog._local_stores.get(self._store.store_id) is not self._store
        ):
            raise ValueError(
                "configured package ingress store must be attached to every service."
            )

    def publish(
        self,
        *,
        operation_id: str,
        package_id: str,
        source_identity_digest: str,
        validation_policy_digest: str,
        source_resolver: ConfiguredPackageSourceResolver,
        validator: ConfiguredPackageValidator,
        limits: SealLimits | None = None,
        excluded_directory_names: tuple[str, ...] = (),
        attempt_ttl_seconds: float = 300,
    ) -> ConfiguredPackageIngressReceipt:
        """Capture, statically validate, and publish one configured package.

        Binding and completion recovery happen before ``source_resolver`` is
        invoked.  This makes a source path a short-lived provider capability,
        never semantic or durable request data.
        """

        capture_limits = _bounded_capture_limits(
            CONFIGURED_PACKAGE_CAPTURE_LIMITS if limits is None else limits
        )
        expected_excluded_directory_names = _validated_excluded_directory_names(
            excluded_directory_names
        )
        capture_policy_digest = configured_package_capture_policy_digest(
            capture_limits,
            excluded_directory_names=expected_excluded_directory_names,
        )
        request = self._ledger.bind_configured_package_ingress_request(
            operation_id=operation_id,
            actor_principal_id=self._actor_principal_id,
            package_id=package_id,
            source_identity_digest=source_identity_digest,
            capture_policy_digest=capture_policy_digest,
            validation_policy_digest=validation_policy_digest,
        )
        completed = self._ledger.recover_configured_package_ingress(request=request)
        if completed is not None:
            self._cleanup_after_completion(request)
            completed.raise_for_conflict()
            return completed
        if not callable(source_resolver):
            raise TypeError("source_resolver must be callable.")
        if not callable(validator):
            raise TypeError("validator must be callable.")
        if (
            isinstance(attempt_ttl_seconds, bool)
            or not isinstance(attempt_ttl_seconds, (int, float))
            or attempt_ttl_seconds <= 0
            or attempt_ttl_seconds > 3600
        ):
            raise ValueError("attempt_ttl_seconds must be between 0 and 3600.")

        # Reauthorize the currently observed package governance before any
        # mutable source capability can be resolved or opened.
        self._authorized_current_head(request)

        requested_ttl_seconds = float(attempt_ttl_seconds)
        active_ttl_seconds = max(requested_ttl_seconds, _MIN_ACTIVE_ATTEMPT_TTL_SECONDS)
        worker_id = f"configured-package-worker-{uuid.uuid4().hex}"
        try:
            claim = self._claim_attempt(
                request=request,
                worker_id=worker_id,
                ttl_seconds=active_ttl_seconds,
            )
        except RealmConflict:
            completed = self._ledger.recover_configured_package_ingress(request=request)
            if completed is None:
                raise
            self._cleanup_after_completion(request)
            completed.raise_for_conflict()
            return completed
        if not claim.leader:
            # A heartbeating leader may legitimately outlive one lease period.
            # Followers wait for a bounded semantic completion, not merely the
            # current lease slice.
            deadline = time.monotonic() + _FOLLOWER_WAIT_SECONDS
            while time.monotonic() < deadline:
                completed = self._ledger.recover_configured_package_ingress(
                    request=request
                )
                if completed is not None:
                    self._cleanup_after_completion(request)
                    completed.raise_for_conflict()
                    return completed
                current = self._ledger.read_configured_package_ingress_attempt(
                    request=request
                )
                if current is None or current.worker_expires_at <= time.time():
                    try:
                        claim = self._claim_attempt(
                            request=request,
                            worker_id=worker_id,
                            ttl_seconds=active_ttl_seconds,
                        )
                    except RealmConflict:
                        completed = self._ledger.recover_configured_package_ingress(
                            request=request
                        )
                        if completed is None:
                            raise
                        self._cleanup_after_completion(request)
                        completed.raise_for_conflict()
                        return completed
                    if claim.leader:
                        break
                time.sleep(0.01)
            else:
                raise ConfiguredPackageIngressInProgress()

        guard = _IngressLivenessGuard(
            ledger=self._ledger,
            request=request,
            attempt=claim.attempt,
            ttl_seconds=requested_ttl_seconds,
        )
        try:
            guard.start()
            return self._publish_claimed_attempt(
                request=request,
                claim_attempt=claim.attempt,
                source_resolver=source_resolver,
                validator=validator,
                capture_limits=capture_limits,
                expected_excluded_directory_names=expected_excluded_directory_names,
                guard=guard,
            )
        except BaseException:
            guard.release_after_failure()
            raise
        finally:
            guard.stop()

    def _publish_claimed_attempt(
        self,
        *,
        request: ConfiguredPackageIngressRequest,
        claim_attempt: ConfiguredPackageIngressAttempt,
        source_resolver: ConfiguredPackageSourceResolver,
        validator: ConfiguredPackageValidator,
        capture_limits: SealLimits,
        expected_excluded_directory_names: tuple[str, ...],
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        attempt = claim_attempt
        if attempt.state == "active":
            try:
                source = source_resolver()
                guard.raise_if_failed()
                if not isinstance(source, AllowedTreeSource):
                    raise TypeError("source_resolver must return an AllowedTreeSource.")
                if (
                    source.excluded_directory_names
                    != expected_excluded_directory_names
                ):
                    raise RealmIntegrityError(
                        "Configured package source exclusions disagree with the "
                        "bound capture policy."
                    )
                capture = self._content_service.capture(
                    actor_principal_id=self._actor_principal_id,
                    change_id=attempt.change_id,
                    store_id=attempt.store_id,
                )
                sealed = capture.seal_tree(
                    source=source,
                    limits=capture_limits,
                    operation_id=attempt.capture_operation_id,
                )
                guard.raise_if_failed()
                paths = whole_tree_catalog_paths(sealed.manifest)
            except ContentRejected as error:
                code = (
                    "capture.source_changed"
                    if error.__class__.__name__ == "SourceChanged"
                    else "capture.content_rejected"
                )
                return self._finish_rejected_capture(
                    request=request,
                    attempt=attempt,
                    code=code,
                    guard=guard,
                )
            except ValueError as error:
                if "Configured package" not in str(error):
                    raise
                return self._finish_rejected_capture(
                    request=request,
                    attempt=attempt,
                    code="capture.package_tree_invalid",
                    guard=guard,
                )
            membership = OwnerMembership(
                attempt.store_id,
                sealed.snapshot_ref,
                CONFIGURED_PACKAGE_INGRESS_ARTIFACT_ROLE,
            )
            self._ledger.hold_owner_content(
                operation_id=f"configured-package-ingress/hold/{attempt.attempt_id}",
                actor_principal_id=self._actor_principal_id,
                change_id=attempt.change_id,
                memberships=(membership,),
            )
            guard.raise_if_failed()
            attempt = self._ledger.promote_configured_package_ingress_capture(
                operation_id=(
                    f"configured-package-ingress/promote/{attempt.attempt_id}"
                ),
                request=request,
                attempt_id=attempt.attempt_id,
                worker_id=attempt.worker_id,
                worker_generation=attempt.worker_generation,
                source_ref=sealed.snapshot_ref,
                owned_paths=paths,
            )
            guard.raise_if_failed()
        elif attempt.state == "adoptable":
            assert attempt.source_ref is not None
            try:
                manifest = self._store.verify_tree(
                    attempt.source_ref, verify_children=True
                )
                guard.raise_if_failed()
                paths = whole_tree_catalog_paths(manifest)
            except ContentRejected as error:
                code = (
                    "capture.source_changed"
                    if error.__class__.__name__ == "SourceChanged"
                    else "capture.content_rejected"
                )
                return self._finish_rejected_capture(
                    request=request,
                    attempt=attempt,
                    code=code,
                    guard=guard,
                )
            except ValueError as error:
                if "Configured package" not in str(error):
                    raise
                return self._finish_rejected_capture(
                    request=request,
                    attempt=attempt,
                    code="capture.package_tree_invalid",
                    guard=guard,
                )
            attempt = self._ledger.promote_configured_package_ingress_capture(
                operation_id=(
                    f"configured-package-ingress/promote/{attempt.attempt_id}"
                ),
                request=request,
                attempt_id=attempt.attempt_id,
                worker_id=attempt.worker_id,
                worker_generation=attempt.worker_generation,
                source_ref=attempt.source_ref,
                owned_paths=paths,
            )
            guard.raise_if_failed()

        validation = self._ledger.read_configured_package_ingress_validation(
            request=request
        )
        if validation is None:
            if attempt.state != "captured" or attempt.source_ref is None:
                raise RealmIntegrityError(
                    "Configured package attempt lost its captured root."
                )
            projection = self._projection_service.project_read_only(
                operation_id=(
                    "configured-package-ingress/validate/"
                    f"{attempt.attempt_id}/{attempt.worker_generation}"
                ),
                actor_principal_id=self._actor_principal_id,
                store_id=attempt.store_id,
                spec=ProjectionSpec(
                    owner_id=attempt.owner_id,
                    mappings=(TreeMapping(attempt.source_ref),),
                ),
                holder_id=attempt.worker_id,
                ttl_seconds=min(guard.active_ttl_seconds, 300),
                consumer_kind="configured-package-validation",
                consumer_metadata={
                    "request_digest": request.digest,
                    "validation_policy_digest": request.validation_policy_digest,
                },
            )
            try:
                guard.raise_if_failed()
                guard.attach_projection(projection)
                validation = validator(projection.root_path)
                guard.raise_if_failed()
            finally:
                guard.detach_projection(projection)
                projection.close()
            if not isinstance(validation, ConfiguredPackageValidationResult):
                raise TypeError(
                    "validator must return ConfiguredPackageValidationResult."
                )
            validation = self._ledger.record_configured_package_ingress_validation(
                operation_id=(
                    f"configured-package-ingress/validation/{request.digest}"
                ),
                request=request,
                attempt_id=attempt.attempt_id,
                worker_id=attempt.worker_id,
                worker_generation=attempt.worker_generation,
                validation=validation,
            )
            guard.raise_if_failed()
            attempt = self._require_attempt(request, expected_worker=attempt)

        if not validation.accepted:
            receipt = ConfiguredPackageIngressReceipt(
                request_digest=request.digest,
                package_id=request.package_id,
                publisher_id=request.publisher_id,
                outcome=ConfiguredPackageIngressOutcome.REJECTED,
                validation=validation,
                source_ref=attempt.source_ref,
                owned_paths=attempt.owned_paths,
                head=None,
                rejection_stage="validation",
                rejection_code="validation.static_rejected",
            )
            return self._finish(request, attempt, receipt, guard=guard)

        assert attempt.source_ref is not None
        publication_operation_id = (
            f"configured-package-ingress/catalog/{request.digest}"
        )
        if attempt.publication_operation_id is not None:
            if attempt.publication_operation_id != publication_operation_id:
                raise RealmIntegrityError(
                    "Configured package publication operation identity changed."
                )
            # The catalog command is independently replayable.  Recover it
            # before interpreting today's head so a crash after its commit
            # preserves this ingress request's original published outcome.
            return self._finish_catalog_publication(
                request=request,
                attempt=attempt,
                validation=validation,
                guard=guard,
            )

        current_head = self._authorized_current_head(request)
        if current_head != request.expected_head:
            if self._application_matches(request, attempt, current_head):
                return self._finish_success(
                    request,
                    attempt,
                    validation,
                    current_head,
                    ConfiguredPackageIngressOutcome.UNCHANGED,
                    guard=guard,
                )
            return self._finish_conflict(
                request,
                attempt,
                ConfiguredPackageHeadChanged.code,
                guard=guard,
            )
        if self._application_matches(request, attempt, current_head):
            assert current_head is not None
            return self._finish_success(
                request,
                attempt,
                validation,
                current_head,
                ConfiguredPackageIngressOutcome.UNCHANGED,
                guard=guard,
            )

        attempt = self._ledger.begin_configured_package_catalog_publication(
            operation_id=(
                "configured-package-ingress/publication-intent/"
                f"{attempt.attempt_id}/{attempt.worker_generation}"
            ),
            request=request,
            attempt_id=attempt.attempt_id,
            worker_id=attempt.worker_id,
            worker_generation=attempt.worker_generation,
            publication_operation_id=publication_operation_id,
        )
        guard.raise_if_failed()
        return self._finish_catalog_publication(
            request=request,
            attempt=attempt,
            validation=validation,
            guard=guard,
        )

    def reap_cleanup_debt(self, *, limit: int = 100) -> tuple[str, ...]:
        cleaned: list[str] = []
        for digest in self._ledger.list_configured_package_ingress_cleanup_debt(
            limit=limit
        ):
            try:
                if self._ledger.cleanup_configured_package_ingress_artifact(
                    operation_id=(f"configured-package-ingress/cleanup/{digest}"),
                    request_digest_value=digest,
                ):
                    cleaned.append(digest)
            except Exception:
                # Durable debt remains discoverable; one poison item must not
                # hide later cleanup work during bootstrap reconciliation.
                continue
        return tuple(cleaned)

    def _claim_attempt(
        self,
        *,
        request: ConfiguredPackageIngressRequest,
        worker_id: str,
        ttl_seconds: float,
    ):
        nonce = uuid.uuid4().hex
        return self._ledger.begin_configured_package_ingress_attempt(
            operation_id=f"configured-package-ingress/attempt-begin/{nonce}",
            request=request,
            attempt_id=f"configured-package-ingress-attempt-{nonce}",
            owner_id=f"configured-package-ingress-owner-{nonce}",
            change_id=f"configured-package-ingress-change-{nonce}",
            store_id=self._store.store_id,
            capture_operation_id=f"configured-package-ingress/capture/{nonce}",
            worker_id=worker_id,
            ttl_seconds=ttl_seconds,
        )

    def _require_attempt(
        self,
        request: ConfiguredPackageIngressRequest,
        *,
        expected_worker: ConfiguredPackageIngressAttempt,
    ) -> ConfiguredPackageIngressAttempt:
        if not isinstance(expected_worker, ConfiguredPackageIngressAttempt):
            raise TypeError("expected_worker must be a configured package attempt.")
        attempt = self._ledger.read_configured_package_ingress_attempt(request=request)
        if attempt is None:
            raise RealmIntegrityError(
                "Configured package ingress lost its active attempt."
            )
        if (
            attempt.attempt_id != expected_worker.attempt_id
            or attempt.worker_id != expected_worker.worker_id
            or attempt.worker_generation != expected_worker.worker_generation
        ):
            raise RealmConflict(
                "Configured package ingress worker fence changed after validation."
            )
        return attempt

    def _application_matches(
        self,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        head: CatalogPackageHead | None,
    ) -> bool:
        if head is None or attempt.source_ref is None:
            return False
        revision = self._catalog.read_revision(
            package_id=request.package_id, revision=head.revision
        )
        if (
            revision.digest != head.manifest_digest
            or revision.owner_id != head.owner_id
        ):
            raise RealmIntegrityError("Catalog head differs from its revision.")
        try:
            application = revision.application(request.publisher_id)
        except KeyError:
            return False
        return (
            application.artifact_ref == attempt.source_ref
            and application.owned_paths == attempt.owned_paths
        )

    def _finish_catalog_publication(
        self,
        *,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        validation: ConfiguredPackageValidationResult,
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        if attempt.source_ref is None or attempt.publication_operation_id is None:
            raise RealmIntegrityError(
                "Configured package publication intent is incomplete."
            )
        try:
            published = self._catalog.publish(
                operation_id=attempt.publication_operation_id,
                package_id=request.package_id,
                publisher_id=request.publisher_id,
                source_owner_id=attempt.owner_id,
                expected_source_owner_revision=1,
                source_store_id=attempt.store_id,
                source_role=CONFIGURED_PACKAGE_INGRESS_ARTIFACT_ROLE,
                root_ref=attempt.source_ref,
                owned_paths=attempt.owned_paths,
                plan_digest=request_digest(
                    {
                        "capture_policy_digest": request.capture_policy_digest,
                        "request_digest": request.digest,
                        "schema": "optpilot.configured-package-ingress-plan.v1",
                        "source_ref": str(attempt.source_ref),
                    }
                ),
                validation_digest=validation.digest,
                smoke_digest=None,
                expected_head=request.expected_head,
            )
            guard.raise_if_failed()
        except RealmConflict:
            current_head = self._authorized_current_head(request)
            if self._application_matches(request, attempt, current_head):
                assert current_head is not None
                return self._finish_success(
                    request,
                    attempt,
                    validation,
                    current_head,
                    ConfiguredPackageIngressOutcome.UNCHANGED,
                    guard=guard,
                )
            if current_head != request.expected_head:
                return self._finish_conflict(
                    request,
                    attempt,
                    ConfiguredPackageHeadChanged.code,
                    guard=guard,
                )
            raise
        except ValueError as error:
            if "overlap" not in str(error).lower():
                raise
            return self._finish_conflict(
                request,
                attempt,
                ConfiguredPackageOwnershipConflict.code,
                guard=guard,
            )
        return self._finish_success(
            request,
            attempt,
            validation,
            published.head,
            ConfiguredPackageIngressOutcome.PUBLISHED,
            guard=guard,
        )

    def _authorized_current_head(
        self, request: ConfiguredPackageIngressRequest
    ) -> CatalogPackageHead | None:
        head = self._catalog.read_head(package_id=request.package_id)
        if head is not None:
            self._ledger.authorize_catalog_package_publication(
                actor_principal_id=self._actor_principal_id,
                package_id=request.package_id,
                expected_head=head,
            )
        return head

    def _finish_rejected_capture(
        self,
        *,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        code: str,
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.REJECTED,
            validation=None,
            source_ref=None,
            owned_paths=(),
            head=None,
            rejection_stage="capture",
            rejection_code=code,
        )
        return self._finish(request, attempt, receipt, guard=guard)

    def _finish_conflict(
        self,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        code: str,
        *,
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        validation = self._ledger.read_configured_package_ingress_validation(
            request=request
        )
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.CONFLICT,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=None,
            conflict_code=code,
        )
        return self._finish(request, attempt, receipt, guard=guard)

    def _finish_success(
        self,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        validation: ConfiguredPackageValidationResult,
        head: CatalogPackageHead,
        outcome: ConfiguredPackageIngressOutcome,
        *,
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=outcome,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=head,
        )
        return self._finish(request, attempt, receipt, guard=guard)

    def _finish(
        self,
        request: ConfiguredPackageIngressRequest,
        attempt: ConfiguredPackageIngressAttempt,
        receipt: ConfiguredPackageIngressReceipt,
        *,
        guard: _IngressLivenessGuard,
    ) -> ConfiguredPackageIngressReceipt:
        guard.final_refresh_and_stop()
        if receipt.outcome in {
            ConfiguredPackageIngressOutcome.PUBLISHED,
            ConfiguredPackageIngressOutcome.UNCHANGED,
        }:
            # Reauthorize current governance, while retaining the exact
            # historical revision observed or published by this request.
            self._authorized_current_head(request)
        result = self._ledger.complete_configured_package_ingress(
            operation_id=f"configured-package-ingress/complete/{request.digest}",
            request=request,
            receipt=receipt,
            attempt_id=attempt.attempt_id,
            worker_id=attempt.worker_id,
            worker_generation=attempt.worker_generation,
        )
        self._cleanup_after_completion(request)
        result.raise_for_conflict()
        return result

    def _cleanup_after_completion(
        self, request: ConfiguredPackageIngressRequest
    ) -> None:
        try:
            self._ledger.cleanup_configured_package_ingress_artifact(
                operation_id=f"configured-package-ingress/cleanup/{request.digest}",
                request_digest_value=request.digest,
            )
        except Exception:
            # Completion is the semantic boundary.  Cleanup debt remains
            # durable and bootstrap reconciliation retries it independently.
            pass


def _seal_limits_dict(limits: SealLimits) -> dict[str, int]:
    return {
        "max_component_bytes": limits.max_component_bytes,
        "max_depth": limits.max_depth,
        "max_entries": limits.max_entries,
        "max_file_bytes": limits.max_file_bytes,
        "max_path_bytes": limits.max_path_bytes,
        "max_total_bytes": limits.max_total_bytes,
    }


def _validated_excluded_directory_names(
    value: tuple[str, ...],
) -> tuple[str, ...]:
    # AllowedTreeSource owns the canonical, portable basename contract.  Use a
    # path-free placeholder so policy binding and source resolution cannot
    # drift into two subtly different validation rules.
    return AllowedTreeSource(
        Path("."), excluded_directory_names=value
    ).excluded_directory_names


def _bounded_capture_limits(limits: SealLimits) -> SealLimits:
    if not isinstance(limits, SealLimits):
        raise TypeError("limits must be a SealLimits value.")
    for field_name, ceiling in _seal_limits_dict(
        CONFIGURED_PACKAGE_CAPTURE_LIMITS
    ).items():
        if getattr(limits, field_name) > ceiling:
            raise ValueError(
                f"configured package {field_name} exceeds the Core hard ceiling."
            )
    if limits.max_file_bytes > limits.max_total_bytes:
        raise ValueError("max_file_bytes cannot exceed max_total_bytes.")
    return limits


__all__ = [
    "CONFIGURED_PACKAGE_CAPTURE_LIMITS",
    "ConfiguredPackageIngressService",
    "ConfiguredPackageSourceResolver",
    "ConfiguredPackageValidator",
    "configured_package_capture_policy_digest",
]
