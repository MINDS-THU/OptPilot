"""Adversarial recovery and provenance tests for catalog publication.

These tests distinguish the durable semantic command from its disposable
preparation attempts.  An exact command replay must recover its immutable
receipt before consulting mutable sources, while every incomplete retry uses
fresh leased authority and the final transaction independently proves the
only valid package root.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from optpilot.realm.catalog_publication import (
    CATALOG_PUBLICATION_ATTEMPT_OWNER_KIND,
    CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE,
    CatalogPackagePublicationRequest,
)
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict
from optpilot.realm.gc import LocalGcBackend
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import request_digest


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


class LostPublicationResponse(RuntimeError):
    """A committed final transaction whose caller did not receive the receipt."""


class RealmCatalogPublicationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=(self.root / "runtime").resolve(),
            actor_principal_id="operator",
        )
        self.counter = 0

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"catalog-recovery/{self.counter}/{label}"

    def create_artifact(
        self,
        *,
        owner_id: str,
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
            principal_id="operator",
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self.op(f"begin-{owner_id}"),
            actor_principal_id="operator",
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id="operator",
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
        owner_id: str,
        source: OwnerMembership,
        source_revision: int,
        publisher_id: str,
        claimed_path: str,
        expected_head=None,
        digest_label: str | None = None,
    ):
        digest_label = digest_label or publisher_id
        return self.runtime.catalog.publish(
            operation_id=operation_id,
            package_id="local_package",
            publisher_id=publisher_id,
            source_owner_id=owner_id,
            expected_source_owner_revision=source_revision,
            source_store_id=source.store_id,
            source_role=source.role,
            root_ref=source.content_ref,
            owned_paths=(claimed_path,),
            plan_digest=request_digest({"plan": digest_label}),
            validation_digest=request_digest({"validation": digest_label}),
            smoke_digest=request_digest({"smoke": digest_label}),
            expected_head=expected_head,
        )

    def remove_artifact_membership(
        self,
        *,
        owner_id: str,
        owner_revision: int,
        membership: OwnerMembership,
    ) -> int:
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self.op(f"remove-{owner_id}-begin"),
            actor_principal_id="operator",
            owner_id=owner_id,
            expected_owner_revision=owner_revision,
            ttl_seconds=60,
        )
        commit = self.runtime.ledger.commit_owner_change(
            operation_id=self.op(f"remove-{owner_id}-commit"),
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=owner_revision,
            additions=(),
            removals=(membership,),
        )
        return commit.owner_revision

    def begin_typed_attempt(self, label: str):
        claimed_path = f"resources/attempt-{label}"
        source_owner_id = f"artifact-attempt-{label}"
        artifact, artifact_revision = self.create_artifact(
            owner_id=source_owner_id,
            claimed_path=claimed_path,
            content=f"attempt {label}\n",
        )
        client_operation_id = f"catalog/recovery/typed-attempt/{label}"
        publication_request = CatalogPackagePublicationRequest(
            actor_principal_id="operator",
            package_id=f"catalog-attempt-{label}",
            publisher_id=f"publisher-attempt-{label}",
            artifact_owner_id=source_owner_id,
            artifact_owner_revision=artifact_revision,
            artifact_store_id=artifact.store_id,
            artifact_role=artifact.role,
            artifact_ref=artifact.content_ref,
            owned_paths=(claimed_path,),
            plan_digest=request_digest({"plan": label}),
            validation_digest=request_digest({"validation": label}),
            smoke_digest=request_digest({"smoke": label}),
            expected_head=None,
            revision_owner_id=(
                self.runtime.ledger.catalog_publication_revision_owner_id(
                    client_operation_id
                )
            ),
        )
        self.assertIsNone(
            self.runtime.ledger.bind_catalog_package_publication_request(
                client_operation_id=client_operation_id,
                publication_request=publication_request,
            )
        )
        attempt_id = f"typed-attempt-{label}"
        owner_id = f"typed-attempt-owner-{label}"
        change = self.runtime.ledger.begin_catalog_package_publication_attempt(
            operation_id=self.op(f"typed-attempt-begin-{label}"),
            client_operation_id=client_operation_id,
            publication_request=publication_request,
            attempt_id=attempt_id,
            owner_id=owner_id,
            change_id=f"typed-attempt-change-{label}",
            store_id=artifact.store_id,
            ttl_seconds=60,
        )
        final_membership = OwnerMembership(
            artifact.store_id,
            artifact.content_ref,
            CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self.op(f"typed-attempt-hold-{label}"),
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(final_membership,),
            source_owner_id=source_owner_id,
        )
        return (
            client_operation_id,
            publication_request,
            attempt_id,
            owner_id,
            change,
            final_membership,
        )

    @contextmanager
    def mutable_preparation_reads_must_not_run(self):
        message = "exact replay must happen before mutable preparation reads"
        with ExitStack() as stack:
            for target, method_name in (
                (
                    self.runtime.ledger,
                    "authorize_catalog_package_publication",
                ),
                (self.runtime.ledger, "read_catalog_package_revision"),
                (self.runtime.ledger, "read_owner_source_anchor"),
                (self.runtime.ledger, "list_owner_memberships"),
                (
                    self.runtime.content_service,
                    "verify_owner_tree_manifest",
                ),
            ):
                stack.enter_context(
                    mock.patch.object(
                        target,
                        method_name,
                        side_effect=AssertionError(message),
                    )
                )
            yield

    def rows(self, sql: str, parameters=()) -> tuple[sqlite3.Row, ...]:
        connection = sqlite3.connect(self.runtime.ledger.database_path)
        connection.row_factory = sqlite3.Row
        try:
            return tuple(connection.execute(sql, parameters))
        finally:
            connection.close()

    def test_attempt_owner_kind_cannot_be_precreated_generically(self) -> None:
        with self.assertRaisesRegex(RealmConflict, "typed domain command"):
            self.runtime.ledger.create_owner(
                operation_id=self.op("precreate-attempt-owner"),
                owner_id="precreated-catalog-attempt-owner",
                owner_kind=CATALOG_PUBLICATION_ATTEMPT_OWNER_KIND,
                principal_id="operator",
            )

        self.assertEqual(
            self.rows(
                "SELECT 1 FROM owners WHERE owner_id = ?",
                ("precreated-catalog-attempt-owner",),
            ),
            (),
        )

    def test_attempt_owner_rejects_generic_mutation_and_change_surfaces(
        self,
    ) -> None:
        (
            _client_operation_id,
            _publication_request,
            _attempt_id,
            owner_id,
            change,
            final_membership,
        ) = self.begin_typed_attempt("generic-surfaces")
        self.runtime.ledger.register_principal(
            operation_id=self.op("register-attempt-observer"),
            principal_id="attempt-observer",
            kind="user",
        )
        self.runtime.ledger.create_owner(
            operation_id=self.op("create-attempt-auxiliary"),
            owner_id="attempt-auxiliary-owner",
            owner_kind="test-owner",
            principal_id="operator",
        )

        attempts = (
            (
                "grant",
                "domain-managed",
                lambda: self.runtime.ledger.grant_owner_permission(
                    operation_id=self.op("generic-attempt-grant"),
                    actor_principal_id="operator",
                    owner_id=owner_id,
                    principal_id="attempt-observer",
                    permission=OwnerPermission.METADATA_READ,
                ),
            ),
            (
                "link-as-parent",
                "domain-managed",
                lambda: self.runtime.ledger.link_child_owner(
                    operation_id=self.op("generic-attempt-link-parent"),
                    actor_principal_id="operator",
                    parent_owner_id=owner_id,
                    child_owner_id="attempt-auxiliary-owner",
                ),
            ),
            (
                "link-as-child",
                "domain-managed",
                lambda: self.runtime.ledger.link_child_owner(
                    operation_id=self.op("generic-attempt-link-child"),
                    actor_principal_id="operator",
                    parent_owner_id="attempt-auxiliary-owner",
                    child_owner_id=owner_id,
                ),
            ),
            (
                "begin",
                "domain-managed",
                lambda: self.runtime.ledger.begin_owner_change(
                    operation_id=self.op("generic-attempt-begin"),
                    actor_principal_id="operator",
                    owner_id=owner_id,
                    expected_owner_revision=0,
                    ttl_seconds=60,
                ),
            ),
            (
                "commit",
                "typed domain transaction",
                lambda: self.runtime.ledger.commit_owner_change(
                    operation_id=self.op("generic-attempt-commit"),
                    actor_principal_id="operator",
                    change_id=change.change_id,
                    expected_owner_revision=0,
                    additions=(final_membership,),
                ),
            ),
            (
                "abort",
                "typed abort",
                lambda: self.runtime.ledger.abort_owner_change(
                    operation_id=self.op("generic-attempt-abort"),
                    actor_principal_id="operator",
                    change_id=change.change_id,
                ),
            ),
        )
        for label, message, attempt in attempts:
            with self.subTest(label=label), self.assertRaisesRegex(
                RealmConflict, message
            ):
                attempt()

        state = self.rows(
            "SELECT attempt.state AS attempt_state, "
            "owner.state AS owner_state, owner.revision AS owner_revision, "
            "change.state AS change_state, lease.state AS lease_state "
            "FROM catalog_package_publication_attempts attempt "
            "JOIN owners owner ON owner.owner_id = attempt.owner_id "
            "JOIN owner_transactions change ON change.change_id = attempt.change_id "
            "JOIN leases lease ON lease.lease_id = change.retention_lease_id "
            "WHERE attempt.owner_id = ?",
            (owner_id,),
        )[0]
        self.assertEqual(
            (
                state["attempt_state"],
                state["owner_state"],
                state["owner_revision"],
                state["change_state"],
                state["lease_state"],
            ),
            ("active", "active", 0, "active", "active"),
        )

    def test_retry_retires_a_live_orphaned_attempt_owner(self) -> None:
        artifact, revision = self.create_artifact(
            owner_id="artifact-live-retry",
            claimed_path="resources/live-retry",
            content="live retry\n",
        )
        operation_id = "catalog/recovery/live-attempt-retry"
        captured: dict[str, object] = {}

        def interrupt_before_final(**kwargs):
            captured.update(kwargs)
            raise KeyboardInterrupt("process stopped before final publication")

        with mock.patch.object(
            self.runtime.ledger,
            "publish_catalog_package_revision",
            side_effect=interrupt_before_final,
        ), mock.patch.object(
            self.runtime.ledger,
            "abort_catalog_package_publication_attempt",
            side_effect=KeyboardInterrupt("process died before attempt cleanup"),
        ), self.assertRaises(KeyboardInterrupt):
            self.publish(
                operation_id=operation_id,
                owner_id="artifact-live-retry",
                source=artifact,
                source_revision=revision,
                publisher_id="publisher-live-retry",
                claimed_path="resources/live-retry",
            )

        publication_request = captured["publication_request"]
        first_attempt_id = captured["attempt_id"]
        before_retry = self.rows(
            "SELECT attempt.owner_id, attempt.change_id, attempt.state AS attempt_state, "
            "owner.state AS owner_state, owner.revision AS owner_revision, "
            "change.state AS change_state, lease.state AS lease_state "
            "FROM catalog_package_publication_attempts attempt "
            "JOIN owners owner ON owner.owner_id = attempt.owner_id "
            "JOIN owner_transactions change ON change.change_id = attempt.change_id "
            "JOIN leases lease ON lease.lease_id = change.retention_lease_id "
            "WHERE attempt.attempt_id = ?",
            (first_attempt_id,),
        )[0]
        self.assertEqual(
            (
                before_retry["attempt_state"],
                before_retry["owner_state"],
                before_retry["owner_revision"],
                before_retry["change_state"],
                before_retry["lease_state"],
            ),
            ("active", "active", 0, "active", "active"),
        )

        receipt = self.publish(
            operation_id=operation_id,
            owner_id="artifact-live-retry",
            source=artifact,
            source_revision=revision,
            publisher_id="publisher-live-retry",
            claimed_path="resources/live-retry",
        )

        after_retry = self.rows(
            "SELECT attempt.owner_id, attempt.state AS attempt_state, "
            "owner.state AS owner_state, owner.revision AS owner_revision, "
            "change.state AS change_state, lease.state AS lease_state "
            "FROM catalog_package_publication_attempts attempt "
            "JOIN owners owner ON owner.owner_id = attempt.owner_id "
            "JOIN owner_transactions change ON change.change_id = attempt.change_id "
            "JOIN leases lease ON lease.lease_id = change.retention_lease_id "
            "WHERE attempt.request_digest = ? ORDER BY attempt.created_at, "
            "attempt.attempt_id",
            (publication_request.digest,),
        )
        self.assertEqual(len(after_retry), 2)
        old_attempt = next(
            row for row in after_retry if row["owner_id"] == before_retry["owner_id"]
        )
        self.assertEqual(
            (
                old_attempt["attempt_state"],
                old_attempt["owner_state"],
                old_attempt["owner_revision"],
                old_attempt["change_state"],
                old_attempt["lease_state"],
            ),
            ("aborted", "deleted", 1, "aborted", "released"),
        )
        self.assertEqual(
            self.rows(
                "SELECT 1 FROM owner_memberships "
                "WHERE owner_id = ? AND removed_revision IS NULL",
                (old_attempt["owner_id"],),
            ),
            (),
        )
        self.assertEqual(receipt.head.revision, 1)

    def test_exact_replay_recovers_before_stale_head_or_source_reads(self) -> None:
        base_artifact, base_revision = self.create_artifact(
            owner_id="artifact-replay-base",
            claimed_path="resources/base",
            content="base\n",
        )
        base = self.publish(
            operation_id="catalog/recovery/replay-base",
            owner_id="artifact-replay-base",
            source=base_artifact,
            source_revision=base_revision,
            publisher_id="publisher-base",
            claimed_path="resources/base",
        )
        artifact, artifact_revision = self.create_artifact(
            owner_id="artifact-replay-target",
            claimed_path="resources/target",
            content="target\n",
        )
        operation_id = "catalog/recovery/replay-target"
        receipt = self.publish(
            operation_id=operation_id,
            owner_id="artifact-replay-target",
            source=artifact,
            source_revision=artifact_revision,
            publisher_id="publisher-target",
            claimed_path="resources/target",
            expected_head=base.head,
        )

        # The source and package head no longer match the mutable preconditions
        # that were true during the original call.  This must not matter to an
        # exact replay of the already completed semantic command.
        self.remove_artifact_membership(
            owner_id="artifact-replay-target",
            owner_revision=artifact_revision,
            membership=artifact,
        )
        with self.mutable_preparation_reads_must_not_run():
            replay = self.publish(
                operation_id=operation_id,
                owner_id="artifact-replay-target",
                source=artifact,
                source_revision=artifact_revision,
                publisher_id="publisher-target",
                claimed_path="resources/target",
                expected_head=base.head,
            )

        self.assertEqual(replay, receipt)

    def test_same_operation_changed_request_conflicts_before_source_reads(self) -> None:
        artifact, revision = self.create_artifact(
            owner_id="artifact-request-conflict",
            claimed_path="resources/conflict",
            content="original\n",
        )
        operation_id = "catalog/recovery/request-conflict"
        receipt = self.publish(
            operation_id=operation_id,
            owner_id="artifact-request-conflict",
            source=artifact,
            source_revision=revision,
            publisher_id="publisher-conflict",
            claimed_path="resources/conflict",
            digest_label="original",
        )

        with self.mutable_preparation_reads_must_not_run(), self.assertRaises(
            RealmConflict
        ):
            self.publish(
                operation_id=operation_id,
                owner_id="artifact-request-conflict",
                source=artifact,
                source_revision=revision,
                publisher_id="publisher-conflict",
                claimed_path="resources/conflict",
                digest_label="changed",
            )

        self.assertEqual(
            self.runtime.catalog.read_head(package_id="local_package"),
            receipt.head,
        )

    def test_expired_attempt_retries_with_fresh_authority(self) -> None:
        artifact, revision = self.create_artifact(
            owner_id="artifact-expired-attempt",
            claimed_path="resources/expired",
            content="expired retry\n",
        )
        operation_id = "catalog/recovery/expired-attempt"
        captured: dict[str, object] = {}

        def interrupt_before_final(**kwargs):
            captured.update(kwargs)
            raise KeyboardInterrupt("process stopped before final publication")

        with mock.patch.object(
            self.runtime.ledger,
            "publish_catalog_package_revision",
            side_effect=interrupt_before_final,
        ), mock.patch.object(
            self.runtime.ledger,
            "abort_catalog_package_publication_attempt",
            side_effect=KeyboardInterrupt("process died before attempt cleanup"),
        ), self.assertRaises(KeyboardInterrupt):
            self.publish(
                operation_id=operation_id,
                owner_id="artifact-expired-attempt",
                source=artifact,
                source_revision=revision,
                publisher_id="publisher-expired",
                claimed_path="resources/expired",
            )

        publication_request = captured["publication_request"]
        attempts_before = self.rows(
            "SELECT attempt_id, change_id, owner_id, state "
            "FROM catalog_package_publication_attempts "
            "WHERE request_digest = ?",
            (publication_request.digest,),
        )
        self.assertEqual(len(attempts_before), 1)
        first_attempt = attempts_before[0]
        self.assertEqual(first_attempt["state"], "active")

        connection = sqlite3.connect(self.runtime.ledger.database_path)
        try:
            connection.execute(
                "UPDATE owner_transactions SET expires_at = 0 "
                "WHERE change_id = ?",
                (first_attempt["change_id"],),
            )
            connection.execute(
                "UPDATE leases SET expires_at = 0 WHERE lease_id = ("
                "SELECT retention_lease_id FROM owner_transactions "
                "WHERE change_id = ?)",
                (first_attempt["change_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        self.runtime.ledger.sweep_expired_leases(
            operation_id=self.op("sweep-expired-attempt")
        )

        receipt = self.publish(
            operation_id=operation_id,
            owner_id="artifact-expired-attempt",
            source=artifact,
            source_revision=revision,
            publisher_id="publisher-expired",
            claimed_path="resources/expired",
        )

        attempts_after = self.rows(
            "SELECT attempt.attempt_id, attempt.change_id, attempt.owner_id, "
            "attempt.state, change.state AS change_state, "
            "owner.state AS owner_state "
            "FROM catalog_package_publication_attempts attempt "
            "JOIN owner_transactions change ON change.change_id = attempt.change_id "
            "JOIN owners owner ON owner.owner_id = attempt.owner_id "
            "WHERE attempt.request_digest = ? ORDER BY attempt.created_at, "
            "attempt.attempt_id",
            (publication_request.digest,),
        )
        self.assertEqual(len(attempts_after), 2)
        self.assertNotEqual(
            attempts_after[0]["attempt_id"], attempts_after[1]["attempt_id"]
        )
        self.assertEqual(
            {row["state"] for row in attempts_after}, {"aborted", "promoted"}
        )
        self.assertEqual(attempts_after[0]["change_state"], "expired")
        self.assertEqual(
            {row["owner_state"] for row in attempts_after}, {"deleted"}
        )
        self.assertEqual(
            self.rows(
                "SELECT membership.owner_id FROM owner_memberships membership "
                "JOIN catalog_package_publication_attempts attempt "
                "ON attempt.owner_id = membership.owner_id "
                "WHERE attempt.request_digest = ? "
                "AND membership.removed_revision IS NULL",
                (publication_request.digest,),
            ),
            (),
        )
        self.assertEqual(receipt.head.revision, 1)

    def test_lost_final_response_is_recovered_without_a_second_revision(self) -> None:
        artifact, revision = self.create_artifact(
            owner_id="artifact-lost-response",
            claimed_path="resources/lost-response",
            content="committed once\n",
        )
        operation_id = "catalog/recovery/lost-response"
        real_publish = self.runtime.ledger.publish_catalog_package_revision

        def commit_then_drop_response(**kwargs):
            real_publish(**kwargs)
            raise LostPublicationResponse("final receipt was lost")

        with mock.patch.object(
            self.runtime.ledger,
            "publish_catalog_package_revision",
            side_effect=commit_then_drop_response,
        ), self.assertRaises(LostPublicationResponse):
            self.publish(
                operation_id=operation_id,
                owner_id="artifact-lost-response",
                source=artifact,
                source_revision=revision,
                publisher_id="publisher-lost-response",
                claimed_path="resources/lost-response",
            )

        recovered = self.publish(
            operation_id=operation_id,
            owner_id="artifact-lost-response",
            source=artifact,
            source_revision=revision,
            publisher_id="publisher-lost-response",
            claimed_path="resources/lost-response",
        )
        counts = self.rows(
            "SELECT "
            "(SELECT COUNT(*) FROM catalog_package_revisions) AS revisions, "
            "(SELECT COUNT(*) FROM catalog_package_publication_proofs) AS proofs, "
            "(SELECT COUNT(*) FROM catalog_package_publication_completions) "
            "AS completions"
        )[0]
        self.assertEqual(
            (counts["revisions"], counts["proofs"], counts["completions"]),
            (1, 1, 1),
        )
        self.assertEqual(
            recovered.head,
            self.runtime.catalog.read_head(package_id="local_package"),
        )

    def test_final_transaction_rejects_a_source_owned_but_unrelated_root(self) -> None:
        artifact, revision = self.create_artifact(
            owner_id="artifact-launder-request",
            claimed_path="resources/launder",
            content="bound artifact\n",
        )
        operation_id = "catalog/recovery/root-laundering"
        captured: dict[str, object] = {}

        def stop_before_final(**kwargs):
            captured.update(kwargs)
            raise RealmConflict("stop after preparation")

        with mock.patch.object(
            self.runtime.ledger,
            "publish_catalog_package_revision",
            side_effect=stop_before_final,
        ), self.assertRaises(RealmConflict):
            self.publish(
                operation_id=operation_id,
                owner_id="artifact-launder-request",
                source=artifact,
                source_revision=revision,
                publisher_id="publisher-launder",
                claimed_path="resources/launder",
            )

        unrelated, unrelated_revision = self.create_artifact(
            owner_id="artifact-unrelated-root",
            claimed_path="resources/launder",
            content="unrelated bytes\n",
        )
        unrelated_manifest = self.runtime.content_service.verify_owner_tree_manifest(
            actor_principal_id="operator",
            owner_id="artifact-unrelated-root",
            expected_owner_revision=unrelated_revision,
            membership=unrelated,
        )
        publication_request = captured["publication_request"]
        attempt_id = "catalog-laundering-attempt"
        attempt_owner_id = "catalog-laundering-attempt-owner"
        attempt_change_id = "catalog-laundering-attempt-change"
        attempt = self.runtime.ledger.begin_catalog_package_publication_attempt(
            operation_id=self.op("begin-laundering-attempt"),
            client_operation_id=operation_id,
            publication_request=publication_request,
            attempt_id=attempt_id,
            owner_id=attempt_owner_id,
            change_id=attempt_change_id,
            store_id=unrelated.store_id,
            ttl_seconds=60,
        )
        laundered_membership = OwnerMembership(
            unrelated.store_id,
            unrelated.content_ref,
            CATALOG_PUBLICATION_ATTEMPT_ROOT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self.op("hold-unrelated-root"),
            actor_principal_id="operator",
            change_id=attempt.change_id,
            memberships=(laundered_membership,),
            source_owner_id="artifact-unrelated-root",
        )

        with self.assertRaisesRegex(
            RealmConflict, "deterministic package composition"
        ):
            self.runtime.ledger.publish_catalog_package_revision(
                operation_id=operation_id,
                publication_request=publication_request,
                attempt_id=attempt_id,
                artifact_manifest=captured["artifact_manifest"],
                previous_tree=None,
                final_manifest=unrelated_manifest,
            )

        self.assertIsNone(
            self.runtime.catalog.read_head(package_id="local_package")
        )
        self.assertEqual(
            self.rows("SELECT 1 FROM catalog_package_revisions"), ()
        )

    def test_copied_application_survives_artifact_tree_physical_gc(self) -> None:
        first_artifact, first_revision = self.create_artifact(
            owner_id="artifact-copy-first",
            claimed_path="resources/first",
            content="first\n",
        )
        first = self.publish(
            operation_id="catalog/recovery/copy-first",
            owner_id="artifact-copy-first",
            source=first_artifact,
            source_revision=first_revision,
            publisher_id="publisher-first",
            claimed_path="resources/first",
        )
        copied_artifact, copied_revision = self.create_artifact(
            owner_id="artifact-to-be-copied",
            claimed_path="resources/copied",
            content="copied\n",
        )
        second = self.publish(
            operation_id="catalog/recovery/copy-second",
            owner_id="artifact-to-be-copied",
            source=copied_artifact,
            source_revision=copied_revision,
            publisher_id="publisher-copied",
            claimed_path="resources/copied",
            expected_head=first.head,
        )
        copied_application = second.manifest.application("publisher-copied")
        self.assertEqual(copied_application.origin_revision, 2)

        self.remove_artifact_membership(
            owner_id="artifact-to-be-copied",
            owner_revision=copied_revision,
            membership=copied_artifact,
        )
        epoch = self.runtime.ledger.start_gc_epoch(
            operation_id=self.op("copied-artifact-gc-start"),
            store_id=copied_artifact.store_id,
        )
        tombstones = self.runtime.ledger.finish_gc_epoch(
            operation_id=self.op("copied-artifact-gc-finish"),
            store_id=copied_artifact.store_id,
            epoch=epoch.epoch,
            grace_seconds=0,
        )
        self.assertIn(
            copied_artifact.content_ref,
            {item.content_ref for item in tombstones},
        )
        claim = self.runtime.ledger.claim_tombstone(
            operation_id=self.op("copied-artifact-gc-claim"),
            store_id=copied_artifact.store_id,
            content_ref=copied_artifact.content_ref,
        )
        deletion_token = claim.deletion_token or ""
        backend = LocalGcBackend(self.runtime.content_store)
        backend.tombstone(
            copied_artifact.content_ref,
            deletion_token=deletion_token,
            still_eligible=lambda: bool(
                self.runtime.ledger.validate_tombstone_claim(
                    store_id=copied_artifact.store_id,
                    content_ref=copied_artifact.content_ref,
                    deletion_token=deletion_token,
                )
            ),
        )
        backend.delete(
            copied_artifact.content_ref,
            deletion_token=deletion_token,
            still_deletable=lambda: bool(
                self.runtime.ledger.validate_tombstone_claim(
                    store_id=copied_artifact.store_id,
                    content_ref=copied_artifact.content_ref,
                    deletion_token=deletion_token,
                )
            ),
        )
        self.runtime.ledger.complete_tombstone(
            operation_id=self.op("copied-artifact-gc-complete"),
            store_id=copied_artifact.store_id,
            content_ref=copied_artifact.content_ref,
            deletion_token=deletion_token,
        )
        self.assertFalse(
            self.runtime.content_store.has_object(copied_artifact.content_ref)
        )

        third_artifact, third_revision = self.create_artifact(
            owner_id="artifact-copy-third",
            claimed_path="resources/third",
            content="third\n",
        )
        third = self.publish(
            operation_id="catalog/recovery/copy-third",
            owner_id="artifact-copy-third",
            source=third_artifact,
            source_revision=third_revision,
            publisher_id="publisher-third",
            claimed_path="resources/third",
            expected_head=second.head,
        )

        copied_again = third.manifest.application("publisher-copied")
        self.assertEqual(copied_again, copied_application)
        self.assertEqual(copied_again.origin_revision, 2)
        self.assertEqual(
            self.runtime.catalog.read_revision(
                package_id="local_package", revision=2
            ),
            second.manifest,
        )
        self.assertEqual(third.head.revision, 3)


if __name__ == "__main__":
    unittest.main()
