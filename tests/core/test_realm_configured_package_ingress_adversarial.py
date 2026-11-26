"""Adversarial recovery and transition-proof tests for schema-v29 ingress.

These tests intentionally exercise the seams between the configured-package
request, its temporary capture owner, catalog publication (schema v27), and
the v29 terminal receipt.  A failure here must be fixed in Core; the tests must
not manufacture provider paths or weaken an exact historical anchor.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.catalog_service import RealmCatalogPublicationService
from optpilot.realm.configured_package_ingress import (
    ConfiguredPackageIngressOutcome,
    ConfiguredPackageIngressReceipt,
    ConfiguredPackageValidationFact,
    ConfiguredPackageValidationResult,
    configured_package_source_identity_digest,
)
from optpilot.realm.configured_package_ingress_service import (
    CONFIGURED_PACKAGE_CAPTURE_LIMITS,
    ConfiguredPackageIngressService,
    configured_package_capture_policy_digest,
)
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.manifests import TreeManifest
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.projection_service import ManagedReadOnlyProjection
from optpilot.realm.refs import canonical_json_bytes, request_digest


class ConfiguredPackageIngressAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "configured"
        (self.source / "resources" / "viewer").mkdir(parents=True)
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: viewer\n", encoding="utf-8"
        )
        self.runtime = LocalRealmRuntime.open(
            realm_root=(self.root / "realm").resolve(),
            actor_principal_id="operator",
        )
        self.source_digest = configured_package_source_identity_digest(
            "adversarial-configured-source"
        )
        self.validation_policy_digest = request_digest(
            {"policy": "adversarial-static-validation-v1"}
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temporary.cleanup()

    @staticmethod
    def accepted(_root: Path) -> ConfiguredPackageValidationResult:
        return ConfiguredPackageValidationResult(
            True,
            (ConfiguredPackageValidationFact("configs.recognized", "info", 1),),
        )

    def publish(
        self,
        operation_id: str,
        *,
        source_resolver=None,
        excluded_directory_names: tuple[str, ...] = (),
        attempt_ttl_seconds: float = 300,
    ):
        return self.runtime.configured_package_ingress.publish(
            operation_id=operation_id,
            package_id="configured_package",
            source_identity_digest=self.source_digest,
            validation_policy_digest=self.validation_policy_digest,
            source_resolver=(
                source_resolver
                if source_resolver is not None
                else lambda: AllowedTreeSource(self.source)
            ),
            validator=self.accepted,
            excluded_directory_names=excluded_directory_names,
            attempt_ttl_seconds=attempt_ttl_seconds,
        )

    def bind_request(self, operation_id: str, *, actor: str = "operator"):
        return self.runtime.ledger.bind_configured_package_ingress_request(
            operation_id=operation_id,
            actor_principal_id=actor,
            package_id="configured_package",
            source_identity_digest=self.source_digest,
            capture_policy_digest=configured_package_capture_policy_digest(
                CONFIGURED_PACKAGE_CAPTURE_LIMITS
            ),
            validation_policy_digest=self.validation_policy_digest,
        )

    def crash_after_catalog_and_take_over(self, operation_id: str):
        """Return the exact validated attempt whose v27 publish already committed."""

        with mock.patch.object(
            self.runtime.ledger,
            "complete_configured_package_ingress",
            side_effect=RuntimeError("injected crash after catalog publication"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.publish(operation_id, attempt_ttl_seconds=0.3)

        head = self.runtime.catalog.read_head(package_id="configured_package")
        self.assertIsNotNone(head)
        self.assertEqual(head.revision, 1)
        request = self.bind_request(operation_id)
        time.sleep(0.35)
        claim = self.runtime.ledger.begin_configured_package_ingress_attempt(
            operation_id=f"{operation_id}/takeover",
            request=request,
            attempt_id=f"{operation_id}/unused-attempt",
            owner_id=f"{operation_id}/unused-owner",
            change_id=f"{operation_id}/unused-change",
            store_id=self.runtime.content_store.store_id,
            capture_operation_id=f"{operation_id}/unused-capture",
            worker_id=f"{operation_id}/takeover-worker",
            ttl_seconds=30,
        )
        self.assertTrue(claim.leader)
        validation = self.runtime.ledger.read_configured_package_ingress_validation(
            request=request
        )
        self.assertIsNotNone(validation)
        self.assertTrue(validation.accepted)
        return request, claim.attempt, validation, head

    def assert_completion_trigger_rejects(
        self,
        *,
        operation_id: str,
        request,
        attempt,
        receipt: ConfiguredPackageIngressReceipt,
    ) -> None:
        operation_request = {
            "attempt_id": attempt.attempt_id,
            "ingress_receipt": receipt.to_dict(),
            "request_digest": request.digest,
            "worker_generation": attempt.worker_generation,
            "worker_id": attempt.worker_id,
        }

        def insert_with_only_transition_evidence(connection, txn_id, now):
            row = connection.execute(
                "SELECT * FROM configured_package_ingress_attempts "
                "WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()
            self.runtime.ledger._record_configured_package_attempt_transition_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                row=row,
                to_state="completed",
                operation_request=operation_request,
                cleanup_state="pending",
                completed_txn_id=txn_id,
            )
            connection.execute(
                "UPDATE configured_package_ingress_attempts "
                "SET state = 'completed', cleanup_state = 'pending', "
                "completed_txn_id = ?, updated_txn_id = ?, updated_at = ? "
                "WHERE attempt_id = ? AND state = 'validated'",
                (txn_id, txn_id, now, attempt.attempt_id),
            )
            connection.execute(
                "INSERT INTO configured_package_ingress_completions("
                "request_digest, attempt_id, outcome, package_id, revision, "
                "conflict_code, rejection_stage, rejection_code, receipt_json, "
                "final_txn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.digest,
                    attempt.attempt_id,
                    receipt.outcome.value,
                    request.package_id,
                    None if receipt.head is None else receipt.head.revision,
                    receipt.conflict_code,
                    receipt.rejection_stage,
                    receipt.rejection_code,
                    canonical_json_bytes(receipt.to_dict()).decode("utf-8"),
                    txn_id,
                    now,
                ),
            )
            return receipt.to_dict()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "configured package completion requires typed finalize",
        ):
            self.runtime.ledger._operate(
                operation_id=operation_id,
                operation_kind="configured-package-ingress.complete",
                request=operation_request,
                body=insert_with_only_transition_evidence,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            state = connection.execute(
                "SELECT state FROM configured_package_ingress_attempts "
                "WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()[0]
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (request.digest,),
            ).fetchone()[0]
        self.assertEqual(state, "validated")
        self.assertEqual(completion_count, 0)

    def bob_ingress_with_admin(self):
        bob = self.runtime.ledger.register_principal(
            operation_id="configured/adversarial/register-bob",
            principal_id="bob",
            kind="user",
        )
        package = self.runtime.ledger.read_catalog_package_record(
            actor_principal_id="operator", package_id="configured_package"
        )
        self.runtime.ledger.grant_owner_permission(
            operation_id="configured/adversarial/grant-bob-admin",
            actor_principal_id="operator",
            owner_id=package.governance_owner_id,
            principal_id="bob",
            permission=OwnerPermission.ADMIN,
        )
        bob_catalog = RealmCatalogPublicationService(
            self.runtime.ledger,
            self.runtime.content_service,
            bob,
            {self.runtime.content_store.store_id: self.runtime.content_store},
        )
        return (
            ConfiguredPackageIngressService(
                self.runtime.ledger,
                self.runtime.content_service,
                self.runtime.projection_service,
                bob_catalog,
                "bob",
                self.runtime.content_store,
            ),
            package,
        )

    def test_bind_only_request_cannot_forge_request_only_completion(self) -> None:
        request = self.bind_request("configured/adversarial/bind-only")
        forged = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.CONFLICT,
            validation=None,
            source_ref=None,
            owned_paths=(),
            head=None,
            conflict_code="configured_package_head_changed",
        )

        with self.assertRaisesRegex(
            ValueError, "completion requires an exact attempt worker"
        ):
            self.runtime.ledger.complete_configured_package_ingress(
                operation_id="configured/adversarial/forged-completion",
                request=request,
                receipt=forged,
                attempt_id=None,
                worker_id=None,
                worker_generation=None,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (request.digest,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 0)

    def test_bound_exclusions_must_match_resolved_tree_source(self) -> None:
        with mock.patch.object(
            self.runtime.content_service,
            "capture",
            wraps=self.runtime.content_service.capture,
        ) as capture:
            with self.assertRaisesRegex(
                RealmIntegrityError, "source exclusions disagree"
            ):
                self.publish(
                    "configured/adversarial/exclusion-mismatch",
                    excluded_directory_names=("node_modules",),
                    source_resolver=lambda: AllowedTreeSource(self.source),
                )

        capture.assert_not_called()
        self.assertIsNone(
            self.runtime.catalog.read_head(package_id="configured_package")
        )

    def test_operation_replay_cannot_change_bound_exclusions(self) -> None:
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "generated.js").write_text(
            "generated\n", encoding="utf-8"
        )
        names = ("node_modules",)
        first = self.publish(
            "configured/adversarial/exclusion-replay",
            excluded_directory_names=names,
            source_resolver=lambda: AllowedTreeSource(
                self.source, excluded_directory_names=names
            ),
        )
        manifest = self.runtime.content_store.verify_tree(first.source_ref)
        self.assertNotIn(
            "node_modules/generated.js",
            {entry.path for entry in manifest.entries},
        )

        resolver_called = False

        def changed_resolver() -> AllowedTreeSource:
            nonlocal resolver_called
            resolver_called = True
            return AllowedTreeSource(self.source)

        with self.assertRaisesRegex(
            RealmConflict, "already used for another configured package"
        ):
            self.publish(
                "configured/adversarial/exclusion-replay",
                source_resolver=changed_resolver,
            )
        self.assertFalse(resolver_called)

    def test_finalized_attempt_rejects_replayed_historical_transition_proof(
        self,
    ) -> None:
        self.publish("configured/adversarial/historical-transition")
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            connection.row_factory = sqlite3.Row
            historical = connection.execute(
                "SELECT transition.* "
                "FROM configured_package_ingress_attempt_transitions transition "
                "JOIN configured_package_ingress_attempts attempt "
                "ON attempt.attempt_id = transition.attempt_id "
                "WHERE attempt.request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?) "
                "AND transition.to_state = 'captured'",
                ("configured/adversarial/historical-transition",),
            ).fetchone()
            self.assertIsNotNone(historical)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE configured_package_ingress_attempts SET "
                    "state = ?, source_ref = ?, owned_paths_json = ?, "
                    "publication_operation_id = ?, worker_id = ?, "
                    "worker_generation = ?, worker_expires_at = ?, "
                    "cleanup_state = ?, completed_txn_id = ?, cleanup_txn_id = ?, "
                    "updated_txn_id = ?, updated_at = ? WHERE attempt_id = ?",
                    (
                        historical["to_state"],
                        historical["source_ref"],
                        historical["owned_paths_json"],
                        historical["publication_operation_id"],
                        historical["worker_id"],
                        historical["worker_generation"],
                        historical["worker_expires_at"],
                        historical["cleanup_state"],
                        historical["completed_txn_id"],
                        historical["cleanup_txn_id"],
                        historical["txn_id"],
                        historical["updated_at"],
                        historical["attempt_id"],
                    ),
                )
            connection.rollback()

    def test_attempt_rejects_cross_request_transition_proof(self) -> None:
        self.publish("configured/adversarial/cross-proof-one")
        self.publish("configured/adversarial/cross-proof-two")
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            connection.row_factory = sqlite3.Row
            proof = connection.execute(
                "SELECT transition.* "
                "FROM configured_package_ingress_attempt_transitions transition "
                "JOIN configured_package_ingress_attempts attempt "
                "ON attempt.attempt_id = transition.attempt_id "
                "JOIN configured_package_ingress_requests request_record "
                "ON request_record.request_digest = attempt.request_digest "
                "WHERE request_record.client_operation_id = ? "
                "AND transition.to_state = 'captured'",
                ("configured/adversarial/cross-proof-one",),
            ).fetchone()
            target = connection.execute(
                "SELECT attempt_id FROM configured_package_ingress_attempts "
                "WHERE request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?)",
                ("configured/adversarial/cross-proof-two",),
            ).fetchone()
            self.assertIsNotNone(proof)
            self.assertIsNotNone(target)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE configured_package_ingress_attempts SET "
                    "state = ?, source_ref = ?, owned_paths_json = ?, "
                    "publication_operation_id = ?, worker_id = ?, "
                    "worker_generation = ?, worker_expires_at = ?, "
                    "cleanup_state = ?, completed_txn_id = ?, cleanup_txn_id = ?, "
                    "updated_txn_id = ?, updated_at = ? WHERE attempt_id = ?",
                    (
                        proof["to_state"],
                        proof["source_ref"],
                        proof["owned_paths_json"],
                        proof["publication_operation_id"],
                        proof["worker_id"],
                        proof["worker_generation"],
                        proof["worker_expires_at"],
                        proof["cleanup_state"],
                        proof["completed_txn_id"],
                        proof["cleanup_txn_id"],
                        proof["txn_id"],
                        proof["updated_at"],
                        target["attempt_id"],
                    ),
                )
            connection.rollback()

    def test_attempt_rejects_unproved_direct_mutation(self) -> None:
        self.publish("configured/adversarial/direct-mutation")
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE configured_package_ingress_attempts "
                    "SET worker_id = 'forged-worker' WHERE request_digest = ("
                    "SELECT request_digest FROM configured_package_ingress_requests "
                    "WHERE client_operation_id = ?)",
                    ("configured/adversarial/direct-mutation",),
                )
            connection.rollback()

    def test_attempt_begin_rejects_skewed_liveness_tuple(self) -> None:
        request = self.bind_request("configured/adversarial/skewed-liveness")
        attempt_id = "configured-adversarial-skewed-attempt"
        owner_id = "configured-adversarial-skewed-owner"
        change_id = "configured-adversarial-skewed-change"
        capture_operation_id = "configured/adversarial/skewed-capture"
        retention_lease_id = "configured-adversarial-skewed-retention"
        worker_id = "configured-adversarial-skewed-worker"
        ttl_seconds = 5.0
        operation_request = {
            "attempt_id": attempt_id,
            "capture_operation_id": capture_operation_id,
            "change_id": change_id,
            "owner_id": owner_id,
            "request_digest": request.digest,
            "store_id": self.runtime.content_store.store_id,
            "ttl_seconds": ttl_seconds,
            "worker_id": worker_id,
        }

        def insert_skewed_attempt(connection, txn_id, now):
            self.runtime.ledger._create_owner_in_txn(
                connection,
                txn_id=txn_id,
                now=now,
                owner_id=owner_id,
                owner_kind="configured-package-ingress-artifact",
                principal_id=request.actor_principal_id,
            )
            retention = self.runtime.ledger._acquire_lease_in_txn(
                connection,
                lease_id=retention_lease_id,
                owner_id=owner_id,
                parent_lease_id=None,
                lease_kind="owner-change-retention",
                audience="realm-ledger",
                holder_id=request.actor_principal_id,
                scope_key=f"owner-change:{change_id}",
                ttl_seconds=ttl_seconds - 1,
                metadata={"change_id": change_id},
                now=now,
            )
            connection.execute(
                "INSERT INTO owner_transactions("
                "change_id, owner_id, base_owner_revision, retention_lease_id, "
                "state, expires_at, created_at, updated_at"
                ") VALUES (?, ?, 0, ?, 'active', ?, ?, ?)",
                (
                    change_id,
                    owner_id,
                    retention_lease_id,
                    retention.expires_at,
                    now,
                    now,
                ),
            )
            begin_request_json = canonical_json_bytes(
                {
                    "kind": "configured-package-ingress.attempt.begin",
                    "request": operation_request,
                }
            ).decode("utf-8")
            connection.execute(
                "INSERT INTO configured_package_ingress_attempts("
                "attempt_id, request_digest, owner_id, change_id, store_id, "
                "capture_operation_id, begin_operation_request_json, state, "
                "source_ref, owned_paths_json, publication_operation_id, "
                "worker_id, worker_generation, worker_expires_at, cleanup_state, "
                "created_txn_id, completed_txn_id, updated_txn_id, cleanup_txn_id, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, NULL, ?, "
                "1, ?, 'none', ?, NULL, ?, NULL, ?, ?)",
                (
                    attempt_id,
                    request.digest,
                    owner_id,
                    change_id,
                    self.runtime.content_store.store_id,
                    capture_operation_id,
                    begin_request_json,
                    worker_id,
                    now + ttl_seconds,
                    txn_id,
                    txn_id,
                    now,
                    now,
                ),
            )
            return {"unexpected": True}

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "configured package ingress attempt requires typed begin",
        ):
            self.runtime.ledger._operate(
                operation_id="configured/adversarial/skewed-attempt-begin",
                operation_kind="configured-package-ingress.attempt.begin",
                request=operation_request,
                body=insert_skewed_attempt,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_attempts "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            owner_count = connection.execute(
                "SELECT COUNT(*) FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()[0]
        self.assertEqual(attempt_count, 0)
        self.assertEqual(owner_count, 0)

    def test_expired_active_takeover_retires_old_owner_and_fences_worker(
        self,
    ) -> None:
        request = self.bind_request("configured/adversarial/stale-active-worker")
        first = self.runtime.ledger.begin_configured_package_ingress_attempt(
            operation_id="configured/adversarial/stale-active-first",
            request=request,
            attempt_id="configured-adversarial-stale-active-attempt-one",
            owner_id="configured-adversarial-stale-active-owner-one",
            change_id="configured-adversarial-stale-active-change-one",
            store_id=self.runtime.content_store.store_id,
            capture_operation_id="configured/adversarial/stale-active-capture-one",
            worker_id="configured-adversarial-stale-active-worker-one",
            ttl_seconds=0.1,
        )
        self.assertTrue(first.leader)
        time.sleep(0.12)
        second = self.runtime.ledger.begin_configured_package_ingress_attempt(
            operation_id="configured/adversarial/stale-active-second",
            request=request,
            attempt_id="configured-adversarial-stale-active-attempt-two",
            owner_id="configured-adversarial-stale-active-owner-two",
            change_id="configured-adversarial-stale-active-change-two",
            store_id=self.runtime.content_store.store_id,
            capture_operation_id="configured/adversarial/stale-active-capture-two",
            worker_id="configured-adversarial-stale-active-worker-two",
            ttl_seconds=5,
        )
        self.assertTrue(second.leader)
        self.assertNotEqual(second.attempt.attempt_id, first.attempt.attempt_id)
        second_expiry = second.attempt.worker_expires_at

        with self.assertRaisesRegex(RealmConflict, "attempt is not live"):
            self.runtime.ledger.heartbeat_configured_package_ingress_attempt(
                operation_id="configured/adversarial/stale-active-heartbeat",
                request=request,
                attempt_id=first.attempt.attempt_id,
                worker_id=first.attempt.worker_id,
                worker_generation=first.attempt.worker_generation,
                ttl_seconds=5,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            old_attempt = connection.execute(
                "SELECT state, completed_txn_id FROM "
                "configured_package_ingress_attempts WHERE attempt_id = ?",
                (first.attempt.attempt_id,),
            ).fetchone()
            old_owner = connection.execute(
                "SELECT state, revision FROM owners WHERE owner_id = ?",
                (first.attempt.owner_id,),
            ).fetchone()
            new_attempt = connection.execute(
                "SELECT state, worker_id, worker_generation, worker_expires_at "
                "FROM configured_package_ingress_attempts WHERE attempt_id = ?",
                (second.attempt.attempt_id,),
            ).fetchone()
        self.assertEqual(old_attempt[0], "aborted")
        self.assertIsNotNone(old_attempt[1])
        self.assertEqual(old_owner, ("deleted", 1))
        self.assertEqual(
            new_attempt,
            ("active", second.attempt.worker_id, 1, second_expiry),
        )

    def test_finalized_artifact_membership_rejects_historical_addition_replay(
        self,
    ) -> None:
        self.publish("configured/adversarial/historical-membership")
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            connection.row_factory = sqlite3.Row
            membership = connection.execute(
                "SELECT membership.* FROM owner_memberships membership "
                "JOIN configured_package_ingress_attempts attempt "
                "ON attempt.owner_id = membership.owner_id "
                "JOIN configured_package_ingress_requests request_record "
                "ON request_record.request_digest = attempt.request_digest "
                "WHERE request_record.client_operation_id = ?",
                ("configured/adversarial/historical-membership",),
            ).fetchone()
            self.assertIsNotNone(membership)
            self.assertEqual(membership["removed_revision"], 2)
            self.assertIsNotNone(membership["removed_txn_id"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE owner_memberships SET removed_revision = NULL, "
                    "removed_txn_id = NULL WHERE owner_id = ? AND store_id = ? "
                    "AND content_ref = ? AND role = ?",
                    (
                        membership["owner_id"],
                        membership["store_id"],
                        membership["content_ref"],
                        membership["role"],
                    ),
                )
            connection.rollback()

    def test_revoked_current_package_admin_blocks_source_resolution(self) -> None:
        self.publish("configured/adversarial/admin-initial")
        bob_ingress, package = self.bob_ingress_with_admin()
        operation_id = "configured/adversarial/bob-bound-before-revoke"
        self.bind_request(operation_id, actor="bob")
        self.runtime.ledger.revoke_owner_permission(
            operation_id="configured/adversarial/revoke-bob-admin",
            actor_principal_id="operator",
            owner_id=package.governance_owner_id,
            principal_id="bob",
            permission=OwnerPermission.ADMIN,
        )
        resolved = False

        def source_resolver():
            nonlocal resolved
            resolved = True
            return AllowedTreeSource(self.source)

        with self.assertRaises(RealmNotFound):
            bob_ingress.publish(
                operation_id=operation_id,
                package_id="configured_package",
                source_identity_digest=self.source_digest,
                validation_policy_digest=self.validation_policy_digest,
                source_resolver=source_resolver,
                validator=self.accepted,
            )
        self.assertFalse(resolved)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_attempts "
                "WHERE request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?)",
                (operation_id,),
            ).fetchone()[0]
        self.assertEqual(attempt_count, 0)

    def test_ingress_actor_must_match_catalog_principal(self) -> None:
        self.publish("configured/adversarial/principal-match-initial")
        bob_ingress, _package = self.bob_ingress_with_admin()

        with self.assertRaisesRegex(
            ValueError, "ingress actor must match its catalog principal"
        ):
            ConfiguredPackageIngressService(
                self.runtime.ledger,
                self.runtime.content_service,
                self.runtime.projection_service,
                bob_ingress._catalog,
                "operator",
                self.runtime.content_store,
            )

    def test_ingress_services_must_share_one_ledger_and_store(self) -> None:
        other = LocalRealmRuntime.open(
            realm_root=(self.root / "other-realm").resolve(),
            actor_principal_id="operator",
        )
        try:
            with self.assertRaisesRegex(ValueError, "share one Realm ledger"):
                ConfiguredPackageIngressService(
                    self.runtime.ledger,
                    other.content_service,
                    self.runtime.projection_service,
                    self.runtime.catalog,
                    "operator",
                    self.runtime.content_store,
                )
            with self.assertRaisesRegex(
                ValueError, "store must be attached to every service"
            ):
                ConfiguredPackageIngressService(
                    self.runtime.ledger,
                    self.runtime.content_service,
                    self.runtime.projection_service,
                    self.runtime.catalog,
                    "operator",
                    other.content_store,
                )
        finally:
            other.close()

    def test_post_validation_read_cannot_adopt_successor_worker_fence(self) -> None:
        real_read = self.runtime.ledger.read_configured_package_ingress_attempt

        def read_with_successor_fence(*, request):
            current = real_read(request=request)
            self.assertIsNotNone(current)
            self.assertEqual(current.state, "validated")
            return replace(
                current,
                worker_id="configured-adversarial-successor-worker",
                worker_generation=current.worker_generation + 1,
            )

        with mock.patch.object(
            self.runtime.ledger,
            "read_configured_package_ingress_attempt",
            side_effect=read_with_successor_fence,
        ):
            with self.assertRaisesRegex(
                RealmConflict, "worker fence changed after validation"
            ):
                self.publish("configured/adversarial/post-validation-fence-swap")

        self.assertIsNone(
            self.runtime.catalog.read_head(package_id="configured_package")
        )
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempt = connection.execute(
                "SELECT state FROM configured_package_ingress_attempts "
                "WHERE request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?)",
                ("configured/adversarial/post-validation-fence-swap",),
            ).fetchone()
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions"
            ).fetchone()[0]
        self.assertEqual(attempt, ("validated",))
        self.assertEqual(completion_count, 0)

    def test_admin_revoked_after_source_resolution_prevents_success(self) -> None:
        initial = self.publish("configured/adversarial/admin-race-initial")
        self.assertEqual(initial.head.revision, 1)
        bob_ingress, package = self.bob_ingress_with_admin()
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: bob-update\n", encoding="utf-8"
        )
        operation_id = "configured/adversarial/admin-revoked-after-read"
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def source_resolver():
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release mutable source")
            return AllowedTreeSource(self.source)

        def publish_as_bob() -> None:
            try:
                bob_ingress.publish(
                    operation_id=operation_id,
                    package_id="configured_package",
                    source_identity_digest=self.source_digest,
                    validation_policy_digest=self.validation_policy_digest,
                    source_resolver=source_resolver,
                    validator=self.accepted,
                    attempt_ttl_seconds=5,
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=publish_as_bob)
        worker.start()
        self.assertTrue(entered.wait(5))
        self.runtime.ledger.revoke_owner_permission(
            operation_id="configured/adversarial/revoke-bob-admin-after-read",
            actor_principal_id="operator",
            owner_id=package.governance_owner_id,
            principal_id="bob",
            permission=OwnerPermission.ADMIN,
        )
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RealmNotFound)
        current = self.runtime.catalog.read_head(package_id="configured_package")
        self.assertEqual(current, initial.head)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?)",
                (operation_id,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 0)

    def test_catalog_commit_crash_recovers_original_published_revision(
        self,
    ) -> None:
        operation_id = "configured/adversarial/crash-after-catalog"
        with mock.patch.object(
            self.runtime.ledger,
            "complete_configured_package_ingress",
            side_effect=RealmConflict("injected crash after catalog commit"),
        ):
            with self.assertRaisesRegex(RealmConflict, "injected crash"):
                self.publish(operation_id, attempt_ttl_seconds=1)

        first_head = self.runtime.catalog.read_head(package_id="configured_package")
        self.assertIsNotNone(first_head)
        self.assertEqual(first_head.revision, 1)
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: advanced\n", encoding="utf-8"
        )
        advanced = self.publish(
            "configured/adversarial/advance-head",
            attempt_ttl_seconds=1,
        )
        self.assertEqual(advanced.head.revision, 2)
        time.sleep(1.05)
        source_reads = 0

        def forbidden_source_reread():
            nonlocal source_reads
            source_reads += 1
            raise AssertionError("historical recovery reread mutable source")

        recovered = self.publish(
            operation_id,
            source_resolver=forbidden_source_reread,
            attempt_ttl_seconds=1,
        )
        self.assertEqual(recovered.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        self.assertEqual(recovered.head.revision, 1)
        self.assertEqual(source_reads, 0)
        current = self.runtime.catalog.read_head(package_id="configured_package")
        self.assertEqual(current.revision, 2)
        self.assertNotEqual(recovered.head, current)

    def test_catalog_completion_cannot_be_reclassified_as_unchanged(self) -> None:
        request, attempt, validation, head = self.crash_after_catalog_and_take_over(
            "configured/adversarial/outcome-classification"
        )
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.UNCHANGED,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=head,
        )

        with self.assertRaisesRegex(
            RealmConflict, "completed publication cannot converge"
        ):
            self.runtime.ledger.complete_configured_package_ingress(
                operation_id=("configured/adversarial/outcome-classification/complete"),
                request=request,
                receipt=receipt,
                attempt_id=attempt.attempt_id,
                worker_id=attempt.worker_id,
                worker_generation=attempt.worker_generation,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (request.digest,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 0)

    def test_catalog_completion_cannot_be_reclassified_as_head_conflict(
        self,
    ) -> None:
        request, attempt, validation, _published_head = (
            self.crash_after_catalog_and_take_over(
                "configured/adversarial/conflict-outcome-classification"
            )
        )
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: advanced-after-original-publication\n", encoding="utf-8"
        )
        advanced = self.publish("configured/adversarial/conflict-outcome-advance-head")
        self.assertEqual(advanced.head.revision, 2)
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.CONFLICT,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=None,
            conflict_code="configured_package_head_changed",
        )

        with self.assertRaisesRegex(RealmConflict, "completed publication"):
            self.runtime.ledger.complete_configured_package_ingress(
                operation_id=(
                    "configured/adversarial/conflict-outcome-classification/complete"
                ),
                request=request,
                receipt=receipt,
                attempt_id=attempt.attempt_id,
                worker_id=attempt.worker_id,
                worker_generation=attempt.worker_generation,
            )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (request.digest,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 0)
        self.assertEqual(
            self.runtime.catalog.read_head(package_id=request.package_id),
            advanced.head,
        )

    def test_completion_trigger_enforces_published_outcome_classification(
        self,
    ) -> None:
        request, attempt, validation, head = self.crash_after_catalog_and_take_over(
            "configured/adversarial/sql-outcome-classification"
        )
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.UNCHANGED,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=head,
        )
        self.assert_completion_trigger_rejects(
            operation_id=("configured/adversarial/sql-outcome-classification/complete"),
            request=request,
            attempt=attempt,
            receipt=receipt,
        )

    def test_completion_trigger_rejects_conflict_after_catalog_completion(
        self,
    ) -> None:
        request, attempt, validation, _published_head = (
            self.crash_after_catalog_and_take_over(
                "configured/adversarial/sql-conflict-outcome-classification"
            )
        )
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: sql-conflict-advanced\n", encoding="utf-8"
        )
        advanced = self.publish("configured/adversarial/sql-conflict-outcome-advance")
        self.assertEqual(advanced.head.revision, 2)
        receipt = ConfiguredPackageIngressReceipt(
            request_digest=request.digest,
            package_id=request.package_id,
            publisher_id=request.publisher_id,
            outcome=ConfiguredPackageIngressOutcome.CONFLICT,
            validation=validation,
            source_ref=attempt.source_ref,
            owned_paths=attempt.owned_paths,
            head=None,
            conflict_code="configured_package_head_changed",
        )
        self.assert_completion_trigger_rejects(
            operation_id=(
                "configured/adversarial/sql-conflict-outcome-classification/complete"
            ),
            request=request,
            attempt=attempt,
            receipt=receipt,
        )
        self.assertEqual(
            self.runtime.catalog.read_head(package_id=request.package_id),
            advanced.head,
        )

    def test_invalid_adoptable_capture_is_durable_without_source_reread(
        self,
    ) -> None:
        operation_id = "configured/adversarial/invalid-adoptable"
        with mock.patch.object(
            self.runtime.ledger,
            "promote_configured_package_ingress_capture",
            side_effect=RealmConflict("injected crash before promotion"),
        ):
            with self.assertRaisesRegex(RealmConflict, "injected crash"):
                self.publish(operation_id, attempt_ttl_seconds=1)
        time.sleep(1.05)
        source_reads = 0

        def forbidden_source_reread():
            nonlocal source_reads
            source_reads += 1
            raise AssertionError("adoptable capture reread mutable source")

        with mock.patch.object(
            self.runtime.content_store,
            "verify_tree",
            return_value=TreeManifest(()),
        ):
            rejected = self.publish(
                operation_id,
                source_resolver=forbidden_source_reread,
                attempt_ttl_seconds=1,
            )
        replay = self.publish(
            operation_id,
            source_resolver=forbidden_source_reread,
            attempt_ttl_seconds=1,
        )
        self.assertEqual(rejected, replay)
        self.assertEqual(rejected.outcome, ConfiguredPackageIngressOutcome.REJECTED)
        self.assertEqual(rejected.rejection_stage, "capture")
        self.assertEqual(rejected.rejection_code, "capture.package_tree_invalid")
        self.assertEqual(source_reads, 0)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (rejected.request_digest,),
            ).fetchone()[0]
        self.assertEqual(completion_count, 1)

    def test_heartbeat_returns_its_immutable_receipt_without_current_reread(
        self,
    ) -> None:
        request = self.bind_request("configured/adversarial/heartbeat-receipt")
        claim = self.runtime.ledger.begin_configured_package_ingress_attempt(
            operation_id="configured/adversarial/heartbeat-attempt",
            request=request,
            attempt_id="configured-adversarial-heartbeat-attempt",
            owner_id="configured-adversarial-heartbeat-owner",
            change_id="configured-adversarial-heartbeat-change",
            store_id=self.runtime.content_store.store_id,
            capture_operation_id="configured/adversarial/heartbeat-capture",
            worker_id="configured-adversarial-heartbeat-worker",
            ttl_seconds=5,
        )
        self.assertTrue(claim.leader)

        # The operation receipt is the heartbeat's durable observation.  A
        # separate read can legitimately see a later phase or renewal and
        # therefore must not be required to equal this receipt.
        with mock.patch.object(
            self.runtime.ledger,
            "read_configured_package_ingress_attempt",
            side_effect=AssertionError("heartbeat reread mutable current attempt"),
        ):
            renewed = self.runtime.ledger.heartbeat_configured_package_ingress_attempt(
                operation_id="configured/adversarial/heartbeat-renew",
                request=request,
                attempt_id=claim.attempt.attempt_id,
                worker_id=claim.attempt.worker_id,
                worker_generation=claim.attempt.worker_generation,
                ttl_seconds=5,
            )
        self.assertEqual(renewed.attempt_id, claim.attempt.attempt_id)
        self.assertGreater(renewed.worker_expires_at, claim.attempt.worker_expires_at)

    def test_initial_projection_heartbeat_failure_releases_attachment(self) -> None:
        with mock.patch.object(
            ManagedReadOnlyProjection,
            "heartbeat",
            side_effect=RealmConflict("injected projection heartbeat failure"),
        ):
            with self.assertRaisesRegex(
                RealmConflict, "injected projection heartbeat failure"
            ):
                self.publish(
                    "configured/adversarial/projection-heartbeat-failure",
                    attempt_ttl_seconds=0.2,
                )

        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            consumer_states = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT lease.state FROM projection_consumers consumer "
                    "JOIN leases lease ON lease.lease_id = consumer.lease_id "
                    "WHERE consumer.consumer_kind = "
                    "'configured-package-validation'"
                )
            )
        self.assertEqual(consumer_states, ("released",))
        self.assertEqual(self.runtime.projection_service._active, {})

    def test_final_handoff_outlives_sub_timeout_writer_contention(self) -> None:
        self.assertGreater(self.runtime.ledger.busy_timeout_ms, 1_200)
        real_complete = self.runtime.ledger.complete_configured_package_ingress
        blocker_errors: list[BaseException] = []

        def complete_behind_writer(**kwargs):
            writer_ready = threading.Event()

            def hold_writer() -> None:
                try:
                    with sqlite3.connect(
                        self.runtime.ledger.database_path,
                        timeout=self.runtime.ledger.busy_timeout_ms / 1000,
                    ) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        writer_ready.set()
                        time.sleep(1.2)
                        connection.commit()
                except BaseException as error:
                    blocker_errors.append(error)
                    writer_ready.set()

            blocker = threading.Thread(target=hold_writer)
            blocker.start()
            self.assertTrue(writer_ready.wait(5))
            try:
                return real_complete(**kwargs)
            finally:
                blocker.join(5)
                self.assertFalse(blocker.is_alive())

        with mock.patch.object(
            self.runtime.ledger,
            "complete_configured_package_ingress",
            side_effect=complete_behind_writer,
        ):
            result = self.publish(
                "configured/adversarial/final-handoff-contention",
                attempt_ttl_seconds=0.05,
            )

        self.assertEqual(blocker_errors, [])
        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            completion_count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_completions "
                "WHERE request_digest = ?",
                (result.request_digest,),
            ).fetchone()[0]
            attempt = connection.execute(
                "SELECT state, cleanup_state FROM "
                "configured_package_ingress_attempts "
                "WHERE request_digest = ?",
                (result.request_digest,),
            ).fetchone()
        self.assertEqual(completion_count, 1)
        self.assertEqual(attempt, ("completed", "complete"))

    def test_tiny_requested_ttl_uses_bounded_active_heartbeat_floor(self) -> None:
        def slow_validation(root: Path) -> ConfiguredPackageValidationResult:
            time.sleep(1.1)
            return self.accepted(root)

        result = self.runtime.configured_package_ingress.publish(
            operation_id="configured/adversarial/tiny-active-ttl",
            package_id="configured_package",
            source_identity_digest=self.source_digest,
            validation_policy_digest=self.validation_policy_digest,
            source_resolver=lambda: AllowedTreeSource(self.source),
            validator=slow_validation,
            attempt_ttl_seconds=0.000_001,
        )

        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempt_heartbeat_count = connection.execute(
                "SELECT COUNT(*) FROM ledger_transactions WHERE operation_kind = "
                "'configured-package-ingress.attempt.heartbeat'"
            ).fetchone()[0]
            projection_heartbeat_revision = connection.execute(
                "SELECT MAX(lease.heartbeat_revision) "
                "FROM projection_consumers consumer "
                "JOIN leases lease ON lease.lease_id = consumer.lease_id "
                "WHERE consumer.consumer_kind = "
                "'configured-package-validation'"
            ).fetchone()[0]
        self.assertGreaterEqual(attempt_heartbeat_count, 3)
        self.assertLess(attempt_heartbeat_count, 12)
        self.assertGreater(projection_heartbeat_revision, 0)

    def test_initial_heartbeat_failure_restores_requested_takeover_ttl(self) -> None:
        real_heartbeat = (
            self.runtime.ledger.heartbeat_configured_package_ingress_attempt
        )
        heartbeat_calls = 0
        source_calls = 0

        def fail_initial_heartbeat(**kwargs):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            if heartbeat_calls == 1:
                raise RealmConflict("injected initial heartbeat failure")
            return real_heartbeat(**kwargs)

        def source_resolver() -> AllowedTreeSource:
            nonlocal source_calls
            source_calls += 1
            return AllowedTreeSource(self.source)

        with mock.patch.object(
            self.runtime.ledger,
            "heartbeat_configured_package_ingress_attempt",
            side_effect=fail_initial_heartbeat,
        ):
            with self.assertRaisesRegex(
                RealmConflict, "injected initial heartbeat failure"
            ):
                self.runtime.configured_package_ingress.publish(
                    operation_id="configured/adversarial/initial-heartbeat-failure",
                    package_id="configured_package",
                    source_identity_digest=self.source_digest,
                    validation_policy_digest=self.validation_policy_digest,
                    source_resolver=source_resolver,
                    validator=self.accepted,
                    attempt_ttl_seconds=0.2,
                )

        self.assertEqual(source_calls, 0)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            expiry = connection.execute(
                "SELECT worker_expires_at FROM configured_package_ingress_attempts "
                "WHERE request_digest = ("
                "SELECT request_digest FROM configured_package_ingress_requests "
                "WHERE client_operation_id = ?)",
                ("configured/adversarial/initial-heartbeat-failure",),
            ).fetchone()[0]
        self.assertLessEqual(expiry - time.time(), 0.2)

        time.sleep(max(0.0, expiry - time.time()) + 0.02)
        result = self.runtime.configured_package_ingress.publish(
            operation_id="configured/adversarial/initial-heartbeat-failure",
            package_id="configured_package",
            source_identity_digest=self.source_digest,
            validation_policy_digest=self.validation_policy_digest,
            source_resolver=source_resolver,
            validator=self.accepted,
            attempt_ttl_seconds=0.2,
        )
        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        self.assertEqual(source_calls, 1)

    def test_slow_source_and_validation_keep_one_fenced_leader(self) -> None:
        source_entered = threading.Event()
        release_source = threading.Event()
        calls_lock = threading.Lock()
        source_calls = 0
        validation_calls = 0

        def slow_source() -> AllowedTreeSource:
            nonlocal source_calls
            with calls_lock:
                source_calls += 1
            source_entered.set()
            if not release_source.wait(5):
                raise AssertionError("test did not release configured source")
            return AllowedTreeSource(self.source)

        def slow_validation(root: Path) -> ConfiguredPackageValidationResult:
            nonlocal validation_calls
            with calls_lock:
                validation_calls += 1
            time.sleep(0.7)
            return self.accepted(root)

        def publish_same_request():
            return self.runtime.configured_package_ingress.publish(
                operation_id="configured/adversarial/slow-fenced-leader",
                package_id="configured_package",
                source_identity_digest=self.source_digest,
                validation_policy_digest=self.validation_policy_digest,
                source_resolver=slow_source,
                validator=slow_validation,
                attempt_ttl_seconds=0.3,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(publish_same_request)
            self.assertTrue(source_entered.wait(5))
            # Wait past the original fence.  The follower must remain a
            # follower because the background guard renews the active change.
            time.sleep(0.4)
            follower = executor.submit(publish_same_request)
            time.sleep(0.05)
            release_source.set()
            result = leader.result(timeout=10)
            follower_result = follower.result(timeout=10)

        self.assertEqual(result, follower_result)
        replay = self.runtime.configured_package_ingress.publish(
            operation_id="configured/adversarial/slow-fenced-leader",
            package_id="configured_package",
            source_identity_digest=self.source_digest,
            validation_policy_digest=self.validation_policy_digest,
            source_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("completed slow ingress reread mutable source")
            ),
            validator=lambda _root: (_ for _ in ()).throw(
                AssertionError("completed slow ingress reran validation")
            ),
            attempt_ttl_seconds=0.3,
        )
        self.assertEqual(result, replay)
        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        self.assertEqual(source_calls, 1)
        self.assertEqual(validation_calls, 1)


if __name__ == "__main__":
    unittest.main()
