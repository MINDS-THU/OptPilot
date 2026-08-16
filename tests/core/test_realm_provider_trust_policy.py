"""Durability and security tests for Realm-owned provider trust policy."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import optpilot.realm.ledger as ledger_module
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.provider_trust_policy import RealmProviderTrustPolicyService
from optpilot.realm.provider_trust_records import (
    PROVIDER_TRUST_POLICY_OWNER_ID,
    ProviderTrustState,
)


IMAGE_A = "python@sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64


class RealmProviderTrustPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "realm.sqlite3"
        self.ledger = RealmLedger(self.database)
        self.counter = 0
        for principal_id in ("alice", "bob"):
            self.ledger.register_principal(
                operation_id=self.op(f"principal-{principal_id}"),
                principal_id=principal_id,
                kind="user",
            )
        self.alice = RealmProviderTrustPolicyService(self.ledger, "alice")
        self.bob = RealmProviderTrustPolicyService(self.ledger, "bob")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def op(self, label: str) -> str:
        self.counter += 1
        return f"provider-trust-test/{self.counter}/{label}"

    def test_fresh_realm_is_empty_and_exact_approval_survives_reopen(self) -> None:
        self.assertEqual(self.alice.list_active(), ())
        operation_id = self.op("approve")
        approved = self.alice.approve(
            operation_id=operation_id,
            image_ref=IMAGE_A,
            python_executable="/usr/local/bin/python3",
            reason="Approved for the local authenticated gateway.",
        )
        replay = self.alice.approve(
            operation_id=operation_id,
            image_ref=IMAGE_A,
            python_executable="/usr/local/bin/python3",
            reason="Approved for the local authenticated gateway.",
        )
        self.assertEqual(replay, approved)
        self.assertEqual(approved.state, ProviderTrustState.APPROVED)
        self.assertEqual(approved.sequence, 1)
        self.assertEqual(approved.image_ref, IMAGE_A)
        self.assertEqual(approved.python_executable, "/usr/local/bin/python3")
        self.assertEqual(self.alice.read_active(image_ref=IMAGE_A).decision, approved)
        with self.assertRaises(RealmConflict):
            self.alice.approve(
                operation_id=operation_id,
                image_ref=IMAGE_B,
            )

        self.ledger.close()
        self.ledger = RealmLedger(self.database)
        self.alice = RealmProviderTrustPolicyService(self.ledger, "alice")
        self.bob = RealmProviderTrustPolicyService(self.ledger, "bob")
        self.assertEqual(self.alice.list_active()[0].decision, approved)

    def test_revoke_appends_history_and_removes_only_active_head(self) -> None:
        approved = self.alice.approve(
            operation_id=self.op("approve"),
            image_ref=IMAGE_A,
            python_executable="/gateway/python",
        )
        with self.assertRaises(RealmConflict):
            self.alice.revoke(
                operation_id=self.op("mismatched-revoke"),
                image_ref=IMAGE_A,
            )
        revoked = self.alice.revoke(
            operation_id=self.op("revoke"),
            image_ref=IMAGE_A,
            python_executable="/gateway/python",
            reason="Digest retired.",
        )
        self.assertEqual(revoked.state, ProviderTrustState.REVOKED)
        self.assertEqual(revoked.sequence, 2)
        self.assertEqual(revoked.previous_decision_id, approved.decision_id)
        self.assertIsNone(self.alice.read_active(image_ref=IMAGE_A))
        self.assertEqual(self.alice.list_active(), ())
        self.assertEqual(self.alice.list_decisions(), (approved, revoked))
        heads = self.alice.list_heads()
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0].decision, revoked)

    def test_policy_is_owner_admin_authorized(self) -> None:
        self.alice.approve(operation_id=self.op("approve"), image_ref=IMAGE_A)
        for action in (
            self.bob.list_active,
            lambda: self.bob.approve(
                operation_id=self.op("unauthorized-approve"),
                image_ref=IMAGE_B,
            ),
        ):
            with self.assertRaises(RealmNotFound):
                action()

        self.ledger.grant_owner_permission(
            operation_id=self.op("grant-admin"),
            actor_principal_id="alice",
            owner_id=PROVIDER_TRUST_POLICY_OWNER_ID,
            principal_id="bob",
            permission=OwnerPermission.ADMIN,
        )
        bob_decision = self.bob.approve(
            operation_id=self.op("admin-approve"),
            image_ref=IMAGE_B,
        )
        self.assertEqual(bob_decision.actor_principal_id, "bob")
        self.assertEqual(
            {item.image_ref for item in self.bob.list_active()},
            {IMAGE_A, IMAGE_B},
        )

    def test_policy_owner_identity_cannot_be_squatted(self) -> None:
        with self.assertRaises(RealmConflict):
            self.ledger.create_owner(
                operation_id=self.op("squat-policy-owner"),
                owner_id=PROVIDER_TRUST_POLICY_OWNER_ID,
                owner_kind="workspace",
                principal_id="bob",
            )
        approved = self.alice.approve(
            operation_id=self.op("approve-after-squat"),
            image_ref=IMAGE_A,
        )
        self.assertEqual(approved.actor_principal_id, "alice")

    def test_invalid_or_unpinned_facts_are_rejected_before_persistence(self) -> None:
        invalid = (
            {"image_ref": "python:latest"},
            {"image_ref": IMAGE_A, "python_executable": "python 3"},
            {"image_ref": IMAGE_A, "contract": "arbitrary-entrypoint-v1"},
            {"image_ref": IMAGE_A, "reason": "  not canonical"},
        )
        for index, arguments in enumerate(invalid):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.alice.approve(
                        operation_id=self.op(f"invalid-{index}"),
                        **arguments,
                    )
        self.assertEqual(self.alice.list_decisions(), ())

    def test_corrupt_decision_proof_fails_closed(self) -> None:
        self.alice.approve(operation_id=self.op("approve"), image_ref=IMAGE_A)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "DROP TRIGGER provider_trust_decision_update_immutable"
            )
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE provider_trust_decisions SET request_json = '{}'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RealmIntegrityError):
            self.alice.list_active()

    def test_sql_decisions_and_policy_are_immutable(self) -> None:
        self.alice.approve(operation_id=self.op("approve"), image_ref=IMAGE_A)
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE provider_trust_decisions SET reason = 'replacement'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM provider_trust_policies")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM provider_trust_heads")
        finally:
            connection.close()


class RealmProviderTrustPolicyMigrationTest(unittest.TestCase):
    def test_populated_v34_realm_migrates_with_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "realm.sqlite3"
            legacy: RealmLedger | None = None
            upgraded: RealmLedger | None = None
            try:
                with (
                    mock.patch.object(ledger_module, "_CURRENT_SCHEMA_VERSION", 34),
                    mock.patch.object(
                        ledger_module,
                        "_MIGRATIONS",
                        ledger_module._MIGRATIONS[:34],
                    ),
                ):
                    legacy = RealmLedger(database)
                legacy.register_principal(
                    operation_id="provider-trust-migration/principal",
                    principal_id="operator",
                    kind="user",
                )
                legacy.close()
                legacy = None

                upgraded = RealmLedger(database)
                service = RealmProviderTrustPolicyService(upgraded, "operator")
                self.assertEqual(service.list_active(), ())
                with sqlite3.connect(database) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        37,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM provider_trust_decisions"
                        ).fetchone()[0],
                        0,
                    )
            finally:
                if legacy is not None:
                    legacy.close()
                if upgraded is not None:
                    upgraded.close()

    def test_open_local_is_narrow_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).absolute()
            with RealmProviderTrustPolicyService.open_local(root) as service:
                approved = service.approve(
                    operation_id="provider-trust-open-local/approve",
                    image_ref=IMAGE_A,
                )
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["authority"],
            )
            with RealmProviderTrustPolicyService.open_local(root) as reopened:
                self.assertEqual(reopened.list_active()[0].decision, approved)


if __name__ == "__main__":
    unittest.main()
