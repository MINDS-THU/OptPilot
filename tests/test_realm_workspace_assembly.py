from __future__ import annotations

import copy
import unittest

from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.refs import BlobRef, SnapshotRef
from optpilot.realm.selections import SelectionRef
from optpilot.realm.workspace_assembly import (
    WorkspaceAssemblyConflict,
    WorkspaceAssemblyEvidenceMismatch,
    WorkspaceAssemblyLineage,
    WorkspaceAssemblyRequest,
    WorkspaceAssemblyResult,
    WorkspaceFocus,
    WorkspaceRequestSource,
    WorkspaceSeed,
    WorkspaceSeedSource,
    WorkspaceSelectionSeed,
    WorkspaceSourceAnchor,
    compile_workspace_assembly,
)


def _file(path: str, content: bytes = b"content") -> TreeEntry:
    return TreeEntry.file(
        path,
        blob_ref=BlobRef.from_bytes(content),
        size=len(content),
        executable=False,
    )


def _tree(*entries: TreeEntry) -> TreeManifest:
    return TreeManifest.build(entries)


def _selection(
    manifest: TreeManifest,
    *,
    package_id: str,
    revision: int = 1,
    relative_path: str | None = None,
) -> SelectionRef:
    if relative_path is None:
        return SelectionRef.build(
            kind="catalog-package",
            source_kind="catalog",
            source_id=package_id,
            source_owner_id=f"catalog-owner-{package_id}-{revision}",
            source_revision=revision,
            owner_revision=0,
            source_sequence=None,
            entity_sequence=None,
            entity_id=package_id,
            entity_ref=str(manifest.snapshot_ref),
            context_digest=(f"{revision:x}" * 64)[:64],
        )
    return SelectionRef.build(
        kind="artifact",
        source_kind="interface-output",
        source_id=package_id,
        source_owner_id=f"output-owner-{package_id}",
        source_revision=revision,
        owner_revision=revision,
        source_sequence=None,
        entity_sequence=None,
        entity_id=f"artifact-{package_id}",
        entity_ref=str(manifest.snapshot_ref),
        relative_path=relative_path,
    )


def _source(
    manifest: TreeManifest,
    *,
    package_id: str,
    store_id: str = "realm-local",
    focuses: tuple[WorkspaceFocus, ...] = (),
) -> WorkspaceSeedSource:
    return WorkspaceSeedSource(
        anchor=WorkspaceSourceAnchor.build(
            selection=_selection(manifest, package_id=package_id),
            store_id=store_id,
            focuses=focuses,
        ),
        tree_manifest=manifest,
    )


def _request(*sources: WorkspaceSeedSource) -> WorkspaceAssemblyRequest:
    return WorkspaceAssemblyRequest(
        operation_id="workspace-create-op",
        actor_principal_id="principal-user",
        workspace_id="workspace-target",
        owner_id="workspace-owner-target",
        title="Environment plus method",
        seed=WorkspaceSelectionSeed.build(
            tuple(
                WorkspaceRequestSource.build(
                    selection=source.anchor.selection,
                    focuses=source.anchor.focuses,
                )
                for source in sources
            )
        ),
    )


def _compile(*sources: WorkspaceSeedSource) -> WorkspaceAssemblyResult:
    return compile_workspace_assembly(
        _request(*sources),
        WorkspaceSeed.build(sources),
    )


