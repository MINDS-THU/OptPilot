from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import optpilot.realm.ledger as ledger_module
from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import (
    ContentRejected,
    InterfaceOutputDrainPending,
    RealmConflict,
    RealmExpired,
    RealmNotFound,
)
from optpilot.realm.interface_output_service import RealmInterfaceOutputSessionService
from optpilot.realm.interface_output_records import INTERFACE_OUTPUT_SESSION_ROLE
from optpilot.realm.interface_outputs import (
    INTERFACE_OUTPUT_SCHEMA,
    InterfaceOutputRecord,
    seal_interface_output_generation,
)
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission, OwnerState
from optpilot.realm.selection_service import RealmSelectionActionService
from optpilot.realm.service import RealmContentService


class RealmInterfaceOutputSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output_root = self.root / "output"
        self.output_root.mkdir()
        self.control = self.root / "outputs.jsonl"
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.principal = self.ledger.register_principal(
            operation_id="interface-session/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_store(
            operation_id="interface-session/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.service = RealmInterfaceOutputSessionService(
            self.ledger,
            self.content,
            actor_principal_id=self.principal.principal_id,
            store_id=self.store.store_id,
        )
        self.handle = self.service.create_session(
            operation_id="interface-session/create",
            launch_id="launch-01",
            ttl_seconds=3600,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    @staticmethod
    def _record(**overrides):
        value = {
            "schema_version": INTERFACE_OUTPUT_SCHEMA,
            "id": "generated-project-01",
            "label": "Generated simulator",
            "kind": "tree",
            "root": "output",
            "path": "generation-01",
        }
        value.update(overrides)
        return value

    def _write_control(self, *records) -> None:
        self.control.write_text(
            "".join(
                json.dumps(record, separators=(",", ":")) + "\n" for record in records
            ),
            encoding="utf-8",
        )

    def _write_generation(
        self, name: str = "generation-01", text: str = "print('ready')\n"
    ) -> None:
        generation = self.output_root / name
        generation.mkdir()
        (generation / "run.py").write_text(text, encoding="utf-8")

    def _expire_session_lease(self) -> None:
        """Advance only the persisted expiry seam without sleeping in tests."""

        with sqlite3.connect(self.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET expires_at = created_at WHERE lease_id = ?",
                (self.handle.lease.lease_id,),
            )
        self.assertGreater(time.time(), self.handle.lease.created_at)

    def test_output_is_owned_before_ready_and_replay_is_path_free(self) -> None:
        self._write_generation()
        self._write_control(self._record())

        first = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control,
            root_handles={"output": self.output_root},
        )
        replay_service = RealmInterfaceOutputSessionService(
            self.ledger,
            self.content,
            actor_principal_id=self.principal.principal_id,
            store_id=self.store.store_id,
        )
        replay = replay_service.capture_control_file(
            handle=self.handle,
            control_file=self.control,
            root_handles={"output": self.output_root},
        )

        self.assertEqual(first, replay)
        self.assertEqual(len(first), 1)
        self.assertIsNotNone(first[0].selection)
        owner = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=self.handle.session.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=owner.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        self.assertIn(first[0].membership, memberships)
        serialized = json.dumps(first[0].to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("generation-01", serialized)

    def test_tree_picker_lists_only_portable_no_follow_directories(self) -> None:
        self._write_generation()
        nested = self.output_root / "generation-01" / "nested"
        nested.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.output_root / "outside-link").symlink_to(
            outside, target_is_directory=True
        )

        paths = self.service.list_tree_selections(
            handle=self.handle,
            root_path=self.output_root,
        )

        self.assertEqual(paths, (".", "generation-01", "generation-01/nested"))
        self.assertNotIn("outside-link", paths)

    def test_tree_picker_bounds_regular_files_as_well_as_directory_choices(
        self,
    ) -> None:
        for index in range(3):
            (self.output_root / f"result-{index}.txt").write_text(
                str(index), encoding="utf-8"
            )

        with self.assertRaisesRegex(ContentRejected, "filesystem entries"):
            self.service.list_tree_selections(
                handle=self.handle,
                root_path=self.output_root,
                max_entries=2,
            )

    def test_supervisor_tree_selection_uses_existing_output_lifecycle(self) -> None:
        self._write_generation()

        first = self.service.capture_tree_selection(
            handle=self.handle,
            label="Generated simulator",
            relative_path="generation-01",
            root_handle="output",
            root_path=self.output_root,
        )
        replay = self.service.capture_tree_selection(
            handle=self.handle,
            label="Generated simulator",
            relative_path="generation-01",
            root_handle="output",
            root_path=self.output_root,
        )

        self.assertEqual(first, replay)
        self.assertEqual(first.state.value, "ready")
        self.assertTrue(first.output_id.startswith("selected-tree-"))
        self.assertEqual(first.record.root_handle, "output")
        self.assertEqual(first.record.relative_path, "generation-01")
        self.assertIsNotNone(first.ready_generation)
        self.assertIsNotNone(first.ready_generation.selection)  # type: ignore[union-attr]
        statuses = self.service.list_statuses(handle=self.handle)
        self.assertEqual(statuses, (first,))

    def test_tree_selection_rejects_absolute_input_and_does_not_follow_links(
        self,
    ) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (self.output_root / "outside-link").symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaises(ContentRejected):
            self.service.capture_tree_selection(
                handle=self.handle,
                label="Invalid",
                relative_path=str(outside),
                root_handle="output",
                root_path=self.output_root,
            )
        self.assertEqual(self.service.list_statuses(handle=self.handle), ())

        failed = self.service.capture_tree_selection(
            handle=self.handle,
            label="Linked tree",
            relative_path="outside-link",
            root_handle="output",
            root_path=self.output_root,
        )
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.error_code, "content_rejected")
        self.assertIsNone(failed.ready_generation)

    def test_tree_picker_reauthorizes_the_live_session(self) -> None:
        closed = self.service.close_capture(
            operation_id="interface-session/tree-picker-close",
            handle=self.handle,
        )
        self.assertEqual(closed.lease.state.value, "released")

        with self.assertRaisesRegex(RealmConflict, "capture lease is closed"):
            self.service.list_tree_selections(
                handle=self.handle,
                root_path=self.output_root,
            )

    def test_conflicting_duplicate_id_is_rejected_without_new_generation(self) -> None:
        self._write_generation()
        self._write_control(self._record())
        self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control,
            root_handles={"output": self.output_root},
        )

        with self.assertRaisesRegex(RealmConflict, "different record"):
            self.service.capture_generation(
                handle=self.handle,
                record=InterfaceOutputRecord.from_dict(
                    self._record(label="Changed label")
                ),
                root_handles={"output": self.output_root},
            )
        self.assertEqual(
            len(
                self.ledger.list_interface_output_generations(
                    actor_principal_id="operator",
                    session_id=self.handle.session.session_id,
                )
            ),
            1,
        )

    def test_keep_reuses_selection_workspace_and_survives_session_retirement(
        self,
    ) -> None:
        self._write_generation()
        self._write_control(self._record())
        generation = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control,
            root_handles={"output": self.output_root},
        )[0]
        assert generation.selection is not None
        selection_service = RealmSelectionActionService(self.ledger, self.principal)

        kept = selection_service.keep_as_editable_workspace(
            operation_id="interface-session/keep",
            selection=generation.selection,
            title="Generated simulator",
        )
        self.assertTrue(kept.eligibility.eligible)
        self.assertIsNotNone(kept.workspace)
        workspace_id = kept.workspace.workspace.workspace_id  # type: ignore[union-attr]

        retirement = self.service.retire_session(
            operation_id="interface-session/retire",
            handle=self.handle,
        )
        self.assertTrue(retirement.session.state.value == "retired")
        source_owner = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=self.handle.session.owner_id,
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertIs(source_owner.state, OwnerState.DELETED)
        workspace, revision = self.ledger.read_workspace(
            actor_principal_id="operator",
            workspace_id=workspace_id,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(workspace.current_revision, 1)
        self.store.verify_tree(revision.root_ref, verify_children=True)
        with self.assertRaises(RealmNotFound):
            selection_service.open_read_only(selection=generation.selection)

    def test_released_session_fence_cannot_capture(self) -> None:
        self._write_generation()
        record = InterfaceOutputRecord.from_dict(self._record())
        closed = self.service.close_capture(
            operation_id="interface-session/release-before-capture",
            handle=self.handle,
        )
        replay = self.service.close_capture(
            operation_id="interface-session/release-before-capture",
            handle=self.handle,
        )
        self.assertEqual(closed, replay)
        self.assertEqual(closed.session.state.value, "active")
        self.assertEqual(closed.lease.state.value, "released")

        with self.assertRaisesRegex(RealmConflict, "not active"):
            self.service.capture_generation(
                handle=self.handle,
                record=record,
                root_handles={"output": self.output_root},
            )
        self.assertEqual(
            self.ledger.list_interface_output_statuses(
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
            ),
            (),
        )

    def test_expired_session_resume_advances_fence_and_replay_is_exact(self) -> None:
        self._expire_session_lease()

        resumed = self.service.resume_expired_session(
            operation_id="interface-session/resume-expired",
            handle=self.handle,
            ttl_seconds=3600,
        )
        replay = self.service.resume_expired_session(
            operation_id="interface-session/resume-expired",
            handle=self.handle,
            ttl_seconds=3600,
        )

        self.assertEqual(replay, resumed)
        self.assertNotEqual(resumed.lease.lease_id, self.handle.lease.lease_id)
        self.assertEqual(resumed.lease.holder_id, self.handle.lease.holder_id)
        self.assertGreater(
            resumed.lease.fencing_token,
            self.handle.lease.fencing_token,
        )
        self.assertEqual(
            resumed.session.session_lease_id,
            resumed.lease.lease_id,
        )
        with sqlite3.connect(self.ledger.database_path) as connection:
            previous = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ?",
                (self.handle.lease.lease_id,),
            ).fetchone()
        self.assertEqual(previous, ("expired",))

        self._write_generation()
        record = InterfaceOutputRecord.from_dict(self._record())
        with self.assertRaisesRegex(RealmConflict, "fence is stale"):
            self.service.capture_generation(
                handle=self.handle,
                record=record,
                root_handles={"output": self.output_root},
            )
        captured = self.service.capture_generation(
            handle=resumed,
            record=record,
            root_handles={"output": self.output_root},
        )
        self.assertEqual(captured.output_id, record.output_id)

    def test_distinct_resume_operations_race_to_one_higher_fence(self) -> None:
        self._expire_session_lease()
        barrier = threading.Barrier(3)
        resumed = []
        failures: list[BaseException] = []

        def resume(operation_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                resumed.append(
                    self.service.resume_expired_session(
                        operation_id=operation_id,
                        handle=self.handle,
                        ttl_seconds=3600,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion path
                failures.append(error)

        workers = [
            threading.Thread(
                target=resume,
                args=(f"interface-session/resume-race/{suffix}",),
                daemon=True,
            )
            for suffix in ("a", "b")
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(resumed), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RealmConflict)
        self.assertRegex(str(failures[0]), "fence is stale")
        winner = resumed[0]
        self.assertGreater(
            winner.lease.fencing_token,
            self.handle.lease.fencing_token,
        )
        recovered = self.service.recover_session(launch_id="launch-01")
        self.assertEqual(recovered, winner)

    def test_untyped_session_lease_replacement_is_rejected_by_schema(self) -> None:
        replacement = self.ledger.acquire_lease(
            operation_id="interface-session/untyped-replacement/acquire",
            actor_principal_id="operator",
            owner_id=self.handle.session.owner_id,
            lease_kind="test-interface-replacement",
            audience="test",
            holder_id=self.handle.lease.holder_id,
            scope_key="test-interface-replacement:launch-01",
            ttl_seconds=3600,
        )
        with sqlite3.connect(self.ledger.database_path) as connection:
            transaction = connection.execute(
                "SELECT txn_id, committed_at FROM ledger_transactions "
                "WHERE operation_id = ?",
                ("interface-session/untyped-replacement/acquire",),
            ).fetchone()
            assert transaction is not None
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "typed fenced resume",
            ):
                connection.execute(
                    "UPDATE interface_output_sessions "
                    "SET session_lease_id = ?, updated_txn_id = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (
                        replacement.lease_id,
                        transaction[0],
                        transaction[1],
                        self.handle.session.session_id,
                    ),
                )

        recovered = self.service.recover_session(launch_id="launch-01")
        self.assertEqual(recovered, self.handle)

    def test_live_released_and_revoked_session_leases_cannot_resume(self) -> None:
        with self.assertRaisesRegex(RealmConflict, "has not expired"):
            self.service.resume_expired_session(
                operation_id="interface-session/resume-live",
                handle=self.handle,
            )

        released = self.service.close_capture(
            operation_id="interface-session/resume-terminal/release",
            handle=self.handle,
        )
        self.assertEqual(released.lease.state.value, "released")
        with self.assertRaisesRegex(RealmConflict, "exact time-expired"):
            self.service.resume_expired_session(
                operation_id="interface-session/resume-released",
                handle=released,
            )

        revoked = self.service.create_session(
            operation_id="interface-session/resume-terminal/create-revoked",
            launch_id="launch-revoked",
        )
        # There is intentionally no public generic revoke path for this
        # reserved lease kind.  Materialize the terminal ledger state only to
        # exercise the resume transaction's fail-closed integrity branch.
        with sqlite3.connect(self.ledger.database_path) as connection:
            connection.execute(
                "UPDATE leases SET state = 'revoked' WHERE lease_id = ?",
                (revoked.lease.lease_id,),
            )
        with self.assertRaisesRegex(RealmConflict, "exact time-expired"):
            self.service.resume_expired_session(
                operation_id="interface-session/resume-revoked",
                handle=revoked,
            )

    def test_resume_materializes_inflight_capture_failed_before_new_fence(
        self,
    ) -> None:
        record = InterfaceOutputRecord.from_dict(self._record())
        begun = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/resume-inflight/begin",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
            attempt_ttl_seconds=3600,
            attempt_id="resume-inflight-attempt",
            operation_prefix="resume-inflight-ops",
        )
        self.assertEqual(begun.state.value, "sealing")
        self._expire_session_lease()

        resumed = self.service.resume_expired_session(
            operation_id="interface-session/resume-inflight",
            handle=self.handle,
            ttl_seconds=3600,
        )

        failed = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.attempt_id, begun.attempt_id)
        self.assertEqual(failed.error_code, "session_ended")
        self.assertGreater(
            resumed.lease.fencing_token,
            self.handle.lease.fencing_token,
        )

    def test_transient_capture_failure_is_visible_and_retryable(self) -> None:
        record = InterfaceOutputRecord.from_dict(self._record())
        with self.assertRaises(ContentRejected):
            self.service.capture_generation(
                handle=self.handle,
                record=record,
                root_handles={"output": self.output_root},
            )
        failed = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.attempt_number, 1)
        self.assertEqual(failed.error_code, "content_rejected")
        self.assertNotIn(str(self.root), json.dumps(failed.to_dict()))

        self._write_generation()
        ready = self.service.capture_generation(
            handle=self.handle,
            record=record,
            root_handles={"output": self.output_root},
        )
        status = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(status.state.value, "ready")
        self.assertEqual(status.attempt_number, 2)
        self.assertEqual(status.ready_generation, ready)

    def test_one_fenced_capture_is_in_flight_per_session(self) -> None:
        first = InterfaceOutputRecord.from_dict(self._record())
        second = InterfaceOutputRecord.from_dict(
            self._record(id="generated-project-02", path="generation-02")
        )
        status = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/begin-first",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=first,
        )
        with self.assertRaisesRegex(RealmConflict, "in progress"):
            self.ledger.begin_interface_output_capture(
                operation_id="interface-session/begin-second",
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
                lease_id=self.handle.lease.lease_id,
                holder_id=self.handle.lease.holder_id,
                fencing_token=self.handle.lease.fencing_token,
                record=second,
            )
        self.ledger.fail_interface_output_capture(
            operation_id="interface-session/fail-first",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            output_id=first.output_id,
            attempt_id=status.attempt_id,
            attempt_number=status.attempt_number,
            error_code="test_failure",
        )
        second_status = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/retry-second",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=second,
        )
        self.assertEqual(second_status.state.value, "sealing")

    def test_byte_quota_is_persisted_and_enforced_at_commit(self) -> None:
        tiny_service = RealmInterfaceOutputSessionService(
            self.ledger,
            self.content,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            max_session_bytes=1,
        )
        tiny = tiny_service.create_session(
            operation_id="interface-session/tiny-create",
            launch_id="launch-tiny",
        )
        (self.output_root / "artifact.txt").write_text("too large", encoding="utf-8")
        record = InterfaceOutputRecord.from_dict(
            self._record(
                id="tiny-file",
                label="Tiny file",
                kind="file",
                path="artifact.txt",
            )
        )

        with self.assertRaisesRegex(RealmConflict, "byte limit"):
            tiny_service.capture_generation(
                handle=tiny,
                record=record,
                root_handles={"output": self.output_root},
            )
        status = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=tiny.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(status.state.value, "failed")
        self.assertEqual(status.error_code, "realm_conflict")
        persisted = self.ledger.read_interface_output_session(
            actor_principal_id="operator",
            session_id=tiny.session.session_id,
        )
        self.assertEqual(persisted.max_logical_bytes, 1)

    def test_empty_session_retirement_is_exactly_replayable(self) -> None:
        first = self.service.retire_session(
            operation_id="interface-session/empty-retire",
            handle=self.handle,
        )
        replay = self.service.retire_session(
            operation_id="interface-session/empty-retire",
            handle=self.handle,
        )
        self.assertEqual(first, replay)
        self.assertEqual(first.owner_revision, first.previous_owner_revision + 1)
        self.assertEqual(first.released_memberships, 0)

    def test_file_output_has_selection_with_explicit_tree_action_answer(self) -> None:
        (self.output_root / "artifact.txt").write_text("result", encoding="utf-8")
        record = InterfaceOutputRecord.from_dict(
            self._record(
                id="generated-file-01",
                label="Generated report",
                kind="file",
                path="artifact.txt",
            )
        )
        generation = self.service.capture_generation(
            handle=self.handle,
            record=record,
            root_handles={"output": self.output_root},
        )
        self.assertIsNotNone(generation.selection)
        selection_service = RealmSelectionActionService(self.ledger, self.principal)
        opened = selection_service.open_read_only(selection=generation.selection)
        self.assertFalse(opened.eligibility.supported)
        self.assertEqual(opened.eligibility.code, "file_artifact_not_tree")

    def test_expired_attempt_cannot_commit_and_is_materialized_failed(self) -> None:
        self._write_generation()
        record = InterfaceOutputRecord.from_dict(self._record())
        status = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/expiring-begin",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
            attempt_ttl_seconds=60,
            attempt_id="expiring-attempt",
            operation_prefix="expiring-attempt-ops",
        )
        capture = self.content.capture(
            actor_principal_id="operator",
            change_id=status.change_id,
            store_id=self.store.store_id,
        )
        sealed = seal_interface_output_generation(
            capture,
            record=record,
            root_handles={"output": self.output_root},
            operation_id="interface-session/expiring-seal",
        )
        membership = OwnerMembership(
            self.store.store_id,
            sealed.content_ref,
            INTERFACE_OUTPUT_SESSION_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id="interface-session/expiring-hold",
            actor_principal_id="operator",
            change_id=status.change_id,
            memberships=(membership,),
        )

        assert status.attempt_expires_at is not None
        with mock.patch(
            "optpilot.realm.ledger.time.time",
            return_value=status.attempt_expires_at + 1,
        ):
            with self.assertRaisesRegex(RealmExpired, "attempt expired"):
                self.ledger.commit_interface_output_generation(
                    operation_id="interface-session/expiring-commit",
                    actor_principal_id="operator",
                    session_id=self.handle.session.session_id,
                    lease_id=self.handle.lease.lease_id,
                    holder_id=self.handle.lease.holder_id,
                    fencing_token=self.handle.lease.fencing_token,
                    output_id=record.output_id,
                    attempt_id=status.attempt_id,
                    attempt_number=status.attempt_number,
                    change_id=status.change_id,
                    sealed=sealed,
                    store_id=self.store.store_id,
                )
        failed = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(failed.state.value, "failed")
        self.assertEqual(failed.error_code, "attempt_expired")
        memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=self.handle.session.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        self.assertNotIn(membership, memberships)

    def test_attempt_identity_cannot_be_reused_or_used_by_stale_worker(self) -> None:
        record = InterfaceOutputRecord.from_dict(self._record())
        first = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/reuse-begin-1",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
            attempt_id="fixed-attempt",
            operation_prefix="fixed-attempt-ops",
        )
        self.ledger.fail_interface_output_capture(
            operation_id="interface-session/reuse-fail-1",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            output_id=record.output_id,
            attempt_id=first.attempt_id,
            attempt_number=first.attempt_number,
            error_code="test_failure",
        )
        with self.assertRaisesRegex(RealmConflict, "identity was reused"):
            self.ledger.begin_interface_output_capture(
                operation_id="interface-session/reuse-begin-2",
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
                lease_id=self.handle.lease.lease_id,
                holder_id=self.handle.lease.holder_id,
                fencing_token=self.handle.lease.fencing_token,
                record=record,
                attempt_id="fixed-attempt",
                operation_prefix="different-attempt-ops",
            )
        second = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/reuse-begin-3",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
            attempt_id="replacement-attempt",
            operation_prefix="replacement-attempt-ops",
        )
        with self.assertRaisesRegex(RealmConflict, "stale"):
            self.ledger.fail_interface_output_capture(
                operation_id="interface-session/stale-fail",
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
                lease_id=self.handle.lease.lease_id,
                holder_id=self.handle.lease.holder_id,
                fencing_token=self.handle.lease.fencing_token,
                output_id=record.output_id,
                attempt_id=first.attempt_id,
                attempt_number=first.attempt_number,
                error_code="stale_worker",
            )
        current = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(current.attempt_id, second.attempt_id)
        self.assertEqual(current.state.value, "sealing")

    def test_persisted_attempt_can_be_resumed_after_supervisor_restart(self) -> None:
        self._write_generation()
        record = InterfaceOutputRecord.from_dict(self._record())
        begun = self.ledger.begin_interface_output_capture(
            operation_id="interface-session/crash-begin",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
            attempt_id="crash-attempt",
            operation_prefix="crash-attempt-ops",
        )
        restarted = RealmInterfaceOutputSessionService(
            self.ledger,
            self.content,
            actor_principal_id="operator",
            store_id=self.store.store_id,
        )
        recovered_handle = restarted.recover_session(launch_id="launch-01")
        generation = restarted.resume_generation(
            handle=recovered_handle,
            output_id=record.output_id,
            root_handles={"output": self.output_root},
        )
        self.assertEqual(generation.output_id, record.output_id)
        self.assertEqual(
            begun.change_id,
            self.ledger.read_interface_output_status(
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
                output_id=record.output_id,
            ).change_id,
        )

    def test_release_closes_inflight_capture_before_retirement(self) -> None:
        record = InterfaceOutputRecord.from_dict(self._record())
        self.ledger.begin_interface_output_capture(
            operation_id="interface-session/release-inflight-begin",
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            lease_id=self.handle.lease.lease_id,
            holder_id=self.handle.lease.holder_id,
            fencing_token=self.handle.lease.fencing_token,
            record=record,
        )
        retired = self.service.retire_session(
            operation_id="interface-session/release-inflight",
            handle=self.handle,
        )
        status = self.ledger.read_interface_output_status(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
            output_id=record.output_id,
        )
        self.assertEqual(retired.session.state.value, "retired")
        self.assertEqual(status.state.value, "failed")
        self.assertEqual(status.error_code, "session_ended")

    def test_bad_control_record_does_not_starve_later_generation(self) -> None:
        self._write_generation(name="generation-02")
        self._write_control(
            self._record(id="missing-generation", path="missing"),
            self._record(id="ready-generation", path="generation-02"),
        )
        captured = self.service.capture_control_file(
            handle=self.handle,
            control_file=self.control,
            root_handles={"output": self.output_root},
        )
        statuses = self.ledger.list_interface_output_statuses(
            actor_principal_id="operator",
            session_id=self.handle.session.session_id,
        )
        self.assertEqual([item.output_id for item in captured], ["ready-generation"])
        self.assertEqual(
            {item.output_id: item.state.value for item in statuses},
            {"missing-generation": "failed", "ready-generation": "ready"},
        )

    def test_terminal_drain_rejects_missing_record_behind_other_supervisor_capture(
        self,
    ) -> None:
        """A later in-flight record cannot hide an earlier missing record."""

        self._write_generation(name="generation-a", text="print('a')\n")
        self._write_generation(name="generation-b", text="print('b')\n")
        self._write_control(
            self._record(id="generation-a", path="generation-a"),
            self._record(id="generation-b", path="generation-b"),
        )
        record_lines: dict[str, int] = {}
        final_records = self.service.read_control_file(
            self.control,
            record_lines=record_lines,
        )

        other_ledger = RealmLedger(self.ledger.database_path)
        other_store = LocalContentStore(
            self.store.root,
            store_id=self.store.store_id,
        )
        try:
            other_content = RealmContentService(
                other_ledger,
                local_stores={other_store.store_id: other_store},
            )
            other_service = RealmInterfaceOutputSessionService(
                other_ledger,
                other_content,
                actor_principal_id=self.principal.principal_id,
                store_id=other_store.store_id,
            )
            other_handle = other_service.recover_session(launch_id="launch-01")
            later = final_records[1]
            other_ledger.begin_interface_output_capture(
                operation_id="interface-session/two-supervisor/later-begin",
                actor_principal_id="operator",
                session_id=other_handle.session.session_id,
                lease_id=other_handle.lease.lease_id,
                holder_id=other_handle.lease.holder_id,
                fencing_token=other_handle.lease.fencing_token,
                record=later,
                attempt_ttl_seconds=60,
                attempt_id="ioa-two-supervisor-later",
                operation_prefix="iop-two-supervisor-later",
            )

            # Supervisor A accepts both final records.  Its earlier A capture
            # is blocked by B's later in-flight attempt; it must not adopt B's
            # work on this deterministic pass.
            with mock.patch.object(
                self.service,
                "resume_generation",
                side_effect=RealmConflict("other supervisor is sealing"),
            ):
                first_pass = self.service.capture_records(
                    handle=self.handle,
                    records=final_records,
                    root_handles={"output": self.output_root},
                    record_lines=record_lines,
                )
            self.assertEqual(first_pass.accepted_records, final_records)
            self.assertEqual(first_pass.generations, ())

            # B commits before A attempts its durable close.  There is no
            # longer a SEALING row, so only exact final-record coverage can
            # detect that A was never registered.
            committed_b = other_service.resume_generation(
                handle=other_handle,
                output_id=later.output_id,
                root_handles={"output": self.output_root},
            )
            self.assertEqual(committed_b.output_id, later.output_id)
            with self.assertRaisesRegex(
                InterfaceOutputDrainPending,
                "coverage is incomplete",
            ):
                self.service.close_capture(
                    operation_id="interface-session/two-supervisor/close",
                    handle=self.handle,
                    require_drained=True,
                    final_records=first_pass.accepted_records,
                )
            still_active = self.service.recover_session(launch_id="launch-01")
            self.assertEqual(still_active.lease.state.value, "active")

            second_pass = self.service.capture_records(
                handle=still_active,
                records=final_records,
                root_handles={"output": self.output_root},
                record_lines=record_lines,
            )
            self.assertEqual(
                [item.output_id for item in second_pass.generations],
                ["generation-a", "generation-b"],
            )
            closed = self.service.close_capture(
                operation_id="interface-session/two-supervisor/close",
                handle=still_active,
                require_drained=True,
                final_records=second_pass.accepted_records,
            )
            self.assertEqual(closed.lease.state.value, "released")
        finally:
            other_store.close()
            other_ledger.close()

    def test_rewritten_durable_id_is_rejected_not_permanent_drain_debt(self) -> None:
        self._write_generation()
        original = InterfaceOutputRecord.from_dict(self._record())
        self.service.capture_generation(
            handle=self.handle,
            record=original,
            root_handles={"output": self.output_root},
        )
        self._write_generation(name="generation-02")
        self._write_control(
            self._record(
                label="Rewritten declaration",
                path="generation-02",
            )
        )
        rejected = []
        lines: dict[str, int] = {}
        parsed = self.service.read_control_file(
            self.control,
            rejected_records=rejected,
            record_lines=lines,
        )
        capture_pass = self.service.capture_records(
            handle=self.handle,
            records=parsed,
            root_handles={"output": self.output_root},
            rejected_records=rejected,
            record_lines=lines,
        )
        self.assertEqual(capture_pass.accepted_records, ())
        self.assertEqual(
            [item.to_dict() for item in rejected],
            [{"line": 1, "code": "conflicting_output_id"}],
        )
        closed = self.service.close_capture(
            operation_id="interface-session/rewrite/close",
            handle=self.handle,
            require_drained=True,
            final_records=capture_pass.accepted_records,
        )
        self.assertEqual(closed.lease.state.value, "released")

    def test_commit_and_drained_close_race_has_only_safe_outcomes(self) -> None:
        self._write_generation()
        record = InterfaceOutputRecord.from_dict(self._record())
        other_ledger = RealmLedger(self.ledger.database_path)
        other_store = LocalContentStore(
            self.store.root,
            store_id=self.store.store_id,
        )
        try:
            other_service = RealmInterfaceOutputSessionService(
                other_ledger,
                RealmContentService(
                    other_ledger,
                    local_stores={other_store.store_id: other_store},
                ),
                actor_principal_id="operator",
                store_id=other_store.store_id,
            )
            other_handle = other_service.recover_session(launch_id="launch-01")
            other_ledger.begin_interface_output_capture(
                operation_id="interface-session/commit-close-race/begin",
                actor_principal_id="operator",
                session_id=other_handle.session.session_id,
                lease_id=other_handle.lease.lease_id,
                holder_id=other_handle.lease.holder_id,
                fencing_token=other_handle.lease.fencing_token,
                record=record,
                attempt_ttl_seconds=60,
                attempt_id="ioa-commit-close-race",
                operation_prefix="iop-commit-close-race",
            )
            barrier = threading.Barrier(2)
            committed = []
            failures: list[BaseException] = []

            def commit() -> None:
                try:
                    barrier.wait(timeout=5)
                    committed.append(
                        other_service.resume_generation(
                            handle=other_handle,
                            output_id=record.output_id,
                            root_handles={"output": self.output_root},
                        )
                    )
                except BaseException as error:  # pragma: no cover - assertion path
                    failures.append(error)

            worker = threading.Thread(target=commit, daemon=True)
            worker.start()
            barrier.wait(timeout=5)
            try:
                closed = self.service.close_capture(
                    operation_id="interface-session/commit-close-race/close",
                    handle=self.handle,
                    require_drained=True,
                    final_records=(record,),
                )
            except InterfaceOutputDrainPending:
                closed = None
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual([item.output_id for item in committed], [record.output_id])
            if closed is None:
                active = self.service.recover_session(launch_id="launch-01")
                self.assertEqual(active.lease.state.value, "active")
                closed = self.service.close_capture(
                    operation_id="interface-session/commit-close-race/close",
                    handle=active,
                    require_drained=True,
                    final_records=(record,),
                )
            self.assertEqual(closed.lease.state.value, "released")
            ready = self.ledger.read_interface_output_generation(
                actor_principal_id="operator",
                session_id=self.handle.session.session_id,
                output_id=record.output_id,
            )
            self.assertEqual(ready.output_id, record.output_id)
        finally:
            other_store.close()
            other_ledger.close()


