from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from optpilot.realm.catalog_publication import CatalogPackageHead
from optpilot.realm.refs import SnapshotRef, canonical_json_bytes, request_digest
from optpilot_studio.ui.coordination_store import (
    ActionState,
    COORDINATION_STORAGE_UNAVAILABLE_MESSAGE,
    CoordinationConflict,
    CoordinationIntegrityError,
    CoordinationNotFound,
    CoordinationStorageUnavailable,
    EntityCoordinate,
    RegistrationCheck,
    RegistrationSetupData,
    RegistrationSetupState,
    RegistrationTestResult,
    StudioCoordinationStore,
    StudyDraftState,
    WorkspacePurpose,
    _sqlite_request_digest,
    coordination_database_path,
    prepare_coordination_database,
    studio_project_state_directory,
)


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


class StudioCoordinationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "state" / "coordination.sqlite3"
        self.clock = _Clock()
        self.store = StudioCoordinationStore(self.database, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _source(
        entity_id: str = "workspace-a", *, revision: int | None = 3
    ) -> EntityCoordinate:
        return EntityCoordinate(
            kind="workspace",
            entity_id=entity_id,
            revision=revision,
            digest="a" * 64,
        )

    @staticmethod
    def _check(*, accepted: bool = True) -> RegistrationCheck:
        return RegistrationCheck(
            workspace_revision=4,
            store_id="local-store",
            artifact_ref=SnapshotRef.from_manifest_bytes(b"checked registration tree"),
            owned_paths=("environments/demo", "resources/notes"),
            accepted=accepted,
            validation_digest=request_digest(
                {"accepted": accepted, "validator": "static-v1"}
            ),
            summary={"errors": 0 if accepted else 1, "files": 7},
            checked_at=1_800_000_100.0,
        )

    @classmethod
    def _checked_setup(
        cls,
        *,
        publication_intent_id: str | None = None,
        test: RegistrationTestResult | None = None,
    ) -> RegistrationSetupData:
        return RegistrationSetupData(
            state=RegistrationSetupState.CHECKED,
            package_id="demo-package",
            catalog_roles=("environment", "resource"),
            publisher_id="configured-package-ingress/publisher-a",
            source_lineage=EntityCoordinate(
                kind="workspace", entity_id="workspace-a", revision=4
            ),
            check=cls._check(),
            test=test,
            expected_catalog_head=None,
            publication_intent_id=publication_intent_id,
        )

    def test_database_is_private_versioned_and_reopens_with_same_identity(self) -> None:
        facts = self.store.diagnostics()
        identity = facts["instance_id"]

        self.assertEqual(facts["schema_version"], 1)
        self.assertEqual(facts["journal_mode"], "wal")
        self.assertEqual(facts["foreign_keys"], 1)
        self.assertEqual(
            coordination_database_path(self.root),
            self.root / ".optpilot-ui" / "studio-coordination.sqlite3",
        )
        if os.name != "nt":
            self.assertEqual(self.database.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

        self.store.close()
        self.store = StudioCoordinationStore(self.database, clock=self.clock)
        self.assertEqual(self.store.instance_id, identity)

    def test_workspace_purpose_upsert_is_stable_and_revision_fenced(self) -> None:
        source = EntityCoordinate(kind="generated-output", entity_id="output-a")
        first = self.store.put_workspace_purpose(
            operation_id="workspace-purpose/create",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
            subject=source,
            label="Generated simulator",
        )
        replay = self.store.put_workspace_purpose(
            operation_id="workspace-purpose/create",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
            subject=source,
            label="Generated simulator",
        )
        semantic_replay = self.store.put_workspace_purpose(
            operation_id="workspace-purpose/reconcile",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
            subject=source,
            label="Generated simulator",
        )

        self.assertEqual(replay, first)
        self.assertEqual(semantic_replay, first)
        self.assertEqual(first.revision, 1)
        with self.assertRaises(CoordinationConflict):
            self.store.put_workspace_purpose(
                operation_id="workspace-purpose/stale",
                workspace_id="workspace-a",
                purpose=WorkspacePurpose.USER_PROJECT,
                subject=source,
                label="Renamed",
                expected_revision=2,
            )
        updated = self.store.put_workspace_purpose(
            operation_id="workspace-purpose/update",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
            subject=source,
            label="Renamed",
            expected_revision=1,
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(
            self.store.list_workspace_purposes(
                purpose=WorkspacePurpose.USER_PROJECT
            ),
            (updated,),
        )

    def test_operation_id_rejects_a_different_canonical_request(self) -> None:
        self.store.put_workspace_purpose(
            operation_id="same-operation",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
        )
        with self.assertRaisesRegex(CoordinationConflict, "another request"):
            self.store.put_workspace_purpose(
                operation_id="same-operation",
                workspace_id="workspace-b",
                purpose=WorkspacePurpose.USER_PROJECT,
            )
        with self.assertRaises(CoordinationNotFound):
            self.store.get_workspace_purpose("workspace-b")

    def test_explicit_study_draft_and_hidden_workspace_commit_atomically(self) -> None:
        draft = self.store.save_study_draft(
            operation_id="draft/save/one",
            draft_id="draft-a",
            actor_id="local-user:a",
            title="Job shop search",
            workspace_id="workspace-draft-a",
            workspace_revision=2,
            study_relative_path="studies/draft-a.yaml",
            config_digest="b" * 64,
        )
        purpose = self.store.get_workspace_purpose("workspace-draft-a")

        self.assertEqual(draft.state, StudyDraftState.ACTIVE)
        self.assertEqual(purpose.purpose, WorkspacePurpose.STUDY_DRAFT_BACKING)
        self.assertEqual(purpose.subject.entity_id, "draft-a")  # type: ignore[union-attr]
        self.assertEqual(
            self.store.list_study_drafts(actor_id="local-user:a"), (draft,)
        )

        self.store.close()
        self.store = StudioCoordinationStore(self.database, clock=self.clock)
        self.assertEqual(self.store.get_study_draft("draft-a"), draft)

        updated = self.store.save_study_draft(
            operation_id="draft/save/two",
            draft_id="draft-a",
            actor_id="local-user:a",
            title="Job shop search v2",
            workspace_id="workspace-draft-a",
            workspace_revision=3,
            study_relative_path="studies/draft-a.yaml",
            config_digest="c" * 64,
            expected_revision=1,
        )
        discarded = self.store.discard_study_draft(
            operation_id="draft/discard",
            draft_id="draft-a",
            actor_id="local-user:a",
            expected_revision=updated.revision,
        )
        self.assertEqual(discarded.state, StudyDraftState.DISCARDED)
        self.assertEqual(self.store.list_study_drafts(actor_id="local-user:a"), ())
        self.assertEqual(
            self.store.list_study_drafts(
                actor_id="local-user:a", include_discarded=True
            ),
            (discarded,),
        )
        with self.assertRaisesRegex(CoordinationConflict, "reactivated"):
            self.store.save_study_draft(
                operation_id="draft/reactivate",
                draft_id="draft-a",
                actor_id="local-user:a",
                title="Again",
                workspace_id="workspace-draft-a",
                workspace_revision=4,
                study_relative_path="studies/draft-a.yaml",
                config_digest="d" * 64,
                expected_revision=discarded.revision,
            )

    def test_study_draft_conflict_rolls_back_every_table_and_receipt(self) -> None:
        self.store.put_workspace_purpose(
            operation_id="workspace/user-project",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
        )
        before = self.store.diagnostics()["records"]
        with self.assertRaisesRegex(CoordinationConflict, "another product purpose"):
            self.store.save_study_draft(
                operation_id="draft/conflicting-purpose",
                draft_id="draft-a",
                actor_id="local-user:a",
                title="Draft",
                workspace_id="workspace-a",
                workspace_revision=1,
                study_relative_path="studies/draft.yaml",
                config_digest="a" * 64,
            )
        after = self.store.diagnostics()["records"]

        self.assertEqual(after, before)
        with self.assertRaises(CoordinationNotFound):
            self.store.get_study_draft("draft-a")

    def test_action_recovers_uncertain_work_and_reuses_core_operation(self) -> None:
        intent = self.store.begin_action(
            operation_id="action/begin",
            intent_id="click-123",
            actor_id="local-user:a",
            action_kind="workspace-creation",
            source=EntityCoordinate(
                kind="generated-output",
                entity_id="output-a",
                revision=1,
                digest="d" * 64,
            ),
            parameters={"title": "Simulator", "selection": {"path": "project"}},
        )
        self.assertEqual(intent.state, ActionState.PENDING)
        self.assertRegex(intent.core_operation_id, r"^studio/action/v1/[0-9a-f]{64}$")

        uncertain = self.store.mark_action_uncertain(
            operation_id="action/uncertain",
            intent_id=intent.intent_id,
            message="Core response was lost",
        )
        self.assertEqual(uncertain.state, ActionState.UNCERTAIN)
        stable_operation_id = uncertain.core_operation_id

        self.store.close()
        self.store = StudioCoordinationStore(self.database, clock=self.clock)
        recovered = self.store.get_action(intent.intent_id)
        self.assertEqual(recovered, uncertain)
        retried = self.store.retry_action(
            operation_id="action/retry", intent_id=intent.intent_id
        )
        self.assertEqual(retried.state, ActionState.PENDING)
        self.assertEqual(retried.attempt_count, 2)
        self.assertEqual(retried.core_operation_id, stable_operation_id)

        result = EntityCoordinate(
            kind="workspace", entity_id="workspace-result", revision=1
        )
        completed = self.store.complete_action(
            operation_id="action/complete",
            intent_id=intent.intent_id,
            result=result,
            core_receipt={"workspace_id": "workspace-result", "revision": 1},
        )
        replay = self.store.complete_action(
            operation_id="action/complete",
            intent_id=intent.intent_id,
            result=result,
            core_receipt={"revision": 1, "workspace_id": "workspace-result"},
        )
        self.assertEqual(replay, completed)
        self.assertEqual(completed.state, ActionState.SUCCEEDED)
        self.assertEqual(
            self.store.retry_action(
                operation_id="action/retry-after-success", intent_id=intent.intent_id
            ),
            completed,
        )

    def test_action_failure_requires_explicit_retry_before_late_success(self) -> None:
        intent = self.store.begin_action(
            operation_id="failure/begin",
            intent_id="failure-a",
            actor_id="local-user:a",
            action_kind="exact-reevaluation",
            source=EntityCoordinate(kind="candidate", entity_id="candidate-a"),
            parameters={"run_id": "run-a"},
        )
        failed = self.store.fail_action(
            operation_id="failure/fail",
            intent_id=intent.intent_id,
            error_code="invalid-source",
            error_message="The Candidate is no longer available.",
        )
        self.assertEqual(failed.state, ActionState.FAILED)
        with self.assertRaisesRegex(CoordinationConflict, "retried"):
            self.store.complete_action(
                operation_id="failure/late-complete",
                intent_id=intent.intent_id,
                result=EntityCoordinate(kind="run", entity_id="run-child"),
            )
        retried = self.store.retry_action(
            operation_id="failure/retry", intent_id=intent.intent_id
        )
        self.assertEqual(retried.core_operation_id, failed.core_operation_id)
        completed = self.store.complete_action(
            operation_id="failure/complete",
            intent_id=intent.intent_id,
            result=EntityCoordinate(kind="run", entity_id="run-child"),
        )
        self.assertEqual(completed.state, ActionState.SUCCEEDED)

    def test_core_operation_id_cannot_be_shared_by_different_intents(self) -> None:
        first = self.store.begin_action(
            operation_id="shared-core/first",
            intent_id="first-intent",
            actor_id="local-user:a",
            action_kind="workspace-creation",
            source=EntityCoordinate(kind="catalog-item", entity_id="item-a"),
            parameters={},
            core_operation_id="existing-core-operation",
        )
        with self.assertRaisesRegex(CoordinationConflict, "another action intent"):
            self.store.begin_action(
                operation_id="shared-core/second",
                intent_id="second-intent",
                actor_id="local-user:a",
                action_kind="workspace-creation",
                source=EntityCoordinate(kind="catalog-item", entity_id="item-b"),
                parameters={},
                core_operation_id=first.core_operation_id,
            )
        self.assertEqual(
            len(self.store.list_actions(actor_id="local-user:a")), 1
        )

    def test_concurrent_replay_creates_one_action(self) -> None:
        second = StudioCoordinationStore(self.database, clock=_Clock())
        barrier = threading.Barrier(2)

        def begin(store: StudioCoordinationStore):
            barrier.wait(timeout=5)
            return store.begin_action(
                operation_id="concurrent/begin",
                intent_id="concurrent-intent",
                actor_id="local-user:a",
                action_kind="workspace-creation",
                source=EntityCoordinate(kind="catalog-item", entity_id="resource-a"),
                parameters={"mode": "editable"},
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                records = tuple(executor.map(begin, (self.store, second)))
        finally:
            second.close()

        self.assertEqual(records[0], records[1])
        facts = self.store.diagnostics()["records"]
        self.assertEqual(facts["action_intents"], 1)
        self.assertEqual(facts["operations"], 1)

    def test_setup_is_reopenable_revisioned_and_bound_to_publication_action(self) -> None:
        configuring = self.store.save_registration_setup(
            operation_id="setup/configure",
            actor_id="local-user:a",
            workspace_id="workspace-a",
            data=RegistrationSetupData(
                state=RegistrationSetupState.CONFIGURING,
                package_id="demo-package",
                catalog_roles=("environment",),
                publisher_id="configured-package-ingress/publisher-a",
                source_lineage=EntityCoordinate(
                    kind="workspace", entity_id="workspace-a", revision=3
                ),
            ),
        )
        self.assertEqual(
            self.store.get_workspace_purpose("workspace-a").purpose,
            WorkspacePurpose.USER_PROJECT,
        )
        checked_data = self._checked_setup()
        checked = self.store.save_registration_setup(
            operation_id="setup/check",
            actor_id="local-user:a",
            workspace_id="workspace-a",
            data=checked_data,
            expected_revision=configuring.revision,
        )
        self.assertEqual(checked.revision, 2)
        self.assertEqual(
            self.store.save_registration_setup(
                operation_id="setup/check/reconcile",
                actor_id="local-user:a",
                workspace_id="workspace-a",
                data=checked_data,
            ),
            checked,
        )
        with self.assertRaises(CoordinationConflict):
            self.store.save_registration_setup(
                operation_id="setup/stale",
                actor_id="local-user:a",
                workspace_id="workspace-a",
                data=RegistrationSetupData(),
                expected_revision=1,
            )

        publication = self.store.begin_action(
            operation_id="publication/begin",
            intent_id="publish-workspace-a",
            actor_id="local-user:a",
            action_kind="catalog-publication",
            source=EntityCoordinate(
                kind="workspace", entity_id="workspace-a", revision=4
            ),
            parameters={
                "artifact_ref": str(checked_data.check.artifact_ref),  # type: ignore[union-attr]
                "package_id": "demo-package",
            },
        )
        linked_data = self._checked_setup(publication_intent_id=publication.intent_id)
        linked = self.store.save_registration_setup(
            operation_id="setup/link-publication",
            actor_id="local-user:a",
            workspace_id="workspace-a",
            data=linked_data,
            expected_revision=checked.revision,
        )
        result = EntityCoordinate(
            kind="catalog-package", entity_id="demo-package", revision=1
        )
        self.store.complete_action(
            operation_id="publication/complete",
            intent_id=publication.intent_id,
            result=result,
            core_receipt={"outcome": "published"},
        )
        registered_data = RegistrationSetupData(
            **{
                **linked_data.__dict__,
                "state": RegistrationSetupState.REGISTERED,
                "publication_result": result,
            }
        )
        registered = self.store.save_registration_setup(
            operation_id="setup/registered",
            actor_id="local-user:a",
            workspace_id="workspace-a",
            data=registered_data,
            expected_revision=linked.revision,
        )

        self.store.close()
        self.store = StudioCoordinationStore(self.database, clock=self.clock)
        self.assertEqual(
            self.store.get_registration_setup(
                actor_id="local-user:a", workspace_id="workspace-a"
            ),
            registered,
        )
        self.assertEqual(
            self.store.get_registration_setup_by_id(registered.setup_id), registered
        )

    def test_setup_rejects_invalid_phase_and_unbound_publication_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "lacks its exact identity"):
            RegistrationSetupData(state=RegistrationSetupState.CHECKED)
        with self.assertRaisesRegex(ValueError, "lacks its exact identity"):
            RegistrationSetupData(
                state=RegistrationSetupState.CHECKED,
                package_id="demo-package",
                catalog_roles=(),
                publisher_id="publisher-a",
                source_lineage=self._source(),
                check=self._check(),
            )
        failed_unclassified = RegistrationSetupData(
            state=RegistrationSetupState.CHECK_FAILED,
            package_id="demo-package",
            catalog_roles=(),
            publisher_id="publisher-a",
            source_lineage=self._source(),
            check=self._check(accepted=False),
        )
        self.assertEqual(
            failed_unclassified.state, RegistrationSetupState.CHECK_FAILED
        )
        self.assertEqual(
            RegistrationSetupData.from_dict(failed_unclassified.to_dict()),
            failed_unclassified,
        )
        with self.assertRaisesRegex(ValueError, "cannot be accepted"):
            RegistrationSetupData(
                state=RegistrationSetupState.CHECK_FAILED,
                package_id="demo-package",
                catalog_roles=("environment",),
                publisher_id="publisher-a",
                source_lineage=self._source(),
                check=self._check(accepted=True),
            )
        failed_test = RegistrationTestResult(
            accepted=False,
            result_digest="f" * 64,
            summary={"failure": "smoke"},
            tested_at=1_800_000_201.0,
        )
        with self.assertRaisesRegex(ValueError, "failed Test"):
            self._checked_setup(
                publication_intent_id="publication-a", test=failed_test
            )

        publication_result = EntityCoordinate(
            kind="catalog-package", entity_id="demo-package", revision=1
        )
        invalid_link = RegistrationSetupData(
            state=RegistrationSetupState.REGISTERED,
            package_id="demo-package",
            catalog_roles=("environment", "resource"),
            publisher_id="configured-package-ingress/publisher-a",
            source_lineage=EntityCoordinate(
                kind="workspace", entity_id="workspace-a", revision=4
            ),
            check=self._check(),
            publication_intent_id="missing-action",
            publication_result=publication_result,
        )
        with self.assertRaisesRegex(CoordinationConflict, "not durable"):
            self.store.save_registration_setup(
                operation_id="setup/invalid-action-link",
                actor_id="local-user:a",
                workspace_id="workspace-a",
                data=invalid_link,
            )
        self.assertEqual(self.store.diagnostics()["records"]["workspace_purposes"], 0)

    def test_registration_types_preserve_exact_realm_refs_and_catalog_head(self) -> None:
        head = CatalogPackageHead(
            package_id="demo-package",
            revision=7,
            owner_id="catalog-owner",
            manifest_digest="e" * 64,
            updated_txn_id=11,
            updated_at=1_800_000_200.0,
        )
        test = RegistrationTestResult(
            accepted=True,
            result_digest="f" * 64,
            summary={"scenarios": 3},
            tested_at=1_800_000_201.0,
        )
        data = RegistrationSetupData(
            **{
                **self._checked_setup(test=test).__dict__,
                "expected_catalog_head": head,
            }
        )
        restored = RegistrationSetupData.from_dict(data.to_dict())

        self.assertEqual(restored, data)
        self.assertIsInstance(restored.check.artifact_ref, SnapshotRef)  # type: ignore[union-attr]
        self.assertEqual(restored.expected_catalog_head, head)

    def test_bounds_and_canonical_paths_are_rejected_before_writing(self) -> None:
        with self.assertRaises(ValueError):
            EntityCoordinate(kind="Workspace", entity_id="a")
        with self.assertRaises(ValueError):
            self.store.save_study_draft(
                operation_id="invalid/path",
                draft_id="draft-a",
                actor_id="local-user:a",
                title="Draft",
                workspace_id="workspace-a",
                workspace_revision=1,
                study_relative_path="../outside.yaml",
                config_digest="a" * 64,
            )
        with self.assertRaises(ValueError):
            self.store.begin_action(
                operation_id="invalid/json",
                intent_id="intent-a",
                actor_id="local-user:a",
                action_kind="workspace-creation",
                source=EntityCoordinate(kind="catalog-item", entity_id="a"),
                parameters={"not_finite": float("nan")},
            )
        self.assertEqual(self.store.diagnostics()["records"]["operations"], 0)

    def test_sql_constraints_and_read_validation_fail_closed_on_corruption(self) -> None:
        record = self.store.put_workspace_purpose(
            operation_id="corruption/create",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
        )
        with sqlite3.connect(self.database) as connection:
            connection.create_function(
                "studio_request_digest",
                1,
                _sqlite_request_digest,
                deterministic=True,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE workspace_purpose_records SET record_json = ? "
                    "WHERE workspace_id = ?",
                    ("{not-json", record.workspace_id),
                )

            payload = record.to_dict()
            payload["label"] = "tampered"
            tampered = canonical_json_bytes(payload).decode("utf-8")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE workspace_purpose_records SET record_json = ? "
                "WHERE workspace_id = ?",
                (tampered, record.workspace_id),
            )
            connection.commit()

        with self.assertRaisesRegex(CoordinationIntegrityError, "digest"):
            self.store.get_workspace_purpose(record.workspace_id)

    def test_replayed_operation_fails_closed_if_its_receipt_is_corrupt(self) -> None:
        self.store.put_workspace_purpose(
            operation_id="receipt/create",
            workspace_id="workspace-a",
            purpose=WorkspacePurpose.USER_PROJECT,
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE coordination_operations SET receipt_json = ? "
                "WHERE operation_id = ?",
                ('{"receipt_version":2}', "receipt/create"),
            )
            connection.commit()

        with self.assertRaisesRegex(
            CoordinationIntegrityError, "receipt version is unsupported"
        ):
            self.store.put_workspace_purpose(
                operation_id="receipt/create",
                workspace_id="workspace-a",
                purpose=WorkspacePurpose.USER_PROJECT,
            )

    def test_unknown_or_changed_schema_history_is_rejected(self) -> None:
        self.store.close()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE coordination_schema_migrations SET migration_digest = ? "
                "WHERE version = 1",
                ("0" * 64,),
            )
            connection.commit()
        with self.assertRaisesRegex(CoordinationIntegrityError, "migration 1 changed"):
            StudioCoordinationStore(self.database)

    def test_unversioned_nonempty_database_is_not_adopted(self) -> None:
        other = self.root / "unversioned" / "coordination.sqlite3"
        other.parent.mkdir()
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
        with self.assertRaisesRegex(CoordinationIntegrityError, "unversioned non-empty"):
            StudioCoordinationStore(other)


class StudioCoordinationMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "projects" / "project-a"
        self.project.mkdir(parents=True)
        self.authority = self.root / "local-authority"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_state_path_is_deterministic_and_authority_local(self) -> None:
        first = studio_project_state_directory(
            self.project, authority_root=self.authority
        )
        second = studio_project_state_directory(
            self.project / ".", authority_root=self.authority
        )
        other = studio_project_state_directory(
            self.root / "projects" / "project-b", authority_root=self.authority
        )

        self.assertEqual(first, second)
        self.assertEqual(first.parent, self.authority / "studio" / "projects")
        self.assertRegex(first.name, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, other)
        self.assertEqual(
            coordination_database_path(
                self.project, authority_root=self.authority
            ),
            first / "studio-coordination.sqlite3",
        )
        self.assertEqual(
            coordination_database_path(self.project),
            self.project / ".optpilot-ui" / "studio-coordination.sqlite3",
        )

    def test_migration_copies_committed_wal_and_preserves_legacy(self) -> None:
        legacy = coordination_database_path(self.project)
        legacy_store = StudioCoordinationStore(legacy, clock=_Clock())
        keeper: sqlite3.Connection | None = None
        migrated_store: StudioCoordinationStore | None = None
        try:
            keeper = sqlite3.connect(legacy)
            keeper.execute("PRAGMA journal_mode = WAL")
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            record = legacy_store.put_workspace_purpose(
                operation_id="legacy/wal-record",
                workspace_id="workspace-from-wal",
                purpose=WorkspacePurpose.USER_PROJECT,
                label="Committed in WAL",
            )
            legacy_identity = legacy_store.instance_id
            legacy_wal = Path(f"{legacy}-wal")
            self.assertTrue(legacy_wal.is_file())
            before_database = legacy.read_bytes()
            before_wal = legacy_wal.read_bytes()

            target = prepare_coordination_database(
                self.project, authority_root=self.authority
            )
            migrated_store = StudioCoordinationStore(target, clock=_Clock())

            self.assertEqual(migrated_store.instance_id, legacy_identity)
            self.assertEqual(
                migrated_store.get_workspace_purpose("workspace-from-wal"), record
            )
            self.assertEqual(legacy.read_bytes(), before_database)
            self.assertEqual(legacy_wal.read_bytes(), before_wal)
        finally:
            if migrated_store is not None:
                migrated_store.close()
            if keeper is not None:
                keeper.close()
            legacy_store.close()

    def test_existing_target_wins_without_inspecting_or_overwriting_it(self) -> None:
        legacy = coordination_database_path(self.project)
        target = coordination_database_path(
            self.project, authority_root=self.authority
        )
        legacy_store = StudioCoordinationStore(legacy, clock=_Clock())
        target_store = StudioCoordinationStore(target, clock=_Clock())
        try:
            legacy_store.put_workspace_purpose(
                operation_id="legacy/record",
                workspace_id="legacy-workspace",
                purpose=WorkspacePurpose.USER_PROJECT,
            )
            target_record = target_store.put_workspace_purpose(
                operation_id="target/record",
                workspace_id="target-workspace",
                purpose=WorkspacePurpose.USER_PROJECT,
            )
            target_identity = target_store.instance_id

            self.assertEqual(
                prepare_coordination_database(
                    self.project, authority_root=self.authority
                ),
                target,
            )
            self.assertEqual(target_store.instance_id, target_identity)
            self.assertEqual(
                target_store.get_workspace_purpose("target-workspace"), target_record
            )
            with self.assertRaises(CoordinationNotFound):
                target_store.get_workspace_purpose("legacy-workspace")
        finally:
            target_store.close()
            legacy_store.close()

    def test_invalid_legacy_history_is_rejected_and_left_in_place(self) -> None:
        legacy = coordination_database_path(self.project)
        store = StudioCoordinationStore(legacy, clock=_Clock())
        store.close()
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                "UPDATE coordination_schema_migrations SET migration_digest = ? "
                "WHERE version = 1",
                ("0" * 64,),
            )
            connection.commit()
        before = legacy.read_bytes()

        with self.assertRaisesRegex(
            CoordinationIntegrityError, "migration 1 changed"
        ):
            prepare_coordination_database(
                self.project, authority_root=self.authority
            )

        self.assertEqual(legacy.read_bytes(), before)
        target = coordination_database_path(
            self.project, authority_root=self.authority
        )
        self.assertFalse(target.exists())
        self.assertEqual(tuple(target.parent.glob("*.tmp")), ())

    def test_migration_fsyncs_copy_and_parent_before_returning(self) -> None:
        legacy = coordination_database_path(self.project)
        store = StudioCoordinationStore(legacy, clock=_Clock())
        store.close()
        from optpilot_studio.ui import coordination_store as module

        file_calls: list[Path] = []
        directory_calls: list[Path] = []
        real_file_fsync = module._fsync_file
        real_directory_fsync = module._fsync_directory

        def record_file(path: Path) -> None:
            file_calls.append(path)
            real_file_fsync(path)

        def record_directory(path: Path) -> None:
            directory_calls.append(path)
            real_directory_fsync(path)

        with mock.patch.object(
            module, "_fsync_file", side_effect=record_file
        ), mock.patch.object(
            module, "_fsync_directory", side_effect=record_directory
        ):
            target = prepare_coordination_database(
                self.project, authority_root=self.authority
            )

        self.assertEqual(len(file_calls), 1)
        self.assertEqual(file_calls[0].parent, target.parent)
        self.assertGreaterEqual(directory_calls.count(target.parent), 2)

    def test_sqlite_operational_errors_are_typed_and_redacted(self) -> None:
        database = self.root / "state" / "coordination.sqlite3"
        store = StudioCoordinationStore(database, clock=_Clock())

        class BrokenConnection:
            def execute(self, *_args: object, **_kwargs: object) -> object:
                raise sqlite3.OperationalError(
                    "disk I/O error at /private/secret/coordination.sqlite3"
                )

            def rollback(self) -> None:
                raise sqlite3.OperationalError("rollback exposed another path")

            def close(self) -> None:
                return None

        try:
            with mock.patch.object(store, "_connect", return_value=BrokenConnection()):
                with self.assertRaises(CoordinationStorageUnavailable) as read_error:
                    store.get_workspace_purpose("workspace-a")
            self.assertEqual(
                str(read_error.exception),
                COORDINATION_STORAGE_UNAVAILABLE_MESSAGE,
            )
            self.assertNotIn("secret", str(read_error.exception))

            with mock.patch.object(store, "_connect", return_value=BrokenConnection()):
                with self.assertRaises(CoordinationStorageUnavailable) as write_error:
                    store.put_workspace_purpose(
                        operation_id="unavailable/write",
                        workspace_id="workspace-a",
                        purpose=WorkspacePurpose.USER_PROJECT,
                    )
            self.assertEqual(
                str(write_error.exception),
                COORDINATION_STORAGE_UNAVAILABLE_MESSAGE,
            )
            self.assertNotIn("secret", str(write_error.exception))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
