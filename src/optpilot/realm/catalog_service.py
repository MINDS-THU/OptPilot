"""Actor-bound publication service for immutable Realm catalog packages."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Sequence

from .catalog_publication import (
    CATALOG_PACKAGE_ROOT_ROLE,
    CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE,
    CatalogPackageApplication,
    CatalogPackageHead,
    CatalogPackageHeadPage,
    CatalogPackagePublicationRequest,
    CatalogPackageRecord,
    CatalogPackageRevisionManifest,
    CatalogPackageRevisionReceipt,
    canonical_catalog_paths,
    catalog_application_or_none,
    compose_catalog_package_tree,
    next_catalog_applications,
    validate_catalog_claimed_tree,
)
from .content import LocalContentStore
from .errors import RealmIntegrityError
from .ledger import PrincipalRecord, RealmLedger
from .manifests import TreeManifest
from .owners import OwnerMembership
from .refs import SnapshotRef, request_digest
from .service import RealmContentService, TreeCompositionSource


@dataclass(frozen=True)
class RealmCatalogPublicationService:
    """Publish one canonical package tree without copying immutable blobs."""

    _ledger: RealmLedger
    _content_service: RealmContentService
    _principal: PrincipalRecord
    _local_stores: dict[str, LocalContentStore]

    def __post_init__(self) -> None:
        if not isinstance(self._ledger, RealmLedger):
            raise TypeError("ledger must be a RealmLedger.")
        if not isinstance(self._content_service, RealmContentService):
            raise TypeError("content_service must be a RealmContentService.")
        if not isinstance(self._principal, PrincipalRecord):
            raise TypeError("principal must be a PrincipalRecord.")
        stores = dict(self._local_stores)
        if not stores or any(
            not isinstance(store_id, str)
            or not isinstance(store, LocalContentStore)
            or store.store_id != store_id
            for store_id, store in stores.items()
        ):
            raise TypeError("local stores must map exact ids to LocalContentStore values.")
        object.__setattr__(self, "_local_stores", stores)

    @property
    def principal_id(self) -> str:
        return self._principal.principal_id

    def read_head(self, *, package_id: str) -> CatalogPackageHead | None:
        return self._ledger.read_catalog_package_head(
            actor_principal_id=self.principal_id,
            package_id=package_id,
        )

    def read_package(self, *, package_id: str) -> CatalogPackageRecord:
        return self._ledger.read_catalog_package_record(
            actor_principal_id=self.principal_id,
            package_id=package_id,
        )

    def list_heads(
        self,
        *,
        limit: int = 100,
        after_package_id: str | None = None,
    ) -> CatalogPackageHeadPage:
        return self._ledger.list_catalog_package_heads(
            actor_principal_id=self.principal_id,
            limit=limit,
            after_package_id=after_package_id,
        )

    def read_revision(
        self, *, package_id: str, revision: int | None = None
    ) -> CatalogPackageRevisionManifest:
        return self._ledger.read_catalog_package_revision(
            actor_principal_id=self.principal_id,
            package_id=package_id,
            revision=revision,
        )

    def publish(
        self,
        *,
        operation_id: str,
        package_id: str,
        publisher_id: str,
        source_owner_id: str,
        expected_source_owner_revision: int,
        source_store_id: str,
        source_role: str,
        root_ref: SnapshotRef,
        owned_paths: Sequence[str],
        plan_digest: str,
        validation_digest: str,
        smoke_digest: str | None,
        expected_head: CatalogPackageHead | None,
        revision_owner_id: str | None = None,
    ) -> CatalogPackageRevisionReceipt:
        """Compose and atomically publish the next exact package revision.

        ``root_ref`` is the newly validated application artifact.  Revision one
        adopts it directly.  Later revisions combine its manifest with the
        exact expected package root and publish only the resulting tree
        manifest; the underlying blob objects are reused.
        """

        if not isinstance(root_ref, SnapshotRef):
            raise TypeError("root_ref must be a SnapshotRef.")
        paths = canonical_catalog_paths(tuple(owned_paths))
        expected_revision_owner_id = (
            self._ledger.catalog_publication_revision_owner_id(operation_id)
        )
        if (
            revision_owner_id is not None
            and revision_owner_id != expected_revision_owner_id
        ):
            raise ValueError(
                "revision_owner_id must be the deterministic owner for operation_id."
            )
        publication_request = CatalogPackagePublicationRequest(
            actor_principal_id=self.principal_id,
            package_id=package_id,
            publisher_id=publisher_id,
            artifact_owner_id=source_owner_id,
            artifact_owner_revision=expected_source_owner_revision,
            artifact_store_id=source_store_id,
            artifact_role=source_role,
            artifact_ref=root_ref,
            owned_paths=paths,
            plan_digest=plan_digest,
            validation_digest=validation_digest,
            smoke_digest=smoke_digest,
            expected_head=expected_head,
            revision_owner_id=expected_revision_owner_id,
        )
        recovered = self._ledger.bind_catalog_package_publication_request(
            client_operation_id=operation_id,
            publication_request=publication_request,
        )
        if recovered is not None:
            return recovered

        if expected_head is not None:
            self._ledger.authorize_catalog_package_publication(
                actor_principal_id=self.principal_id,
                package_id=package_id,
                expected_head=expected_head,
            )
        store = self._local_stores.get(source_store_id)
        if store is None:
            raise RealmIntegrityError(
                "Catalog package source store is unavailable to this provider."
            )
        artifact_membership = OwnerMembership(
            source_store_id,
            root_ref,
            source_role,
        )
        artifact_manifest = self._content_service.verify_owner_tree_manifest(
            actor_principal_id=self.principal_id,
            owner_id=source_owner_id,
            expected_owner_revision=expected_source_owner_revision,
            membership=artifact_membership,
        )
        validate_catalog_claimed_tree(
            artifact_manifest,
            claims=paths,
            label="Catalog publication artifact",
        )
        application_anchor = self._ledger.read_owner_source_anchor(
            actor_principal_id=self.principal_id,
            owner_id=source_owner_id,
            revision=expected_source_owner_revision,
        )

        previous_manifest: CatalogPackageRevisionManifest | None = None
        previous_tree: TreeManifest | None = None
        previous_membership: OwnerMembership | None = None
        previous_source_revision: int | None = None
        if expected_head is not None:
            if not isinstance(expected_head, CatalogPackageHead):
                raise TypeError("expected_head must be a CatalogPackageHead or None.")
            if expected_head.package_id != package_id:
                raise ValueError("Expected catalog head belongs to another package.")
            previous_manifest = self.read_revision(
                package_id=package_id,
                revision=expected_head.revision,
            )
            if (
                previous_manifest.owner_id != expected_head.owner_id
                or previous_manifest.digest != expected_head.manifest_digest
            ):
                raise RealmIntegrityError(
                    "Expected catalog head differs from its immutable revision."
                )
            previous_membership = self._catalog_root_membership(previous_manifest)
            previous_source_revision = previous_manifest.owner_revision
            if previous_membership.store_id != source_store_id:
                raise ValueError(
                    "Catalog manifest composition currently requires one content store."
                )
            previous_tree = self._content_service.verify_owner_tree_manifest(
                actor_principal_id=self.principal_id,
                owner_id=previous_manifest.owner_id,
                expected_owner_revision=previous_source_revision,
                membership=previous_membership,
            )
            validate_catalog_claimed_tree(
                previous_tree,
                claims=tuple(
                    path
                    for application in previous_manifest.applications
                    for path in application.owned_paths
                ),
                label="Existing catalog package",
            )

        new_application = CatalogPackageApplication(
            publisher_id=publisher_id,
            origin_revision=(
                1 if previous_manifest is None else previous_manifest.revision + 1
            ),
            source_owner_id=source_owner_id,
            source_owner_revision=expected_source_owner_revision,
            source_owner_manifest_digest=application_anchor.owner_manifest_digest,
            artifact_ref=root_ref,
            owned_paths=paths,
            plan_digest=plan_digest,
            validation_digest=validation_digest,
            smoke_digest=smoke_digest,
        )
        applications = next_catalog_applications(
            previous_manifest=previous_manifest,
            application=new_application,
        )
        package_tree = compose_catalog_package_tree(
            previous_tree=previous_tree,
            previous_application=(
                None
                if previous_manifest is None
                else catalog_application_or_none(previous_manifest, publisher_id)
            ),
            artifact_tree=artifact_manifest,
            applications=applications,
        )

        nonce = uuid.uuid4().hex
        attempt_id = f"catalog-publication-attempt-{nonce}"
        attempt_owner_id = f"catalog-publication-attempt-owner-{nonce}"
        attempt_change_id = f"catalog-publication-attempt-change-{nonce}"
        attempt_operation_id = f"catalog-package.attempt.begin/{nonce}"
        change = self._ledger.begin_catalog_package_publication_attempt(
            operation_id=attempt_operation_id,
            client_operation_id=operation_id,
            publication_request=publication_request,
            attempt_id=attempt_id,
            owner_id=attempt_owner_id,
            change_id=attempt_change_id,
            store_id=source_store_id,
            ttl_seconds=300,
        )
        final_membership = OwnerMembership(
            source_store_id,
            package_tree.snapshot_ref,
            CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE,
        )
        composition_request_digest: str | None = None
        direct_source_owner_id: str | None
        if package_tree.snapshot_ref == root_ref:
            direct_source_owner_id = source_owner_id
        elif (
            previous_manifest is not None
            and previous_membership is not None
            and package_tree.snapshot_ref == previous_manifest.root_ref
        ):
            direct_source_owner_id = previous_manifest.owner_id
        else:
            direct_source_owner_id = None
            composition_sources: list[TreeCompositionSource] = []
            if previous_manifest is not None and previous_membership is not None:
                assert previous_source_revision is not None
                composition_sources.append(
                    TreeCompositionSource(
                        owner_id=previous_manifest.owner_id,
                        owner_revision=previous_source_revision,
                        membership=previous_membership,
                    ),
                )
            composition_sources.append(
                TreeCompositionSource(
                    owner_id=source_owner_id,
                    owner_revision=expected_source_owner_revision,
                    membership=artifact_membership,
                )
            )
            deduplicated_sources = _deduplicate_sources(tuple(composition_sources))
            composition_request = {
                "change_id": change.change_id,
                "manifest_ref": str(package_tree.snapshot_ref),
                "schema": "optpilot.tree-composition-request.v1",
                "sources": [
                    {
                        "membership": item.membership.to_dict(),
                        "owner_id": item.owner_id,
                        "owner_revision": item.owner_revision,
                    }
                    for item in deduplicated_sources
                ],
                "store_id": source_store_id,
            }
            composition_request_digest = request_digest(composition_request)

        try:
            if composition_request_digest is not None:
                sealed = self._content_service.compose_tree(
                    operation_id=f"catalog-package.attempt.compose/{nonce}",
                    actor_principal_id=self.principal_id,
                    change_id=change.change_id,
                    store_id=source_store_id,
                    sources=deduplicated_sources,
                    manifest=package_tree,
                    hold_membership=final_membership,
                )
                if sealed.snapshot_ref != package_tree.snapshot_ref:
                    raise RealmIntegrityError(
                        "Catalog composition published an unexpected tree root."
                    )
            if composition_request_digest is None:
                self._ledger.hold_owner_content(
                    operation_id=f"catalog-package.attempt.hold/{nonce}",
                    actor_principal_id=self.principal_id,
                    change_id=change.change_id,
                    memberships=(final_membership,),
                    source_owner_id=direct_source_owner_id,
                )
            return self._ledger.publish_catalog_package_revision(
                operation_id=operation_id,
                publication_request=publication_request,
                attempt_id=attempt_id,
                artifact_manifest=artifact_manifest,
                previous_tree=previous_tree,
                final_manifest=package_tree,
                composition_request_digest=composition_request_digest,
            )
        except BaseException:
            try:
                self._ledger.abort_catalog_package_publication_attempt(
                    operation_id=(
                        "catalog-package.attempt.abort/" + uuid.uuid4().hex
                    ),
                    client_operation_id=operation_id,
                    publication_request=publication_request,
                    attempt_id=attempt_id,
                )
            except BaseException:
                # Preserve the publication failure; retry begins by retiring any
                # surviving attempt bound to the same semantic request.
                pass
            raise

    def _catalog_root_membership(
        self,
        manifest: CatalogPackageRevisionManifest,
    ) -> OwnerMembership:
        memberships = self._ledger.list_owner_memberships(
            actor_principal_id=self.principal_id,
            owner_id=manifest.owner_id,
        )
        expected = tuple(
            membership
            for membership in memberships
            if membership.role == CATALOG_PACKAGE_ROOT_ROLE
            and membership.content_ref == manifest.root_ref
        )
        if len(memberships) != 1 or len(expected) != 1:
            raise RealmIntegrityError(
                "Catalog revision does not retain exactly one canonical package root."
            )
        return expected[0]

def _deduplicate_sources(
    values: tuple[TreeCompositionSource, ...],
) -> tuple[TreeCompositionSource, ...]:
    result: list[TreeCompositionSource] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


__all__ = ["RealmCatalogPublicationService"]
