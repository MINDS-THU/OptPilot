"""Integration contracts for the general exact-selection workspace creator."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import SnapshotRef, request_digest
from optpilot.realm.workspace_assembly import (
    workspace_source_prefix,
    WorkspaceAssemblyConflict,
    WorkspaceFocus,
    WorkspaceRequestSource,
    WorkspaceSelectionSeed,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class RealmWorkspaceCreationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:workspace-creation-service-test",
        )
        self.addCleanup(self.runtime.close)
        self.actor = self.runtime.actor_principal_id
        self.counter = 0

    def _op(self, label: str) -> str:
        self.counter += 1
        return f"workspace-creation-service/{self.counter}/{label}"

    def _publish_package(
        self,
        label: str,
        *,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
    ) -> tuple[Any, Any]:
        self.counter += 1
        token = f"{self.counter}-{label}"
        source = self.root / f"source-{token}"
        source.mkdir()
        for relative_path, value in files.items():
            target = source / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")

        owner_id = f"workspace-creation-artifact-{token}"
        package_id = f"workspace-creation-package-{token}"
        publisher_id = f"workspace-creation-publisher/{token}"
        self.runtime.ledger.create_owner(
            operation_id=self._op(f"create-artifact-{token}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id=self.actor,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self._op(f"begin-artifact-{token}"),
            actor_principal_id=self.actor,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=self.actor,
            change_id=change.change_id,
            store_id=self.runtime.content_store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source),
            operation_id=self._op(f"seal-artifact-{token}"),
        )
        membership = OwnerMembership(
            self.runtime.content_store.store_id,
            sealed.snapshot_ref,
            PACKAGE_ARTIFACT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self._op(f"hold-artifact-{token}"),
            actor_principal_id=self.actor,
            change_id=change.change_id,
            memberships=(membership,),
        )
        committed = self.runtime.ledger.commit_owner_change(
            operation_id=self._op(f"commit-artifact-{token}"),
            actor_principal_id=self.actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        identity = {
            "artifact_ref": str(membership.content_ref),
            "package_id": package_id,
            "publisher_id": publisher_id,
        }
        published = self.runtime.catalog.publish(
            operation_id=self._op(f"publish-package-{token}"),
            package_id=package_id,
            publisher_id=publisher_id,
            source_owner_id=owner_id,
            expected_source_owner_revision=committed.owner_revision,
            source_store_id=membership.store_id,
            source_role=membership.role,
            root_ref=membership.content_ref,
            owned_paths=owned_paths,
            plan_digest=request_digest({"plan": identity}),
            validation_digest=request_digest({"validation": identity}),
            smoke_digest=request_digest({"smoke": identity}),
            expected_head=None,
        )
        selection = self.runtime.ledger.mint_catalog_package_application_selection(
            actor_principal_id=self.actor,
            package_id=package_id,
            publisher_id=publisher_id,
        )
        return selection, published

    @staticmethod
    def _seed(*selections: Any) -> WorkspaceSelectionSeed:
        return WorkspaceSelectionSeed.build(
            [
                WorkspaceRequestSource.build(selection=selection)
                for selection in selections
            ]
        )

    def _content_counts(self) -> dict[str, int]:
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            return {
                str(kind): int(count)
                for kind, count in connection.execute(
                    "SELECT kind, COUNT(*) FROM content_objects GROUP BY kind"
                )
            }

    def _request_state(self, operation_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            connection.row_factory = sqlite3.Row
            request = connection.execute(
                "SELECT request_digest, workspace_id, owner_id "
                "FROM workspace_assembly_requests "
                "WHERE client_operation_id = ?",
                (operation_id,),
            ).fetchone()
            self.assertIsNotNone(request)
            assert request is not None
            workspace_count = connection.execute(
                "SELECT COUNT(*) FROM managed_workspaces WHERE workspace_id = ?",
                (request["workspace_id"],),
            ).fetchone()[0]
            target_owner = connection.execute(
                "SELECT state FROM owners WHERE owner_id = ?",
                (request["owner_id"],),
            ).fetchone()
            attempts = tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT attempt.attempt_id, attempt.state, "
                    "owner.state AS owner_state, change.state AS change_state "
                    "FROM workspace_assembly_attempts attempt "
                    "JOIN owners owner ON owner.owner_id = attempt.owner_id "
                    "JOIN owner_transactions change "
                    "ON change.change_id = attempt.change_id "
                    "WHERE attempt.request_digest = ? "
                    "ORDER BY attempt.created_at, attempt.attempt_id",
                    (request["request_digest"],),
                )
            )
        return {
            "attempts": attempts,
            "owner_id": request["owner_id"],
            "target_owner_state": (
                None if target_owner is None else target_owner["state"]
            ),
            "workspace_count": int(workspace_count),
            "workspace_id": request["workspace_id"],
        }

    def _assert_failed_creation_is_inactive(
        self,
        operation_id: str,
        *,
        expected_attempts: int,
    ) -> dict[str, Any]:
        state = self._request_state(operation_id)
        self.assertEqual(state["workspace_count"], 0)
        self.assertIsNone(state["target_owner_state"])
        self.assertEqual(len(state["attempts"]), expected_attempts)
        for attempt in state["attempts"]:
            self.assertNotEqual(attempt["state"], "active")
            self.assertNotEqual(attempt["owner_state"], "active")
            self.assertNotEqual(attempt["change_state"], "active")
        return state

    def test_single_catalog_selection_adopts_without_content_publication(
        self,
    ) -> None:
        selection, published = self._publish_package(
            "adopt",
            files={"resources/simulator/run.py": "VALUE = 1\n"},
            owned_paths=("resources/simulator",),
        )
        before_counts = self._content_counts()
        before_refs = tuple(self.runtime.content_store.iter_live_refs())

        with (
            mock.patch.object(
                self.runtime.content_service,
                "compose_tree",
                side_effect=AssertionError("adoption must not compose content"),
            ),
            mock.patch.object(
                LocalContentStore,
                "_publish_blob_from_fd",
                side_effect=AssertionError("adoption must not publish blobs"),
            ),
        ):
            created = self.runtime.editable_workspaces.create_workspace(
                operation_id="workspace-creation/adopt-exact-package",
                title="Adopt exact package",
                seed=self._seed(selection),
            )

        workspace, revision = self.runtime.ledger.read_workspace(
            actor_principal_id=self.actor,
            workspace_id=created.workspace_id,
        )
        self.assertEqual(created.outcome, "adopt")
        self.assertFalse(created.recovered)
        self.assertEqual(created.source_count, 1)
        self.assertEqual(workspace.current_revision, 1)
        self.assertEqual(revision.root_ref, SnapshotRef.parse(selection.entity_ref))
        self.assertEqual(revision.root_ref, published.manifest.root_ref)
        self.assertEqual(self._content_counts(), before_counts)
        self.assertEqual(
            tuple(self.runtime.content_store.iter_live_refs()), before_refs
        )

    def test_two_catalog_packages_union_one_tree_without_blob_copy_and_open(
        self,
    ) -> None:
        environment, _ = self._publish_package(
            "environment",
            files={
                "environments/toy/environment.yaml": "config: environment\n",
                "environments/toy/evaluator.py": "def evaluate(): return 1\n",
            },
            owned_paths=("environments/toy",),
        )
        method, _ = self._publish_package(
            "method",
            files={
                "methods/search/method.yaml": "config: method\n",
                "methods/search/method.py": "class Method: pass\n",
            },
            owned_paths=("methods/search",),
        )
        before_counts = self._content_counts()
        before_refs = set(self.runtime.content_store.iter_live_refs())

        with mock.patch.object(
            LocalContentStore,
            "_publish_blob_from_fd",
            side_effect=AssertionError("workspace union must not publish blobs"),
        ):
            created = self.runtime.editable_workspaces.create_workspace(
                operation_id="workspace-creation/union-two-packages",
                title="Environment and method",
                seed=self._seed(environment, method),
            )

        after_create_counts = self._content_counts()
        self.assertEqual(created.outcome, "union")
        self.assertEqual(created.source_count, 2)
        self.assertEqual(
            after_create_counts.get("blob", 0), before_counts.get("blob", 0)
        )
        self.assertEqual(
            after_create_counts.get("tree", 0), before_counts.get("tree", 0) + 1
        )
        workspace, revision = self.runtime.ledger.read_workspace(
            actor_principal_id=self.actor,
            workspace_id=created.workspace_id,
        )
        self.assertEqual(workspace.current_revision, 1)
        self.assertEqual(
            set(self.runtime.content_store.iter_live_refs()) - before_refs,
            {revision.root_ref},
        )

        checkout = self.runtime.editable_workspaces.open_workspace(
            operation_id=self._op("open-union"),
            workspace_id=created.workspace_id,
            expected_workspace_revision=1,
        )
        self.assertEqual(
            (checkout.root_path / f"{workspace_source_prefix(environment)}/environments/toy/environment.yaml").read_text(
                encoding="utf-8"
            ),
            "config: environment\n",
        )
        self.assertEqual(
            (checkout.root_path / f"{workspace_source_prefix(method)}/methods/search/method.yaml").read_text(
                encoding="utf-8"
            ),
            "config: method\n",
        )
        self.assertEqual(self._content_counts(), after_create_counts)

    def test_exact_operation_replay_recovers_without_resolving_sources(self) -> None:
        selection, _ = self._publish_package(
            "replay",
            files={"resources/replay/value.txt": "replay\n"},
            owned_paths=("resources/replay",),
        )
        operation_id = "workspace-creation/replay-exact-request"
        seed = self._seed(selection)
        created = self.runtime.editable_workspaces.create_workspace(
            operation_id=operation_id,
            title="Replay exact request",
            seed=seed,
        )

        with mock.patch.object(
            self.runtime.content_service,
            "verify_selection_tree_manifest",
            side_effect=AssertionError("completed replay must not read its source"),
        ):
            replayed = self.runtime.editable_workspaces.create_workspace(
                operation_id=operation_id,
                title="Replay exact request",
                seed=seed,
            )

        self.assertFalse(created.recovered)
        self.assertTrue(replayed.recovered)
        self.assertEqual(replayed.workspace_id, created.workspace_id)
        self.assertEqual(replayed.workspace_revision, created.workspace_revision)
        self.assertEqual(replayed.outcome, created.outcome)
        self.assertEqual(replayed.source_count, created.source_count)
        self.assertEqual(replayed.assembly_digest, created.assembly_digest)

    def test_changed_intent_under_same_operation_conflicts_before_source_read(
        self,
    ) -> None:
        selection, _ = self._publish_package(
            "changed-intent",
            files={"resources/intent/value.txt": "intent\n"},
            owned_paths=("resources/intent",),
        )
        operation_id = "workspace-creation/changed-intent"
        seed = self._seed(selection)
        created = self.runtime.editable_workspaces.create_workspace(
            operation_id=operation_id,
            title="Original title",
            seed=seed,
        )

        with mock.patch.object(
            self.runtime.content_service,
            "verify_selection_tree_manifest",
            side_effect=AssertionError("changed intent must fail at request binding"),
        ):
            with self.assertRaisesRegex(RealmConflict, "different request"):
                self.runtime.editable_workspaces.create_workspace(
                    operation_id=operation_id,
                    title="Changed title",
                    seed=seed,
                )

        workspaces = self.runtime.editable_workspaces.list_workspaces()
        self.assertEqual(
            [item.workspace_id for item in workspaces], [created.workspace_id]
        )

    def test_oversized_semantic_request_is_rejected_before_ledger_binding(
        self,
    ) -> None:
        selection, _ = self._publish_package(
            "oversized-request",
            files={"resources/large/value.txt": "large\n"},
            owned_paths=("resources/large",),
        )
        focuses = tuple(
            WorkspaceFocus(
                kind="resource",
                focus_id=("x" * 500) + f"{index:03d}",
                relative_path=f"focus/{index}.yaml",
            )
            for index in range(200)
        )
        seed = WorkspaceSelectionSeed.build(
            (
                WorkspaceRequestSource.build(
                    selection=selection,
                    focuses=focuses,
                ),
            )
        )

        with mock.patch.object(
            self.runtime.ledger,
            "bind_workspace_assembly_request",
        ) as bind_request:
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                self.runtime.editable_workspaces.create_workspace(
                    operation_id="workspace-creation/oversized-semantic-request",
                    title="Oversized semantic request",
                    seed=seed,
                )
        bind_request.assert_not_called()

    def test_identical_concurrent_union_has_one_composer_and_both_recover(
        self,
    ) -> None:
        left, _ = self._publish_package(
            "concurrent-left",
            files={"resources/left/value.txt": "left\n"},
            owned_paths=("resources/left",),
        )
        right, _ = self._publish_package(
            "concurrent-right",
            files={"resources/right/value.txt": "right\n"},
            owned_paths=("resources/right",),
        )
        operation_id = "workspace-creation/concurrent-identical-union"
        seed = self._seed(left, right)
        composer_entered = threading.Event()
        follower_claimed = threading.Event()
        release_composer = threading.Event()
        call_lock = threading.Lock()
        compose_call_count = 0
        original_compose = self.runtime.content_service.compose_tree
        original_begin = self.runtime.ledger.begin_workspace_assembly_attempt

        def blocked_compose(*args: Any, **kwargs: Any):
            nonlocal compose_call_count
            with call_lock:
                compose_call_count += 1
            composer_entered.set()
            if not release_composer.wait(timeout=10):
                raise AssertionError("test did not release the workspace composer")
            return original_compose(*args, **kwargs)

        def observed_begin(*args: Any, **kwargs: Any):
            claim = original_begin(*args, **kwargs)
            if not claim.composer:
                follower_claimed.set()
            return claim

        def create():
            return self.runtime.editable_workspaces.create_workspace(
                operation_id=operation_id,
                title="Concurrent exact union",
                seed=seed,
                ttl_seconds=30,
            )

        with (
            mock.patch.object(
                self.runtime.content_service,
                "compose_tree",
                side_effect=blocked_compose,
            ),
            mock.patch.object(
                self.runtime.ledger,
                "begin_workspace_assembly_attempt",
                side_effect=observed_begin,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            composer_future = executor.submit(create)
            self.assertTrue(composer_entered.wait(timeout=10))
            follower_future = executor.submit(create)
            try:
                self.assertTrue(follower_claimed.wait(timeout=10))
                with call_lock:
                    self.assertEqual(compose_call_count, 1)
            finally:
                release_composer.set()
            composer = composer_future.result(timeout=20)
            follower = follower_future.result(timeout=20)

        self.assertEqual(composer.workspace_id, follower.workspace_id)
        self.assertEqual(composer.assembly_digest, follower.assembly_digest)
        self.assertFalse(composer.recovered)
        self.assertTrue(follower.recovered)
        state = self._request_state(operation_id)
        self.assertEqual(state["workspace_count"], 1)
        self.assertEqual(
            [attempt["state"] for attempt in state["attempts"]],
            ["promoted"],
        )

    def test_follower_timeout_leaves_live_composer_for_safe_replay(self) -> None:
        left, _ = self._publish_package(
            "timeout-left",
            files={"resources/left/value.txt": "left\n"},
            owned_paths=("resources/left",),
        )
        right, _ = self._publish_package(
            "timeout-right",
            files={"resources/right/value.txt": "right\n"},
            owned_paths=("resources/right",),
        )
        operation_id = "workspace-creation/follower-timeout"
        seed = self._seed(left, right)
        composer_entered = threading.Event()
        follower_claimed = threading.Event()
        release_composer = threading.Event()
        compose_call_count = 0
        call_lock = threading.Lock()
        original_compose = self.runtime.content_service.compose_tree
        original_begin = self.runtime.ledger.begin_workspace_assembly_attempt

        def blocked_compose(*args: Any, **kwargs: Any):
            nonlocal compose_call_count
            with call_lock:
                compose_call_count += 1
            composer_entered.set()
            if not release_composer.wait(timeout=10):
                raise AssertionError("test did not release the workspace composer")
            return original_compose(*args, **kwargs)

        def observed_begin(*args: Any, **kwargs: Any):
            claim = original_begin(*args, **kwargs)
            if not claim.composer:
                follower_claimed.set()
            return claim

        def create():
            return self.runtime.editable_workspaces.create_workspace(
                operation_id=operation_id,
                title="Follower timeout union",
                seed=seed,
                ttl_seconds=30,
            )

        with (
            mock.patch(
                "optpilot.realm.editable_workspace_service."
                "_WORKSPACE_ASSEMBLY_FOLLOWER_WAIT_SECONDS",
                0.05,
            ),
            mock.patch.object(
                self.runtime.content_service,
                "compose_tree",
                side_effect=blocked_compose,
            ),
            mock.patch.object(
                self.runtime.ledger,
                "begin_workspace_assembly_attempt",
                side_effect=observed_begin,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            composer_future = executor.submit(create)
            self.assertTrue(composer_entered.wait(timeout=10))
            follower_future = executor.submit(create)
            self.assertTrue(follower_claimed.wait(timeout=10))
            try:
                with self.assertRaisesRegex(
                    RealmConflict,
                    "still in progress; retry the same operation",
                ):
                    follower_future.result(timeout=5)
                state_while_composing = self._request_state(operation_id)
                self.assertEqual(state_while_composing["workspace_count"], 0)
                self.assertEqual(
                    [
                        (
                            attempt["state"],
                            attempt["owner_state"],
                            attempt["change_state"],
                        )
                        for attempt in state_while_composing["attempts"]
                    ],
                    [("active", "active", "active")],
                )
                with call_lock:
                    self.assertEqual(compose_call_count, 1)
            finally:
                release_composer.set()
            composer = composer_future.result(timeout=20)

        replay = create()
        self.assertFalse(composer.recovered)
        self.assertTrue(replay.recovered)
        self.assertEqual(replay.workspace_id, composer.workspace_id)

    def test_two_packages_sharing_a_name_are_kept_apart(self) -> None:
        # One package has a file where the other has a folder. Before each
        # package got its own folder this was fatal to the pairing; now the
        # two simply never meet.
        file_source, _ = self._publish_package(
            "file-collision",
            files={"resources/shared": "a file\n"},
            owned_paths=("resources/shared",),
        )
        directory_source, _ = self._publish_package(
            "directory-collision",
            files={"resources/shared/child.txt": "a child\n"},
            owned_paths=("resources/shared",),
        )

        created = self.runtime.editable_workspaces.create_workspace(
            operation_id="workspace-creation/file-directory-kept-apart",
            title="Two packages",
            seed=self._seed(file_source, directory_source),
        )

        self.assertEqual(created.outcome, "union")
        checkout = self.runtime.editable_workspaces.open_workspace(
            operation_id=self._op("open-kept-apart"),
            workspace_id=created.workspace_id,
            expected_workspace_revision=1,
        )
        self.assertTrue(
            (checkout.root_path / f"{workspace_source_prefix(file_source)}/resources/shared").is_file()
        )
        self.assertTrue(
            (
                checkout.root_path
                / f"{workspace_source_prefix(directory_source)}/resources/shared/child.txt"
            ).is_file()
        )

    def test_a_refused_assembly_leaves_no_workspace_or_attempt(self) -> None:
        # Which conflict refused it is the assembly module's business; what
        # matters here is that nothing is left behind when it does. Two
        # sources claiming one folder is the live case (two revisions of one
        # package), and it is raised from the same place as any other.
        left, _ = self._publish_package(
            "cleanup-left",
            files={"resources/left/left.txt": "left\n"},
            owned_paths=("resources/left",),
        )
        right, _ = self._publish_package(
            "cleanup-right",
            files={"resources/right/right.txt": "right\n"},
            owned_paths=("resources/right",),
        )
        operation_id = "workspace-creation/refused-assembly"
        before_counts = self._content_counts()

        def refuse(*args: Any, **kwargs: Any):
            raise WorkspaceAssemblyConflict(
                code="source-prefix",
                path="shared",
                other_path="shared",
                left_root_ref=left.entity_ref_parsed
                if hasattr(left, "entity_ref_parsed")
                else SnapshotRef.parse(left.entity_ref),
                right_root_ref=SnapshotRef.parse(right.entity_ref),
            )

        with mock.patch(
            "optpilot.realm.editable_workspace_service.compile_workspace_assembly",
            side_effect=refuse,
        ):
            with self.assertRaisesRegex(WorkspaceAssemblyConflict, "source folder"):
                self.runtime.editable_workspaces.create_workspace(
                    operation_id=operation_id,
                    title="Refused assembly",
                    seed=self._seed(left, right),
                )

        self.assertEqual(self._content_counts(), before_counts)
        self._assert_failed_creation_is_inactive(operation_id, expected_attempts=0)
    def test_finalization_failure_aborts_attempt_owner_and_retry_succeeds(
        self,
    ) -> None:
        left, _ = self._publish_package(
            "finalize-left",
            files={"resources/left/value.txt": "left\n"},
            owned_paths=("resources/left",),
        )
        right, _ = self._publish_package(
            "finalize-right",
            files={"resources/right/value.txt": "right\n"},
            owned_paths=("resources/right",),
        )
        operation_id = "workspace-creation/injected-finalization-failure"
        seed = self._seed(left, right)

        with mock.patch.object(
            self.runtime.ledger,
            "finalize_workspace_assembly",
            side_effect=RuntimeError("injected finalization failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected finalization failure"):
                self.runtime.editable_workspaces.create_workspace(
                    operation_id=operation_id,
                    title="Retryable union",
                    seed=seed,
                )

        failed = self._assert_failed_creation_is_inactive(
            operation_id, expected_attempts=1
        )
        self.assertEqual(failed["attempts"][0]["state"], "aborted")
        self.assertEqual(failed["attempts"][0]["owner_state"], "deleted")

        retried = self.runtime.editable_workspaces.create_workspace(
            operation_id=operation_id,
            title="Retryable union",
            seed=seed,
        )
        self.assertEqual(retried.outcome, "union")
        self.assertFalse(retried.recovered)
        final_state = self._request_state(operation_id)
        self.assertEqual(final_state["workspace_count"], 1)
        self.assertEqual(
            [attempt["state"] for attempt in final_state["attempts"]],
            ["aborted", "promoted"],
        )
        self.assertTrue(
            all(
                attempt["owner_state"] == "deleted"
                and attempt["change_state"] != "active"
                for attempt in final_state["attempts"]
            )
        )


if __name__ == "__main__":
    unittest.main()
