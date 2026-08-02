from __future__ import annotations

import json
import os
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from optpilot.attempts import AttemptEnvelope, OutputDeclaration
from optpilot.realm.attempt_finalizer import (
    RealmAttemptFinalizationError,
    RealmAttemptFinalizer,
)
from optpilot.realm.errors import RealmConflict, RealmError, RealmIntegrityError
from optpilot.realm.manifests import SealLimits
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.run_attempt_records import RUN_ARTIFACT_ROLE
from optpilot.realm.service import RealmContentService
from optpilot.runtime_binding import LayeredVolumeScopeSource, TRIAL_SCOPE
from tests.test_realm_local_attempt_launcher import _RetainedRuntimeFixture


class RealmAttemptFinalizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)
        self.binding = self.fixture.bind()
        self.trial_root = self.binding.scope_paths[TRIAL_SCOPE]
        self.content = RealmContentService(
            self.fixture.ledger,
            local_stores={self.fixture.store.store_id: self.fixture.store},
        )
        self.finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
        )

    def declaration(
        self,
        name: str,
        *,
        path: str | None = None,
        kind: str = "file",
    ) -> OutputDeclaration:
        return OutputDeclaration(
            declaration_id=f"environment:{name}",
            name=name,
            path=path or name,
            kind=kind,
            media_type=(
                "application/vnd.optpilot.tree"
                if kind == "tree"
                else "text/plain"
            ),
            metadata={"producer": "test"},
        )

    def envelope(
        self,
        declarations: tuple[OutputDeclaration, ...] = (),
    ) -> AttemptEnvelope:
        attempt = self.binding.receipt.attempt
        return AttemptEnvelope(
            attempt_id=attempt.attempt_id,
            evaluation_spec_digest=attempt.evaluation_spec_digest,
            binding_id=attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 0.5}, "metadata": {}},
            metric_values={"score": 0.5},
            constraint_results={},
            output_declarations=declarations,
            event_summary={"primary_metric": "score"},
            execution_metadata={"worker": "test"},
            error={},
        )

    def finalize(self, envelope: AttemptEnvelope):
        return self.finalizer.finalize(
            envelope=envelope,
            binding=self.binding,
            change_id=self.binding.receipt.attempt.capture_change_id,
        )

    def planned_memberships(self) -> tuple[tuple[str, str, str], ...]:
        change_id = self.binding.receipt.attempt.capture_change_id
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            return tuple(
                connection.execute(
                    "SELECT store_id, content_ref, role "
                    "FROM owner_transaction_additions WHERE change_id = ? "
                    "ORDER BY store_id, content_ref, role",
                    (change_id,),
                )
            )

    def provisionally_held_refs(self) -> tuple[str, ...]:
        change_id = self.binding.receipt.attempt.capture_change_id
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT held.content_ref "
                    "FROM owner_transactions change "
                    "JOIN lease_content held "
                    "ON held.lease_id = change.retention_lease_id "
                    "WHERE change.change_id = ? ORDER BY held.content_ref",
                    (change_id,),
                )
            )

    def live_staging_count(self) -> int:
        change_id = self.binding.receipt.attempt.capture_change_id
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM staging_allocations "
                    "WHERE change_id = ? AND state IN ('published', 'finalized')",
                    (change_id,),
                ).fetchone()[0]
            )

    def test_zero_declarations_returns_identity_finalization_without_holds(self) -> None:
        envelope = self.envelope()
        finalization = self.finalize(envelope)

        self.assertEqual(finalization.envelope, envelope)
        self.assertEqual(finalization.captured_artifacts, ())
        self.assertEqual(finalization.effective_outcome, "success")
        self.assertIsNone(finalization.effective_code)
        self.assertEqual(self.planned_memberships(), ())

    def test_file_capture_is_verified_operator_only_and_exactly_held(self) -> None:
        (self.trial_root / "report.txt").write_text("report\n", encoding="utf-8")
        declaration = self.declaration("report", path="report.txt")
        finalization = self.finalize(self.envelope((declaration,)))

        self.assertEqual(len(finalization.captured_artifacts), 1)
        artifact = finalization.captured_artifacts[0]
        content_ref = BlobRef.parse(artifact.content_ref)
        manifest = self.fixture.store.verify_blob(content_ref)
        self.assertEqual(manifest.size, len(b"report\n"))
        self.assertEqual(artifact.size_bytes, manifest.size)
        self.assertEqual(artifact.visibility, "operator")
        self.assertEqual(artifact.declaration, declaration)
        self.assertEqual(
            self.planned_memberships(),
            ((self.fixture.store.store_id, str(content_ref), RUN_ARTIFACT_ROLE),),
        )
        self.assertEqual(self.live_staging_count(), 0)
        encoded = json.dumps(finalization.to_dict(), sort_keys=True)
        self.assertNotIn(str(self.fixture.root), encoded)

    def test_layered_trial_volume_captures_from_its_writable_upper(self) -> None:
        fixture = _RetainedRuntimeFixture(trial_workspace_seed=True)
        self.addCleanup(fixture.close)
        binding = fixture.bind()
        trial_scope = next(
            item
            for item in binding.portable_spec.scopes
            if item.name == TRIAL_SCOPE
        )
        self.assertIsInstance(trial_scope.source, LayeredVolumeScopeSource)
        trial_root = binding.scope_paths[TRIAL_SCOPE]
        self.assertEqual(
            (trial_root / "seeded" / "input.json").read_text(encoding="utf-8"),
            '{"value": 4.0}\n',
        )
        (trial_root / "report.txt").write_text("report\n", encoding="utf-8")
        finalizer = RealmAttemptFinalizer(
            fixture.ledger,
            fixture.content,
            actor_principal_id="operator",
            store_id=fixture.store.store_id,
        )
        attempt = binding.receipt.attempt
        envelope = AttemptEnvelope(
            attempt_id=attempt.attempt_id,
            evaluation_spec_digest=attempt.evaluation_spec_digest,
            binding_id=attempt.binding_id,
            outcome="success",
            phase="environment_evaluation",
            wall_clock_seconds=0.1,
            validation={"accepted": True, "errors": []},
            materialization={"runtime_spec": {"x": 0.5}, "metadata": {}},
            metric_values={"score": 0.5},
            constraint_results={},
            output_declarations=(
                OutputDeclaration(
                    declaration_id="environment:report",
                    name="report",
                    path="report.txt",
                    kind="file",
                    media_type="text/plain",
                    metadata={"producer": "test"},
                ),
            ),
            event_summary={"primary_metric": "score"},
            execution_metadata={"worker": "test"},
            error={},
        )

        finalization = finalizer.finalize(
            envelope=envelope,
            binding=binding,
            change_id=attempt.capture_change_id,
        )

        artifact = finalization.captured_artifacts[0]
        manifest = fixture.store.verify_blob(BlobRef.parse(artifact.content_ref))
        self.assertEqual(manifest.size, len(b"report\n"))

    def test_tree_capture_is_keyed_and_replays_original_after_source_changes(self) -> None:
        tree = self.trial_root / "bundle"
        (tree / "nested").mkdir(parents=True)
        (tree / "nested" / "model.json").write_text(
            '{"version":1}\n', encoding="utf-8"
        )
        declaration = self.declaration("bundle", kind="tree")
        envelope = self.envelope((declaration,))

        first = self.finalize(envelope)
        (tree / "nested" / "model.json").write_text(
            '{"version":2}\n', encoding="utf-8"
        )
        (tree / "new.txt").write_text("new\n", encoding="utf-8")
        replay = self.finalize(envelope)

        self.assertEqual(replay, first)
        artifact = replay.captured_artifacts[0]
        content_ref = SnapshotRef.parse(artifact.content_ref)
        manifest = self.fixture.store.verify_tree(content_ref)
        self.assertEqual(
            tuple(item.path for item in manifest.entries),
            ("nested", "nested/model.json"),
        )
        self.assertEqual(artifact.size_bytes, len(b'{"version":1}\n'))
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            completed = connection.execute(
                "SELECT COUNT(*) FROM completed_tree_captures WHERE change_id = ?",
                (self.binding.receipt.attempt.capture_change_id,),
            ).fetchone()[0]
        self.assertEqual(completed, 1)
        self.assertEqual(
            self.planned_memberships(),
            ((self.fixture.store.store_id, str(content_ref), RUN_ARTIFACT_ROLE),),
        )

    def test_file_and_tree_result_is_accepted_by_atomic_run_adoption(self) -> None:
        (self.trial_root / "report.txt").write_text("report\n", encoding="utf-8")
        tree = self.trial_root / "bundle"
        tree.mkdir()
        (tree / "data.json").write_text('{"ok":true}\n', encoding="utf-8")
        envelope = self.envelope(
            (
                self.declaration("report", path="report.txt"),
                self.declaration("bundle", kind="tree"),
            )
        )
        finalization = self.finalize(envelope)
        receipt = self.binding.receipt
        owner = self.fixture.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=receipt.run.owner_id,
            permission=OwnerPermission.DERIVE,
        )

        adopted = self.fixture.ledger.adopt_run_attempt(
            operation_id="attempt-finalizer/adopt",
            actor_principal_id="operator",
            run_id=receipt.run.run_id,
            attempt_id=receipt.attempt.attempt_id,
            change_id=receipt.attempt.capture_change_id,
            finalization=finalization,
            expected_run_revision=receipt.run.current_revision,
            expected_owner_revision=owner.revision,
            **self.fixture.controller_arguments(),
        )

        self.assertEqual(adopted.attempt.state, "terminal")
        self.assertEqual(
            tuple(item.declaration_id for item in adopted.artifacts),
            ("environment:bundle", "environment:report"),
        )
        self.assertEqual(
            {item.content_ref for item in adopted.artifacts},
            {
                BlobRef.parse(finalization.captured_artifacts[0].content_ref),
                SnapshotRef.parse(finalization.captured_artifacts[1].content_ref),
            },
        )

    def test_partial_tree_then_missing_file_is_recoverable_by_exact_retry(self) -> None:
        tree = self.trial_root / "bundle"
        tree.mkdir()
        (tree / "item.txt").write_text("tree\n", encoding="utf-8")
        declarations = (
            self.declaration("bundle", kind="tree"),
            self.declaration("later", path="later.txt"),
        )
        envelope = self.envelope(declarations)

        with self.assertRaises(RealmAttemptFinalizationError) as raised:
            self.finalize(envelope)
        self.assertEqual(raised.exception.code, "artifact_capture_failed")
        self.assertEqual(raised.exception.declaration_id, "environment:later")
        self.assertNotIn(str(self.fixture.root), str(raised.exception))
        self.assertEqual(len(self.planned_memberships()), 1)

        (self.trial_root / "later.txt").write_text("later\n", encoding="utf-8")
        recovered = self.finalize(envelope)
        self.assertEqual(len(recovered.captured_artifacts), 2)
        self.assertEqual(len(self.planned_memberships()), 2)
        with sqlite3.connect(self.fixture.ledger.database_path) as connection:
            completed = connection.execute(
                "SELECT COUNT(*) FROM completed_tree_captures WHERE change_id = ?",
                (self.binding.receipt.attempt.capture_change_id,),
            ).fetchone()[0]
        self.assertEqual(completed, 1)

    def test_unchanged_file_replay_is_equal_but_changed_file_fails_closed(self) -> None:
        report = self.trial_root / "report.txt"
        report.write_text("one\n", encoding="utf-8")
        envelope = self.envelope((self.declaration("report", path="report.txt"),))

        first = self.finalize(envelope)
        self.assertEqual(self.finalize(envelope), first)
        self.assertEqual(self.live_staging_count(), 0)
        report.write_text("two\n", encoding="utf-8")
        with self.assertRaises(RealmAttemptFinalizationError) as raised:
            self.finalize(envelope)
        self.assertEqual(raised.exception.code, "artifact_capture_failed")
        self.assertEqual(raised.exception.cause_type, "RealmConflict")
        # The original exact provisional membership remains authoritative;
        # finalization never guesses that the changed blob supersedes it.
        self.assertEqual(
            self.planned_memberships()[0][1],
            first.captured_artifacts[0].content_ref,
        )
        self.assertEqual(
            self.provisionally_held_refs(),
            (first.captured_artifacts[0].content_ref,),
        )
        self.assertEqual(self.live_staging_count(), 0)

    @unittest.skipUnless(os.name == "posix", "symlink rejection requires POSIX")
    def test_missing_symlink_escape_and_tampered_declarations_are_rejected(self) -> None:
        missing = self.envelope((self.declaration("missing"),))
        with self.assertRaises(RealmAttemptFinalizationError) as raised:
            self.finalize(missing)
        self.assertEqual(raised.exception.code, "artifact_capture_failed")

        outside = self.fixture.root / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.trial_root / "link.txt")
        linked = self.envelope(
            (self.declaration("link", path="link.txt"),)
        )
        # Binding validation itself rejects the unsupported volume node before
        # the finalizer can open the declared selection.
        with self.assertRaises(RealmError):
            self.finalize(linked)

        with self.assertRaisesRegex(ValueError, "safe portable"):
            self.declaration("escape", path="../outside.txt")

        declaration = self.declaration("safe", path="safe.txt")
        tampered = self.envelope((declaration,))
        object.__setattr__(declaration, "path", "../outside.txt")
        with self.assertRaisesRegex(RealmIntegrityError, "not canonical"):
            self.finalize(tampered)
        self.assertEqual(self.planned_memberships(), ())

    def test_configured_limits_bound_capture_without_leaking_host_paths(self) -> None:
        (self.trial_root / "large.txt").write_text("12345", encoding="utf-8")
        finalizer = RealmAttemptFinalizer(
            self.fixture.ledger,
            self.content,
            actor_principal_id="operator",
            store_id=self.fixture.store.store_id,
            seal_limits=SealLimits(
                max_entries=4,
                max_depth=4,
                max_total_bytes=4,
                max_file_bytes=4,
                max_path_bytes=128,
                max_component_bytes=64,
            ),
        )
        envelope = self.envelope(
            (self.declaration("large", path="large.txt"),)
        )
        with self.assertRaises(RealmAttemptFinalizationError) as raised:
            finalizer.finalize(
                envelope=envelope,
                binding=self.binding,
                change_id=self.binding.receipt.attempt.capture_change_id,
            )
        self.assertEqual(raised.exception.code, "artifact_capture_failed")
        self.assertNotIn(str(self.fixture.root), json.dumps(raised.exception.to_dict()))

    def test_attempt_binding_and_capture_change_identities_must_match_exactly(self) -> None:
        envelope = self.envelope()
        mismatches = (
            replace(envelope, attempt_id="attempt-other"),
            replace(envelope, binding_id="binding-other"),
            replace(
                envelope,
                evaluation_spec_digest="sha256:" + "f" * 64,
            ),
        )
        for mismatched in mismatches:
            with self.subTest(identity=mismatched.to_dict()):
                with self.assertRaisesRegex(RealmConflict, "identity differs"):
                    self.finalize(mismatched)

        with self.assertRaisesRegex(RealmConflict, "capture change differs"):
            self.finalizer.finalize(
                envelope=envelope,
                binding=self.binding,
                change_id="change-other",
            )
        self.assertEqual(self.planned_memberships(), ())


if __name__ == "__main__":
    unittest.main()
