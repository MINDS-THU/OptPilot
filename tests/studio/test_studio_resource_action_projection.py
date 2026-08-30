"""A realm-published resource action runs in a writable per-run copy.

Projections -- the realm's materialized snapshots -- are sealed read-only
(0o500 directories, verified by contract), because they are a shared cache
named by content hash. Core's setup contract runs setup steps "in the
editable copy". Publishing a package silently swapped the catalog-resolved
source from the author's writable folder to the sealed projection, so the
first setup step that wrote into its tree failed with EACCES -- live, on
devs-gen-interface's generate.

The fix: Studio borrows the exact revision, copies the resource into a
per-run resource-action-* runtime directory, runs the action there, and
deletes the copy when the run settles; an orphan sweep covers crashes.
The projection is never written to and never loosened.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import yaml

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _prepare_resource_action_execution,
    _resolve_catalog_identifier,
    _resource_action_review,
    _resource_action_run_status,
    _start_resource_action_run,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS

PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"

_PREPARE_PY = """
import pathlib
pathlib.Path(".runtime").mkdir(exist_ok=True)
pathlib.Path(".runtime/marker").write_text("setup ran here")
print("setup complete")
"""

_GENERATE_PY = """
import os, pathlib
marker = pathlib.Path(".runtime/marker")
assert marker.is_file(), "setup must have run in this same writable tree"
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "bundle.txt").write_text("generated from: " + str(pathlib.Path.cwd()))
print("bundle generated")
"""


@unittest.skipUnless(os.name == "posix", "local Realm projections are POSIX-only")
class RealmResourceActionExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:studio-resource-action-projection-test",
        )
        self.addCleanup(self.runtime.close)
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(self.state.close_catalog_projections)
        self._counter = 0
        self._publish_generator_package()

    def _operation(self, label: str) -> str:
        self._counter += 1
        return f"studio-resource-action-projection/{self._counter}/{label}"

    def _publish(
        self,
        *,
        package_id: str,
        publisher_id: str,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
    ) -> Any:
        suffix = f"{self._counter}-{uuid.uuid4().hex[:8]}"
        owner_id = f"studio-action-projection-artifact-{suffix}"
        source = self.root / f"source-{suffix}"
        source.mkdir()
        for relative, content in files.items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        actor = self.runtime.actor_principal_id
        self.runtime.ledger.create_owner(
            operation_id=self._operation(f"create-{suffix}"),
            owner_id=owner_id,
            owner_kind="package-plan-artifact",
            principal_id=actor,
        )
        change = self.runtime.ledger.begin_owner_change(
            operation_id=self._operation(f"begin-{suffix}"),
            actor_principal_id=actor,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=TEST_LEASE_TTL_SECONDS,
        )
        sealed = self.runtime.content_service.capture(
            actor_principal_id=actor,
            change_id=change.change_id,
            store_id=self.runtime.content_store.store_id,
        ).seal_tree(
            source=AllowedTreeSource(source),
            operation_id=self._operation(f"seal-{suffix}"),
        )
        membership = OwnerMembership(
            self.runtime.content_store.store_id,
            sealed.snapshot_ref,
            PACKAGE_ARTIFACT_ROLE,
        )
        self.runtime.ledger.hold_owner_content(
            operation_id=self._operation(f"hold-{suffix}"),
            actor_principal_id=actor,
            change_id=change.change_id,
            memberships=(membership,),
        )
        committed = self.runtime.ledger.commit_owner_change(
            operation_id=self._operation(f"commit-{suffix}"),
            actor_principal_id=actor,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        identity = {
            "package_id": package_id,
            "publisher_id": publisher_id,
            "artifact": str(membership.content_ref),
        }
        return self.runtime.catalog.publish(
            operation_id=self._operation(f"publish-{suffix}"),
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
            expected_head=self.runtime.catalog.read_head(package_id=package_id),
        )

    def _publish_generator_package(self) -> None:
        manifest = yaml.safe_dump(
            {
                "apiVersion": "optpilot.io/v1",
                "config": "resource",
                "id": "demo-generator",
                "name": "Demo generator",
                "purpose": "generator",
                "actions": [
                    {
                        "id": "generate",
                        "label": "Generate a bundle",
                        "command": ["python", "generate.py"],
                        "grants": {"network": "enabled"},
                        "runtime": {
                            "sandbox": "process",
                            "setup": {
                                "steps": [
                                    {
                                        "uses": "command",
                                        "command": ["python", "prepare.py"],
                                    }
                                ]
                            },
                        },
                        "timeoutSeconds": 120,
                    }
                ],
            },
            sort_keys=False,
        )
        self._publish(
            package_id="demo-gallery",
            publisher_id="publisher",
            files={
                "resources/demo-generator/optpilot.resource.yaml": manifest,
                "resources/demo-generator/prepare.py": _PREPARE_PY,
                "resources/demo-generator/generate.py": _GENERATE_PY,
            },
            owned_paths=("resources/demo-generator",),
        )

    def _resource_entry(self) -> dict:
        resources = _catalog_payload(self.state)["resources"]
        return next(item for item in resources if item["id"] == "demo-generator")

    def _await_run(self, request_id: str, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = _resource_action_run_status(self.state, request_id)
            if status["status"] != "running":
                return status
            if time.monotonic() >= deadline:
                self.fail("Resource action run did not settle.")
            time.sleep(0.05)

    def _projection_runtime_markers(self) -> list[Path]:
        return [
            path
            for path in (self.root / "realm").rglob(".runtime")
            if "projection" in str(path)
        ]

    def test_entry_resolves_inside_a_sealed_projection(self) -> None:
        # The regression's precondition: after publication the catalog points
        # at the read-only projection, not at a writable folder.
        entry = self._resource_entry()
        source_path = _resolve_catalog_identifier(
            self.state, "resource", entry["uid"]
        )
        self.assertIn("projection", str(source_path))
        mode = stat.S_IMODE(source_path.stat().st_mode)
        self.assertEqual(mode & 0o222, 0, f"projection is writable: {oct(mode)}")

    def test_action_runs_in_a_writable_copy_and_leaves_no_trace(self) -> None:
        entry = self._resource_entry()
        review = _resource_action_review(
            self.state, resource_uid=entry["uid"], action_id="generate"
        )
        response, _status = _start_resource_action_run(
            self.state,
            {
                "request_id": str(uuid.uuid4()),
                "resource_uid": entry["uid"],
                "action_id": "generate",
                "_approved_action_contract_digest": review[
                    "action_contract_digest"
                ],
            },
        )
        status = self._await_run(str(response["request_id"]))
        self.assertEqual(
            status["status"], "succeeded", status.get("error") or status
        )
        result = status["result"]
        output_root = Path(str(result["output_root"]))
        self.assertTrue((output_root / "bundle.txt").is_file())
        generated_from = (output_root / "bundle.txt").read_text()
        self.assertNotIn("projection", generated_from)
        # The exact regression signature: nothing wrote into any projection.
        self.assertEqual(self._projection_runtime_markers(), [])
        # The per-run copy is deleted once the run settles.
        self.assertEqual(
            list(self.state.runtime_dir.glob("resource-action-copy-*")), []
        )

    def test_prepared_manifest_is_never_inside_a_projection(self) -> None:
        entry = self._resource_entry()
        request_id = str(uuid.uuid4())
        manifest_path, cleanup, _sanitize = _prepare_resource_action_execution(
            self.state, entry["uid"], request_id
        )
        try:
            self.assertNotIn("projection", str(manifest_path))
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(
                str(manifest_path).startswith(str(self.state.runtime_dir)),
                manifest_path,
            )
            # The copy is writable where the projection is not.
            probe = manifest_path.parent / ".runtime"
            probe.mkdir()
            (probe / "marker").write_text("writable")
        finally:
            cleanup()
        self.assertFalse(manifest_path.exists())
        self.assertEqual(
            list(self.state.runtime_dir.glob("resource-action-copy-*")), []
        )

    def test_readable_ids_also_get_the_writable_copy(self) -> None:
        # The assistant's tool schema tells the model to pass a qualified_id
        # or plain id -- the ~490-char ref token is not something a model
        # reliably echoes. A readable name that bypassed the copy would
        # reproduce the original EACCES on the sealed projection.
        entry = self._resource_entry()
        for readable in (
            str(entry.get("qualified_id") or "") or entry["id"],
            entry["id"],
        ):
            with self.subTest(readable=readable):
                manifest_path, cleanup, _sanitize = _prepare_resource_action_execution(
                    self.state, readable, str(uuid.uuid4())
                )
                try:
                    self.assertNotIn("projection", str(manifest_path))
                    self.assertTrue(
                        str(manifest_path).startswith(
                            str(self.state.runtime_dir)
                        ),
                        manifest_path,
                    )
                finally:
                    cleanup()

    def test_orphan_sweep_removes_an_abandoned_copy(self) -> None:
        entry = self._resource_entry()
        request_id = str(uuid.uuid4())
        manifest_path, _cleanup, _sanitize = _prepare_resource_action_execution(
            self.state, entry["uid"], request_id
        )
        self.assertTrue(manifest_path.is_file())  # abandoned: no cleanup call
        self.state._cleanup_orphaned_resource_action_runtimes()
        self.assertEqual(
            list(self.state.runtime_dir.glob("resource-action-copy-*")), []
        )


if __name__ == "__main__":
    unittest.main()
