"""Deliberate deletion of a chosen run (design §12).

Deletion erases a retired run's record and leaves an immutable note in its
place, so a deleted run is never mistaken for one that never existed. These
prove the ordering that makes a crash safe -- note first, rows after, in one
transaction -- and that the schema itself refuses every path around it.
"""

from __future__ import annotations

import sqlite3
import unittest

from optpilot.realm.errors import RealmConflict, RealmNotFound, RunRecordDeleted
from optpilot.realm.run_deletion_service import delete_run_and_reclaim

from tests.core.test_realm_run_retirement import RealmRunRetirementTest

IMAGE_DIGEST = "sha256:" + "a" * 64


class RealmRunDeletionTest(RealmRunRetirementTest):
    """Reuses the retirement scaffold: setUp admits a run with one trial."""

    def _finish_and_retire(self) -> None:
        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close"))
        self.finish(operation_id=self.op("finish"))
        change = self.begin_retirement()
        self.retire(operation_id=self.op("retire"), change_id=change.change_id)

    def _delete(self, operation_id: str, **overrides):
        request = dict(
            operation_id=operation_id,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
            expected_run_revision=5,
            named_image_digests=(IMAGE_DIGEST,),
        )
        request.update(overrides)
        return self.ledger.delete_run_record(**request)

    def _direct_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def test_delete_erases_the_record_and_leaves_the_note(self) -> None:
        self._finish_and_retire()
        run_id = self.run.run.run_id

        note = self._delete(self.op("delete"))
        self.assertEqual(note.run_id, run_id)
        self.assertEqual(note.run_terminal_state, "cancelled")
        self.assertEqual(note.named_image_digests, (IMAGE_DIGEST,))
        self.assertEqual(note.deleted_counts["run_definition_manifests"], 1)
        self.assertEqual(note.deleted_counts["run_terminal_seals"], 1)
        self.assertEqual(note.deleted_counts["run_logical_trials"], 1)
        self.assertGreater(note.deleted_counts["run_events"], 0)

        connection = self._direct_connection()
        for table in note.deleted_counts:
            remaining = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            self.assertEqual(remaining, 0, f"{table} still has rows")
        for table in (
            "run_namespaces",
            "run_revisions",
            "run_retirements",
            "run_finalizations",
            "run_deletions",
        ):
            present = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            self.assertGreater(present, 0, f"{table} lost its skeleton rows")

        read_back = self.ledger.read_run_deletion(
            actor_principal_id="operator", run_id=run_id
        )
        self.assertEqual(read_back, note)

    def test_delete_is_replayable_and_refused_a_second_time(self) -> None:
        self._finish_and_retire()
        operation = self.op("delete")
        first = self._delete(operation)
        again = self._delete(operation)
        self.assertEqual(first, again)
        with self.assertRaises(RealmConflict) as caught:
            self._delete(self.op("delete-again"))
        self.assertIn("already deleted", str(caught.exception))

    def test_delete_requires_retirement_first(self) -> None:
        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close"))
        self.finish(operation_id=self.op("finish"))
        with self.assertRaises(RealmConflict) as caught:
            self._delete(self.op("delete"), expected_run_revision=4)
        self.assertIn("retire it first", str(caught.exception))

    def test_delete_rejects_a_stale_revision_and_an_unknown_run(self) -> None:
        self._finish_and_retire()
        with self.assertRaises(RealmConflict):
            self._delete(self.op("delete-stale"), expected_run_revision=4)
        with self.assertRaises(RealmNotFound):
            self._delete(self.op("delete-unknown"), run_id="run-never-existed")

    def test_the_schema_refuses_erasure_without_the_note(self) -> None:
        # Even raw SQL cannot remove a run's rows before its note exists.
        self._finish_and_retire()
        connection = self._direct_connection()
        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "DELETE FROM run_logical_trials WHERE run_id = ?",
                    (self.run.run.run_id,),
                )

    def test_the_note_itself_is_immutable(self) -> None:
        self._finish_and_retire()
        self._delete(self.op("delete"))
        connection = self._direct_connection()
        run_id = self.run.run.run_id
        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE run_deletions SET run_terminal_state = 'succeeded' "
                    "WHERE run_id = ?",
                    (run_id,),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "DELETE FROM run_deletions WHERE run_id = ?", (run_id,)
                )

    def test_reads_answer_deleted_instead_of_crashing(self) -> None:
        # The skeleton a deleted run keeps would flow into readers whose
        # invariants assume the full record; each funnel must answer with the
        # typed deletion error (a kind of not-found) rather than an integrity
        # failure.
        self._finish_and_retire()
        run_id = self.run.run.run_id
        self._delete(self.op("delete"))
        with self.assertRaises(RunRecordDeleted):
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id=run_id
            )
        with self.assertRaises(RunRecordDeleted):
            self.ledger.read_run_terminal_seal(
                actor_principal_id="operator", run_id=run_id
            )
        self.assertIsInstance(RunRecordDeleted("x"), RealmNotFound)

    def test_a_deleted_run_still_lists_and_is_marked(self) -> None:
        self._finish_and_retire()
        run_id = self.run.run.run_id
        before = {
            item.run_id: item
            for item in self.ledger.list_runs(
                actor_principal_id="operator"
            ).items
        }
        self.assertFalse(before[run_id].deleted)
        self._delete(self.op("delete"))
        after = {
            item.run_id: item
            for item in self.ledger.list_runs(
                actor_principal_id="operator"
            ).items
        }
        self.assertTrue(after[run_id].deleted)
        self.assertEqual(after[run_id].state, "cancelled")
        self.assertEqual(after[run_id].retention_state, "retired")

    def test_the_note_cannot_be_written_for_an_unretired_run(self) -> None:
        # The INSERT guard is schema-level: no code path, including a future
        # bug, can leave a note for a run that was never retired.
        connection = self._direct_connection()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            with connection:
                connection.execute(
                    "INSERT INTO run_deletions VALUES "
                    "(?, 1, 0, 1, 'operator', NULL, 'cancelled', 1.0, "
                    "'{}', '[]', 1.0)",
                    (self.run.run.run_id,),
                )
        self.assertIn("retired", str(caught.exception))


    def _reclaim(self):
        return delete_run_and_reclaim(
            ledger=self.ledger,
            content_store=self.store,
            actor_principal_id="operator",
            run_id=self.run.run.run_id,
        )

    def test_the_service_takes_a_terminal_run_all_the_way(self) -> None:
        # From a merely finished run: fresh controller term, retirement,
        # erasure, and a no-grace collection epoch, all in one call.
        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close"))
        self.finish(operation_id=self.op("finish"))

        outcome = self._reclaim()
        self.assertEqual(outcome.note.run_id, self.run.run.run_id)
        self.assertEqual(outcome.note.run_terminal_state, "cancelled")
        self.assertGreaterEqual(outcome.reclaimed_objects, 0)
        with self.assertRaises(RunRecordDeleted):
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id=self.run.run.run_id
            )

        # Running the whole sequence again is a resume, not a second deletion:
        # the same note comes back and nothing further is collected.
        again = self._reclaim()
        self.assertEqual(again.note, outcome.note)
        self.assertEqual(again.reclaimed_objects, 0)

    def test_shared_content_survives_deletion_of_its_source_run(self) -> None:
        # A review decision keeps its own reference to the candidate's tree.
        # Deleting the run that produced it erases the run's record, but the
        # collection pass computes liveness across every owner -- the shared
        # bytes must stay, and the decision must stay readable.
        from optpilot.realm.owners import OwnerMembership
        from optpilot.realm.review_collection_service import (
            RealmReviewCollectionService,
        )
        from optpilot.realm.run_workbench import RunWorkbenchReadModel

        principal = self.ledger.register_principal(
            operation_id=self.op("review-principal"),
            principal_id="operator",
            kind="human",
        )
        service = RealmReviewCollectionService(self.ledger, principal)
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.run.run.run_id
        )
        candidate = RunWorkbenchReadModel.from_snapshot(snapshot).page(
            "candidate"
        )["items"][0]
        saved = service.add_candidate(
            operation_id=self.op("review-add"),
            run_id=self.run.run.run_id,
            presentation_selection=candidate["selection"],
            note="Keep this decision after the run is deleted.",
        )
        source_change = self.ledger.begin_owner_change(
            operation_id=self.op("source-release"),
            actor_principal_id="operator",
            owner_id="candidate-source-owner",
            expected_owner_revision=1,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id=self.op("source-release-commit"),
            actor_principal_id="operator",
            change_id=source_change.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=(
                OwnerMembership(
                    self.store.store_id,
                    self.candidate_binding.content_ref,
                    "candidate-source",
                ),
            ),
        )

        self.terminalize_trial()
        self.close_submissions(operation_id=self.op("close"))
        self.finish(operation_id=self.op("finish"))
        self._reclaim()

        with self.assertRaises(RunRecordDeleted):
            self.ledger.read_run_snapshot(
                actor_principal_id="operator", run_id=self.run.run.run_id
            )
        self.store.verify_tree(self.candidate_binding.content_ref)
        retained = service.read_for_run(
            run_id=self.run.run.run_id, revision=1
        )
        self.assertIsNotNone(retained)
        self.assertEqual(retained.revision_digest, saved.revision_digest)

    def test_the_service_refuses_a_live_run(self) -> None:
        with self.assertRaises(RealmConflict) as caught:
            self._reclaim()
        self.assertIn("still live", str(caught.exception))
        # Nothing changed: the run still reads normally.
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="operator", run_id=self.run.run.run_id
        )
        self.assertEqual(snapshot.run.state, "running")


# The scaffold class defines its own tests; loading them again here would run
# every retirement test twice. Making the inherited names non-callable keeps
# them off the subclass, and load_tests keeps the imported scaffold class
# itself out of this module's discovery.
for _name in dir(RealmRunRetirementTest):
    if _name.startswith("test_"):
        setattr(RealmRunDeletionTest, _name, None)
del _name


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(RealmRunDeletionTest)


if __name__ == "__main__":
    unittest.main()