class RealmInterfaceOutputSessionMigrationTest(unittest.TestCase):
    def test_populated_v31_session_upgrades_and_resumes_with_higher_fence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "realm.sqlite3"
            store = LocalContentStore(root / "store", store_id="migration-store")
            legacy: RealmLedger | None = None
            upgraded: RealmLedger | None = None
            try:
                with (
                    mock.patch.object(ledger_module, "_CURRENT_SCHEMA_VERSION", 31),
                    mock.patch.object(
                        ledger_module,
                        "_MIGRATIONS",
                        ledger_module._MIGRATIONS[:31],
                    ),
                ):
                    legacy = RealmLedger(database)
                legacy.register_principal(
                    operation_id="interface-session/v31/principal",
                    principal_id="operator",
                    kind="human",
                )
                legacy.register_store(
                    operation_id="interface-session/v31/store",
                    store_id=store.store_id,
                    backend_kind=store.BACKEND_KIND,
                    root_marker=store.root_marker,
                )
                legacy_service = RealmInterfaceOutputSessionService(
                    legacy,
                    RealmContentService(
                        legacy,
                        local_stores={store.store_id: store},
                    ),
                    actor_principal_id="operator",
                    store_id=store.store_id,
                )
                legacy_handle = legacy_service.create_session(
                    operation_id="interface-session/v31/create",
                    launch_id="launch-v31",
                    ttl_seconds=3600,
                )
                with sqlite3.connect(database) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        31,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM realm_meta "
                            "WHERE key = 'schema_version'"
                        ).fetchone(),
                        ("31",),
                    )
                legacy.close()
                legacy = None

                upgraded = RealmLedger(database)
                upgraded_service = RealmInterfaceOutputSessionService(
                    upgraded,
                    RealmContentService(
                        upgraded,
                        local_stores={store.store_id: store},
                    ),
                    actor_principal_id="operator",
                    store_id=store.store_id,
                )
                recovered = upgraded_service.recover_session(
                    launch_id="launch-v31"
                )
                self.assertEqual(recovered, legacy_handle)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        35,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM realm_meta "
                            "WHERE key = 'schema_version'"
                        ).fetchone(),
                        ("35",),
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT version FROM schema_migrations "
                            "WHERE version = 32"
                        ).fetchone(),
                        (32,),
                    )
                    connection.execute(
                        "UPDATE leases SET expires_at = created_at "
                        "WHERE lease_id = ?",
                        (recovered.lease.lease_id,),
                    )

                resumed = upgraded_service.resume_expired_session(
                    operation_id="interface-session/v31/resume-after-upgrade",
                    handle=recovered,
                    ttl_seconds=3600,
                )
                self.assertNotEqual(
                    resumed.lease.lease_id,
                    recovered.lease.lease_id,
                )
                self.assertGreater(
                    resumed.lease.fencing_token,
                    recovered.lease.fencing_token,
                )
                self.assertEqual(
                    resumed.session.session_lease_id,
                    resumed.lease.lease_id,
                )
                with sqlite3.connect(database) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT state FROM leases WHERE lease_id = ?",
                            (recovered.lease.lease_id,),
                        ).fetchone(),
                        ("expired",),
                    )
            finally:
                if legacy is not None:
                    legacy.close()
                if upgraded is not None:
                    upgraded.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
