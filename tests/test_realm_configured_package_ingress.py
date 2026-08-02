from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from optpilot.realm.configured_package_ingress import (
    ConfiguredPackageHeadChanged,
    ConfiguredPackageIngressOutcome,
    ConfiguredPackageOwnershipConflict,
    ConfiguredPackageValidationFact,
    ConfiguredPackageValidationResult,
    configured_package_source_identity_digest,
)
from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.manifests import SealLimits
from optpilot.realm.refs import request_digest


class ConfiguredPackageIngressTest(unittest.TestCase):
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
            "configured-source-one"
        )
        self.validation_policy_digest = request_digest({"policy": "static-test-v1"})

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
        source_digest: str | None = None,
        source_resolver=None,
        validator=None,
        attempt_ttl_seconds: float = 300,
    ):
        return self.runtime.configured_package_ingress.publish(
            operation_id=operation_id,
            package_id="configured_package",
            source_identity_digest=source_digest or self.source_digest,
            validation_policy_digest=self.validation_policy_digest,
            source_resolver=(
                source_resolver
                if source_resolver is not None
                else lambda: AllowedTreeSource(self.source)
            ),
            validator=validator or self.accepted,
            attempt_ttl_seconds=attempt_ttl_seconds,
        )

    def test_publish_replay_unchanged_and_cleanup_are_exact(self) -> None:
        first = self.publish("configured/publish/one")
        replay = self.publish(
            "configured/publish/one",
            source_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("replay read mutable source")
            ),
            validator=lambda _root: (_ for _ in ()).throw(
                AssertionError("replay reran validation")
            ),
        )
        unchanged = self.publish("configured/publish/two")

        self.assertEqual(first.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        self.assertEqual(replay, first)
        self.assertEqual(unchanged.outcome, ConfiguredPackageIngressOutcome.UNCHANGED)
        self.assertEqual(unchanged.head.revision, 1)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempts = connection.execute(
                "SELECT state, cleanup_state FROM "
                "configured_package_ingress_attempts ORDER BY created_at"
            ).fetchall()
            requests = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT request_json FROM configured_package_ingress_requests"
                )
            )
        self.assertEqual(
            attempts,
            [("completed", "complete"), ("completed", "complete")],
        )
        self.assertNotIn(str(self.source), "\n".join(requests))

    def test_validation_rejection_is_durable_and_never_publishes(self) -> None:
        rejected = ConfiguredPackageValidationResult(
            False,
            (ConfiguredPackageValidationFact("configs.invalid", "error", 2),),
        )
        first = self.publish(
            "configured/rejected/validation",
            validator=lambda _root: rejected,
        )
        replay = self.publish(
            "configured/rejected/validation",
            source_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("rejection replay read source")
            ),
            validator=lambda _root: (_ for _ in ()).throw(
                AssertionError("rejection replay validated")
            ),
        )
        self.assertEqual(first, replay)
        self.assertEqual(first.outcome, ConfiguredPackageIngressOutcome.REJECTED)
        self.assertEqual(first.rejection_stage, "validation")
        self.assertEqual(first.source_ref, replay.source_ref)
        self.assertIsNone(
            self.runtime.catalog.read_head(package_id="configured_package")
        )

    def test_empty_capture_rejection_is_terminal_and_path_free(self) -> None:
        shutil.rmtree(self.source)
        self.source.mkdir()
        first = self.publish("configured/rejected/empty")
        replay = self.publish(
            "configured/rejected/empty",
            source_resolver=lambda: (_ for _ in ()).throw(
                AssertionError("capture rejection replay read source")
            ),
        )
        self.assertEqual(first, replay)
        self.assertEqual(first.outcome, ConfiguredPackageIngressOutcome.REJECTED)
        self.assertEqual(first.rejection_stage, "capture")
        self.assertEqual(first.rejection_code, "capture.package_tree_invalid")
        self.assertIsNone(first.source_ref)
        self.assertEqual(first.owned_paths, ())

    def test_new_request_with_deletions_replaces_same_source_application(self) -> None:
        (self.source / "methods").mkdir()
        (self.source / "methods" / "solver.yaml").write_text(
            "config: method\nid: solver\n", encoding="utf-8"
        )
        first = self.publish("configured/delete/one")
        shutil.rmtree(self.source / "methods")
        second = self.publish("configured/delete/two")
        self.assertEqual(first.head.revision, 1)
        self.assertEqual(second.head.revision, 2)
        self.assertEqual(second.owned_paths, ("resources",))
        manifest = self.runtime.catalog.read_revision(
            package_id="configured_package", revision=2
        )
        tree = self.runtime.content_store.verify_tree(manifest.root_ref)
        self.assertFalse(
            any(entry.path.startswith("methods") for entry in tree.entries)
        )

    def test_different_source_identity_cannot_take_over_owned_paths(self) -> None:
        self.publish("configured/owner/one")
        other_digest = configured_package_source_identity_digest(
            "configured-source-two"
        )
        with self.assertRaises(ConfiguredPackageOwnershipConflict) as raised:
            self.publish(
                "configured/owner/two",
                source_digest=other_digest,
            )
        self.assertEqual(raised.exception.code, "configured_package_ownership_conflict")
        with self.assertRaises(ConfiguredPackageOwnershipConflict):
            self.publish(
                "configured/owner/two",
                source_digest=other_digest,
                source_resolver=lambda: (_ for _ in ()).throw(
                    AssertionError("conflict replay read source")
                ),
            )

    def test_bound_head_change_is_terminal_unless_exact_artifact_won(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        first_error: list[BaseException] = []

        def delayed_resolver():
            entered.set()
            release.wait(5)
            return AllowedTreeSource(self.source)

        def delayed_publish() -> None:
            try:
                self.publish(
                    "configured/head/loser",
                    source_resolver=delayed_resolver,
                )
            except BaseException as error:
                first_error.append(error)

        thread = threading.Thread(target=delayed_publish)
        thread.start()
        self.assertTrue(entered.wait(5))
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: winner\n", encoding="utf-8"
        )
        self.publish("configured/head/winner")
        (self.source / "resources" / "viewer" / "resource.yaml").write_text(
            "id: loser\n", encoding="utf-8"
        )
        release.set()
        thread.join(10)
        self.assertEqual(len(first_error), 1)
        self.assertIsInstance(first_error[0], ConfiguredPackageHeadChanged)
        with self.assertRaises(ConfiguredPackageHeadChanged):
            self.publish(
                "configured/head/loser",
                source_resolver=lambda: (_ for _ in ()).throw(
                    AssertionError("head conflict replay read source")
                ),
            )

    def test_same_request_has_one_mutable_source_reader(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        lock = threading.Lock()

        def resolver():
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            release.wait(5)
            return AllowedTreeSource(self.source)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                self.publish,
                "configured/concurrent/same",
                source_resolver=resolver,
                attempt_ttl_seconds=5,
            )
            self.assertTrue(entered.wait(5))
            second = executor.submit(
                self.publish,
                "configured/concurrent/same",
                source_resolver=resolver,
                attempt_ttl_seconds=5,
            )
            time.sleep(0.05)
            release.set()
            left = first.result(timeout=10)
            right = second.result(timeout=10)
        self.assertEqual(calls, 1)
        self.assertEqual(left, right)

    def test_heartbeat_fences_slow_source_and_validation_from_follower(self) -> None:
        resolver_entered = threading.Event()
        resolver_calls = 0

        def slow_resolver():
            nonlocal resolver_calls
            resolver_calls += 1
            resolver_entered.set()
            time.sleep(0.35)
            return AllowedTreeSource(self.source)

        def slow_validator(root: Path) -> ConfiguredPackageValidationResult:
            self.assertTrue((root / "resources/viewer/resource.yaml").is_file())
            time.sleep(0.35)
            self.assertTrue((root / "resources/viewer/resource.yaml").is_file())
            return self.accepted(root)

        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(
                self.publish,
                "configured/heartbeat/slow",
                source_resolver=slow_resolver,
                validator=slow_validator,
                attempt_ttl_seconds=0.12,
            )
            self.assertTrue(resolver_entered.wait(5))
            time.sleep(0.18)
            follower = executor.submit(
                self.publish,
                "configured/heartbeat/slow",
                source_resolver=lambda: (_ for _ in ()).throw(
                    AssertionError("follower reread source")
                ),
                validator=lambda _root: (_ for _ in ()).throw(
                    AssertionError("follower reran validation")
                ),
                attempt_ttl_seconds=0.12,
            )
            result = leader.result(timeout=10)
            follower_result = follower.result(timeout=10)

        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        self.assertEqual(follower_result, result)
        self.assertEqual(resolver_calls, 1)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            attempt_heartbeats = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_attempt_transitions "
                "transition_record JOIN ledger_transactions txn "
                "ON txn.txn_id = transition_record.txn_id "
                "WHERE txn.operation_kind = "
                "'configured-package-ingress.attempt.heartbeat'"
            ).fetchone()[0]
            owner_change_heartbeats = connection.execute(
                "SELECT MAX(heartbeat_revision) FROM leases "
                "WHERE lease_kind = 'owner-change-retention'"
            ).fetchone()[0]
            projection_heartbeats = connection.execute(
                "SELECT MAX(heartbeat_revision) FROM leases "
                "WHERE lease_kind = 'projection-consumer'"
            ).fetchone()[0]
        self.assertGreater(attempt_heartbeats, 2)
        self.assertGreater(owner_change_heartbeats, 0)
        self.assertGreater(projection_heartbeats, 0)

    def test_expired_completed_capture_is_adopted_without_source_reread(self) -> None:
        real_promote = self.runtime.ledger.promote_configured_package_ingress_capture
        failures = 0

        def fail_twice(**kwargs):
            nonlocal failures
            failures += 1
            if failures <= 2:
                raise RealmConflict("injected crash before capture promotion")
            return real_promote(**kwargs)

        with mock.patch.object(
            self.runtime.ledger,
            "promote_configured_package_ingress_capture",
            side_effect=fail_twice,
        ):
            with self.assertRaises(RealmConflict):
                self.publish(
                    "configured/adopt/crash",
                    attempt_ttl_seconds=0.3,
                )
            time.sleep(0.32)
            (self.source / "resources" / "viewer" / "resource.yaml").write_text(
                "id: changed-after-capture\n", encoding="utf-8"
            )
            with self.assertRaises(RealmConflict):
                self.publish(
                    "configured/adopt/crash",
                    source_resolver=lambda: (_ for _ in ()).throw(
                        AssertionError("completed capture was reread")
                    ),
                    attempt_ttl_seconds=0.1,
                )
            time.sleep(0.12)
            result = self.publish(
                "configured/adopt/crash",
                source_resolver=lambda: (_ for _ in ()).throw(
                    AssertionError("adoptable capture was reread")
                ),
                attempt_ttl_seconds=0.2,
            )
        self.assertEqual(result.outcome, ConfiguredPackageIngressOutcome.PUBLISHED)
        projected = self.runtime.content_store.verify_tree(result.source_ref)
        file_entry = next(entry for entry in projected.entries if entry.kind == "file")
        blob = self.runtime.content_store.verify_blob(file_entry.blob_ref)
        self.assertNotEqual(blob.size, len("id: changed-after-capture\n"))

    def test_hard_capture_ceiling_rejects_before_binding_or_source_read(self) -> None:
        called = False

        def resolver():
            nonlocal called
            called = True
            return AllowedTreeSource(self.source)

        with self.assertRaisesRegex(ValueError, "hard ceiling"):
            self.runtime.configured_package_ingress.publish(
                operation_id="configured/limits/too-large",
                package_id="configured_package",
                source_identity_digest=self.source_digest,
                validation_policy_digest=self.validation_policy_digest,
                source_resolver=resolver,
                validator=self.accepted,
                limits=SealLimits(max_total_bytes=21 * 1024**3),
            )
        self.assertFalse(called)
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM configured_package_ingress_requests "
                "WHERE client_operation_id = 'configured/limits/too-large'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