class WorkspaceAssemblyTests(unittest.TestCase):
    def test_semantic_seed_rejects_more_than_256_sources(self) -> None:
        manifest = _tree(TreeEntry.directory("package"), _file("package/value.txt"))
        sources = tuple(
            WorkspaceRequestSource.build(
                selection=_selection(manifest, package_id=f"package-{index}")
            )
            for index in range(257)
        )

        with self.assertRaisesRegex(ValueError, "256-source"):
            WorkspaceSelectionSeed.build(sources)

    def test_semantic_seed_rejects_more_than_256_focuses(self) -> None:
        manifest = _tree(TreeEntry.directory("package"), _file("package/value.txt"))
        focuses = tuple(
            WorkspaceFocus(
                kind="resource",
                focus_id=f"focus-{index}",
                relative_path=f"focus/{index}.yaml",
            )
            for index in range(257)
        )
        source = WorkspaceRequestSource.build(
            selection=_selection(manifest, package_id="focused-package"),
            focuses=focuses,
        )

        with self.assertRaisesRegex(ValueError, "256-focus"):
            WorkspaceSelectionSeed.build((source,))

    def test_one_root_is_adopted_without_rewriting_its_manifest(self) -> None:
        manifest = _tree(
            TreeEntry.directory("package"),
            _file("package/environment.yaml", b"environment"),
        )
        source = _source(manifest, package_id="example")
        request = _request(source)

        result = compile_workspace_assembly(request, WorkspaceSeed.build((source,)))

        self.assertEqual(result.outcome, "adopt")
        self.assertIs(result.tree_manifest, manifest)
        self.assertEqual(result.root_ref, manifest.snapshot_ref)
        self.assertEqual(result.request_digest, request.digest)
        self.assertEqual(len(result.digest), 64)

    def test_identical_exact_roots_are_deduplicated_but_all_anchors_remain(
        self,
    ) -> None:
        manifest = _tree(TreeEntry.directory("shared"), _file("shared/model.py"))
        result = _compile(
            _source(manifest, package_id="environment-package"),
            _source(manifest, package_id="method-package"),
        )

        self.assertEqual(result.outcome, "adopt")
        self.assertEqual(result.lineage.distinct_root_refs, (manifest.snapshot_ref,))
        self.assertEqual(len(result.lineage.sources), 2)

    def test_union_merges_only_directory_ancestors_and_preserves_paths(self) -> None:
        environment = _tree(
            TreeEntry.directory("src"),
            TreeEntry.directory("src/environment"),
            _file("src/environment/config.yaml", b"environment"),
        )
        method = _tree(
            TreeEntry.directory("src"),
            TreeEntry.directory("src/method"),
            _file("src/method/config.yaml", b"method"),
        )

        result = _compile(
            _source(environment, package_id="environment"),
            _source(method, package_id="method"),
        )

        self.assertEqual(result.outcome, "union")
        self.assertEqual(
            tuple(entry.path for entry in result.tree_manifest.entries),
            (
                "src",
                "src/environment",
                "src/environment/config.yaml",
                "src/method",
                "src/method/config.yaml",
            ),
        )
        self.assertNotIn(
            result.root_ref,
            result.lineage.distinct_root_refs,
        )

    def test_compilation_is_independent_of_source_input_order(self) -> None:
        first = _source(
            _tree(TreeEntry.directory("a"), _file("a/one.txt", b"one")),
            package_id="first",
        )
        second = _source(
            _tree(TreeEntry.directory("b"), _file("b/two.txt", b"two")),
            package_id="second",
        )

        forward = _request(first, second)
        reverse = _request(second, first)
        forward_result = compile_workspace_assembly(
            forward, WorkspaceSeed.build((first, second))
        )
        reverse_result = compile_workspace_assembly(
            reverse, WorkspaceSeed.build((second, first))
        )

        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(forward_result.to_dict(), reverse_result.to_dict())
        self.assertEqual(forward_result.digest, reverse_result.digest)

    def test_semantic_request_is_bindable_without_manifest_or_store_state(self) -> None:
        source = _source(
            _tree(TreeEntry.directory("package"), _file("package/config.yaml")),
            package_id="package",
        )
        request = _request(source)
        payload = request.to_dict()
        encoded = str(payload)

        self.assertNotIn("tree_manifest", encoded)
        self.assertNotIn("store_id", encoded)
        self.assertEqual(payload["workspace_id"], "workspace-target")
        self.assertEqual(payload["owner_id"], "workspace-owner-target")
        self.assertEqual(WorkspaceAssemblyRequest.from_dict(payload), request)
        changed_target = WorkspaceAssemblyRequest(
            operation_id=request.operation_id,
            actor_principal_id=request.actor_principal_id,
            workspace_id="different-workspace-target",
            owner_id=request.owner_id,
            title=request.title,
            seed=request.seed,
        )
        self.assertNotEqual(changed_target.digest, request.digest)

    def test_resolution_evidence_must_match_requested_selections_one_to_one(
        self,
    ) -> None:
        first = _source(_tree(_file("first.txt")), package_id="first")
        second = _source(_tree(_file("second.txt")), package_id="second")

        with self.assertRaisesRegex(
            WorkspaceAssemblyEvidenceMismatch, "missing=.*extra="
        ):
            compile_workspace_assembly(
                _request(first, second),
                WorkspaceSeed.build((first,)),
            )
        with self.assertRaisesRegex(
            WorkspaceAssemblyEvidenceMismatch, "missing=.*extra="
        ):
            compile_workspace_assembly(
                _request(first),
                WorkspaceSeed.build((first, second)),
            )

    def test_resolution_evidence_cannot_change_focus_lineage(self) -> None:
        manifest = _tree(
            TreeEntry.directory("configs"),
            _file("configs/environment.yaml", b"environment"),
        )
        focus = WorkspaceFocus(
            kind="environment",
            focus_id="environment",
            relative_path="configs/environment.yaml",
        )
        requested = _source(manifest, package_id="package", focuses=(focus,))
        resolved_without_focus = _source(manifest, package_id="package")

        with self.assertRaisesRegex(WorkspaceAssemblyEvidenceMismatch, "focus lineage"):
            compile_workspace_assembly(
                _request(requested),
                WorkspaceSeed.build((resolved_without_focus,)),
            )

    def test_same_path_files_conflict_even_when_their_bytes_match(self) -> None:
        shared_file = _file("shared.txt", b"identical")
        left = _tree(
            shared_file,
            TreeEntry.directory("left"),
            _file("left/only.txt", b"left"),
        )
        right = _tree(
            shared_file,
            TreeEntry.directory("right"),
            _file("right/only.txt", b"right"),
        )

        with self.assertRaises(WorkspaceAssemblyConflict) as raised:
            _compile(
                _source(left, package_id="left"),
                _source(right, package_id="right"),
            )

        self.assertEqual(raised.exception.code, "file-file")
        self.assertEqual(raised.exception.path, "shared.txt")

    def test_file_directory_overlap_is_rejected(self) -> None:
        left = _tree(_file("shared", b"file"))
        right = _tree(
            TreeEntry.directory("shared"),
            _file("shared/child.txt", b"child"),
        )

        with self.assertRaises(WorkspaceAssemblyConflict) as raised:
            _compile(
                _source(left, package_id="left"),
                _source(right, package_id="right"),
            )

        self.assertEqual(raised.exception.code, "file-directory")
        self.assertEqual(raised.exception.path, "shared")

    def test_casefolded_cross_root_collision_is_rejected(self) -> None:
        upper = _tree(
            TreeEntry.directory("Source"),
            _file("Source/model.py", b"upper"),
        )
        lower = _tree(
            TreeEntry.directory("source"),
            _file("source/config.yaml", b"lower"),
        )

        with self.assertRaises(WorkspaceAssemblyConflict) as raised:
            _compile(
                _source(upper, package_id="upper"),
                _source(lower, package_id="lower"),
            )

        self.assertEqual(raised.exception.code, "portable-path")
        self.assertEqual(
            {raised.exception.path, raised.exception.other_path},
            {"Source", "source"},
        )

    def test_seed_rejects_sources_spanning_content_stores(self) -> None:
        first = _source(_tree(_file("first.txt")), package_id="first", store_id="one")
        second = _source(
            _tree(_file("second.txt")), package_id="second", store_id="two"
        )

        with self.assertRaisesRegex(ValueError, "one content store"):
            WorkspaceSeed.build((first, second))

    def test_source_rejects_manifest_that_differs_from_selection_root(self) -> None:
        selected = _tree(_file("selected.txt", b"selected"))
        different = _tree(_file("different.txt", b"different"))
        anchor = WorkspaceSourceAnchor.build(
            selection=_selection(selected, package_id="selected"),
            store_id="realm-local",
        )

        with self.assertRaisesRegex(ValueError, "manifest differs"):
            WorkspaceSeedSource(anchor=anchor, tree_manifest=different)

    def test_source_rejects_nested_or_non_tree_selection(self) -> None:
        manifest = _tree(
            TreeEntry.directory("nested"),
            _file("nested/file.txt", b"nested"),
        )
        with self.assertRaisesRegex(ValueError, "whole-tree"):
            WorkspaceSourceAnchor(
                selection=_selection(
                    manifest,
                    package_id="nested",
                    relative_path="nested",
                ),
                store_id="realm-local",
                root_ref=manifest.snapshot_ref,
            )

        non_tree = SelectionRef.build(
            kind="artifact",
            source_kind="interface-output",
            source_id="blob-output",
            source_owner_id="blob-owner",
            source_revision=1,
            owner_revision=1,
            source_sequence=None,
            entity_sequence=None,
            entity_id="blob-artifact",
            entity_ref=str(BlobRef.from_bytes(b"blob")),
        )
        with self.assertRaisesRegex(ValueError, "tree root"):
            WorkspaceSourceAnchor.build(
                selection=non_tree,
                store_id="realm-local",
            )

    def test_focus_is_package_relative_present_and_retained_as_lineage(self) -> None:
        manifest = _tree(
            TreeEntry.directory("configs"),
            _file("configs/environment.yaml", b"environment"),
        )
        focus = WorkspaceFocus(
            kind="environment",
            focus_id="example-environment",
            relative_path="configs/environment.yaml",
        )
        source = _source(manifest, package_id="example", focuses=(focus,))

        result = _compile(source)

        self.assertEqual(result.lineage.sources[0].focuses, (focus,))
        missing = WorkspaceFocus(
            kind="method",
            focus_id="missing",
            relative_path="configs/missing.yaml",
        )
        with self.assertRaisesRegex(ValueError, "absent"):
            _source(manifest, package_id="missing", focuses=(missing,))

    def test_records_round_trip_and_reject_noncanonical_tampering(self) -> None:
        environment = _source(
            _tree(TreeEntry.directory("env"), _file("env/config.yaml", b"env")),
            package_id="environment",
        )
        method = _source(
            _tree(
                TreeEntry.directory("method"),
                _file("method/config.yaml", b"method"),
            ),
            package_id="method",
        )
        request = _request(environment, method)
        result = compile_workspace_assembly(
            request, WorkspaceSeed.build((environment, method))
        )

        self.assertEqual(
            WorkspaceAssemblyRequest.from_dict(request.to_dict()),
            request,
        )
        self.assertEqual(
            WorkspaceAssemblyLineage.from_dict(result.lineage.to_dict()),
            result.lineage,
        )
        self.assertEqual(result.lineage.workspace_id, request.workspace_id)
        self.assertEqual(result.lineage.owner_id, request.owner_id)
        self.assertEqual(result.lineage.outcome, "union")
        self.assertEqual(result.lineage.final_root_ref, result.root_ref)
        self.assertEqual(len(result.lineage.assembly_digest), 64)
        self.assertEqual(
            WorkspaceAssemblyResult.from_dict(result.to_dict()),
            result,
        )
        tampered = copy.deepcopy(result.to_dict())
        tampered["root_ref"] = str(SnapshotRef("f" * 64))
        with self.assertRaisesRegex(ValueError, "differs"):
            WorkspaceAssemblyResult.from_dict(tampered)
        tampered_lineage = copy.deepcopy(result.lineage.to_dict())
        tampered_lineage["outcome"] = "adopt"
        with self.assertRaisesRegex(ValueError, "outcome"):
            WorkspaceAssemblyLineage.from_dict(tampered_lineage)
        tampered_digest = copy.deepcopy(result.lineage.to_dict())
        tampered_digest["assembly_digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "digest differs"):
            WorkspaceAssemblyLineage.from_dict(tampered_digest)

    def test_duplicate_selection_focuses_are_merged_canonically(self) -> None:
        manifest = _tree(
            TreeEntry.directory("configs"),
            _file("configs/environment.yaml", b"environment"),
            _file("configs/method.yaml", b"method"),
        )
        selection = _selection(manifest, package_id="combined")
        environment = WorkspaceFocus(
            kind="environment",
            focus_id="environment",
            relative_path="configs/environment.yaml",
        )
        method = WorkspaceFocus(
            kind="method",
            focus_id="method",
            relative_path="configs/method.yaml",
        )
        first = WorkspaceSeedSource(
            anchor=WorkspaceSourceAnchor.build(
                selection=selection,
                store_id="realm-local",
                focuses=(environment,),
            ),
            tree_manifest=manifest,
        )
        second = WorkspaceSeedSource(
            anchor=WorkspaceSourceAnchor.build(
                selection=selection,
                store_id="realm-local",
                focuses=(method,),
            ),
            tree_manifest=manifest,
        )

        seed = WorkspaceSeed.build((second, first))

        self.assertEqual(len(seed.sources), 1)
        self.assertEqual(
            set(seed.sources[0].anchor.focuses),
            {environment, method},
        )


if __name__ == "__main__":
    unittest.main()
