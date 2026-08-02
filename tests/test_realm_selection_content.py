"""Exact-authority and no-copy proofs for retained selection content reads."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.errors import ContentCorrupt, ContentRejected, RealmNotFound
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.refs import BlobRef
from optpilot.realm.run_records import (
    RUN_CANDIDATE_ROLE,
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.selection_content_service import (
    MAX_BYTE_READ_LENGTH,
    MAX_TREE_PAGE_LIMIT,
    RealmSelectionContentService,
)
from optpilot.realm.selections import SelectionRef
from tests.realm_run_support import (
    prepare_test_run_closure,
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)
from tests import test_realm_selection_derivation as selection_derivation
from tests import test_realm_interface_output_session as interface_output_fixture
from tests import test_realm_operator_jobs as operator_job_fixture


class RealmSelectionContentTest(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the established run/candidate/artifact authority fixture while
        # making its candidate tree large enough to exercise real pagination.
        fixture = selection_derivation.RealmSelectionDerivationTest(
            methodName="runTest"
        )

        def publish_source_tree(
            this,
            *,
            owner_id: str,
            directory_name: str,
            role: str,
        ) -> OwnerMembership:
            this.ledger.create_owner(
                operation_id=this.op(f"create-{owner_id}"),
                owner_id=owner_id,
                owner_kind="workspace",
                principal_id="operator",
            )
            source = this.root / directory_name
            source.mkdir()
            (source / "README.md").write_text("candidate\n", encoding="utf-8")
            (source / "run.py").write_text(
                "print('candidate')\n", encoding="utf-8"
            )
            package = source / "package"
            package.mkdir()
            (package / "model.py").write_text("VALUE = 7\n", encoding="utf-8")
            change = this.ledger.begin_owner_change(
                operation_id=this.op(f"begin-{owner_id}"),
                actor_principal_id="operator",
                owner_id=owner_id,
                expected_owner_revision=0,
                ttl_seconds=60,
            )
            capture = this.store.capture(
                change_id=change.change_id,
                authority=this.ledger.content_capture_handle(
                    actor_principal_id="operator",
                    change_id=change.change_id,
                    store_id=this.store.store_id,
                ),
            )
            sealed = capture.seal_tree(source=AllowedTreeSource(source))
            membership = OwnerMembership(this.store.store_id, sealed.snapshot_ref, role)
            this.ledger.hold_owner_content(
                operation_id=this.op(f"hold-{owner_id}"),
                actor_principal_id="operator",
                change_id=change.change_id,
                memberships=(membership,),
            )
            this.ledger.commit_owner_change(
                operation_id=this.op(f"commit-{owner_id}"),
                actor_principal_id="operator",
                change_id=change.change_id,
                expected_owner_revision=0,
                additions=(membership,),
            )
            return membership

        fixture._publish_source_tree = types.MethodType(  # type: ignore[method-assign]
            publish_source_tree, fixture
        )
        fixture.setUp()
        self.fixture = fixture
        self.service = RealmSelectionContentService(
            fixture.ledger,
            fixture.principals["operator"],
            local_stores={fixture.store.store_id: fixture.store},
        )
        self.other_service = RealmSelectionContentService(
            fixture.ledger,
            fixture.principals["other"],
            local_stores={fixture.store.store_id: fixture.store},
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _select(self, kind: str, entity_id: str) -> SelectionRef:
        return self.fixture._select(kind, entity_id)

    def _authority_counts(self) -> tuple[int, int, int, int]:
        connection = self.fixture.ledger._connect()
        try:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "owners",
                    "owner_memberships",
                    "content_objects",
                    "managed_workspaces",
                )
            )
        finally:
            connection.close()

    def test_candidate_and_tree_artifact_have_browsable_readable_parity(self) -> None:
        candidate = self._select("candidate", "candidate-files")
        artifact = self._select("artifact", self.fixture.tree_artifact_id)
        before_counts = self._authority_counts()
        before_refs = tuple(self.fixture.store.iter_live_refs())

        candidate_summary = self.service.describe(selection=candidate)
        artifact_summary = self.service.describe(selection=artifact)
        self.assertEqual(candidate_summary.content_kind, "tree")
        self.assertEqual(artifact_summary.content_kind, "tree")
        self.assertTrue(candidate_summary.eligibility.eligible)
        self.assertTrue(artifact_summary.eligibility.eligible)

        candidate_page = self.service.list_tree(selection=candidate, limit=10)
        artifact_page = self.service.list_tree(selection=artifact, limit=10)
        self.assertEqual(
            [item.relative_path for item in candidate_page.entries],
            ["README.md", "package", "package/model.py", "run.py"],
        )
        self.assertEqual(
            [item.relative_path for item in artifact_page.entries],
            ["model.json"],
        )
        candidate_bytes = self.service.read_range(
            selection=candidate,
            relative_path="package/model.py",
        )
        artifact_bytes = self.service.read_range(
            selection=artifact,
            relative_path="model.json",
        )
        self.assertEqual(candidate_bytes.data, b"VALUE = 7\n")
        self.assertEqual(artifact_bytes.data, b'{"ok":true}\n')
        self.assertTrue(candidate_bytes.eof)
        self.assertTrue(artifact_bytes.eof)

        # Public DTOs contain no store id, physical content ref, or host path.
        payload = json.dumps(
            {
                "candidate": candidate_summary.to_dict(),
                "candidate_page": candidate_page.to_dict(),
                "artifact": artifact_summary.to_dict(),
                "artifact_page": artifact_page.to_dict(),
            },
            sort_keys=True,
        )
        self.assertNotIn(self.fixture.store.store_id, payload)
        self.assertNotIn("tree:sha256:", payload)
        self.assertNotIn(str(self.fixture.root), payload)

        self.assertEqual(self._authority_counts(), before_counts)
        self.assertEqual(tuple(self.fixture.store.iter_live_refs()), before_refs)

    def test_blob_artifact_supports_bounded_ranges_but_not_tree_browsing(self) -> None:
        selection = self._select("artifact", self.fixture.file_artifact_id)
        summary = self.service.describe(selection=selection)
        self.assertEqual(summary.content_kind, "blob")
        self.assertEqual(summary.total_bytes, len(b"report\n"))

        first = self.service.read_range(
            selection=selection,
            offset=0,
            length=3,
        )
        second = self.service.read_range(
            selection=selection,
            offset=3,
            length=100,
        )
        self.assertEqual(first.data, b"rep")
        self.assertFalse(first.eof)
        self.assertEqual(second.data, b"ort\n")
        self.assertTrue(second.eof)
        page = self.service.list_tree(selection=selection)
        self.assertFalse(page.eligibility.eligible)
        self.assertEqual(page.eligibility.code, "selection_content_not_tree")
        self.assertEqual(page.entries, ())
        with self.assertRaisesRegex(ValueError, "does not accept relative_path"):
            self.service.read_range(selection=selection, relative_path="report.txt")
        with self.assertRaisesRegex(ValueError, "offset exceeds"):
            self.service.read_range(selection=selection, offset=100)

    def test_blob_describe_and_range_do_not_rehash_the_full_payload(self) -> None:
        selection = self._select("artifact", self.fixture.file_artifact_id)
        with mock.patch(
            "optpilot.realm.content._hash_blob_fd",
            side_effect=AssertionError("bounded reads must not hash the full blob"),
        ) as full_hash:
            summary = self.service.describe(selection=selection)
            selected = self.service.read_range(
                selection=selection,
                offset=1,
                length=2,
            )
        self.assertEqual(summary.total_bytes, len(b"report\n"))
        self.assertEqual(selected.data, b"ep")
        full_hash.assert_not_called()

        # Manifest-only verification still rejects physical mutations that
        # violate the immutable-object contract, such as a size substitution.
        blob_ref = BlobRef.parse(selection.entity_ref)
        object_directory = self.fixture.store._object_directory(blob_ref)
        data = object_directory / "data"
        original = data.read_bytes()
        os.chmod(object_directory, 0o700)
        os.chmod(data, 0o600)
        data.write_bytes(b"x")
        os.chmod(data, 0o400)
        os.chmod(object_directory, 0o500)
        try:
            with mock.patch(
                "optpilot.realm.content._hash_blob_fd",
                side_effect=AssertionError("full hash must remain unused"),
            ) as full_hash:
                with self.assertRaisesRegex(ContentCorrupt, "payload size"):
                    self.service.describe(selection=selection)
            full_hash.assert_not_called()
        finally:
            os.chmod(object_directory, 0o700)
            os.chmod(data, 0o600)
            data.write_bytes(original)
            os.chmod(data, 0o400)
            os.chmod(object_directory, 0o500)

    def test_manifest_pagination_is_deterministic_bounded_and_selection_bound(self) -> None:
        selection = self._select("candidate", "candidate-files")
        first = self.service.list_tree(selection=selection, limit=2)
        self.assertEqual(
            [item.relative_path for item in first.entries],
            ["README.md", "package"],
        )
        self.assertIsNotNone(first.next_cursor)
        repeat = self.service.list_tree(selection=selection, limit=2)
        self.assertEqual(repeat, first)
        second = self.service.list_tree(
            selection=selection,
            cursor=first.next_cursor,
            limit=2,
        )
        self.assertEqual(
            [item.relative_path for item in second.entries],
            ["package/model.py", "run.py"],
        )
        self.assertIsNone(second.next_cursor)

        artifact = self._select("artifact", self.fixture.tree_artifact_id)
        with self.assertRaisesRegex(ValueError, "invalid for this selection"):
            self.service.list_tree(
                selection=artifact,
                cursor=first.next_cursor,
            )
        assert first.next_cursor is not None
        tampered = first.next_cursor[:-1] + (
            "0" if first.next_cursor[-1] != "0" else "1"
        )
        with self.assertRaisesRegex(ValueError, "invalid for this selection"):
            self.service.list_tree(selection=selection, cursor=tampered)
        for limit in (0, MAX_TREE_PAGE_LIMIT + 1, True):
            with self.assertRaises(ValueError):
                self.service.list_tree(selection=selection, limit=limit)

    def test_ranges_and_relative_paths_are_strictly_bounded(self) -> None:
        selection = self._select("candidate", "candidate-files")
        for path in ("../run.py", "package/../run.py", "/run.py", "package\\model.py"):
            with self.assertRaises(ContentRejected):
                self.service.read_range(selection=selection, relative_path=path)
        with self.assertRaises(RealmNotFound):
            self.service.read_range(selection=selection, relative_path="missing.txt")
        with self.assertRaises(RealmNotFound):
            self.service.read_range(selection=selection, relative_path="package")
        for length in (0, MAX_BYTE_READ_LENGTH + 1, True):
            with self.assertRaises(ValueError):
                self.service.read_range(
                    selection=selection,
                    relative_path="run.py",
                    length=length,
                )

    def test_stale_foreign_revoked_and_retired_selections_fail_closed(self) -> None:
        selection = self._select("candidate", "candidate-files")
        stale = replace(
            selection,
            source_revision=selection.source_revision + 100,
            selection_digest=SelectionRef.build(
                kind=selection.kind,
                source_kind=selection.source_kind,
                source_id=selection.source_id,
                source_owner_id=selection.source_owner_id,
                source_revision=selection.source_revision + 100,
                owner_revision=selection.owner_revision,
                source_sequence=selection.source_sequence,
                entity_sequence=selection.entity_sequence,
                entity_id=selection.entity_id,
                entity_ref=selection.entity_ref,
                context_digest=selection.context_digest,
                relative_path=selection.relative_path,
            ).selection_digest,
        )
        with self.assertRaises(RealmNotFound) as foreign:
            self.other_service.describe(selection=selection)
        with self.assertRaises(RealmNotFound) as missing:
            self.service.describe(selection=stale)
        self.assertEqual(str(foreign.exception), str(missing.exception))

        self.fixture.ledger.grant_owner_permission(
            operation_id=self.fixture.op("selection-content-grant"),
            actor_principal_id="operator",
            owner_id=self.fixture.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        self.assertTrue(
            self.other_service.describe(selection=selection).eligibility.eligible
        )
        self.fixture.ledger.revoke_owner_permission(
            operation_id=self.fixture.op("selection-content-revoke"),
            actor_principal_id="operator",
            owner_id=self.fixture.created.run.owner_id,
            principal_id="other",
            permission=OwnerPermission.BYTES_READ,
        )
        with self.assertRaises(RealmNotFound):
            self.other_service.list_tree(selection=selection)

        self.fixture.owner_revision = self.fixture.ledger.read_owner(
            actor_principal_id="operator",
            owner_id=self.fixture.created.run.owner_id,
        ).revision
        self.fixture._retire_source_run()
        retired = self.service.describe(selection=selection)
        self.assertFalse(retired.eligibility.eligible)
        self.assertEqual(retired.eligibility.code, "selection_content_unavailable")

    @unittest.skipIf(os.name == "nt", "Managed symlink tampering is POSIX-only.")
    def test_blob_read_rejects_symlink_substitution(self) -> None:
        selection = self._select("artifact", self.fixture.file_artifact_id)
        blob_ref = BlobRef.parse(selection.entity_ref)
        object_directory = self.fixture.store._object_directory(blob_ref)
        data = object_directory / "data"
        external_fd, external_name = tempfile.mkstemp(dir=self.fixture.root)
        os.close(external_fd)
        external = Path(external_name)
        external.write_bytes(b"evil\n")
        saved = object_directory / "data.saved"
        os.chmod(object_directory, 0o700)
        os.rename(data, saved)
        os.symlink(external, data)
        os.chmod(object_directory, 0o500)
        try:
            with self.assertRaises(ContentCorrupt):
                self.service.read_range(selection=selection)
        finally:
            os.chmod(object_directory, 0o700)
            data.unlink()
            os.rename(saved, data)
            os.chmod(object_directory, 0o500)
            external.unlink()

    def test_parameter_candidate_is_semantic_only(self) -> None:
        selection = self._admit_parameter_candidate()
        summary = self.service.describe(selection=selection)
        self.assertFalse(summary.eligibility.eligible)
        self.assertTrue(summary.eligibility.supported)
        self.assertEqual(
            summary.eligibility.code, "parameter_candidate_semantic_only"
        )
        self.assertEqual(summary.content_kind, "semantic")
        page = self.service.list_tree(selection=selection)
        byte_read = self.service.read_range(selection=selection)
        self.assertEqual(
            page.eligibility.code, "parameter_candidate_semantic_only"
        )
        self.assertEqual(
            byte_read.eligibility.code, "parameter_candidate_semantic_only"
        )
        self.assertIsNone(byte_read.data)

    def test_interface_output_blob_uses_the_same_content_api(self) -> None:
        fixture = interface_output_fixture.RealmInterfaceOutputSessionTest(
            methodName="runTest"
        )
        fixture.setUp()
        try:
            (fixture.output_root / "artifact.txt").write_text(
                "interface result", encoding="utf-8"
            )
            record = interface_output_fixture.InterfaceOutputRecord.from_dict(
                fixture._record(
                    id="selection-content-file",
                    label="Selection content file",
                    kind="file",
                    path="artifact.txt",
                )
            )
            generation = fixture.service.capture_generation(
                handle=fixture.handle,
                record=record,
                root_handles={"output": fixture.output_root},
            )
            assert generation.selection is not None
            service = RealmSelectionContentService(
                fixture.ledger,
                fixture.principal,
                local_stores={fixture.store.store_id: fixture.store},
            )
            summary = service.describe(selection=generation.selection)
            result = service.read_range(selection=generation.selection)
            self.assertEqual(summary.content_kind, "blob")
            self.assertEqual(result.data, b"interface result")
        finally:
            fixture.tearDown()

    def test_operator_job_blob_has_distinct_content_read_authority(self) -> None:
        fixture = operator_job_fixture.RealmOperatorJobTest(methodName="runTest")
        fixture.setUp()
        try:
            terminal, addition = fixture.finish_job_with_declared_output(kind="file")
            selection = fixture.ledger.mint_operator_job_output_selection(
                actor_principal_id="operator",
                job_id=terminal.job_id,
                output_id="primary-output",
            )
            projection_resolution = fixture.ledger.resolve_selection(
                actor_principal_id="operator",
                selection=selection,
            )
            content_resolution = fixture.ledger.resolve_selection_for_content_read(
                actor_principal_id="operator",
                selection=selection,
            )
            self.assertFalse(projection_resolution.eligibility.eligible)
            self.assertEqual(
                projection_resolution.eligibility.code,
                "operator_job_file_output_not_tree",
            )
            self.assertTrue(content_resolution.eligibility.eligible)
            self.assertEqual(content_resolution.root, addition)
        finally:
            fixture.tearDown()

    def _admit_parameter_candidate(self) -> SelectionRef:
        fixture = self.fixture
        closure, bindings, source_owner, source_revision = prepare_test_run_closure(
            ledger=fixture.ledger,
            store=fixture.store,
            root=fixture.root,
            actor_principal_id="operator",
            prefix="selection-content-parameters",
        )
        manifest = prepare_test_run_control_manifest(closure, max_trials=1)
        run_definition, definition_bindings = prepare_test_run_definition(
            closure, manifest, bindings
        )
        created = fixture.ledger.create_run_namespace(
            operation_id=fixture.op("content-parameter-run-create"),
            actor_principal_id="operator",
            controller_holder_id="content-controller-parameters",
            controller_ttl_seconds=600,
            run_definition=run_definition,
            definition_bindings=definition_bindings,
            source_owner_id=source_owner,
            expected_source_owner_revision=source_revision,
            run_id="selection-content-parameters",
            owner_id="selection-content-parameters-owner",
        )
        change = fixture.ledger.begin_owner_change(
            operation_id=fixture.op("content-parameter-admission-begin"),
            actor_principal_id="operator",
            owner_id=created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        admitted = fixture.ledger.commit_run_candidate_admissions(
            operation_id=fixture.op("content-parameter-admission"),
            actor_principal_id="operator",
            run_id=created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            controller_lease_id=created.controller_lease.lease_id,
            controller_holder_id=created.controller_lease.holder_id,
            controller_fencing_token=created.controller_lease.fencing_token,
            change_id=change.change_id,
            plan=RunAdmissionPlan(
                (
                    CandidateAdmission(
                        "content-candidate-parameters",
                        NormalizedCandidateEnvelope.build(
                            candidate_format="parameters", spec={"x": 1}
                        ),
                    ),
                ),
                (
                    LogicalTrialAdmission(
                        "content-trial-parameters",
                        "content-candidate-parameters",
                    ),
                ),
            ),
        )
        return fixture.ledger.mint_run_selection(
            actor_principal_id="operator",
            run_id=created.run.run_id,
            kind="candidate",
            entity_id="content-candidate-parameters",
            expected_run_revision=admitted.revision.revision,
            expected_head_sequence=admitted.revision.last_sequence,
        )


if __name__ == "__main__":
    unittest.main()
