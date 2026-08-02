from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict, RealmNotFound
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission
from optpilot.realm.manifests import TreeEntry, TreeManifest
from optpilot.realm.refs import BlobRef
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.run_closure import ScopeLayer, ScopePath
from optpilot.realm.service import RealmContentService
from optpilot.realm.workspaces import WORKSPACE_REVISION_ROLE, WorkspaceLineage
from optpilot.retained_study_compiler import RetainedStudyCompileError
from optpilot.retained_study_service import (
    RETAINED_STUDY_SOURCE_OWNER_KIND,
    RETAINED_STUDY_SOURCE_ROLE,
    RetainedStudyPreparationReceipt,
    RetainedStudyService,
    _retained_method_context_paths,
    _retained_trial_workspace_mappings,
)


_ENVIRONMENT = """\
apiVersion: optpilot.io/v1
config: environment
id: retained-local-environment
description: Retained local package fixture
evaluator:
  python: local_package.evaluate:evaluate
  pythonPath: [../..]
  settings: {}
candidate:
  format: parameters
  description: Parameter accepted by the retained fixture evaluator.
  parameters:
    schema:
      x:
        valueType: float
        min: 0.0
        max: 1.0
metrics:
  source: return
  keys: [score]
"""

_METHOD = """\
apiVersion: optpilot.io/v1
config: method
id: retained-local-method
description: Retained local package fixture
entrypoint:
  python: local_package.method:RetainedMethod
  pythonPath: [../..]
  protocol: batch
settings:
  batchSize: 1
accepts:
  formats: [parameters]
  requires:
    context: [candidate.parameters.schema]
"""

_STUDY = """\
apiVersion: optpilot.io/v1
config: study
name: retained-local-study
description: Retained nested package fixture
environmentConfig: ../environments/environment.yaml
methodConfig: ../methods/method.yaml
objective:
  metric: score
  direction: maximize
budget:
  maxTrials: 2
execution:
  parallelism: 1
  timeoutSeconds: 30
reproducibility:
  seed: 7
"""


def _write_package(root: Path, *, method_protocol: str = "batch") -> Path:
    study = root / "configs" / "studies" / "study.yaml"
    environment = root / "configs" / "environments" / "environment.yaml"
    method = root / "configs" / "methods" / "method.yaml"
    for path in (study, environment, method):
        path.parent.mkdir(parents=True, exist_ok=True)
    study.write_text(_STUDY, encoding="utf-8")
    environment.write_text(_ENVIRONMENT, encoding="utf-8")
    method.write_text(
        _METHOD.replace("protocol: batch", f"protocol: {method_protocol}"),
        encoding="utf-8",
    )
    source = root / "local_package"
    source.mkdir()
    (source / "evaluate.py").write_text(
        "def evaluate(candidate, context):\n    return {'score': candidate['x']}\n",
        encoding="utf-8",
    )
    (source / "method.py").write_text(
        "class RetainedMethod:\n    pass\n", encoding="utf-8"
    )
    return study


