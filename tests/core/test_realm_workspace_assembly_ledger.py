"""Durability and authority tests for whole-tree workspace assembly."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot.realm.service import TreeCompositionSource
from optpilot.realm.workspace_assembly import (
    WorkspaceAssemblyRequest,
    WorkspaceRequestSource,
    WorkspaceSeed,
    WorkspaceSeedSource,
    WorkspaceSelectionSeed,
    WorkspaceSourceAnchor,
    compile_workspace_assembly,
)
from optpilot.realm.workspaces import (
    WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE,
    WORKSPACE_REVISION_ROLE,
    WorkspaceAssemblyLineage,
    WorkspaceLineage,
)


class RealmWorkspaceAssemblyLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.realm_root = self.root / "realm"
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id="local-user:workspace-assembly-ledger",
        )
        self.addCleanup(self.runtime.close)
        self.actor = self.runtime.actor_principal_id
        self.index = 0

    def _op(self, label: str) -> str:
        self.index += 1
        return f"workspace-assembly-ledger/{self.index}/{label}"

    def _source(self, label: str, files: dict[str, str]):
        ledger = self.runtime.ledger
        store = self.runtime.content_store
        source_root = self.root / f"source-{label}"
        source_root.mkdir()
        for relative_path, value in files.items():
            target = source_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")
        source_owner_id = f"assembly-source-owner-{label}"
        workspace_id = f"assembly-source-workspace-{label}"
        workspace_owner_id = f"assembly-source-workspace-owner-{label}"
        ledger.create_owner(
            operation_id=self._op(f"create-source-owner-{label}"),
            owner_id=source_owner_id,
            owner_kind="resource",
            principal_id=self.actor,
        )
        change = ledger.begin_owner_change(
            operation_id=self._op(f"begin-source-{label}"),
            actor_principal_id=self.actor,
            owner_id=source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=self.actor,
            change_id=change.change_id,
            store_id=store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source_root),
            operation_id=self._op(f"seal-source-{label}"),
        )
        source_membership = OwnerMembership(
            store.store_id,
            sealed.snapshot_ref,
            "assembly-source-root",
        )
        ledger.hold_owner_content(
            operation_id=self._op(f"hold-source-{label}"),
            actor_principal_id=self.actor,
            change_id=change.change_id,
            memberships=(source_membership,),
        )
        source_commit = ledger.commit_owner_change(
            operation_id=self._op(f"commit-source-{label}"),
            actor_principal_id=self.actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )
        ledger.create_workspace_from_snapshot(
            operation_id=self._op(f"create-source-workspace-{label}"),
            actor_principal_id=self.actor,
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_commit.owner_revision,
            title=f"Assembly source {label}",
            root=OwnerMembership(
                store.store_id,
                sealed.snapshot_ref,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id=source_owner_id,
                source_id=source_owner_id,
                source_revision=source_commit.owner_revision,
                source_store_id=store.store_id,
                source_ref=sealed.snapshot_ref,
            ),
            workspace_id=workspace_id,
            owner_id=workspace_owner_id,
        )
        selection = ledger.mint_workspace_selection(
            actor_principal_id=self.actor,
            workspace_id=workspace_id,
            expected_workspace_revision=1,
        )
        membership, manifest = (
            self.runtime.content_service.verify_selection_tree_manifest(
                actor_principal_id=self.actor,
                selection=selection,
            )
        )
        return {
            "selection": selection,
            "membership": membership,
            "manifest": manifest,
            "workspace_id": workspace_id,
        }

    def _request_and_result(self, operation_id: str, sources, *, actor=None):
        actor = self.actor if actor is None else actor
        request = WorkspaceAssemblyRequest(
            operation_id=operation_id,
            actor_principal_id=actor,
            workspace_id=f"assembled-{operation_id.rsplit('/', 1)[-1]}",
            owner_id=f"assembled-owner-{operation_id.rsplit('/', 1)[-1]}",
            title="Assembled workspace",
            seed=WorkspaceSelectionSeed.build(
                [
                    WorkspaceRequestSource.build(selection=item["selection"])
                    for item in sources
                ]
            ),
        )
        evidence_by_digest = {
            item["selection"].selection_digest: item for item in sources
        }
        seed = WorkspaceSeed.build(
            [
                WorkspaceSeedSource(
                    anchor=WorkspaceSourceAnchor.build(
                        selection=source.selection,
                        store_id=self.runtime.content_store.store_id,
                        focuses=source.focuses,
                    ),
                    tree_manifest=evidence_by_digest[source.selection.selection_digest][
                        "manifest"
                    ],
                )
                for source in request.seed.sources
            ]
        )
        return request, compile_workspace_assembly(request, seed)

    def _composition_sources(self, request, sources):
        evidence_by_digest = {
            item["selection"].selection_digest: item for item in sources
        }
        result = []
        seen = set()
        for request_source in request.seed.sources:
            item = evidence_by_digest[request_source.selection.selection_digest]
            value = TreeCompositionSource(
                owner_id=request_source.selection.source_owner_id,
                owner_revision=request_source.selection.owner_revision,
                membership=item["membership"],
            )
            key = (value.owner_id, value.owner_revision, value.membership)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return tuple(result)

    def _begin_and_compose_union(self, request, result, sources, label: str):
        attempt_id = f"workspace-{label}-attempt"
        change = self.runtime.ledger.begin_workspace_assembly_attempt(
            operation_id=self._op(f"begin-{label}"),
            request=request,
            attempt_id=attempt_id,
            owner_id=f"workspace-{label}-attempt-owner",
            change_id=f"workspace-{label}-attempt-change",
            store_id=result.store_id,
            ttl_seconds=60,
        )
        composition_sources = self._composition_sources(request, sources)
        self.runtime.content_service.compose_tree(
            operation_id=self._op(f"compose-{label}"),
            actor_principal_id=self.actor,
            change_id=change.change_id,
            store_id=result.store_id,
            sources=composition_sources,
            manifest=result.tree_manifest,
            hold_membership=OwnerMembership(
                result.store_id,
                result.root_ref,
                WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE,
            ),
        )
        composition_request = {
            "change_id": change.change_id,
            "manifest_ref": str(result.root_ref),
            "schema": "optpilot.tree-composition-request.v1",
            "sources": [
                {
                    "membership": item.membership.to_dict(),
                    "owner_id": item.owner_id,
                    "owner_revision": item.owner_revision,
                }
                for item in composition_sources
            ],
            "store_id": result.store_id,
        }
        return attempt_id, request_digest(composition_request)

    def test_adopt_replays_after_source_retirement_without_source_reads(self) -> None:
        source = self._source("adopt", {"simulator.py": "VALUE = 1\n"})
        operation_id = self._op("final-adopt")
        request, result = self._request_and_result(operation_id, (source,))

        self.assertIsNone(
            self.runtime.ledger.bind_workspace_assembly_request(request=request)
        )
        committed = self.runtime.ledger.finalize_workspace_assembly(
            operation_id=operation_id,
            request=request,
            result=result,
        )
        self.assertIsInstance(committed.revision.lineage, WorkspaceAssemblyLineage)
        self.assertEqual(committed.revision.root_ref, source["manifest"].snapshot_ref)
        self.runtime.ledger.retire_workspace(
            operation_id=self._op("retire-adopt-source"),
            actor_principal_id=self.actor,
            workspace_id=source["workspace_id"],
            expected_workspace_revision=1,
        )

        self.runtime.close()
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.realm_root,
            actor_principal_id=self.actor,
        )
        with mock.patch.object(
            self.runtime.ledger,
            "_resolve_selection_in_txn",
            side_effect=AssertionError("recovery must not resolve mutable sources"),
        ):
            replay = self.runtime.ledger.bind_workspace_assembly_request(
                request=request
            )
            recovered = self.runtime.ledger.recover_workspace_assembly(request=request)
        self.assertEqual(replay, committed)
        self.assertEqual(recovered, committed)

    def test_same_operation_id_with_changed_request_conflicts(self) -> None:
        source = self._source("binding", {"model.py": "MODEL = 1\n"})
        operation_id = self._op("binding")
        request, _result = self._request_and_result(operation_id, (source,))
        self.runtime.ledger.bind_workspace_assembly_request(request=request)

        with self.assertRaises(RealmConflict):
            self.runtime.ledger.bind_workspace_assembly_request(
                request=replace(request, title="A changed title")
            )

    def test_finalize_requires_actor_derive_authority(self) -> None:
        source = self._source("acl", {"environment.py": "ENV = 1\n"})
        other = "local-user:workspace-assembly-other"
        self.runtime.ledger.register_principal(
            operation_id=self._op("register-other"),
            principal_id=other,
            kind="human",
        )
        operation_id = self._op("acl-final")
        request, result = self._request_and_result(operation_id, (source,), actor=other)
        self.runtime.ledger.bind_workspace_assembly_request(request=request)

        with self.assertRaises(RealmNotFound):
            self.runtime.ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=result,
            )
        with sqlite3.connect(self.runtime.ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM managed_workspaces WHERE workspace_id = ?",
                    (request.workspace_id,),
                ).fetchone()
            )

    def test_source_retirement_before_finalize_is_unavailable(self) -> None:
        source = self._source("retired", {"method.py": "METHOD = 1\n"})
        operation_id = self._op("retired-final")
        request, result = self._request_and_result(operation_id, (source,))
        self.runtime.ledger.bind_workspace_assembly_request(request=request)
        self.runtime.ledger.retire_workspace(
            operation_id=self._op("retire-before-final"),
            actor_principal_id=self.actor,
            workspace_id=source["workspace_id"],
            expected_workspace_revision=1,
        )

        with self.assertRaises(RealmNotFound):
            self.runtime.ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=result,
            )

    def test_active_attempt_is_shared_then_expired_attempt_is_replaced(self) -> None:
        first = self._source("attempt-a", {"a.py": "A = 1\n"})
        second = self._source("attempt-b", {"b.py": "B = 1\n"})
        operation_id = self._op("attempt-final")
        request, _result = self._request_and_result(operation_id, (first, second))
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        first_claim = ledger.begin_workspace_assembly_attempt(
            operation_id=self._op("begin-attempt-one"),
            request=request,
            attempt_id="workspace-attempt-one",
            owner_id="workspace-attempt-owner-one",
            change_id="workspace-attempt-change-one",
            store_id=self.runtime.content_store.store_id,
            ttl_seconds=0.1,
        )
        follower_claim = ledger.begin_workspace_assembly_attempt(
            operation_id=self._op("begin-attempt-two"),
            request=request,
            attempt_id="workspace-attempt-two",
            owner_id="workspace-attempt-owner-two",
            change_id="workspace-attempt-change-two",
            store_id=self.runtime.content_store.store_id,
            ttl_seconds=60,
        )
        self.assertTrue(first_claim.composer)
        self.assertFalse(follower_claim.composer)
        self.assertEqual(follower_claim.attempt_id, first_claim.attempt_id)
        self.assertEqual(follower_claim.change_id, first_claim.change_id)

        time.sleep(0.12)
        replacement_claim = ledger.begin_workspace_assembly_attempt(
            operation_id=self._op("begin-attempt-replacement"),
            request=request,
            attempt_id="workspace-attempt-two",
            owner_id="workspace-attempt-owner-two",
            change_id="workspace-attempt-change-two",
            store_id=self.runtime.content_store.store_id,
            ttl_seconds=0.001,
        )
        self.assertTrue(replacement_claim.composer)
        self.assertEqual(replacement_claim.attempt_id, "workspace-attempt-two")
        time.sleep(0.01)
        self.assertEqual(
            ledger.reap_workspace_assembly_attempts(
                operation_id=self._op("reap-attempts"),
                actor_principal_id=self.actor,
                limit=1,
            ),
            ("workspace-attempt-two",),
        )
        with sqlite3.connect(ledger.database_path) as connection:
            states = dict(
                connection.execute(
                    "SELECT attempt_id, state FROM workspace_assembly_attempts"
                )
            )
            owners = dict(
                connection.execute(
                    "SELECT owner_id, state FROM owners "
                    "WHERE owner_id LIKE 'workspace-attempt-owner-%'"
                )
            )
        self.assertEqual(
            states,
            {
                "workspace-attempt-one": "aborted",
                "workspace-attempt-two": "aborted",
            },
        )
        self.assertEqual(set(owners.values()), {"deleted"})

    def test_union_requires_exact_composition_and_promotes_atomically(self) -> None:
        first = self._source("union-a", {"environment/a.py": "A = 1\n"})
        second = self._source("union-b", {"method/b.py": "B = 1\n"})
        sources = (first, second)
        operation_id = self._op("union-final")
        request, result = self._request_and_result(operation_id, sources)
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        attempt = ledger.begin_workspace_assembly_attempt(
            operation_id=self._op("begin-union"),
            request=request,
            attempt_id="workspace-union-attempt",
            owner_id="workspace-union-attempt-owner",
            change_id="workspace-union-attempt-change",
            store_id=result.store_id,
            ttl_seconds=60,
        )
        composition_sources = self._composition_sources(request, sources)
        hold = OwnerMembership(
            result.store_id,
            result.root_ref,
            WORKSPACE_ASSEMBLY_ATTEMPT_ROOT_ROLE,
        )
        self.runtime.content_service.compose_tree(
            operation_id=self._op("compose-union"),
            actor_principal_id=self.actor,
            change_id=attempt.change_id,
            store_id=result.store_id,
            sources=composition_sources,
            manifest=result.tree_manifest,
            hold_membership=hold,
        )
        composition_request = {
            "change_id": attempt.change_id,
            "manifest_ref": str(result.root_ref),
            "schema": "optpilot.tree-composition-request.v1",
            "sources": [
                {
                    "membership": item.membership.to_dict(),
                    "owner_id": item.owner_id,
                    "owner_revision": item.owner_revision,
                }
                for item in composition_sources
            ],
            "store_id": result.store_id,
        }
        committed = ledger.finalize_workspace_assembly(
            operation_id=operation_id,
            request=request,
            result=result,
            attempt_id="workspace-union-attempt",
            composition_request_digest=request_digest(composition_request),
        )

        self.assertEqual(committed.revision.root_ref, result.root_ref)
        self.assertEqual(committed.revision.lineage, result.lineage)
        with sqlite3.connect(ledger.database_path) as connection:
            attempt_row = connection.execute(
                "SELECT state FROM workspace_assembly_attempts "
                "WHERE attempt_id = 'workspace-union-attempt'"
            ).fetchone()
            owner_row = connection.execute(
                "SELECT state FROM owners "
                "WHERE owner_id = 'workspace-union-attempt-owner'"
            ).fetchone()
            active_attempt_roots = connection.execute(
                "SELECT COUNT(*) FROM owner_memberships "
                "WHERE owner_id = 'workspace-union-attempt-owner' "
                "AND removed_revision IS NULL"
            ).fetchone()[0]
        self.assertEqual(attempt_row, ("promoted",))
        self.assertEqual(owner_row, ("deleted",))
        self.assertEqual(active_attempt_roots, 0)

    def test_union_rejects_authorized_blob_rearrangement_at_finalization(
        self,
    ) -> None:
        first = self._source("rearrange-a", {"a.py": "A = 1\n"})
        second = self._source("rearrange-b", {"b.py": "B = 1\n"})
        sources = (first, second)
        operation_id = self._op("rearranged-final")
        request, expected = self._request_and_result(operation_id, sources)
        manifest_by_root = {
            manifest.snapshot_ref: manifest
            for manifest in expected.source_tree_manifests
        }
        resolved_seed = WorkspaceSeed.build(
            tuple(
                WorkspaceSeedSource(
                    anchor=anchor,
                    tree_manifest=manifest_by_root[anchor.root_ref],
                )
                for anchor in expected.lineage.sources
            )
        )
        authorized_file = next(
            entry for entry in first["manifest"].entries if entry.kind == "file"
        )
        assert authorized_file.blob_ref is not None
        assert authorized_file.size is not None
        assert authorized_file.executable is not None
        rearranged_manifest = TreeManifest.build(
            (
                TreeEntry.file(
                    "moved.py",
                    blob_ref=authorized_file.blob_ref,
                    size=authorized_file.size,
                    executable=authorized_file.executable,
                ),
            )
        )
        rearranged_lineage = WorkspaceAssemblyLineage.build(
            request=request,
            seed=resolved_seed,
            outcome="union",
            final_root_ref=rearranged_manifest.snapshot_ref,
        )
        rearranged = replace(
            expected,
            root_ref=rearranged_manifest.snapshot_ref,
            tree_manifest=rearranged_manifest,
            lineage=rearranged_lineage,
        )
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        attempt_id, composition_digest = self._begin_and_compose_union(
            request,
            rearranged,
            sources,
            "rearranged",
        )

        with self.assertRaisesRegex(RealmConflict, "deterministic recompilation"):
            ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=rearranged,
                attempt_id=attempt_id,
                composition_request_digest=composition_digest,
            )
        with sqlite3.connect(ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM managed_workspaces WHERE workspace_id = ?",
                    (request.workspace_id,),
                ).fetchone()
            )

    def test_union_allows_source_head_advance_after_composition(self) -> None:
        first = self._source("advance-a", {"environment.py": "ENV = 1\n"})
        second = self._source("advance-b", {"method.py": "METHOD = 1\n"})
        sources = (first, second)
        operation_id = self._op("advance-final")
        request, result = self._request_and_result(operation_id, sources)
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        attempt_id, composition_digest = self._begin_and_compose_union(
            request, result, sources, "advance"
        )

        checkout = self.runtime.editable_workspaces.open_workspace(
            operation_id=self._op("open-advanced-source"),
            workspace_id=first["workspace_id"],
            expected_workspace_revision=1,
        )
        (checkout.root_path / "unrelated.txt").write_text(
            "new source head\n", encoding="utf-8"
        )
        advanced = self.runtime.editable_workspaces.commit_workspace(
            operation_id=self._op("commit-advanced-source"),
            workspace_id=first["workspace_id"],
            expected_workspace_revision=1,
        )
        self.assertEqual(advanced.current_revision, 2)

        committed = ledger.finalize_workspace_assembly(
            operation_id=operation_id,
            request=request,
            result=result,
            attempt_id=attempt_id,
            composition_request_digest=composition_digest,
        )
        self.assertEqual(committed.revision.root_ref, result.root_ref)

    def test_union_rejects_source_retirement_after_composition(self) -> None:
        first = self._source("remove-a", {"environment.py": "ENV = 1\n"})
        second = self._source("remove-b", {"method.py": "METHOD = 1\n"})
        sources = (first, second)
        operation_id = self._op("remove-final")
        request, result = self._request_and_result(operation_id, sources)
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        attempt_id, composition_digest = self._begin_and_compose_union(
            request, result, sources, "remove"
        )
        ledger.retire_workspace(
            operation_id=self._op("retire-composed-source"),
            actor_principal_id=self.actor,
            workspace_id=first["workspace_id"],
            expected_workspace_revision=1,
        )

        with self.assertRaises(RealmNotFound):
            ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=result,
                attempt_id=attempt_id,
                composition_request_digest=composition_digest,
            )

    def test_union_rejects_wrong_composition_digest_without_partial_workspace(
        self,
    ) -> None:
        first = self._source("wrong-a", {"a.py": "A = 1\n"})
        second = self._source("wrong-b", {"b.py": "B = 1\n"})
        sources = (first, second)
        operation_id = self._op("wrong-composition-final")
        request, result = self._request_and_result(operation_id, sources)
        ledger = self.runtime.ledger
        ledger.bind_workspace_assembly_request(request=request)
        attempt_id, correct_digest = self._begin_and_compose_union(
            request, result, sources, "wrong"
        )

        with self.assertRaises(RealmNotFound):
            ledger.finalize_workspace_assembly(
                operation_id=operation_id,
                request=request,
                result=result,
                attempt_id=attempt_id,
                composition_request_digest="0" * 64,
            )
        with sqlite3.connect(ledger.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM managed_workspaces WHERE workspace_id = ?",
                    (request.workspace_id,),
                ).fetchone()
            )
        committed = ledger.finalize_workspace_assembly(
            operation_id=operation_id,
            request=request,
            result=result,
            attempt_id=attempt_id,
            composition_request_digest=correct_digest,
        )
        self.assertEqual(committed.revision.root_ref, result.root_ref)


if __name__ == "__main__":
    unittest.main()
