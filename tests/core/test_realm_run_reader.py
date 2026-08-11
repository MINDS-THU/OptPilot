"""Authorization and lifecycle checks for canonical Realm run discovery."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.content import LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmIntegrityError, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerPermission
from optpilot.realm.run_catalog import (
    RUN_CATALOG_MAX_PAGE_SIZE,
    RUN_CATALOG_ORDER,
    RUN_CATALOG_PAGE_SCHEMA,
    RUN_CATALOG_PAGE_TOKEN_SCHEMA,
)
from optpilot.realm.run_reader import LocalRealmContext
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.run_views import (
    BORROWED_RUN_VIEW_SCHEMA,
    RUN_VIEW_MINTABLE_SELECTION_KINDS,
    RUN_VIEW_REF_SCHEMA,
    RunViewRef,
)
from optpilot.realm.selections import SelectionRef
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


class RealmRunReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        for principal_id in ("owner", "reader"):
            self.ledger.register_principal(
                operation_id=f"reader/principal/{principal_id}",
                principal_id=principal_id,
                kind="human",
            )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="reader/store/local-a",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        (
            self.closure,
            self.closure_bindings,
            self.source_owner_id,
            self.source_owner_revision,
        ) = prepare_test_run_closure(
            ledger=self.ledger,
            store=self.store,
            root=self.root,
            actor_principal_id="owner",
            prefix="reader",
        )
        self.manifest = prepare_test_run_control_manifest(
            self.closure,
            max_trials=20,
        )
        self.created = {}
        for name in ("hidden", "derive", "bytes", "admin", "metadata"):
            self.created[name] = self._create_run(name=name, actor="owner")

        self.ledger.grant_owner_permission(
            operation_id="reader/grant/derive",
            actor_principal_id="owner",
            owner_id=self.created["derive"].run.owner_id,
            principal_id="reader",
            permission=OwnerPermission.DERIVE,
        )
        self.ledger.grant_owner_permission(
            operation_id="reader/grant/bytes",
            actor_principal_id="owner",
            owner_id=self.created["bytes"].run.owner_id,
            principal_id="reader",
            permission=OwnerPermission.BYTES_READ,
        )
        self.ledger.grant_owner_permission(
            operation_id="reader/grant/admin",
            actor_principal_id="owner",
            owner_id=self.created["admin"].run.owner_id,
            principal_id="reader",
            permission=OwnerPermission.ADMIN,
        )
        self.ledger.grant_owner_permission(
            operation_id="reader/grant/metadata",
            actor_principal_id="owner",
            owner_id=self.created["metadata"].run.owner_id,
            principal_id="reader",
            permission=OwnerPermission.METADATA_READ,
        )

        source_grant = self.ledger.grant_owner_permission(
            operation_id="reader/grant/source-derive",
            actor_principal_id="owner",
            owner_id=self.source_owner_id,
            principal_id="reader",
            permission=OwnerPermission.DERIVE,
        )
        self.source_owner_revision = source_grant.added_revision
        self.created["reader-owned"] = self._create_run(
            name="reader-owned",
            actor="reader",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def _create_run(self, *, name: str, actor: str):
        run_definition, definition_bindings = prepare_test_run_definition(
            self.closure, self.manifest, self.closure_bindings
        )
        return self.ledger.create_run_namespace(
            operation_id=f"reader/run/{name}/create",
            actor_principal_id=actor,
            controller_holder_id=f"controller-{name}",
            controller_ttl_seconds=120,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=self.source_owner_id,
            expected_source_owner_revision=self.source_owner_revision,
            run_id=f"run-{name}",
            owner_id=f"run-owner-{name}",
        )

    def _admit(self, *, name: str, suffixes: tuple[str, ...]):
        created = self.created[name]
        snapshot = self.ledger.read_run_snapshot(
            actor_principal_id="owner",
            run_id=created.run.run_id,
        )
        owner = self.ledger.read_owner(
            actor_principal_id="owner",
            owner_id=created.run.owner_id,
            permission=OwnerPermission.ADMIN,
        )
        change = self.ledger.begin_owner_change(
            operation_id=f"reader/run/{name}/admit/{snapshot.revision.revision}/begin",
            actor_principal_id="owner",
            owner_id=created.run.owner_id,
            expected_owner_revision=owner.revision,
            ttl_seconds=120,
        )
        candidates = tuple(
            CandidateAdmission(
                candidate_id=f"candidate-{suffix}",
                envelope=NormalizedCandidateEnvelope.build(
                    candidate_format="parameters",
                    spec={"x": suffix},
                ),
                lineage={"parents": []},
                generator={"method_id": "test-method"},
            )
            for suffix in suffixes
        )
        trials = tuple(
            LogicalTrialAdmission(
                logical_trial_id=f"trial-{suffix}",
                candidate_id=f"candidate-{suffix}",
            )
            for suffix in suffixes
        )
        lease = created.controller_lease
        return self.ledger.commit_run_candidate_admissions(
            operation_id=f"reader/run/{name}/admit/{snapshot.revision.revision}/commit",
            actor_principal_id="owner",
            run_id=created.run.run_id,
            expected_run_revision=snapshot.revision.revision,
            expected_owner_revision=owner.revision,
            controller_lease_id=lease.lease_id,
            controller_holder_id=lease.holder_id,
            controller_fencing_token=lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(candidates, trials),
        )

    def _realm_content_counts(self) -> dict[str, int]:
        connection = sqlite3.connect(self.ledger.database_path)
        try:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "managed_workspaces",
                    "workspace_revisions",
                    "content_objects",
                    "owner_memberships",
                    "leases",
                )
            }
        finally:
            connection.close()

    def test_visibility_exactly_matches_metadata_read_authorization(self) -> None:
        page = self.ledger.list_runs(actor_principal_id="reader")
        visible = {entry.run_id for entry in page.items}
        self.assertEqual(
            visible,
            {"run-reader-owned", "run-admin", "run-metadata"},
        )
        self.assertNotIn("run-hidden", visible)
        self.assertNotIn("run-derive", visible)
        self.assertNotIn("run-bytes", visible)

        # The same exact direct-grant hierarchy governs individual reads.
        for name in ("hidden", "derive", "bytes"):
            with self.assertRaises(RealmNotFound):
                self.ledger.read_run_snapshot(
                    actor_principal_id="reader",
                    run_id=self.created[name].run.run_id,
                )
        for name in ("admin", "metadata"):
            snapshot = self.ledger.read_run_snapshot(
                actor_principal_id="reader",
                run_id=self.created[name].run.run_id,
            )
            self.assertEqual(snapshot.run.run_id, f"run-{name}")

        owner_page = self.ledger.list_runs(actor_principal_id="owner")
        self.assertEqual(
            {entry.run_id for entry in owner_page.items},
            {
                "run-hidden",
                "run-derive",
                "run-bytes",
                "run-admin",
                "run-metadata",
            },
        )
        payload = page.to_dict()
        self.assertEqual(payload["schema"], RUN_CATALOG_PAGE_SCHEMA)
        self.assertEqual(payload["order"], RUN_CATALOG_ORDER)
        self.assertNotIn("owner_id", json.dumps(payload))
        self.assertNotIn("controller", json.dumps(payload))

    def test_keyset_pages_are_bounded_scoped_tamper_evident_and_path_free(self) -> None:
        first = self.ledger.list_runs(actor_principal_id="reader", limit=2)
        self.assertEqual(len(first.items), 2)
        self.assertTrue(first.has_more)
        token = first.next_page_token
        self.assertIsNotNone(token)
        second = self.ledger.list_runs(
            actor_principal_id="reader",
            page_token=token,
            limit=2,
        )
        combined = first.items + second.items
        self.assertEqual(len(combined), 3)
        self.assertEqual(
            list(combined),
            sorted(
                combined,
                key=lambda item: (item.updated_at, item.run_id),
                reverse=True,
            ),
        )
        self.assertEqual(len({item.run_id for item in combined}), 3)

        decoded = json.loads(
            base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode(
                "utf-8"
            )
        )
        self.assertEqual(decoded["schema"], RUN_CATALOG_PAGE_TOKEN_SCHEMA)
        self.assertEqual(
            set(decoded),
            {
                "schema",
                "order",
                "scope",
                "cursor_updated_at",
                "cursor_created_txn_id",
                "signature",
            },
        )
        token_text = json.dumps(decoded, sort_keys=True)
        self.assertNotIn('"run_id":', token_text)
        for entry in combined:
            self.assertNotIn(entry.run_id, token_text)
        self.assertNotIn("reader", token_text)
        self.assertNotIn(str(self.root), token_text)
        self.assertNotIn(str(self.ledger.database_path), token_text)

        replacement = "A" if token[len(token) // 2] != "A" else "B"
        tampered = (
            token[: len(token) // 2]
            + replacement
            + token[len(token) // 2 + 1 :]
        )
        with self.assertRaisesRegex(ValueError, "malformed|signature|fields"):
            self.ledger.list_runs(
                actor_principal_id="reader",
                page_token=tampered,
                limit=2,
            )
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.ledger.list_runs(
                actor_principal_id="reader",
                page_token="not-ascii-雪",
                limit=2,
            )
        with self.assertRaisesRegex(ValueError, "different query scope"):
            self.ledger.list_runs(
                actor_principal_id="owner",
                page_token=token,
                limit=2,
            )
        with self.assertRaisesRegex(ValueError, "different query scope"):
            self.ledger.list_runs(
                actor_principal_id="reader",
                page_token=token,
                limit=1,
            )
        for invalid_limit in (0, True, RUN_CATALOG_MAX_PAGE_SIZE + 1):
            with self.assertRaisesRegex(ValueError, "between 1"):
                self.ledger.list_runs(
                    actor_principal_id="reader",
                    limit=invalid_limit,
                )

        # The page boundary is the metadata-granted run in deterministic
        # creation order.  Revoking that grant invalidates its old token
        # without revealing a different owner's run facts.
        self.assertEqual(first.items[-1].run_id, "run-metadata")
        self.ledger.revoke_owner_permission(
            operation_id="reader/revoke/metadata",
            actor_principal_id="owner",
            owner_id=self.created["metadata"].run.owner_id,
            principal_id="reader",
            permission=OwnerPermission.METADATA_READ,
        )
        with self.assertRaisesRegex(ValueError, "boundary is unavailable"):
            self.ledger.list_runs(
                actor_principal_id="reader",
                page_token=token,
                limit=2,
            )

    def test_borrowed_run_view_is_live_path_free_and_has_no_realm_footprint(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        self.ledger.grant_owner_permission(
            operation_id="reader/grant/local-view",
            actor_principal_id="owner",
            owner_id=self.created["admin"].run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        self._admit(name="admin", suffixes=("view-a",))

        before_open = self._realm_content_counts()
        opened = context.open_run_view(run_id="run-admin")
        page = context.run_view_workbench_page(
            ref=opened.ref,
            kind="candidate",
        )
        context.detach_run_view(ref=opened.ref)
        self.assertEqual(self._realm_content_counts(), before_open)

        payload = opened.to_dict()
        self.assertEqual(payload["schema"], BORROWED_RUN_VIEW_SCHEMA)
        self.assertEqual(
            payload["ref"],
            {"schema": RUN_VIEW_REF_SCHEMA, "run_id": "run-admin"},
        )
        self.assertEqual(payload["head"], page["head"])
        self.assertEqual(payload["mode"], "read_only")
        self.assertFalse(payload["durable"])
        self.assertFalse(payload["authorizing"])
        self.assertEqual(
            payload["capabilities"]["run_workbench"]["code"], "ready"
        )
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.ledger.database_path), serialized)
        self.assertNotIn("owner_id", serialized)
        self.assertNotIn("principal_id", serialized)
        self.assertNotIn("workspace", serialized)
        self.assertNotIn("lease", serialized)

        with self.assertRaises(RealmIntegrityError):
            RunViewRef.from_dict(
                {
                    "schema": RUN_VIEW_REF_SCHEMA,
                    "run_id": "run-admin",
                    "path": str(self.root),
                }
            )

        old_head = opened.head
        self._admit(name="admin", suffixes=("view-b",))
        before_refresh = self._realm_content_counts()
        refreshed = context.refresh_run_view(ref=opened.ref)
        self.assertEqual(refreshed.ref, opened.ref)
        self.assertGreater(refreshed.revision, old_head["revision"])
        self.assertGreater(refreshed.sequence, old_head["sequence"])
        self.assertEqual(self._realm_content_counts(), before_refresh)
        context.close()

    def test_run_view_action_bridge_mints_only_exact_supported_selections(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        for name in ("admin", "metadata"):
            self.ledger.grant_owner_permission(
                operation_id=f"reader/grant/local-selection/{name}",
                actor_principal_id="owner",
                owner_id=self.created[name].run.owner_id,
                principal_id=context.principal_id,
                permission=OwnerPermission.METADATA_READ,
            )
        self._admit(name="admin", suffixes=("select-a",))
        self._admit(name="metadata", suffixes=("other",))
        view = context.open_run_view(run_id="run-admin")
        candidate_page = context.run_view_workbench_page(
            ref=view.ref,
            kind="candidate",
        )
        candidate_presentation = candidate_page["items"][0]["selection"]

        before_actions = self._realm_content_counts()
        selected = context.mint_run_view_selection(
            ref=view.ref,
            presentation_selection=candidate_presentation,
        )
        self.assertTrue(selected.eligibility.supported)
        self.assertTrue(selected.eligibility.eligible)
        self.assertIsInstance(selected.selection, SelectionRef)
        self.assertEqual(selected.selection.source_id, "run-admin")
        self.assertEqual(
            selected.selection.source_revision,
            candidate_presentation["revision"],
        )
        self.assertEqual(
            selected.selection.source_sequence,
            candidate_presentation["sequence"],
        )
        self.assertEqual(
            RUN_VIEW_MINTABLE_SELECTION_KINDS,
            frozenset({"candidate", "artifact"}),
        )

        logical_page = context.run_view_workbench_page(
            ref=view.ref,
            kind="logical_trial",
        )
        unsupported = context.mint_run_view_selection(
            ref=view.ref,
            presentation_selection=logical_page["items"][0]["selection"],
        )
        self.assertFalse(unsupported.eligibility.supported)
        self.assertFalse(unsupported.eligibility.eligible)
        self.assertEqual(
            unsupported.eligibility.code,
            "workbench_selection_kind_not_mintable",
        )
        self.assertIsNone(unsupported.selection)
        self.assertEqual(self._realm_content_counts(), before_actions)

        tampered = dict(candidate_presentation)
        tampered["entity_id"] = "candidate-tampered"
        with self.assertRaisesRegex(ValueError, "integrity check"):
            context.mint_run_view_selection(
                ref=view.ref,
                presentation_selection=tampered,
            )

        # The presentation digest is intentionally public and is not
        # authority.  Even a caller that recomputes it cannot invent a row.
        fabricated = dict(logical_page["items"][0]["selection"])
        fabricated["entity_id"] = "trial-not-present"
        identity = {
            key: value for key, value in fabricated.items() if key != "selection_id"
        }
        fabricated_digest = hashlib.sha256(
            b"optpilot/run-workbench-selection/v1\0"
            + json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        fabricated["selection_id"] = f"sha256:{fabricated_digest}"
        with self.assertRaisesRegex(ValueError, "does not identify an item"):
            context.mint_run_view_selection(
                ref=view.ref,
                presentation_selection=fabricated,
            )

        other_view = context.open_run_view(run_id="run-metadata")
        other_page = context.run_view_workbench_page(
            ref=other_view.ref,
            kind="candidate",
        )
        with self.assertRaisesRegex(ValueError, "different run View"):
            context.mint_run_view_selection(
                ref=view.ref,
                presentation_selection=other_page["items"][0]["selection"],
            )

        self._admit(name="admin", suffixes=("select-b",))
        with self.assertRaisesRegex(RealmConflict, "head changed"):
            context.mint_run_view_selection(
                ref=view.ref,
                presentation_selection=candidate_presentation,
            )
        context.close()

    def test_borrowed_view_authorization_and_context_lifecycle_fail_closed(self) -> None:
        context = LocalRealmContext.open(ledger=self.ledger)
        missing_messages = []
        for run_id in ("run-hidden", "run-does-not-exist"):
            with self.assertRaises(RealmNotFound) as raised:
                context.open_run_view(run_id=run_id)
            missing_messages.append(str(raised.exception))
            with self.assertRaises(RealmNotFound) as refreshed:
                context.refresh_run_view(ref=RunViewRef(run_id=run_id))
            self.assertEqual(str(refreshed.exception), str(raised.exception))
            with self.assertRaises(RealmNotFound) as selected:
                context.mint_run_view_selection(
                    ref=RunViewRef(run_id=run_id),
                    presentation_selection={},
                )
            self.assertEqual(str(selected.exception), str(raised.exception))
        self.assertEqual(missing_messages, ["Entity not found.", "Entity not found."])

        ref = RunViewRef(run_id="run-hidden")
        context.close()
        for operation in (
            lambda: context.open_run_view(run_id=ref.run_id),
            lambda: context.refresh_run_view(ref=ref),
            lambda: context.run_view_workbench_page(ref=ref, kind="candidate"),
            lambda: context.mint_run_view_selection(
                ref=ref,
                presentation_selection={},
            ),
            lambda: context.detach_run_view(ref=ref),
        ):
            with self.assertRaisesRegex(RuntimeError, "context is closed"):
                operation()

    def test_local_context_binds_principal_and_fails_closed_after_close(self) -> None:
        with self.assertRaisesRegex(TypeError, "LocalRealmContext.open"):
            LocalRealmContext()
        context = LocalRealmContext.open(ledger=self.ledger)
        second_context = LocalRealmContext.open(ledger=self.ledger)
        self.assertEqual(context.principal_id, second_context.principal_id)
        self.assertRegex(context.principal_id, r"^local-user:sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(self.root), context.principal_id)
        second_context.close()
        self.assertNotIn(
            "actor_principal_id",
            inspect.signature(context.list_runs).parameters,
        )
        for method in (
            context.open_run_view,
            context.refresh_run_view,
            context.run_view_workbench_page,
            context.mint_run_view_selection,
            context.detach_run_view,
        ):
            self.assertFalse(
                {
                    "actor_principal_id",
                    "principal_id",
                    "path",
                    "root",
                    "workspace_id",
                }
                & set(inspect.signature(method).parameters)
            )
        self.assertFalse(hasattr(context, "runs"))

        self.ledger.grant_owner_permission(
            operation_id="reader/grant/local-context",
            actor_principal_id="owner",
            owner_id=self.created["admin"].run.owner_id,
            principal_id=context.principal_id,
            permission=OwnerPermission.METADATA_READ,
        )
        self._admit(name="admin", suffixes=("a", "b"))

        catalog = context.list_runs(limit=10)
        self.assertEqual(
            {item.run_id for item in catalog.items},
            {"run-admin"},
        )
        summary = context.summary(run_id="run-admin")
        self.assertEqual(summary.run_id, "run-admin")
        self.assertEqual(summary.candidate_count, 2)
        first = context.workbench_page(
            run_id="run-admin",
            kind="candidate",
            limit=1,
        )
        old_token = first["page"]["next_page_token"]
        self.assertIsNotNone(old_token)

        self._admit(name="admin", suffixes=("c",))
        with self.assertRaisesRegex(ValueError, "different run head or kind"):
            context.workbench_page(
                run_id="run-admin",
                kind="candidate",
                page_token=old_token,
                limit=1,
            )

        context.close()
        # The injected ledger deliberately stays open, but every public
        # operation on the lifecycle context itself still fails closed.
        for operation in (
            lambda: context.list_runs(),
            lambda: context.summary(run_id="run-admin"),
            lambda: context.workbench_page(
                run_id="run-admin", kind="candidate"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "context is closed"):
                operation()
        self.assertEqual(
            self.ledger.list_runs(actor_principal_id="owner").items[0].run_id,
            "run-admin",
        )


if __name__ == "__main__":
    unittest.main()