class RetainedStudyServiceTest(unittest.TestCase):
    def test_malformed_persisted_method_context_path_is_a_realm_conflict(self) -> None:
        method_context = {
            "instructions": ["../escape.md"],
            "references": [],
        }
        environment = SimpleNamespace(
            authored_config=ScopePath(
                "study-package-source", "environments/environment.yaml"
            ),
            projection_contract={
                "method_context": {
                    "logical_scope": "method-context",
                    "source": {
                        "path": "environments",
                        "scope": "study-package-source",
                    },
                },
                "schema": "optpilot.retained-package-input-projection.v1",
            },
            candidate_contract={"context": {"methodContext": method_context}},
            evaluator_contract={
                "adapter": {"config": {"context": {"methodContext": method_context}}}
            },
        )
        manifest = TreeManifest.build((TreeEntry.directory("environments"),))

        with self.assertRaisesRegex(RealmConflict, "path changed"):
            _retained_method_context_paths(
                environment,
                package_manifest=manifest,
            )

    def test_persisted_method_context_replay_requires_exact_mirrors_and_tree_files(
        self,
    ) -> None:
        manifest = TreeManifest.build(
            (
                TreeEntry.directory("environments"),
                TreeEntry.file(
                    "environments/prompt.md",
                    blob_ref=BlobRef.from_bytes(b"prompt"),
                    size=6,
                    executable=False,
                ),
            )
        )

        def environment(candidate, evaluator):
            return SimpleNamespace(
                authored_config=ScopePath(
                    "study-package-source", "environments/environment.yaml"
                ),
                projection_contract={
                    "method_context": {
                        "logical_scope": "method-context",
                        "source": {
                            "path": "environments",
                            "scope": "study-package-source",
                        },
                    },
                    "schema": "optpilot.retained-package-input-projection.v1",
                },
                candidate_contract={"context": {"methodContext": candidate}},
                evaluator_contract={
                    "adapter": {"config": {"context": {"methodContext": evaluator}}}
                },
            )

        valid = {"instructions": ["prompt.md"], "references": []}
        cases = (
            (
                "divergent",
                environment(
                    valid,
                    {"instructions": [], "references": []},
                ),
                manifest,
                "declarations changed",
            ),
            (
                "missing-entry",
                environment(
                    {"instructions": ["missing.md"], "references": []},
                    {"instructions": ["missing.md"], "references": []},
                ),
                manifest,
                "regular package-tree entry",
            ),
            (
                "projection-without-declaration",
                environment(
                    {"instructions": [], "references": []},
                    {"instructions": [], "references": []},
                ),
                manifest,
                "no declared paths",
            ),
        )
        for name, retained_environment, tree, message in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(RealmConflict, message),
            ):
                _retained_method_context_paths(
                    retained_environment,
                    package_manifest=tree,
                )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.study_path = _write_package(self.package_root)
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_principal(
            operation_id="retained-service/principal",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_principal(
            operation_id="retained-service/delegate-principal",
            principal_id="delegate",
            kind="agent",
        )
        self.ledger.register_store(
            operation_id="retained-service/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content_service = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.projection_service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.provider = ProcessProviderIdentity(
            builder_fingerprint="a" * 64,
            platform="test-platform",
        )
        self.service = RetainedStudyService(
            self.ledger,
            self.content_service,
            self.projection_service,
            self.provider,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()

    def prepare(self, *, operation_id: str = "retained-service/prepare"):
        return self.service.prepare_local_package(
            operation_id=operation_id,
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id="source-owner",
            study_definition_owner_id="definition-owner",
        )

    def package_workspace_selection(self):
        source_owner_id = "selected-package-origin"
        self.ledger.create_owner(
            operation_id="retained-service/selected/create-origin",
            owner_id=source_owner_id,
            owner_kind="resource",
            principal_id="operator",
        )
        change = self.ledger.begin_owner_change(
            operation_id="retained-service/selected/begin-origin",
            actor_principal_id="operator",
            owner_id=source_owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        sealed = self.content_service.capture(
            actor_principal_id="operator",
            change_id=change.change_id,
            store_id=self.store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(self.package_root),
            operation_id="retained-service/selected/seal-origin",
        )
        source_membership = OwnerMembership(
            self.store.store_id,
            sealed.snapshot_ref,
            "selected-package-origin",
        )
        self.ledger.hold_owner_content(
            operation_id="retained-service/selected/hold-origin",
            actor_principal_id="operator",
            change_id=change.change_id,
            memberships=(source_membership,),
        )
        source_commit = self.ledger.commit_owner_change(
            operation_id="retained-service/selected/commit-origin",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(source_membership,),
        )
        self.ledger.create_workspace_from_snapshot(
            operation_id="retained-service/selected/create-workspace",
            actor_principal_id="operator",
            source_owner_id=source_owner_id,
            expected_source_owner_revision=source_commit.owner_revision,
            title="Selected package",
            root=OwnerMembership(
                self.store.store_id,
                sealed.snapshot_ref,
                WORKSPACE_REVISION_ROLE,
            ),
            lineage=WorkspaceLineage(
                source_kind="owner-revision",
                source_owner_id=source_owner_id,
                source_id=source_owner_id,
                source_revision=source_commit.owner_revision,
                source_store_id=self.store.store_id,
                source_ref=sealed.snapshot_ref,
            ),
            workspace_id="selected-package-workspace",
            owner_id="selected-package-workspace-owner",
        )
        return self.ledger.mint_workspace_selection(
            actor_principal_id="operator",
            workspace_id="selected-package-workspace",
            expected_workspace_revision=1,
        )

    def test_seals_then_compiles_projection_and_launches_without_copying_refs(
        self,
    ) -> None:
        original_method = self.package_root / "configs" / "methods" / "method.yaml"
        from optpilot.realm.local_study_package import plan_local_study_package

        planner_calls: list[tuple[Path, Path]] = []

        def plan_from_projection(study: Path, root: Path):
            planner_calls.append((study, root))
            # Mutation after capture cannot affect the compiler's projected bytes.
            original_method.write_text(
                _METHOD.replace("protocol: batch", "protocol: session"),
                encoding="utf-8",
            )
            return plan_local_study_package(study, root)

        with mock.patch(
            "optpilot.retained_study_service.plan_local_study_package",
            side_effect=plan_from_projection,
        ):
            receipt = self.prepare()

        self.assertEqual(len(planner_calls), 1)
        projected_study, projected_root = planner_calls[0]
        self.assertNotEqual(projected_root, self.package_root)
        projected_root.relative_to(self.projection_service.root_binding.path)
        self.assertEqual(
            projected_study.relative_to(projected_root).as_posix(),
            "configs/studies/study.yaml",
        )
        self.assertEqual(
            receipt.study_definition.manifest.run_definition.method_revision.protocol,
            "optpilot.method.batch.v1",
        )
        source_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id="source-owner"
        )
        self.assertEqual(source_memberships, (receipt.source_membership,))
        self.assertEqual(receipt.source_membership.role, RETAINED_STUDY_SOURCE_ROLE)
        definition_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id="definition-owner"
        )
        self.assertEqual(len(definition_memberships), 2)
        self.assertEqual(
            {item.content_ref for item in definition_memberships},
            {receipt.package.snapshot_ref},
        )
        refs_before_run = tuple(self.store.iter_live_refs())

        run = self.service.launch_definition_run(
            operation_id="retained-service/launch",
            actor_principal_id="operator",
            controller_holder_id="controller",
            controller_ttl_seconds=60,
            preparation=receipt,
            run_id="run-retained",
            owner_id="run-retained-owner",
        )

        self.assertEqual(
            run.definition_digest,
            receipt.study_definition.manifest.run_definition_digest,
        )
        self.assertEqual(tuple(self.store.iter_live_refs()), refs_before_run)
        run_memberships = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id=run.run.owner_id
        )
        self.assertEqual(
            {(item.role, item.content_ref) for item in run_memberships},
            {(item.role, item.content_ref) for item in definition_memberships},
        )

    def test_selected_package_adopts_without_capture_and_projects_once(self) -> None:
        selection = self.package_workspace_selection()
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            object_count = int(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ]
            )
        finally:
            connection.close()
        live_refs = tuple(self.store.iter_live_refs())

        with (
            mock.patch.object(
                self.content_service,
                "capture",
                wraps=self.content_service.capture,
            ) as capture,
            mock.patch.object(
                self.projection_service,
                "project_read_only",
                wraps=self.projection_service.project_read_only,
            ) as project,
        ):
            receipt = self.service.prepare_selected_package(
                operation_id="retained-service/selected/prepare",
                actor_principal_id="operator",
                package_selection=selection,
                study_config_relative_path="configs/studies/study.yaml",
                source_owner_id="selected-retained-source",
                study_definition_owner_id="selected-definition",
            )
            self.ledger.retire_workspace(
                operation_id="retained-service/selected/retire-origin",
                actor_principal_id="operator",
                workspace_id="selected-package-workspace",
                expected_workspace_revision=1,
            )
            replay = self.service.prepare_selected_package(
                operation_id="retained-service/selected/prepare",
                actor_principal_id="operator",
                package_selection=selection,
                study_config_relative_path="configs/studies/study.yaml",
                source_owner_id="selected-retained-source",
                study_definition_owner_id="selected-definition",
            )

        self.assertEqual(replay, receipt)
        self.assertEqual(capture.call_count, 0)
        self.assertEqual(project.call_count, 1)
        self.assertEqual(receipt.package.source_anchor.owner_revision, 0)
        self.assertEqual(receipt.source_membership.role, RETAINED_STUDY_SOURCE_ROLE)
        self.assertEqual(
            self.ledger.read_owner_selection_provenance(
                actor_principal_id="operator",
                owner_id="selected-retained-source",
            ),
            selection,
        )
        self.assertEqual(tuple(self.store.iter_live_refs()), live_refs)
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM content_objects"
                    ).fetchone()[0]
                ),
                object_count,
            )
        finally:
            connection.close()

    def test_selected_package_validates_portable_config_path_before_projection(
        self,
    ) -> None:
        selection = self.package_workspace_selection()
        invalid_paths = (
            "/configs/studies/study.yaml",
            "../study.yaml",
            "configs//studies/study.yaml",
        )
        for index, invalid_path in enumerate(invalid_paths):
            owner_id = f"invalid-selected-source-{index}"
            with self.subTest(path=invalid_path), self.assertRaises(ValueError):
                self.service.prepare_selected_package(
                    operation_id=f"retained-service/selected/invalid-{index}",
                    actor_principal_id="operator",
                    package_selection=selection,
                    study_config_relative_path=invalid_path,
                    source_owner_id=owner_id,
                    study_definition_owner_id=f"invalid-selected-definition-{index}",
                )
            with self.assertRaises(RealmNotFound):
                self.ledger.read_owner(
                    actor_principal_id="operator", owner_id=owner_id
                )

        for index, absent_or_directory in enumerate(
            ("configs/studies/missing.yaml", "configs/studies")
        ):
            with (
                self.subTest(path=absent_or_directory),
                mock.patch.object(
                    self.projection_service,
                    "project_read_only",
                    wraps=self.projection_service.project_read_only,
                ) as project,
                self.assertRaisesRegex(RealmConflict, "absent"),
            ):
                self.service.prepare_selected_package(
                    operation_id=f"retained-service/selected/absent-{index}",
                    actor_principal_id="operator",
                    package_selection=selection,
                    study_config_relative_path=absent_or_directory,
                    source_owner_id=f"absent-selected-source-{index}",
                    study_definition_owner_id=f"absent-selected-definition-{index}",
                )
            self.assertEqual(project.call_count, 0)

    def test_committed_replay_reuses_source_and_definition_without_recapture(
        self,
    ) -> None:
        with mock.patch.object(
            self.content_service,
            "capture",
            wraps=self.content_service.capture,
        ) as capture:
            first = self.prepare()
            replay = self.prepare()

        self.assertEqual(capture.call_count, 1)
        self.assertEqual(replay, first)
        self.assertEqual(
            RetainedStudyPreparationReceipt.from_dict(first.to_dict()), first
        )

    def test_fresh_launch_identity_can_retain_equivalent_definition_no_copy(
        self,
    ) -> None:
        first = self.prepare(operation_id="retained-service/equivalent-first")
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            content_object_count = connection.execute(
                "SELECT COUNT(*) FROM content_objects"
            ).fetchone()[0]
        finally:
            connection.close()
        live_refs = tuple(self.store.iter_live_refs())

        second = self.service.prepare_local_package(
            operation_id="retained-service/equivalent-second",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id="source-owner-second",
            study_definition_owner_id="definition-owner-second",
        )

        self.assertNotEqual(
            first.study_definition.owner.owner_id,
            second.study_definition.owner.owner_id,
        )
        self.assertNotEqual(
            first.package.source_anchor.owner_id,
            second.package.source_anchor.owner_id,
        )
        self.assertEqual(first.package.snapshot_ref, second.package.snapshot_ref)
        self.assertEqual(
            first.study_definition.manifest.run_definition_digest,
            second.study_definition.manifest.run_definition_digest,
        )
        self.assertEqual(tuple(self.store.iter_live_refs()), live_refs)
        connection = sqlite3.connect(self.root / "realm.sqlite3")
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM content_objects").fetchone()[
                    0
                ],
                content_object_count,
            )
        finally:
            connection.close()

        launched = self.service.launch_definition_run(
            operation_id="retained-service/equivalent-second-launch",
            actor_principal_id="operator",
            controller_holder_id="equivalent-second-controller",
            controller_ttl_seconds=60,
            preparation=second,
            run_id="equivalent-second-run",
            owner_id="equivalent-second-run-owner",
        )
        self.assertEqual(
            launched.definition_digest,
            second.study_definition.manifest.run_definition_digest,
        )

    def test_committed_replay_reconstructs_package_method_context_paths(self) -> None:
        environment = self.package_root / "configs/environments/environment.yaml"
        environment.write_text(
            _ENVIRONMENT
            + """
methodContext:
  instructions: [prompt.md]
  references:
    - name: cases
      path: cases.yaml
      type: dataset
""",
            encoding="utf-8",
        )
        (environment.parent / "prompt.md").write_text("optimize", encoding="utf-8")
        (environment.parent / "cases.yaml").write_text("cases: []\n", encoding="utf-8")

        first = self.prepare()
        replay = self.prepare()

        self.assertEqual(replay, first)
        self.assertEqual(
            replay.package.method_context_instruction_paths,
            ("configs/environments/prompt.md",),
        )
        self.assertEqual(
            replay.package.method_context_reference_paths,
            ("configs/environments/cases.yaml",),
        )
        owner = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id="source-owner",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(owner.owner_kind, RETAINED_STUDY_SOURCE_OWNER_KIND)
        self.assertEqual(owner.revision, 1)

    def test_committed_replay_reconstructs_trial_workspace_layers(self) -> None:
        environment_path = self.package_root / "configs/environments/environment.yaml"
        environment_path.write_text(
            _ENVIRONMENT
            + """
trialWorkspace:
  - from: seed.json
    to: inputs/seed.json
  - from: fixtures
    to: fixtures
""",
            encoding="utf-8",
        )
        (environment_path.parent / "seed.json").write_text(
            '{"seed": 7}\n', encoding="utf-8"
        )
        (environment_path.parent / "fixtures").mkdir()
        (environment_path.parent / "fixtures" / "case.txt").write_text(
            "case", encoding="utf-8"
        )

        first = self.prepare()
        replay = self.prepare()

        self.assertEqual(replay, first)
        self.assertEqual(
            replay.package.trial_workspace_mappings,
            (
                ("configs/environments/seed.json", "inputs/seed.json"),
                ("configs/environments/fixtures", "fixtures"),
            ),
        )
        environment = replay.study_definition.manifest.run_definition.evaluation_closure.environment_revision
        self.assertEqual(
            tuple(
                (layer.source_subpath, layer.destination_subpath)
                for layer in sorted(
                    environment.attempt_input_layers,
                    key=lambda item: item.precedence,
                )
            ),
            replay.package.trial_workspace_mappings,
        )
        self.assertNotIn(
            str(self.package_root),
            json.dumps(replay.to_dict(), sort_keys=True),
        )

    def test_trial_workspace_replay_rejects_contract_layer_and_tree_tampering(
        self,
    ) -> None:
        environment_path = self.package_root / "configs/environments/environment.yaml"
        environment_path.write_text(
            _ENVIRONMENT
            + """
trialWorkspace:
  - from: seed.json
    to: input.json
""",
            encoding="utf-8",
        )
        (environment_path.parent / "seed.json").write_text("seed", encoding="utf-8")
        receipt = self.prepare()
        environment = receipt.study_definition.manifest.run_definition.evaluation_closure.environment_revision
        manifest = self.store.verify_tree(
            receipt.package.snapshot_ref,
            verify_children=True,
        )
        layer = environment.attempt_input_layers[0]
        cases = (
            (
                "contract-without-layer",
                replace(environment, attempt_input_layers=()),
                manifest,
                "no input layers",
            ),
            (
                "layer-without-contract",
                replace(environment, projection_contract={}),
                manifest,
                "lost their contract",
            ),
            (
                "missing-source",
                replace(
                    environment,
                    attempt_input_layers=(
                        ScopeLayer(
                            layer.scope,
                            layer.snapshot_ref,
                            source_subpath="configs/environments/missing.json",
                            destination_subpath=layer.destination_subpath,
                            precedence=0,
                        ),
                    ),
                ),
                manifest,
                "absent from the package tree",
            ),
        )
        for name, retained_environment, tree, message in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(RealmConflict, message),
            ):
                _retained_trial_workspace_mappings(
                    retained_environment,
                    package_manifest=tree,
                )

    def test_lost_tree_completion_response_recovers_without_live_source_rescan(
        self,
    ) -> None:
        original = self.ledger._record_completed_tree_capture
        lost_once = False

        def record_then_lose_response(**kwargs):
            nonlocal lost_once
            original(**kwargs)
            if not lost_once:
                lost_once = True
                raise RuntimeError("tree seal response lost")

        with (
            mock.patch.object(
                self.ledger,
                "_record_completed_tree_capture",
                side_effect=record_then_lose_response,
            ),
            mock.patch.object(
                self.store,
                "_seal_tree",
                wraps=self.store._seal_tree,
            ) as physical_seal,
        ):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                self.prepare(operation_id="retained-service/lost-tree-response")

            # A broken live checkout must not affect replay of the completed seal.
            (self.package_root / "configs" / "methods" / "method.yaml").write_text(
                _METHOD.replace("protocol: batch", "protocol: session"),
                encoding="utf-8",
            )
            receipt = self.prepare(operation_id="retained-service/lost-tree-response")

        self.assertEqual(physical_seal.call_count, 1)
        self.assertEqual(
            receipt.study_definition.manifest.run_definition.method_revision.protocol,
            "optpilot.method.batch.v1",
        )
        source = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id="source-owner",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(source.revision, 1)

    def test_committed_definition_replay_is_independent_of_changed_source_owner(
        self,
    ) -> None:
        receipt = self.prepare()
        change = self.ledger.begin_owner_change(
            operation_id="retained-service/change/begin",
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=1,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id="retained-service/change/commit",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=(receipt.source_membership,),
        )
        with mock.patch.object(
            self.content_service,
            "verify_owner_tree_manifest",
            wraps=self.content_service.verify_owner_tree_manifest,
        ) as verify_manifest:
            replay = self.prepare()
        self.assertEqual(replay, receipt)
        self.assertEqual(
            verify_manifest.call_args.kwargs["owner_id"],
            "definition-owner",
        )
        self.assertEqual(
            verify_manifest.call_args.kwargs["expected_owner_revision"],
            0,
        )
        run = self.service.launch_definition_run(
            operation_id="retained-service/source-independent-launch",
            actor_principal_id="operator",
            controller_holder_id="controller-independent",
            controller_ttl_seconds=60,
            preparation=replay,
            run_id="run-source-independent",
            owner_id="run-source-independent-owner",
        )
        self.assertEqual(
            run.definition_digest,
            receipt.study_definition.manifest.run_definition_digest,
        )

        self.ledger.create_owner(
            operation_id="retained-service/conflicting-owner",
            owner_id="other-source-owner",
            owner_kind="workspace",
            principal_id="operator",
        )
        with self.assertRaises(RealmConflict):
            self.service.prepare_local_package(
                operation_id="retained-service/conflicting-prepare",
                actor_principal_id="operator",
                store_id=self.store.store_id,
                package_root=self.package_root,
                study_config_path=self.study_path,
                source_owner_id="other-source-owner",
                study_definition_owner_id="other-definition-owner",
            )

    def test_changed_source_without_definition_fails_closed(self) -> None:
        live_method = self.package_root / "configs" / "methods" / "method.yaml"
        live_method.write_text(
            _METHOD.replace("protocol: batch", "protocol: session"),
            encoding="utf-8",
        )
        with self.assertRaises(RetainedStudyCompileError):
            self.prepare(operation_id="retained-service/uncommitted")
        membership = self.ledger.list_owner_memberships(
            actor_principal_id="operator", owner_id="source-owner"
        )[0]
        change = self.ledger.begin_owner_change(
            operation_id="retained-service/uncommitted-change/begin",
            actor_principal_id="operator",
            owner_id="source-owner",
            expected_owner_revision=1,
            ttl_seconds=60,
        )
        self.ledger.commit_owner_change(
            operation_id="retained-service/uncommitted-change/commit",
            actor_principal_id="operator",
            change_id=change.change_id,
            expected_owner_revision=1,
            additions=(),
            removals=(membership,),
        )

        with self.assertRaisesRegex(RealmConflict, "source owner revision"):
            self.prepare(operation_id="retained-service/uncommitted-retry")

    def test_unsupported_config_retains_source_but_creates_no_definition_or_run(
        self,
    ) -> None:
        live_method = self.package_root / "configs" / "methods" / "method.yaml"
        live_method.write_text(
            _METHOD.replace("protocol: batch", "protocol: session"),
            encoding="utf-8",
        )
        with mock.patch.object(
            self.content_service,
            "capture",
            wraps=self.content_service.capture,
        ) as capture:
            with self.assertRaises(RetainedStudyCompileError) as raised:
                self.prepare(operation_id="retained-service/unsupported")
            # A later live-checkout fix cannot rewrite the already-retained source.
            live_method.write_text(_METHOD, encoding="utf-8")
            with self.assertRaises(RetainedStudyCompileError) as replay_raised:
                self.prepare(operation_id="retained-service/unsupported-retry")
        self.assertEqual(raised.exception.code, "method_mode_unsupported")
        self.assertEqual(replay_raised.exception.code, "method_mode_unsupported")
        self.assertEqual(capture.call_count, 1)

        source = self.ledger.read_owner(
            actor_principal_id="operator",
            owner_id="source-owner",
            permission=OwnerPermission.METADATA_READ,
        )
        self.assertEqual(source.revision, 1)
        self.assertEqual(
            len(
                self.ledger.list_owner_memberships(
                    actor_principal_id="operator", owner_id="source-owner"
                )
            ),
            1,
        )
        with self.assertRaises(RealmNotFound):
            self.ledger.read_owner(
                actor_principal_id="operator",
                owner_id="definition-owner",
            )
        self.assertEqual(self.ledger.list_runs(actor_principal_id="operator").items, ())

    def test_portable_receipt_and_manifests_exclude_host_and_projection_paths(
        self,
    ) -> None:
        receipt = self.prepare()
        encoded = json.dumps(receipt.to_dict(), sort_keys=True)

        self.assertNotIn(str(self.package_root), encoded)
        self.assertNotIn(str(self.projection_service.root_binding.path), encoded)
        self.assertNotIn(str(self.study_path), encoded)
        self.assertEqual(
            receipt.package.study_config_path, "configs/studies/study.yaml"
        )

    def test_manifest_recovery_needs_exact_membership_but_only_derive_acl(self) -> None:
        receipt = self.prepare()
        self.ledger.grant_owner_permission(
            operation_id="retained-service/grant-derive",
            actor_principal_id="operator",
            owner_id="source-owner",
            principal_id="delegate",
            permission=OwnerPermission.DERIVE,
        )

        manifest = self.content_service.verify_owner_tree_manifest(
            actor_principal_id="delegate",
            owner_id="source-owner",
            expected_owner_revision=2,
            membership=receipt.source_membership,
        )

        self.assertEqual(manifest.snapshot_ref, receipt.package.snapshot_ref)
        with self.assertRaises(RealmNotFound):
            self.content_service.verify_owner_tree_manifest(
                actor_principal_id="delegate",
                owner_id="source-owner",
                expected_owner_revision=2,
                membership=OwnerMembership(
                    store_id=receipt.source_membership.store_id,
                    content_ref=receipt.source_membership.content_ref,
                    role="wrong-role",
                ),
            )


if __name__ == "__main__":
    unittest.main()
