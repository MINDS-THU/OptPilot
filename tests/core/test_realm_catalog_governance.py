"""Adversarial tests for stable catalog package governance.

Catalog package identity and authority belong to one stable governance owner.
The owner of an immutable package revision is only a content/provenance anchor:
it must never become the moving ACL surface for the package.  These tests keep
that distinction explicit and exercise the public APIs that must enforce it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot.realm.catalog_publication import (
    CATALOG_PACKAGE_GOVERNANCE_OWNER_KIND,
    CATALOG_PACKAGE_REVISION_OWNER_KIND,
)
from optpilot.realm.catalog_service import RealmCatalogPublicationService
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership, OwnerPermission, OwnerState
from optpilot.realm.refs import request_digest
from optpilot.realm.selection_service import RealmSelectionActionService
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


class RealmCatalogGovernanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=(self.root / "runtime").resolve(),
            actor_principal_id="operator",
        )
        self.counter = 0
        self.principals = {
            "operator": self.runtime.principal,
            "bob": self.runtime.ledger.register_principal(
                operation_id=self.op("register-bob"),
                principal_id="bob",
                kind="user",
            ),
        }
        self.catalogs = {
            principal_id: RealmCatalogPublicationService(
                self.runtime.ledger,
                self.runtime.content_service,
                principal,
                {self.runtime.content_store.store_id: self.runtime.content_store},
            )
            for principal_id, principal in self.principals.items()
        }

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"catalog-governance/{self.counter}/{label}"

    def create_artifact(
        self,
        *,
        owner_id: str,
        actor_principal_id: str,
        claimed_path: str,
        content: str,
    ) -> tuple[OwnerMembership, int]:
        source = self.root / f"source-{owner_id}"
        source.mkdir()
        artifact_path = source / claimed_path / "README.md"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text(content, encoding="utf-8")

        self.runtime.ledger.create_owner(
            operation_id=self.op(f"create-{owner_id}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id=actor_principal_id,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self.op(f"begin-{owner_id}"),
            actor_principal_id=actor_principal_id,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            store_id=self.runtime.content_store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source),
            operation_id=self.op(f"seal-{owner_id}"),
        )
        membership = OwnerMembership(
            self.runtime.content_store.store_id,
            sealed.snapshot_ref,
            PACKAGE_ARTIFACT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self.op(f"hold-{owner_id}"),
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            memberships=(membership,),
        )
        commit = self.runtime.ledger.commit_owner_change(
            operation_id=self.op(f"commit-{owner_id}"),
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return membership, commit.owner_revision

    def publish(
        self,
        *,
        actor_principal_id: str,
        package_id: str,
        publisher_id: str,
        source_owner_id: str,
        source: OwnerMembership,
        source_revision: int,
        claimed_path: str,
        expected_head=None,
    ):
        identity = {
            "package_id": package_id,
            "publisher_id": publisher_id,
            "root_ref": str(source.content_ref),
        }
        return self.catalogs[actor_principal_id].publish(
            operation_id=self.op(f"publish-{package_id}-{publisher_id}"),
            package_id=package_id,
            publisher_id=publisher_id,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            source_store_id=source.store_id,
            source_role=source.role,
            root_ref=source.content_ref,
            owned_paths=(claimed_path,),
            plan_digest=request_digest({"plan": identity}),
            validation_digest=request_digest({"validation": identity}),
            smoke_digest=request_digest({"smoke": identity}),
            expected_head=expected_head,
        )

    def grant(
        self,
        *,
        owner_id: str,
        principal_id: str,
        permission: OwnerPermission,
    ) -> None:
        self.runtime.ledger.grant_owner_permission(
            operation_id=self.op(
                f"grant-{owner_id}-{principal_id}-{permission.value}"
            ),
            actor_principal_id="operator",
            owner_id=owner_id,
            principal_id=principal_id,
            permission=permission,
        )

    def revoke(
        self,
        *,
        owner_id: str,
        principal_id: str,
        permission: OwnerPermission,
    ) -> None:
        self.runtime.ledger.revoke_owner_permission(
            operation_id=self.op(
                f"revoke-{owner_id}-{principal_id}-{permission.value}"
            ),
            actor_principal_id="operator",
            owner_id=owner_id,
            principal_id=principal_id,
            permission=permission,
        )

    def first_publication(self, *, package_id: str = "local_package"):
        claimed_path = f"resources/{package_id}-first"
        artifact, revision = self.create_artifact(
            owner_id=f"artifact-{package_id}-first",
            actor_principal_id="operator",
            claimed_path=claimed_path,
            content=f"{package_id} first\n",
        )
        return self.publish(
            actor_principal_id="operator",
            package_id=package_id,
            publisher_id="publisher-first",
            source_owner_id=f"artifact-{package_id}-first",
            source=artifact,
            source_revision=revision,
            claimed_path=claimed_path,
        )

    def test_package_has_one_distinct_stable_governance_owner(self) -> None:
        first = self.first_publication()

        self.assertEqual(first.package.package_id, "local_package")
        self.assertEqual(
            first.manifest.governance_owner_id,
            first.package.governance_owner_id,
        )
        self.assertNotEqual(
            first.package.governance_owner_id,
            first.owner.owner_id,
        )
        governance = self.runtime.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=first.package.governance_owner_id,
        )
        self.assertEqual(
            governance.owner_kind, CATALOG_PACKAGE_GOVERNANCE_OWNER_KIND
        )
        self.assertEqual(first.owner.owner_kind, CATALOG_PACKAGE_REVISION_OWNER_KIND)
        self.assertEqual(first.owner.revision, 0)

    def test_later_publication_requires_stable_package_admin_not_derive(self) -> None:
        first = self.first_publication()
        governance_owner_id = first.package.governance_owner_id
        for permission in (
            OwnerPermission.METADATA_READ,
            OwnerPermission.BYTES_READ,
            OwnerPermission.DERIVE,
        ):
            self.grant(
                owner_id=governance_owner_id,
                principal_id="bob",
                permission=permission,
            )

        claimed_path = "resources/bob-denied"
        artifact, revision = self.create_artifact(
            owner_id="artifact-bob-denied",
            actor_principal_id="bob",
            claimed_path=claimed_path,
            content="must not publish\n",
        )
        refs_before = tuple(self.runtime.content_store.iter_live_refs())

        with self.assertRaises(RealmNotFound):
            self.publish(
                actor_principal_id="bob",
                package_id="local_package",
                publisher_id="publisher-bob",
                source_owner_id="artifact-bob-denied",
                source=artifact,
                source_revision=revision,
                claimed_path=claimed_path,
                expected_head=first.head,
            )

        self.assertEqual(
            self.runtime.catalog.read_head(package_id="local_package"), first.head
        )
        self.assertEqual(
            tuple(self.runtime.content_store.iter_live_refs()), refs_before
        )

    def test_admin_grant_survives_multiple_head_advances(self) -> None:
        first = self.first_publication()
        governance_owner_id = first.package.governance_owner_id
        self.grant(
            owner_id=governance_owner_id,
            principal_id="bob",
            permission=OwnerPermission.ADMIN,
        )
        governance_before = self.runtime.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=governance_owner_id,
        )

        second_path = "resources/bob-second"
        second_artifact, second_revision = self.create_artifact(
            owner_id="artifact-bob-second",
            actor_principal_id="bob",
            claimed_path=second_path,
            content="second\n",
        )
        second = self.publish(
            actor_principal_id="bob",
            package_id="local_package",
            publisher_id="publisher-bob-second",
            source_owner_id="artifact-bob-second",
            source=second_artifact,
            source_revision=second_revision,
            claimed_path=second_path,
            expected_head=first.head,
        )

        third_path = "resources/bob-third"
        third_artifact, third_revision = self.create_artifact(
            owner_id="artifact-bob-third",
            actor_principal_id="bob",
            claimed_path=third_path,
            content="third\n",
        )
        third = self.publish(
            actor_principal_id="bob",
            package_id="local_package",
            publisher_id="publisher-bob-third",
            source_owner_id="artifact-bob-third",
            source=third_artifact,
            source_revision=third_revision,
            claimed_path=third_path,
            expected_head=second.head,
        )

        self.assertEqual(second.package, first.package)
        self.assertEqual(third.package, first.package)
        self.assertEqual(third.head.revision, 3)
        self.assertEqual(
            self.runtime.ledger.read_owner(
                actor_principal_id="operator",
                owner_id=governance_owner_id,
            ),
            governance_before,
        )

    def test_revoke_invalidates_an_already_minted_historical_selection(self) -> None:
        first = self.first_publication()
        second_path = "resources/operator-second"
        second_artifact, second_revision = self.create_artifact(
            owner_id="artifact-operator-second",
            actor_principal_id="operator",
            claimed_path=second_path,
            content="second\n",
        )
        self.publish(
            actor_principal_id="operator",
            package_id="local_package",
            publisher_id="publisher-second",
            source_owner_id="artifact-operator-second",
            source=second_artifact,
            source_revision=second_revision,
            claimed_path=second_path,
            expected_head=first.head,
        )

        governance_owner_id = first.package.governance_owner_id
        permissions = (
            OwnerPermission.METADATA_READ,
            OwnerPermission.BYTES_READ,
            OwnerPermission.DERIVE,
        )
        for permission in permissions:
            self.grant(
                owner_id=governance_owner_id,
                principal_id="bob",
                permission=permission,
            )
        selection = self.runtime.ledger.mint_catalog_package_application_selection(
            actor_principal_id="bob",
            package_id="local_package",
            publisher_id="publisher-first",
            revision=1,
        )
        bob_actions = RealmSelectionActionService(
            self.runtime.ledger, self.principals["bob"]
        )
        opened = bob_actions.open_read_only(selection=selection)
        self.assertTrue(opened.eligibility.eligible)
        self.assertEqual(opened.view.root_ref, first.manifest.root_ref)

        for permission in permissions:
            self.revoke(
                owner_id=governance_owner_id,
                principal_id="bob",
                permission=permission,
            )

        with self.assertRaises(RealmNotFound):
            bob_actions.open_read_only(selection=selection)
        with self.assertRaises(RealmNotFound):
            self.catalogs["bob"].read_revision(
                package_id="local_package", revision=1
            )

    def test_revision_owner_rejects_every_mutating_owner_surface(self) -> None:
        receipt = self.first_publication()
        revision_owner_id = receipt.owner.owner_id
        self.runtime.ledger.create_owner(
            operation_id=self.op("create-auxiliary-owner"),
            owner_id="auxiliary-owner",
            owner_kind="test-owner",
            principal_id="operator",
        )

        attempts = (
            (
                "grant",
                lambda: self.runtime.ledger.grant_owner_permission(
                    operation_id=self.op("mutate-revision-grant"),
                    actor_principal_id="operator",
                    owner_id=revision_owner_id,
                    principal_id="bob",
                    permission=OwnerPermission.METADATA_READ,
                ),
            ),
            (
                "revoke",
                lambda: self.runtime.ledger.revoke_owner_permission(
                    operation_id=self.op("mutate-revision-revoke"),
                    actor_principal_id="operator",
                    owner_id=revision_owner_id,
                    principal_id="bob",
                    permission=OwnerPermission.METADATA_READ,
                ),
            ),
            (
                "link-as-parent",
                lambda: self.runtime.ledger.link_child_owner(
                    operation_id=self.op("mutate-revision-link-parent"),
                    actor_principal_id="operator",
                    parent_owner_id=revision_owner_id,
                    child_owner_id="auxiliary-owner",
                ),
            ),
            (
                "link-as-child",
                lambda: self.runtime.ledger.link_child_owner(
                    operation_id=self.op("mutate-revision-link-child"),
                    actor_principal_id="operator",
                    parent_owner_id="auxiliary-owner",
                    child_owner_id=revision_owner_id,
                ),
            ),
            (
                "begin-change",
                lambda: self.runtime.ledger.begin_owner_change(
                    operation_id=self.op("mutate-revision-begin-change"),
                    actor_principal_id="operator",
                    owner_id=revision_owner_id,
                    expected_owner_revision=0,
                    ttl_seconds=TEST_LEASE_TTL_SECONDS,
                ),
            ),
        )
        for label, attempt in attempts:
            with self.subTest(label=label), self.assertRaises(RealmConflict):
                attempt()

        owner = self.runtime.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=revision_owner_id,
        )
        self.assertEqual(owner.revision, 0)
        self.assertIs(owner.state, OwnerState.ACTIVE)

    def test_head_listing_is_bounded_sorted_and_filters_by_stable_acl(self) -> None:
        receipts = {
            package_id: self.first_publication(package_id=package_id)
            for package_id in (
                "catalog-alpha",
                "catalog-bravo",
                "catalog-charlie",
            )
        }
        for package_id in ("catalog-alpha", "catalog-charlie"):
            self.grant(
                owner_id=receipts[package_id].package.governance_owner_id,
                principal_id="bob",
                permission=OwnerPermission.METADATA_READ,
            )

        first_page = self.catalogs["bob"].list_heads(
            limit=1, after_package_id=None
        )
        self.assertEqual(
            tuple(head.package_id for head in first_page.heads),
            ("catalog-alpha",),
        )
        self.assertEqual(first_page.next_after_package_id, "catalog-alpha")

        second_page = self.catalogs["bob"].list_heads(
            limit=1,
            after_package_id=first_page.next_after_package_id,
        )
        self.assertEqual(
            tuple(head.package_id for head in second_page.heads),
            ("catalog-charlie",),
        )
        if second_page.next_after_package_id is not None:
            self.assertEqual(
                second_page.next_after_package_id,
                second_page.heads[-1].package_id,
            )

        exhausted = self.catalogs["bob"].list_heads(
            limit=1,
            after_package_id="catalog-charlie",
        )
        self.assertEqual(exhausted.heads, ())
        self.assertIsNone(exhausted.next_after_package_id)

        operator_page = self.runtime.catalog.list_heads(
            limit=2, after_package_id=None
        )
        self.assertEqual(
            tuple(head.package_id for head in operator_page.heads),
            ("catalog-alpha", "catalog-bravo"),
        )
        self.assertEqual(operator_page.next_after_package_id, "catalog-bravo")

        with self.assertRaises(ValueError):
            self.runtime.catalog.list_heads(limit=0, after_package_id=None)
        with self.assertRaises(ValueError):
            self.runtime.catalog.list_heads(limit=201, after_package_id=None)


if __name__ == "__main__":
    unittest.main()
