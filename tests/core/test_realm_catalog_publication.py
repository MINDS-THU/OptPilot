from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from optpilot.realm.catalog_publication import CATALOG_PACKAGE_ROOT_ROLE
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


class RealmCatalogPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.realm_root = (self.root / "runtime").resolve()
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"catalog-test/{self.counter}/{label}"

    def create_artifact(
        self,
        owner_id: str,
        files: dict[str, str],
    ) -> tuple[OwnerMembership, int]:
        source = self.root / f"source-{owner_id}"
        source.mkdir()
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.runtime.ledger.create_owner(
            operation_id=self.op(f"create-{owner_id}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id="operator",
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self.op(f"begin-{owner_id}"),
            actor_principal_id="operator",
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        capture = self.runtime.content_service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.runtime.content_store.store_id,
        )
        sealed = capture.seal_tree(
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
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(membership,),
        )
        commit = self.runtime.ledger.commit_owner_change(
            operation_id=self.op(f"commit-{owner_id}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return membership, commit.owner_revision

    def publish(
        self,
        *,
        operation_id: str,
        publisher_id: str,
        source_owner_id: str,
        source: OwnerMembership,
        source_revision: int,
        owned_paths: tuple[str, ...],
        expected_head=None,
        suffix: str = "a",
    ):
        return self.runtime.catalog.publish(
            operation_id=operation_id,
            package_id="local_package",
            publisher_id=publisher_id,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_revision,
            source_store_id=source.store_id,
            source_role=source.role,
            root_ref=source.content_ref,
            owned_paths=owned_paths,
            plan_digest=request_digest({"plan": suffix}),
            validation_digest=request_digest({"validation": suffix}),
            smoke_digest=request_digest({"smoke": suffix}),
            expected_head=expected_head,
        )

    def content_object_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            return {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT kind, COUNT(*) FROM content_objects "
                    "GROUP BY kind ORDER BY kind"
                )
            }

    def tree_paths(self, membership_or_ref) -> tuple[str, ...]:
        content_ref = getattr(membership_or_ref, "content_ref", membership_or_ref)
        manifest = self.runtime.content_store.verify_tree(content_ref)
        return tuple(entry.path for entry in manifest.entries)

    def tree_blob_refs(self, membership_or_ref) -> frozenset[str]:
        content_ref = getattr(membership_or_ref, "content_ref", membership_or_ref)
        manifest = self.runtime.content_store.verify_tree(content_ref)
        return frozenset(
            str(entry.blob_ref)
            for entry in manifest.entries
            if entry.kind == "file"
        )

    def test_first_publication_reuses_exact_artifact_root_and_survives_restart(
        self,
    ) -> None:
        artifact, revision = self.create_artifact(
            "artifact-a",
            {"resources/tool/README.md": "tool\n"},
        )
        before_objects = self.content_object_counts()
        before_refs = tuple(self.runtime.content_store.iter_live_refs())

        receipt = self.publish(
            operation_id="catalog/publish/artifact-a",
            publisher_id="workspace-a/plan-a",
            source_owner_id="artifact-a",
            source=artifact,
            source_revision=revision,
            owned_paths=("resources/tool",),
        )

        self.assertEqual(receipt.head.revision, 1)
        self.assertEqual(receipt.manifest.root_ref, artifact.content_ref)
        self.assertEqual(
            receipt.manifest.applications[0].artifact_ref, artifact.content_ref
        )
        self.assertEqual(self.content_object_counts(), before_objects)
        self.assertEqual(
            tuple(self.runtime.content_store.iter_live_refs()), before_refs
        )
        self.assertEqual(
            self.runtime.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=receipt.owner.owner_id,
            ),
            (
                OwnerMembership(
                    artifact.store_id,
                    artifact.content_ref,
                    CATALOG_PACKAGE_ROOT_ROLE,
                ),
            ),
        )

        self.runtime.close()
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="operator",
        )
        self.assertEqual(
            self.runtime.catalog.read_revision(package_id="local_package"),
            receipt.manifest,
        )
        self.assertEqual(
            self.runtime.catalog.read_head(package_id="local_package"), receipt.head
        )
        self.assertEqual(
            self.tree_paths(receipt.manifest.root_ref),
            ("resources", "resources/tool", "resources/tool/README.md"),
        )

    def test_later_revision_publishes_only_one_tree_and_preserves_history(
        self,
    ) -> None:
        first_artifact, first_revision = self.create_artifact(
            "artifact-first",
            {"resources/first/README.md": "first\n"},
        )
        first = self.publish(
            operation_id="catalog/publish/first",
            publisher_id="publisher-first",
            source_owner_id="artifact-first",
            source=first_artifact,
            source_revision=first_revision,
            owned_paths=("resources/first",),
            suffix="first",
        )
        second_artifact, second_revision = self.create_artifact(
            "artifact-second",
            {"resources/second/README.md": "second\n"},
        )
        before_counts = self.content_object_counts()
        before_refs = set(self.runtime.content_store.iter_live_refs())

        with mock.patch.object(
            type(self.runtime.content_store),
            "_publish_blob_from_fd",
            side_effect=AssertionError("catalog composition must not publish blobs"),
        ):
            second = self.publish(
                operation_id="catalog/publish/second",
                publisher_id="publisher-second",
                source_owner_id="artifact-second",
                source=second_artifact,
                source_revision=second_revision,
                owned_paths=("resources/second",),
                expected_head=first.head,
                suffix="second",
            )

        after_counts = self.content_object_counts()
        self.assertEqual(after_counts.get("blob", 0), before_counts.get("blob", 0))
        self.assertEqual(after_counts.get("tree", 0), before_counts.get("tree", 0) + 1)
        self.assertEqual(
            set(self.runtime.content_store.iter_live_refs()) - before_refs,
            {second.manifest.root_ref},
        )
        self.assertEqual(second.head.revision, 2)
        self.assertEqual(
            tuple(item.publisher_id for item in second.manifest.applications),
            ("publisher-first", "publisher-second"),
        )
        self.assertEqual(
            self.tree_paths(second.manifest.root_ref),
            (
                "resources",
                "resources/first",
                "resources/first/README.md",
                "resources/second",
                "resources/second/README.md",
            ),
        )
        self.assertEqual(
            self.tree_blob_refs(second.manifest.root_ref),
            self.tree_blob_refs(first_artifact) | self.tree_blob_refs(second_artifact),
        )
        self.assertEqual(
            self.runtime.catalog.read_revision(
                package_id="local_package", revision=1
            ),
            first.manifest,
        )
        self.assertEqual(
            self.tree_paths(first.manifest.root_ref),
            ("resources", "resources/first", "resources/first/README.md"),
        )
        self.assertEqual(
            self.runtime.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id=second.owner.owner_id,
            ),
            (
                OwnerMembership(
                    self.runtime.content_store.store_id,
                    second.manifest.root_ref,
                    CATALOG_PACKAGE_ROOT_ROLE,
                ),
            ),
        )

    def test_republishing_one_publisher_replaces_its_claimed_subtree(self) -> None:
        original_artifact, original_revision = self.create_artifact(
            "artifact-replace-original",
            {
                "resources/tool/keep.txt": "old\n",
                "resources/tool/stale.txt": "stale\n",
            },
        )
        original = self.publish(
            operation_id="catalog/publish/replace-original",
            publisher_id="publisher-tool",
            source_owner_id="artifact-replace-original",
            source=original_artifact,
            source_revision=original_revision,
            owned_paths=("resources/tool",),
            suffix="original",
        )
        neighbor_artifact, neighbor_revision = self.create_artifact(
            "artifact-replace-neighbor",
            {"resources/neighbor/value.txt": "neighbor\n"},
        )
        neighbor = self.publish(
            operation_id="catalog/publish/replace-neighbor",
            publisher_id="publisher-neighbor",
            source_owner_id="artifact-replace-neighbor",
            source=neighbor_artifact,
            source_revision=neighbor_revision,
            owned_paths=("resources/neighbor",),
            expected_head=original.head,
            suffix="neighbor",
        )
        replacement_artifact, replacement_revision = self.create_artifact(
            "artifact-replace-new",
            {"resources/tool/fresh.txt": "fresh\n"},
        )

        replacement = self.publish(
            operation_id="catalog/publish/replace-new",
            publisher_id="publisher-tool",
            source_owner_id="artifact-replace-new",
            source=replacement_artifact,
            source_revision=replacement_revision,
            owned_paths=("resources/tool",),
            expected_head=neighbor.head,
            suffix="replacement",
        )

        self.assertEqual(replacement.head.revision, 3)
        self.assertEqual(
            self.tree_paths(replacement.manifest.root_ref),
            (
                "resources",
                "resources/neighbor",
                "resources/neighbor/value.txt",
                "resources/tool",
                "resources/tool/fresh.txt",
            ),
        )
        self.assertEqual(
            replacement.manifest.application("publisher-tool").artifact_ref,
            replacement_artifact.content_ref,
        )
        self.assertEqual(
            self.tree_paths(neighbor.manifest.root_ref),
            (
                "resources",
                "resources/neighbor",
                "resources/neighbor/value.txt",
                "resources/tool",
                "resources/tool/keep.txt",
                "resources/tool/stale.txt",
            ),
        )

    def test_invalid_claims_do_not_advance_the_head_or_publish_compositions(
        self,
    ) -> None:
        base_artifact, base_revision = self.create_artifact(
            "artifact-invalid-base",
            {"resources/shared/README.md": "base\n"},
        )
        base = self.publish(
            operation_id="catalog/publish/invalid-base",
            publisher_id="publisher-base",
            source_owner_id="artifact-invalid-base",
            source=base_artifact,
            source_revision=base_revision,
            owned_paths=("resources/shared",),
        )
        collision_artifact, collision_revision = self.create_artifact(
            "artifact-invalid-collision",
            {"Resources/Shared/child.txt": "collision\n"},
        )
        missing_artifact, missing_revision = self.create_artifact(
            "artifact-invalid-missing",
            {"resources/actual/child.txt": "actual\n"},
        )
        unclaimed_artifact, unclaimed_revision = self.create_artifact(
            "artifact-invalid-unclaimed",
            {
                "resources/valid/child.txt": "valid\n",
                "extras/not-owned.txt": "extra\n",
            },
        )
        before_counts = self.content_object_counts()

        with self.assertRaisesRegex(ValueError, "overlapping path claims"):
            self.publish(
                operation_id="catalog/publish/invalid-collision",
                publisher_id="publisher-collision",
                source_owner_id="artifact-invalid-collision",
                source=collision_artifact,
                source_revision=collision_revision,
                owned_paths=("Resources/Shared",),
                expected_head=base.head,
            )
        with self.assertRaisesRegex(ValueError, "paths absent"):
            self.publish(
                operation_id="catalog/publish/invalid-missing",
                publisher_id="publisher-missing",
                source_owner_id="artifact-invalid-missing",
                source=missing_artifact,
                source_revision=missing_revision,
                owned_paths=("resources/missing",),
                expected_head=base.head,
            )
        with self.assertRaisesRegex(ValueError, "outside its ownership claims"):
            self.publish(
                operation_id="catalog/publish/invalid-unclaimed",
                publisher_id="publisher-unclaimed",
                source_owner_id="artifact-invalid-unclaimed",
                source=unclaimed_artifact,
                source_revision=unclaimed_revision,
                owned_paths=("resources/valid",),
                expected_head=base.head,
            )
        with self.assertRaisesRegex(ValueError, "owned paths overlap"):
            self.publish(
                operation_id="catalog/publish/invalid-self-overlap",
                publisher_id="publisher-overlap",
                source_owner_id="artifact-invalid-missing",
                source=missing_artifact,
                source_revision=missing_revision,
                owned_paths=("resources/actual", "resources/actual/child.txt"),
                expected_head=base.head,
            )

        self.assertEqual(
            self.runtime.catalog.read_head(package_id="local_package"), base.head
        )
        self.assertEqual(self.content_object_counts(), before_counts)

    def test_concurrent_expected_head_allows_one_winner_and_exact_replay(self) -> None:
        base_artifact, base_revision = self.create_artifact(
            "artifact-race-base",
            {"resources/base/README.md": "base\n"},
        )
        base = self.publish(
            operation_id="catalog/publish/race-base",
            publisher_id="publisher-base",
            source_owner_id="artifact-race-base",
            source=base_artifact,
            source_revision=base_revision,
            owned_paths=("resources/base",),
        )
        left_artifact, left_revision = self.create_artifact(
            "artifact-race-left",
            {"resources/left/README.md": "left\n"},
        )
        right_artifact, right_revision = self.create_artifact(
            "artifact-race-right",
            {"resources/right/README.md": "right\n"},
        )

        def contender(label, artifact, revision):
            try:
                return self.publish(
                    operation_id=f"catalog/publish/race-{label}",
                    publisher_id=f"publisher-{label}",
                    source_owner_id=f"artifact-race-{label}",
                    source=artifact,
                    source_revision=revision,
                    owned_paths=(f"resources/{label}",),
                    expected_head=base.head,
                    suffix=label,
                )
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    lambda args: contender(*args),
                    (
                        ("left", left_artifact, left_revision),
                        ("right", right_artifact, right_revision),
                    ),
                )
            )
        receipts = tuple(item for item in outcomes if not isinstance(item, Exception))
        conflicts = tuple(item for item in outcomes if isinstance(item, RealmConflict))
        self.assertEqual(len(receipts), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        winner = receipts[0]
        self.assertEqual(
            self.runtime.catalog.read_head(package_id="local_package"), winner.head
        )

        winner_label = next(
            label
            for label in ("left", "right")
            if f"publisher-{label}"
            in {item.publisher_id for item in winner.manifest.applications}
        )
        winner_artifact = left_artifact if winner_label == "left" else right_artifact
        winner_revision = left_revision if winner_label == "left" else right_revision
        replay = self.publish(
            operation_id=f"catalog/publish/race-{winner_label}",
            publisher_id=f"publisher-{winner_label}",
            source_owner_id=f"artifact-race-{winner_label}",
            source=winner_artifact,
            source_revision=winner_revision,
            owned_paths=(f"resources/{winner_label}",),
            expected_head=base.head,
            suffix=winner_label,
        )
        self.assertEqual(replay, winner)

    def test_generic_open_and_keep_use_full_current_or_historical_root(self) -> None:
        first_artifact, first_revision = self.create_artifact(
            "artifact-selection-first",
            {"resources/first/value.txt": "first\n"},
        )
        first = self.publish(
            operation_id="catalog/publish/selection-first",
            publisher_id="publisher-first",
            source_owner_id="artifact-selection-first",
            source=first_artifact,
            source_revision=first_revision,
            owned_paths=("resources/first",),
            suffix="first",
        )
        second_artifact, second_revision = self.create_artifact(
            "artifact-selection-second",
            {"resources/second/value.txt": "second\n"},
        )
        second = self.publish(
            operation_id="catalog/publish/selection-second",
            publisher_id="publisher-second",
            source_owner_id="artifact-selection-second",
            source=second_artifact,
            source_revision=second_revision,
            owned_paths=("resources/second",),
            expected_head=first.head,
            suffix="second",
        )

        current_selection = (
            self.runtime.ledger.mint_catalog_package_application_selection(
                actor_principal_id="operator",
                package_id="local_package",
                publisher_id="publisher-first",
            )
        )
        historical_selection = (
            self.runtime.ledger.mint_catalog_package_application_selection(
                actor_principal_id="operator",
                package_id="local_package",
                publisher_id="publisher-first",
                revision=1,
            )
        )
        current_open = self.runtime.selection_actions.open_read_only(
            selection=current_selection
        )
        historical_open = self.runtime.selection_actions.open_read_only(
            selection=historical_selection
        )
        self.assertTrue(current_open.eligibility.eligible)
        self.assertEqual(current_open.view.root_ref, second.manifest.root_ref)
        self.assertTrue(historical_open.eligibility.eligible)
        self.assertEqual(historical_open.view.root_ref, first.manifest.root_ref)
        self.assertEqual(current_selection.entity_ref, str(second.manifest.root_ref))
        self.assertEqual(historical_selection.entity_ref, str(first.manifest.root_ref))

        before_counts = self.content_object_counts()
        before_refs = tuple(self.runtime.content_store.iter_live_refs())
        kept = self.runtime.selection_actions.keep_as_editable_workspace(
            operation_id="catalog/selection/keep-current",
            selection=current_selection,
            title="Kept catalog package",
            workspace_id="catalog-kept-workspace",
            owner_id="catalog-kept-workspace-owner",
        )

        self.assertTrue(kept.eligibility.eligible)
        self.assertEqual(kept.workspace.revision.root_ref, second.manifest.root_ref)
        self.assertEqual(self.content_object_counts(), before_counts)
        self.assertEqual(
            tuple(self.runtime.content_store.iter_live_refs()), before_refs
        )
        self.assertEqual(
            self.runtime.ledger.list_owner_memberships(
                actor_principal_id="operator",
                owner_id="catalog-kept-workspace-owner",
            ),
            (
                OwnerMembership(
                    self.runtime.content_store.store_id,
                    second.manifest.root_ref,
                    WORKSPACE_REVISION_ROLE,
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
